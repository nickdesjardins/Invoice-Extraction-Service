"""FastAPI application: CORS, error handling and the public API surface.

Endpoints
---------
* ``GET  /health``            - liveness + which models are active.
* ``POST /api/v1/extract``    - structured invoice extraction with fallback routing.

Error contract (every non-2xx body uses :class:`~app.schemas.ErrorResponse`):

==========  ========================  ==============================================
HTTP        ``error`` code            When
==========  ========================  ==============================================
422         ``validation_error``      Request body failed validation (e.g. text < 10 chars)
422         ``unparseable_text``      Engines ran fine but found no invoice in the text
503         ``providers_unavailable`` Every model in the chain failed (retryable)
4xx         ``http_error``            Routing errors such as 404 / 405
500         ``internal_error``        Unexpected exception (details only in server logs)
==========  ========================  ==============================================

Error bodies never carry raw upstream text. Provider messages can echo project ids,
quota descriptions or fragments of the submitted document, so the verbose form goes
to the server log keyed by ``X-Request-ID`` and the client sees a sanitised summary.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import get_settings
from app.extractors import (
    AllProvidersFailedError,
    ExtractionError,
    UnparseableTextError,
    extract_invoice,
    get_dispatcher,
)
from app.schemas import (
    ErrorResponse,
    ExtractRequest,
    ExtractResponse,
    HealthResponse,
    ProviderStatus,
)

logger = logging.getLogger("app.main")

RETRY_AFTER_SECONDS = "5"
# Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY -> _CONTENT in 2025; a literal keeps us
# independent of which name the installed version exposes.
HTTP_422_UNPROCESSABLE = 422

#: A client may supply its own correlation id, but it is echoed into response headers
#: and logs, so it is constrained to a safe, bounded character set.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    )
    # At DEBUG the HTTP client libraries dump full request bodies, which for this
    # service means the caller's invoice text (often personal data). Keep the wire
    # loggers at INFO regardless so turning on DEBUG stays safe with real documents.
    for noisy in ("httpx", "httpcore", "httpx2", "openai", "google_genai"):
        logging.getLogger(noisy).setLevel(max(logging.getLevelNamesMapping()[level], logging.INFO))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    dispatcher = get_dispatcher()
    chain = dispatcher.models
    active = dispatcher.active_providers
    if not active:
        logger.error(
            "OPENROUTER_API_KEY is not set. Every extraction request will return HTTP 503."
        )

    # Build the SDK client now: construction is synchronous and reads CA bundles
    # from disk, so doing it lazily would block the event loop on the first request.
    dispatcher.warm_up()

    logger.info(
        "%s v%s starting (environment=%s, chain=%s, timeout=%ss)",
        settings.service_name,
        __version__,
        settings.environment,
        chain,
        settings.timeout_seconds,
    )
    try:
        yield
    finally:
        await dispatcher.aclose()
        logger.info("%s shutting down", settings.service_name)


def _error_response(
    status_code: int,
    *,
    error: str,
    message: str,
    detail: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=error, message=message, detail=detail)
    return JSONResponse(
        status_code=status_code, content=jsonable_encoder(body.model_dump()), headers=headers
    )


def create_app() -> FastAPI:
    settings = get_settings()
    hide_docs = settings.is_production

    app = FastAPI(
        title="Invoice Extraction Service",
        version=__version__,
        description=(
            "Deterministic structured extraction of invoice data from unstructured text, "
            "with multi-model fallback routing across free OpenRouter models."
        ),
        lifespan=lifespan,
        # The schema describes an unauthenticated endpoint that spends model credits;
        # do not advertise it publicly in production.
        docs_url=None if hide_docs else "/docs",
        redoc_url=None if hide_docs else "/redoc",
        openapi_url=None if hide_docs else "/openapi.json",
    )

    # --- CORS --------------------------------------------------------------
    allow_any_origin = settings.cors_origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # Browsers reject credentials with a wildcard origin, so only enable them
        # when an explicit allow-list is configured.
        allow_credentials=not allow_any_origin,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Process-Time-Ms", "Retry-After"],
    )

    # --- Request context middleware ---------------------------------------
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get("x-request-id")
        request_id = supplied if supplied and _REQUEST_ID_RE.match(supplied) else uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last line of defence
            # Produce the 500 here rather than in an exception handler. A handler
            # runs outside this middleware, so its response would miss the headers
            # below and, more importantly, the CORS headers a browser needs to read
            # the error at all.
            logger.exception("request %s: unhandled exception", request_id)
            response = _error_response(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                error="internal_error",
                message="An unexpected error occurred. The incident has been logged.",
                detail={"request_id": request_id},
            )

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
        return response

    # --- Exception handlers -----------------------------------------------
    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Drop 'input' from each error: it echoes the submitted document back to the
        # caller and can be megabytes long.
        errors = [
            {key: value for key, value in error.items() if key not in ("input", "url")}
            for error in exc.errors()
        ]
        return _error_response(
            HTTP_422_UNPROCESSABLE,
            error="validation_error",
            message="Request body failed validation.",
            detail=jsonable_encoder(errors),
        )

    @app.exception_handler(UnparseableTextError)
    async def _handle_unparseable(request: Request, exc: UnparseableTextError) -> JSONResponse:
        logger.info(
            "request %s: unparseable text (%s)",
            getattr(request.state, "request_id", "unknown"),
            exc.log_message,
        )
        return _error_response(
            HTTP_422_UNPROCESSABLE,
            error="unparseable_text",
            message="The supplied text does not appear to contain an invoice, bill or receipt.",
            detail={"provider": exc.provider},
        )

    @app.exception_handler(AllProvidersFailedError)
    async def _handle_all_failed(request: Request, exc: AllProvidersFailedError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "request %s: all providers failed: %s",
            request_id,
            "; ".join(err.log_message for err in exc.errors),
        )
        headers = {"Retry-After": RETRY_AFTER_SECONDS} if exc.retryable else None
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error="providers_unavailable",
            message="Every extraction model failed.",
            detail=exc.as_dict(),
            headers=headers,
        )

    @app.exception_handler(ExtractionError)
    async def _handle_extraction_error(request: Request, exc: ExtractionError) -> JSONResponse:
        # Safety net for any ExtractionError that escapes the dispatcher.
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error("request %s: extraction error: %s", request_id, exc.log_message)
        headers = {"Retry-After": RETRY_AFTER_SECONDS} if exc.retryable else None
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error="providers_unavailable",
            message="The extraction engine failed.",
            detail={
                "provider": exc.provider,
                "error_type": type(exc).__name__,
                "message": exc.message,
                "retryable": exc.retryable,
            },
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Keeps routing errors (404, 405, ...) inside the same ErrorResponse envelope.
        return _error_response(
            exc.status_code,
            error="http_error",
            message=str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    # --- Routes ------------------------------------------------------------
    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["operations"],
        summary="Service health and the active model chain",
        responses={503: {"model": HealthResponse, "description": "No API key configured"}},
    )
    async def health(response: Response) -> HealthResponse:
        """Readiness signal.

        Reports the configured model chain and whether an API key is present. It
        does not call OpenRouter, so it stays cheap and cannot be rate limited; a
        model can still fail at request time, which is what the chain is for.
        """
        current = get_settings()
        dispatcher = get_dispatcher()
        providers = [
            ProviderStatus(
                name="openrouter",
                model=extractor.model,
                role="primary" if index == 0 else "fallback",
                configured=extractor.is_configured(),
            )
            for index, extractor in enumerate(dispatcher.extractors)
        ]
        active = [p.model for p in providers if p.configured]
        if len(active) == len(providers):
            health_status = "ok"
        elif active:
            health_status = "degraded"
        else:
            health_status = "unavailable"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status=health_status,
            service=current.service_name,
            version=__version__,
            environment=current.environment,
            providers=providers,
            active_providers=active,
        )

    @app.post(
        "/api/v1/extract",
        response_model=ExtractResponse,
        tags=["extraction"],
        summary="Extract structured invoice data from unstructured text",
        responses={
            422: {
                "model": ErrorResponse,
                "description": "Invalid request or text contains no invoice",
            },
            503: {"model": ErrorResponse, "description": "Primary and fallback engines both failed"},
            500: {"model": ErrorResponse, "description": "Unexpected server error"},
        },
    )
    async def extract(payload: ExtractRequest, request: Request) -> ExtractResponse:
        started = time.perf_counter()
        invoice, model_used, fallback_triggered = await extract_invoice(payload.text)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "request %s: extracted invoice vendor=%r number=%r model=%s fallback=%s elapsed_ms=%.0f",
            getattr(request.state, "request_id", "unknown"),
            invoice.vendor_name,
            invoice.invoice_number,
            model_used,
            fallback_triggered,
            elapsed_ms,
        )
        return ExtractResponse(
            success=True,
            data=invoice,
            model_used=model_used,
            execution_time_ms=elapsed_ms,
            fallback_triggered=fallback_triggered,
        )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - manual launch helper
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=_settings.environment == "development",
        log_level=_settings.log_level.lower(),
    )

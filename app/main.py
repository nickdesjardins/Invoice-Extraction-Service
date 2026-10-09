"""FastAPI application: CORS, error handling and the public API surface.

Endpoints
---------
* ``GET  /health``            - liveness + which models are active.
* ``POST /api/v1/extract``    - structured invoice extraction with fallback routing.
* ``GET  /``                  - browser playground (see :mod:`app.playground`), unless
  ``PLAYGROUND_ENABLED=false``.

Error contract (every non-2xx body uses :class:`~app.schemas.ErrorResponse`):

==========  ========================  ==============================================
HTTP        ``error`` code            When
==========  ========================  ==============================================
422         ``validation_error``      Request body failed validation (e.g. text < 10 chars),
                                      or is larger than ``MAX_BODY_BYTES``
422         ``unparseable_text``      Engines ran fine but found no invoice in the text
503         ``providers_unavailable`` Every model in the chain failed (retryable)
503         ``providers_unavailable`` The ceiling on live model calls is reached; no model was
                                      called and ``Retry-After`` is the real wait
4xx         ``http_error``            Routing errors such as 404 / 405, and 400 for a body
                                      that cannot be read as JSON at all
500         ``internal_error``        Unexpected exception (details only in server logs)
==========  ========================  ==============================================

Error bodies never carry raw upstream text. Provider messages can echo project ids,
quota descriptions or fragments of the submitted document, so the verbose form goes
to the server log keyed by ``X-Request-ID`` and the client sees a sanitised summary.
"""

from __future__ import annotations

import asyncio
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
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__, model_chooser
from app.budget import CallLimitError, extract_invoice
from app.config import get_settings
from app.extractors import (
    AllProvidersFailedError,
    ExtractionError,
    UnparseableTextError,
    get_dispatcher,
)
from app.model_catalogue import Listing, check_chain, describe
from app.playground import install_playground
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

#: The whole catalogue check gets this long at startup, however long the chain is.
CATALOGUE_DEADLINE_SECONDS = 5.0

#: No request body may be larger than this. The largest valid document is 50,000
#: characters, which is at most 600,000 bytes of JSON however its characters are
#: escaped, so a larger body is padding or an attempt to fill the host's memory.
MAX_BODY_BYTES = 1_000_000


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


async def report_model_listings(
    chain: list[str], base_url: str, automatic: bool = False
) -> dict[str, Listing]:
    """Log what OpenRouter's catalogue says about each configured model.

    This only reports. Whatever it finds, and whatever goes wrong while asking, the
    service starts and routes requests exactly as it would have without the check.
    ``automatic`` is true when the service looks after its own chain: a model that is
    not listed is then looked into after the first request that fails, so the line is
    a warning. Otherwise the models are named in ``OPENROUTER_MODELS`` and nothing
    replaces one: the line is an error, and says what the admin can do and where.
    """
    try:
        listings = await asyncio.wait_for(
            asyncio.to_thread(check_chain, chain, base_url), timeout=CATALOGUE_DEADLINE_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - a status report must never stop startup
        logger.warning(
            "The model catalogue check did not finish (%s); starting anyway.", type(exc).__name__
        )
        return {}
    for listing in listings:
        if listing.status == "not_listed" and automatic:
            logger.warning(
                "%s is not listed by OpenRouter (%s). The service looks for a replacement "
                "after the first request that fails. If this line appears at every start, "
                "the published list is not helping: look at the scheduled test's last runs, "
                "or measure again and change DEFAULT_MODEL_CHAIN.",
                listing.model,
                listing.note,
            )
        elif listing.status == "not_listed":
            # One line, and an error: with LOG_LEVEL=WARNING it is all an admin sees.
            logger.error(
                "%s is not listed by OpenRouter (%s). This deployment names its models in "
                "OPENROUTER_MODELS, so the service will not replace it. Put a working id "
                "there, or empty that line (in .env, or in the host's settings) and restart: "
                "the service then looks after its chain itself.",
                listing.model,
                listing.note,
            )
        elif listing.needs_attention():
            logger.warning("%s", describe(listing))
        else:
            logger.info("%s", describe(listing))
    return {listing.model: listing for listing in listings}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    # A service that slept for weeks may wake up on models that are gone. One key-less
    # read of the list this project's scheduled test published puts tested models in
    # place before the first request. It calls no model, and it cannot stop startup.
    await model_chooser.at_startup()
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

    model_chooser.log_mode()

    # Free models are withdrawn without notice, so say at once if a configured id is
    # gone. Skipped without a key: nothing can be called then anyway.
    app.state.model_listings = {}
    if settings.model_catalogue_check and active:
        app.state.model_listings = await report_model_listings(
            chain, settings.openrouter_base_url, automatic=not settings.models_pinned
        )
        model_chooser.note_listings(app.state.model_listings)
    try:
        yield
    finally:
        await model_chooser.stop()
        app.state.model_listings = {}
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


def _engine_error_detail(exc: ExtractionError) -> dict[str, Any]:
    """What a client is told about one engine error: the same four keys from every handler."""
    return {
        "provider": exc.provider,
        "error_type": type(exc).__name__,
        "message": exc.message,
        "retryable": exc.retryable,
    }


class BodyTooLargeError(StarletteHTTPException):
    """A request body grew past ``MAX_BODY_BYTES`` while it was being read."""

    def __init__(self) -> None:
        super().__init__(status_code=HTTP_422_UNPROCESSABLE)


def _body_too_large(request: Request) -> JSONResponse:
    """Refuse an oversized body with the envelope of any other request that fails validation."""
    logger.warning(
        "request %s: refused a body of more than %d bytes",
        getattr(request.state, "request_id", "unknown"),
        MAX_BODY_BYTES,
    )
    return _error_response(
        HTTP_422_UNPROCESSABLE,
        error="validation_error",
        message="Request body failed validation.",
        detail=[
            {
                "type": "bytes_too_long",
                "loc": ["body"],
                "msg": f"Request body should have at most {MAX_BODY_BYTES} bytes",
            }
        ],
    )


class BodyLimitMiddleware:
    """Refuse a request body larger than ``MAX_BODY_BYTES`` before it is held in memory.

    Without it the whole body is read and parsed before validation can say no: a
    caller could send 20 MB at a time to a host that has 512 MB.

    Starlette has a limit of its own (``max_body_size``). It answers 413 in plain
    text, which would be a new status and a new shape for this API, so the limit is
    kept here, where it can answer in the service's own envelope.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # A length that is declared is believed before a single byte is read.
        headers = Headers(scope=scope)
        try:
            declared = int(headers.get("content-length", ""))
        except ValueError:
            declared = 0  # none declared (or not a number): the count below decides
        if "transfer-encoding" in headers:
            # A body sent in pieces has no declared length, whatever else is said: a
            # server that takes both headers reads the pieces. The count below decides.
            declared = 0
        if declared > MAX_BODY_BYTES:
            refusal = _body_too_large(Request(scope))
            await refusal(scope, receive, send)
            return

        received = 0

        async def counted() -> Message:
            # A body can arrive in pieces with no declared length, so count the pieces.
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    # FastAPI lets an HTTPException through while it reads a body,
                    # and the handler registered in create_app answers this one.
                    raise BodyTooLargeError()
            return message

        await self.app(scope, counted, send)


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

    # Three layers wrap the routes. The one added last is the outermost, so they are
    # added from the inside out: the body limit, the request context, then CORS.

    # --- Body size ---------------------------------------------------------
    # Innermost: its 422 passes through the two layers added below and leaves with
    # X-Request-ID and the CORS headers a browser needs.
    app.add_middleware(BodyLimitMiddleware)

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
            # Produce the 500 here rather than in an exception handler. A handler for
            # unexpected exceptions runs outside every layer added here, so its
            # response would miss the headers below and, more importantly, the CORS
            # headers a browser needs to read the error at all. This response still
            # passes through the CORS layer, which is added after this one.
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

    # --- CORS --------------------------------------------------------------
    # Outermost, so that every answer of the layers inside it carries the CORS
    # headers, the 500 above included. A preflight request is answered here and goes
    # no further, so it carries no X-Request-ID.
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

    @app.exception_handler(BodyTooLargeError)
    async def _handle_body_too_large(request: Request, exc: BodyTooLargeError) -> JSONResponse:
        return _body_too_large(request)

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

    @app.exception_handler(CallLimitError)
    async def _handle_call_limit(request: Request, exc: CallLimitError) -> JSONResponse:
        # A ceiling the service keeps on purpose (app.budget), reached before any model
        # was called. It answers in the envelope of the safety net below, so a client
        # meets no new status and no new shape; only Retry-After differs: it is the
        # real wait. The message is written for a visitor, the log line for the admin.
        logger.warning(
            "request %s: refused before any model was called: %s",
            getattr(request.state, "request_id", "unknown"),
            exc.detail,
        )
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error="providers_unavailable",
            message=exc.message,
            detail=_engine_error_detail(exc),
            headers={"Retry-After": str(exc.wait_seconds)},
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
            detail=_engine_error_detail(exc),
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
            503: {
                "model": ErrorResponse,
                "description": "Every model failed, or the ceiling on live model calls is reached",
            },
            500: {"model": ErrorResponse, "description": "Unexpected server error"},
        },
    )
    async def extract(payload: ExtractRequest, request: Request) -> ExtractResponse:
        started = time.perf_counter()
        chain = get_dispatcher().models
        try:
            # The chain in use, behind the one ceiling for everybody (app.budget): a
            # refusal is raised here, before any model is called.
            invoice, model_used, fallback_triggered = await extract_invoice(payload.text)
        except AllProvidersFailedError as exc:
            # No model answered. Told to the chooser, which looks into it afterwards;
            # the answer to the caller is exactly what it was.
            model_chooser.note(
                [
                    model_chooser.Failure(
                        err.provider or "", type(err).__name__, err.retryable, err.message
                    )
                    for err in exc.errors
                ],
                answered=False,
            )
            raise
        if fallback_triggered:
            # A later model gave the answer, so the ones before it did not. Why is not
            # known here: the engine returns the winner only. A model that said "not an
            # invoice" is therefore among them, and a request that ends in that verdict
            # is not told at all. The page's route sees every attempt and tells them apart.
            earlier = chain[: chain.index(model_used)] if model_used in chain else []
            model_chooser.note([model_chooser.Failure(model) for model in earlier], answered=True)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        request_id = getattr(request.state, "request_id", "unknown")
        logger.info(
            "request %s: extracted invoice model=%s fallback=%s elapsed_ms=%.0f",
            request_id,
            model_used,
            fallback_triggered,
            elapsed_ms,
        )
        # What the document said stays out of the log at its usual level: on a public
        # deployment these are visitors' documents, and a host keeps its logs for days.
        logger.debug(
            "request %s: vendor=%r number=%r", request_id, invoice.vendor_name, invoice.invoice_number
        )
        return ExtractResponse(
            success=True,
            data=invoice,
            model_used=model_used,
            execution_time_ms=elapsed_ms,
            fallback_triggered=fallback_triggered,
        )

    # --- Playground ----------------------------------------------------------
    # The home page and its /playground/* routes. They sit beside the public API
    # rather than in it, so none of them appears in the OpenAPI schema.
    if settings.playground_enabled:
        install_playground(app)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - manual launch helper
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "app.main:app",
        # Loopback only, so the address uvicorn prints is one a browser can open.
        # To serve other machines: uvicorn app.main:app --host 0.0.0.0
        host="127.0.0.1",
        port=8000,
        reload=_settings.environment == "development",
        log_level=_settings.log_level.lower(),
    )

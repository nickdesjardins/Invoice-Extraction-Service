"""The playground: a home page that drives the real extraction pipeline.

Routes, all left out of the OpenAPI schema because the public API is still just
``/health`` and ``/api/v1/extract``:

* ``GET  /``                   - the page.
* ``GET  /playground/config``  - model chain, scenarios, fault catalogue, benchmark snapshot.
* ``POST /playground/extract`` - one extraction, streamed as NDJSON events.
* ``POST /playground/tests``   - run the pytest suite (never in production).
* ``GET  /playground/models``  - how the service looks after its chain: the whole picture.
* ``POST /playground/models/test`` - test the free models now.
* ``/playground/assets/*``     - stylesheet, scripts, icon.

``/playground/extract`` exists because the public endpoint reports only the winner:
``model_used``, ``execution_time_ms`` and ``fallback_triggered``. The page also
shows every attempt while it happens, and can make a model fail on purpose. Both
come from :mod:`app.playground.simulation`, wrapped around the unmodified
dispatcher. The last event carries the status and body the public endpoint returns
for the same outcome, produced by the application's own exception handlers.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from app import __version__, budget, model_chooser
from app.config import get_settings
from app.extractors import ExtractionDispatcher, ExtractionError, get_dispatcher
from app.playground import suite
from app.playground.catalogue import BENCHMARK, BENCHMARK_SNAPSHOT, GROUPS, scenarios_for
from app.playground.simulation import (
    FAULTS,
    SIMULATED_TIMEOUT_SECONDS,
    FaultId,
    traced_dispatcher,
)
from app.schemas import ErrorResponse, ExtractRequest, ExtractResponse

logger = logging.getLogger("app.playground")

STATIC_DIR = Path(__file__).parent / "static"

#: Everything the page loads is served from this origin, so nothing else is allowed.
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'"
    ),
    "Cache-Control": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

#: Response headers of the public endpoint that are part of its contract.
_REPORTED_HEADERS = ("retry-after",)

_TEXT_SCHEMA = ExtractRequest.model_json_schema()["properties"]["text"]

router = APIRouter(include_in_schema=False)


class PlaygroundExtractRequest(ExtractRequest):
    """An extraction request plus an optional plan of simulated failures."""

    simulate: list[Optional[FaultId]] = Field(
        default_factory=list,
        description=(
            "One entry per chain position: a fault to simulate there, or null to call "
            "the real model. Missing trailing entries mean real calls."
        ),
    )


def same_origin_only(request: Request) -> None:
    """Refuse requests that did not come from the playground's own page.

    The playground's POST routes spend model allowance or start a process, so they
    are meant for the page itself. A browser says where a request comes from, and
    three cases follow from what it says:

    * ``Sec-Fetch-Site`` is there: only ``same-origin`` (the page's own script) and
      ``none`` (the visitor's own action) pass.
    * It is missing but ``Origin`` is there, as from a browser too old for the first
      header: the request passes only when that origin is this service's own host.
    * Neither is there, so the caller is no browser (curl, a script, the test suite).
      That is welcome while developing and refused in production, where the public
      API is the way in for programs.

    Any client can forge these headers. They stop another site's page from spending
    the allowance through a visitor's browser; :mod:`app.budget` is what limits how
    much can be spent at all.
    """
    site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    if site is not None:
        allowed = site in ("same-origin", "none")
    elif origin is not None:
        try:
            origin_host = urlsplit(origin).netloc.lower()
        except ValueError:  # no address at all, such as "http://[": not this service's own
            origin_host = ""
        # Two empty names are no match: a request that names no host proves nothing.
        allowed = origin_host != "" and origin_host == request.headers.get("host", "").lower()
    else:
        allowed = not get_settings().is_production
    if not allowed:
        raise HTTPException(
            status_code=403, detail="The playground only accepts requests from its own page."
        )


class _RevalidatedStaticFiles(StaticFiles):
    """Static assets the browser must revalidate, so an edited file is never served stale."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def install_playground(app: FastAPI) -> None:
    """Add the home page and its support routes to ``app``."""
    app.include_router(router)
    app.mount(
        "/playground/assets",
        _RevalidatedStaticFiles(directory=STATIC_DIR / "assets"),
        name="playground-assets",
    )


# ---------------------------------------------------------------------------
# Page and configuration
# ---------------------------------------------------------------------------


@router.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=_PAGE_HEADERS)


@router.get("/playground/config")
async def playground_config(request: Request) -> dict[str, Any]:
    """Everything the page needs to draw itself for this deployment."""
    settings = get_settings()
    extractors = get_dispatcher().extractors
    benchmark = {row["model"]: row for row in BENCHMARK}
    suite_available, suite_reason = suite.availability()
    # Filled in by the startup check in app.main; empty when that did not run.
    listings = getattr(request.app.state, "model_listings", {})
    return {
        "service": {
            "name": settings.service_name,
            "version": __version__,
            "environment": settings.environment,
            "docs_url": request.app.docs_url,
        },
        "chain": [
            {
                "index": index,
                "model": extractor.model,
                "role": "primary" if index == 0 else "fallback",
                "configured": extractor.is_configured(),
                "benchmark": benchmark.get(extractor.model),
                # What OpenRouter's catalogue said at startup, or None if it was not asked.
                "listing": (
                    {
                        "status": listings[extractor.model].status,
                        "expires": listings[extractor.model].expires,
                    }
                    if extractor.model in listings
                    else None
                ),
            }
            for index, extractor in enumerate(extractors)
        ],
        "limits": {
            "min_chars": _TEXT_SCHEMA["minLength"],
            "max_chars": _TEXT_SCHEMA["maxLength"],
            "timeout_seconds": settings.timeout_seconds,
            "simulated_timeout_seconds": SIMULATED_TIMEOUT_SECONDS,
            # The one ceiling on live model calls, for all visitors together (app.budget).
            "calls_per_minute": budget.MINUTE_CEILING,
        },
        "faults": [
            {"id": fault.id, "label": fault.label, "summary": fault.summary}
            for fault in FAULTS.values()
        ],
        "groups": list(GROUPS),
        "scenarios": scenarios_for(len(extractors)),
        # A dated record, not a live reading: see BENCHMARK_SNAPSHOT.
        "benchmark": {**BENCHMARK_SNAPSHOT, "rows": list(BENCHMARK)},
        "suite": {"available": suite_available, "reason": suite_reason},
    }


# ---------------------------------------------------------------------------
# Traced extraction
# ---------------------------------------------------------------------------


@router.post("/playground/extract", dependencies=[Depends(same_origin_only)])
async def playground_extract(
    payload: PlaygroundExtractRequest, request: Request
) -> StreamingResponse:
    """Run one extraction and stream what happens, one JSON object per line.

    Events, in order: ``start``, then ``attempt_started`` / ``attempt_finished`` for
    each model the chain tries, then one ``result``. A request that fails validation
    never starts a stream; it gets the same 422 envelope as the public endpoint.
    """
    chain_length = len(get_dispatcher().extractors)
    # A page that was drawn for a longer chain (the service has repaired its chain
    # since, or woke up on a shorter one) still sends "call the real model" for the
    # places that are gone. Nothing is asked of a model that is not there, so that is
    # no mistake of the visitor's. A failure to simulate in such a place is one.
    if any(payload.simulate[chain_length:]):
        raise RequestValidationError(
            [
                {
                    "type": "too_long",
                    "loc": ("body", "simulate"),
                    "msg": (
                        f"List should have at most {chain_length} items, "
                        "one per model in the chain"
                    ),
                }
            ]
        )
    return StreamingResponse(
        _events(payload.text, payload.simulate[:chain_length], request),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _events(
    text: str, simulate: list[Optional[str]], request: Request
) -> AsyncIterator[bytes]:
    events: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()
    real: list[dict[str, Any]] = []  # the attempts of this run that called a model

    def listener(event: dict[str, Any]) -> None:
        events.put_nowait(event)
        if event["simulated"]:
            return
        if event["event"] == "attempt_started":
            real.append(event)
        else:
            real[-1] = event

    dispatcher = traced_dispatcher(simulate, listener)

    async def run() -> None:
        try:
            events.put_nowait(await _result_event(dispatcher, text, request))
            # A real model that did not answer is news for the chooser. A run with a
            # simulated failure in it is not evidence: nobody knows what the real
            # model in that place would have done.
            failed = [attempt for attempt in real if attempt.get("outcome") == "failed"]
            if failed and not any(simulate):
                model_chooser.note(
                    [
                        model_chooser.Failure(
                            a["model"], a["error_type"], a["retryable"], a["message"]
                        )
                        for a in failed
                    ],
                    answered=len(failed) < len(real),
                )
        finally:
            events.put_nowait(None)  # always release the reader below

    yield _line(
        {
            "event": "start",
            "request_id": getattr(request.state, "request_id", None),
            "chain": [
                {
                    "index": index,
                    "model": extractor.model,
                    "role": "primary" if index == 0 else "fallback",
                    "fault": simulate[index] if index < len(simulate) else None,
                }
                for index, extractor in enumerate(dispatcher.extractors)
            ],
        }
    )
    task = asyncio.create_task(run())
    try:
        while (event := await events.get()) is not None:
            yield _line(event)
    finally:
        # Reached with the task still running only when the client went away;
        # cancelling stops the chain from spending further model calls.
        task.cancel()


def _line(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


async def _result_event(
    dispatcher: ExtractionDispatcher, text: str, request: Request
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        status_code, headers, body = await _public_outcome(dispatcher, text, request)
    except Exception:  # noqa: BLE001 - the stream has begun, so a crash must travel as an event
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception("request %s: unhandled exception in the playground", request_id)
        status_code, headers = 500, {}
        body = ErrorResponse(
            error="internal_error",
            message="An unexpected error occurred. The incident has been logged.",
            detail={"request_id": request_id},
        ).model_dump()
    return {
        "event": "result",
        "status_code": status_code,
        "headers": headers,
        "body": body,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _public_outcome(
    dispatcher: ExtractionDispatcher, text: str, request: Request
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Run the chain and report it as ``POST /api/v1/extract`` would: status, headers, body."""
    started = time.perf_counter()
    try:
        invoice, model_used, fallback_triggered = await dispatcher.extract(text)
    except ExtractionError as exc:
        response = await _answer_like_the_api(request, exc)
        headers = {
            name: response.headers[name] for name in _REPORTED_HEADERS if name in response.headers
        }
        return response.status_code, headers, json.loads(response.body)

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    request_id = getattr(request.state, "request_id", "unknown")
    logger.info(
        "request %s: playground extracted invoice model=%s fallback=%s elapsed_ms=%.0f",
        request_id,
        model_used,
        fallback_triggered,
        elapsed_ms,
    )
    # As in the public endpoint: what the document said is logged at DEBUG only.
    logger.debug(
        "request %s: vendor=%r number=%r", request_id, invoice.vendor_name, invoice.invoice_number
    )
    body = ExtractResponse(
        success=True,
        data=invoice,
        model_used=model_used,
        execution_time_ms=elapsed_ms,
        fallback_triggered=fallback_triggered,
    )
    return 200, {}, body.model_dump(mode="json")


async def _answer_like_the_api(request: Request, exc: ExtractionError) -> Response:
    """Ask the application's own exception handlers how they answer ``exc``.

    Reusing them, rather than restating their envelopes here, is what guarantees
    the page shows exactly what the public endpoint returns (and logs the same).
    """
    for cls in type(exc).__mro__:
        handler = request.app.exception_handlers.get(cls)
        if handler is not None:
            response = handler(request, exc)
            return await response if inspect.isawaitable(response) else response
    raise exc


# ---------------------------------------------------------------------------
# The chain's upkeep
# ---------------------------------------------------------------------------


@router.get("/playground/models")
async def models_picture() -> JSONResponse:
    """How the service looks after its model chain: the whole picture, every time.

    Not a change to the answer before, so the page needs no memory of its own: a
    tab opened in the middle of a test, a second tab and a reload all draw the same
    thing. The page asks again every two seconds while something is going on. Asking
    starts nothing and calls no model.
    """
    return JSONResponse(model_chooser.picture(), headers={"Cache-Control": "no-store"})


@router.post("/playground/models/test", dependencies=[Depends(same_origin_only)])
async def start_models_test() -> JSONResponse:
    """Test the free models now. Every guard is in :func:`app.model_chooser.start`.

    The test belongs to the service, not to the tab that asked: closing the tab
    stops nothing, and every tab sees it through ``GET /playground/models``.
    """
    code, again_at = await model_chooser.start("page", {"code": "asked", "models": []})
    if code == "started":
        return JSONResponse({"started": True}, status_code=202)
    body = ErrorResponse(
        error="test_refused",
        message="A test of the models cannot start right now.",
        detail={"reason": code, "again_at": again_at},
    )
    return JSONResponse(body.model_dump(), status_code=409)


# ---------------------------------------------------------------------------
# Automated test suite
# ---------------------------------------------------------------------------


@router.post("/playground/tests")
def run_tests(request: Request) -> dict[str, Any]:
    """Run the pytest suite and return every test's outcome.

    A plain ``def`` on purpose: FastAPI runs it in a worker thread, so the seconds
    pytest takes do not block the event loop.
    """
    available, reason = suite.availability()
    if not available:
        raise HTTPException(status_code=404, detail=reason)
    # Asked after the 404 above, not before it: where the runner is switched off
    # there is nothing to guard, and every caller is told the same thing.
    same_origin_only(request)
    if not suite.RUN_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A test run is already in progress.")
    try:
        return suite.run_suite()
    except suite.SuiteError as exc:
        logger.error("the test suite could not be run: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        suite.RUN_LOCK.release()

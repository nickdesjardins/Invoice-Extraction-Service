"""Tests for the size limit on request bodies (``app.main.BodyLimitMiddleware``).

A body larger than ``MAX_BODY_BYTES`` gets the 422 ``validation_error`` envelope, and
gets it without being read into memory. That second half is what these tests watch:
each oversized body is a generator that counts how many pieces were taken from it.

They run in process like the rest of the suite, so nothing here shows what a real
server does with the connection afterwards.

The limit is the innermost of three layers around the routes (the request context
and CORS are the other two), which is why its refusal leaves with the usual headers.
The last test here holds the order of the outer two.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable

import httpx
import pytest

from app.main import MAX_BODY_BYTES, app
from tests.test_extract import (  # noqa: F401 - `http`, `http_raw` and `router` are fixtures
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    http,
    http_raw,
    ok,
    router,
)

#: What every oversized body is answered with, whichever way it was found out.
REFUSAL = {
    "success": False,
    "error": "validation_error",
    "message": "Request body failed validation.",
    "detail": [
        {
            "type": "bytes_too_long",
            "loc": ["body"],
            "msg": "Request body should have at most 1000000 bytes",
        }
    ],
}

PIECE = b"x" * 65_536
#: 20 MB, should anybody read it all.
PIECES_IN_ALL = 320

JSON = {"Content-Type": "application/json"}
POST_ROUTES = ["/api/v1/extract", "/playground/extract"]


class Body:
    """A request body that arrives in pieces and counts the pieces taken from it."""

    def __init__(self) -> None:
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(PIECES_IN_ALL):
            self.pulled += 1
            yield PIECE


async def in_pieces(body: bytes) -> AsyncIterator[bytes]:
    """Any body, sent without a declared length."""
    for start in range(0, len(body), len(PIECE)):
        yield body[start : start + len(PIECE)]


def document(total_bytes: int) -> bytes:
    """A JSON body of exactly ``total_bytes``, far too long a text to be a valid request."""
    frame = len(b'{"text": ""}')
    return b'{"text": "' + b"x" * (total_bytes - frame) + b'"}'


# ---------------------------------------------------------------------------
# Body size limit - an oversized body is refused before it is read
# ---------------------------------------------------------------------------


def test_the_limit_is_a_megabyte_and_fits_every_valid_document() -> None:
    """50,000 characters are at most 600,000 bytes of JSON, however they are escaped."""
    assert MAX_BODY_BYTES == 1_000_000
    largest = json.dumps({"text": "\N{GRINNING FACE}" * 50_000, "simulate": ["rate_limit"] * 9})
    assert 600_000 < len(largest.encode()) < MAX_BODY_BYTES


@pytest.mark.parametrize("path", POST_ROUTES)
@pytest.mark.asyncio
async def test_a_declared_length_over_the_limit_is_refused_before_a_byte_is_read(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], path: str
) -> None:
    fake = router(default=ok())
    body = Body()

    response = await http.post(
        path,
        content=body,
        headers={**JSON, "Content-Length": "20000000", "Origin": "https://app.example"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert response.json() == REFUSAL
    assert body.pulled == 0
    assert fake.calls == []
    # The service's usual headers: a refusal is answered like everything else.
    assert response.headers["x-request-id"]
    assert float(response.headers["x-process-time-ms"]) >= 0
    assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize("path", POST_ROUTES)
@pytest.mark.asyncio
async def test_a_body_with_no_declared_length_is_counted_as_it_arrives(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], path: str
) -> None:
    fake = router(default=ok())
    body = Body()

    response = await http.post(
        path, content=body, headers={**JSON, "Origin": "https://app.example"}
    )

    assert "content-length" not in response.request.headers  # it really was undeclared
    assert response.status_code == 422
    assert response.json() == REFUSAL
    # Sixteen pieces are 1,048,576 bytes: the first piece past the limit ends the reading.
    assert body.pulled == 16
    assert fake.calls == []
    assert response.headers["x-request-id"]
    assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize("declared", [True, False], ids=["declared", "undeclared"])
@pytest.mark.asyncio
async def test_a_body_of_exactly_the_limit_is_read_and_one_byte_more_is_not(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], declared: bool
) -> None:
    router(default=ok())

    async def post(body: bytes) -> httpx.Response:
        content: Any = body if declared else in_pieces(body)
        return await http.post("/api/v1/extract", content=content, headers=JSON)

    at_the_limit = await post(document(MAX_BODY_BYTES))
    one_more = await post(document(MAX_BODY_BYTES + 1))

    # At the limit the body is read, and it is validation that finds the text too long.
    assert at_the_limit.status_code == 422
    assert [entry["type"] for entry in at_the_limit.json()["detail"]] == ["string_too_long"]
    assert one_more.json() == REFUSAL


@pytest.mark.asyncio
async def test_the_largest_valid_document_still_gets_through(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    """The limit must refuse nothing that is a valid request, in its bulkiest spelling."""
    fake = router(default=ok())
    text = "\N{GRINNING FACE}" * 50_000  # each one is twelve bytes once JSON has escaped it
    body = json.dumps({"text": text}).encode()
    assert len(body) > 600_000

    response = await http.post("/api/v1/extract", content=body, headers=JSON)

    assert response.status_code == 200, response.text
    assert text in fake.calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_a_declared_length_that_lies_is_caught_by_the_count(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    """A real server holds a client to its declared length. The count does not rely on it."""
    router(default=ok())
    body = Body()

    response = await http.post(
        "/api/v1/extract", content=body, headers={**JSON, "Content-Length": "10"}
    )

    assert response.json() == REFUSAL
    assert body.pulled == 16


@pytest.mark.asyncio
async def test_a_length_that_is_not_a_number_leaves_it_to_the_count(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    router(default=ok())
    small = json.dumps({"text": SAMPLE_INVOICE_TEXT}).encode()

    response = await http.post(
        "/api/v1/extract", content=in_pieces(small), headers={**JSON, "Content-Length": "lots"}
    )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_a_body_sent_in_pieces_is_counted_whatever_length_is_also_declared(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    """A server that takes both headers reads the pieces, not the length. So does the limit."""
    router(default=ok())
    small = json.dumps({"text": SAMPLE_INVOICE_TEXT}).encode()
    both = {**JSON, "Content-Length": "2000000", "Transfer-Encoding": "chunked"}

    answered = await http.post("/api/v1/extract", content=in_pieces(small), headers=both)
    body = Body()
    declared = {**both, "Content-Length": "20"}
    refused = await http.post("/api/v1/extract", content=body, headers=declared)

    assert answered.status_code == 200, answered.text
    assert refused.json() == REFUSAL and body.pulled == 16  # the count still ends the reading


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/health"), ("GET", "/"), ("POST", "/playground/tests"), ("POST", "/no-such-route")],
)
@pytest.mark.asyncio
async def test_the_limit_holds_on_every_route(
    http: httpx.AsyncClient, method: str, path: str
) -> None:
    """One rule for the whole service: a route that ignores its body is no way around it."""
    response = await http.request(method, path, headers={"Content-Length": "20000000"})

    assert response.status_code == 422
    assert response.json() == REFUSAL


@pytest.mark.asyncio
async def test_a_refused_body_is_logged_under_its_request_id(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="app.main"):
        await http.post(
            "/api/v1/extract",
            content=Body(),
            headers={**JSON, "Content-Length": "20000000", "X-Request-ID": "req-big-1"},
        )
        await http.post(
            "/api/v1/extract", content=Body(), headers={**JSON, "X-Request-ID": "req-big-2"}
        )

    assert [record.getMessage() for record in caplog.records] == [
        "request req-big-1: refused a body of more than 1000000 bytes",
        "request req-big-2: refused a body of more than 1000000 bytes",
    ]


# ---------------------------------------------------------------------------
# Body size limit - what it leaves alone
# ---------------------------------------------------------------------------

#: Bodies under the limit that no JSON decoder can read. Built here and not in the
#: parameters: pytest puts every parameter into the test's name.
UNDECODABLE = {
    "not-utf-8": b'{"text": "Invoice ' + bytes([0xFF, 0xFE]) + b' NW-2024-0187 total"}',
    "nested-too-deep": b"[" * 100_000 + b"]" * 100_000,
}


@pytest.mark.parametrize("case", sorted(UNDECODABLE))
@pytest.mark.asyncio
async def test_a_body_that_cannot_be_decoded_is_still_a_400_in_the_envelope(
    http_raw: httpx.AsyncClient, case: str
) -> None:
    """A fifth status of POST /api/v1/extract, and older than the limit: FastAPI's own
    answer to a body it cannot read as JSON at all. Pinned here so that it is known."""
    response = await http_raw.post("/api/v1/extract", content=UNDECODABLE[case], headers=JSON)

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "error": "http_error",
        "message": "There was an error parsing the body",
        "detail": None,
    }


@pytest.mark.asyncio
async def test_startup_and_shutdown_pass_through_the_limit_untouched() -> None:
    """A server sends more than requests through these layers, and "lifespan" has no
    headers to read. Without the pass-through the service would start and never run
    its own startup."""
    inbox: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    inbox.put_nowait({"type": "lifespan.startup"})
    inbox.put_nowait({"type": "lifespan.shutdown"})
    sent: list[str] = []

    async def send(message: dict[str, str]) -> None:
        sent.append(message["type"])

    await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, inbox.get, send)

    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# ---------------------------------------------------------------------------
# Body size limit - the layers around it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_crash_is_answered_with_the_headers_a_browser_needs(
    http_raw: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CORS is the outermost layer: without its header a page on another origin could
    not even read the 500 that the request-context layer writes."""

    async def boom(_: str) -> None:
        raise RuntimeError("unexpected boom")

    monkeypatch.setattr("app.main.extract_invoice", boom)

    response = await http_raw.post(
        "/api/v1/extract",
        json={"text": SAMPLE_INVOICE_TEXT},
        headers={"Origin": "https://app.example"},
    )

    assert response.status_code == 500 and response.json()["error"] == "internal_error"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["x-request-id"] == response.json()["detail"]["request_id"]
    assert "X-Request-ID" in response.headers["access-control-expose-headers"]

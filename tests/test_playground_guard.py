"""Tests for the guard on the playground's POST routes (``same_origin_only``).

The three routes spend model allowance or start a process, so they are for the page
itself. A browser says where a request comes from; these tests go through what it
can say, in the environments where the answer differs.

The guard is not a lock: any program can send the headers a browser would. What it
stops is another site's page using a visitor's browser, and in production a caller
that is no browser at all.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx
import pytest

from app import extractors
from app.playground import suite
from tests.test_extract import (  # noqa: F401 - `http` and `router` are fixtures
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    http,
    ok,
    router,
)
from tests.test_model_chooser import (  # noqa: F401 - `world` is a fixture
    FALLBACK,
    HEALTHY,
    PRIMARY,
    World,
    settled,
    world,
)
from tests.test_playground import result_of

#: The test client calls the service "testserver", so that is its Host.
OWN = "http://testserver"
REFUSED = "The playground only accepts requests from its own page."

#: (Sec-Fetch-Site, Origin, passes). The same in every environment.
WHAT_A_BROWSER_SAYS: list[tuple[Optional[str], Optional[str], bool]] = [
    # A browser that sends Sec-Fetch-Site is taken at its word, whatever Origin says.
    ("same-origin", OWN, True),
    ("same-origin", None, True),
    ("none", None, True),
    ("cross-site", "https://evil.example", False),
    ("cross-site", OWN, False),
    ("same-site", "http://other.testserver", False),
    ("cross-origin", OWN, False),  # not a value browsers send: unknown means no
    ("", OWN, False),
    # A browser too old for that header still names the page's origin on a POST.
    (None, OWN, True),
    (None, "https://testserver", True),  # the host is what is compared, not the scheme
    (None, "http://TESTSERVER", True),
    (None, "https://evil.example", False),
    (None, "http://testserver:8000", False),  # another port is another origin
    (None, "http://testserver.evil.example", False),
    (None, "http://evil.example/testserver", False),
    (None, "null", False),  # what a sandboxed frame or a local file sends
    (None, "http://[", False),  # no address at all: refused like any other origin, not a crash
    (None, "http://[::1", False),
    (None, "", False),
]


def headers_for(site: Optional[str], origin: Optional[str]) -> dict[str, str]:
    headers = {}
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    if origin is not None:
        headers["Origin"] = origin
    return headers


def in_environment(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", name)
    extractors.reset_extractors()


# ---------------------------------------------------------------------------
# Playground guard - who may start a run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize(("site", "origin", "passes"), WHAT_A_BROWSER_SAYS)
@pytest.mark.asyncio
async def test_a_run_starts_only_for_the_page_itself(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    monkeypatch: pytest.MonkeyPatch,
    allowance: Callable[..., list[None]],
    environment: str,
    site: Optional[str],
    origin: Optional[str],
    passes: bool,
) -> None:
    in_environment(environment, monkeypatch)
    fake = router(default=ok())
    readings = allowance(remaining=900)

    response = await http.post(
        "/playground/extract", json={"text": SAMPLE_INVOICE_TEXT}, headers=headers_for(site, origin)
    )

    if passes:
        assert result_of(response)["status_code"] == 200
    else:
        assert response.status_code == 403
        assert response.json() == {
            "success": False,
            "error": "http_error",
            "message": REFUSED,
            "detail": None,
        }
        # A refused request spends nothing: no model, and no question to OpenRouter.
        assert fake.calls == [] and readings == []


@pytest.mark.parametrize(
    ("environment", "passes"),
    [("development", True), ("test", True), ("staging", True), ("production", False)],
)
@pytest.mark.asyncio
async def test_a_caller_that_is_no_browser_is_refused_in_production_only(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    passes: bool,
) -> None:
    """curl and scripts send neither header. Handy while developing; in production
    the public API is the way in for programs."""
    in_environment(environment, monkeypatch)
    fake = router(default=ok())

    response = await http.post("/playground/extract", json={"text": SAMPLE_INVOICE_TEXT})
    press = await http.post("/playground/models/test")

    assert "origin" not in response.request.headers
    assert "sec-fetch-site" not in response.request.headers
    if passes:
        assert result_of(response)["status_code"] == 200
        # Past the guard. This suite names its models, so no test starts: see below.
        assert press.status_code == 409 and press.json()["detail"]["reason"] == "pinned"
    else:
        assert response.status_code == 403
        assert response.json()["message"] == REFUSED
        assert fake.calls == []
        assert press.status_code == 403 and press.json()["message"] == REFUSED


@pytest.mark.asyncio
async def test_reading_the_page_needs_no_header_even_in_production(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is on the POST routes. A crawler or a health probe is no browser either."""
    in_environment("production", monkeypatch)

    assets = "/playground/assets/app.js"
    for path in ("/", "/playground/config", "/playground/models", assets, "/health"):
        assert (await http.get(path)).status_code == 200, path
        cross_site = await http.get(path, headers={"Sec-Fetch-Site": "cross-site"})
        assert cross_site.status_code == 200, path


@pytest.mark.asyncio
async def test_the_public_api_is_not_behind_the_guard(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], monkeypatch: pytest.MonkeyPatch
) -> None:
    in_environment("production", monkeypatch)
    router(default=ok())

    for headers in ({}, {"Sec-Fetch-Site": "cross-site", "Origin": "https://app.example"}):
        response = await http.post(
            "/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT}, headers=headers
        )
        assert response.status_code == 200, headers


# ---------------------------------------------------------------------------
# Playground guard - who may start a test of the models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize(("site", "origin", "passes"), WHAT_A_BROWSER_SAYS)
@pytest.mark.asyncio
async def test_a_test_of_the_models_starts_only_for_the_page_itself(
    http: httpx.AsyncClient,
    world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
    allowance: Callable[..., list[None]],
    environment: str,
    site: Optional[str],
    origin: Optional[str],
    passes: bool,
) -> None:
    """The button spends more than a run does: up to 42 free requests a press."""
    made = world([PRIMARY, FALLBACK], HEALTHY)  # the setting left blank: a press can start a test
    in_environment(environment, monkeypatch)
    readings = allowance(remaining=900)

    response = await http.post("/playground/models/test", headers=headers_for(site, origin))

    if passes:
        assert response.status_code == 202 and response.json() == {"started": True}
        await settled()
        assert len(made.test_calls) == 14  # the chain's two models, seven documents each
    else:
        assert response.status_code == 403
        assert response.json() == {
            "success": False,
            "error": "http_error",
            "message": REFUSED,
            "detail": None,
        }
        # A refused press spends nothing: no model, no list, no question to OpenRouter.
        await settled()
        assert made.test_calls == [] and made.list_reads == 0 and readings == []


# ---------------------------------------------------------------------------
# Playground guard - who may start the test suite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("site", "origin", "passes"), WHAT_A_BROWSER_SAYS)
@pytest.mark.asyncio
async def test_the_suite_starts_only_for_the_page_itself(
    http: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    site: Optional[str],
    origin: Optional[str],
    passes: bool,
) -> None:
    outcome = {"summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0}, "sections": []}
    started: list[None] = []
    monkeypatch.setattr(suite, "run_suite", lambda: started.append(None) or outcome)

    response = await http.post("/playground/tests", headers=headers_for(site, origin))

    assert response.status_code == (200 if passes else 403)
    assert len(started) == (1 if passes else 0)
    if not passes:
        assert response.json()["message"] == REFUSED


@pytest.mark.parametrize(
    "headers",
    [{}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}],
    ids=["no-browser", "the-page", "another-site", "sandboxed"],
)
@pytest.mark.asyncio
async def test_where_the_suite_is_switched_off_every_caller_is_told_so(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    """In production there is nothing to guard, so "not here" comes before "not you"."""
    in_environment("production", monkeypatch)
    monkeypatch.setattr(suite, "run_suite", lambda: pytest.fail("must not run in production"))

    response = await http.post("/playground/tests", headers=headers)

    assert response.status_code == 404
    assert "production" in response.json()["message"]


# ---------------------------------------------------------------------------
# Playground guard - a request that names no host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", [None, ""], ids=["no-host", "empty-host"])
@pytest.mark.parametrize("origin", ["null", "", "evil.example"])
@pytest.mark.asyncio
async def test_an_origin_without_a_host_matches_no_request_without_one(
    router: Callable[..., FakeRouter],
    monkeypatch: pytest.MonkeyPatch,
    host: Optional[str],
    origin: str,
) -> None:
    """Two empty names are equal, and that must not count as "the service's own host".

    The test client always names a host, so this request is handed to the application
    as a server would hand it over: HTTP/1.0, where a Host header is not required.
    """
    from app.main import app

    in_environment("production", monkeypatch)
    fake = router(default=ok())
    headers = [(b"content-type", b"application/json"), (b"origin", origin.encode())]
    if host is not None:
        headers.append((b"host", host.encode()))
    body = json.dumps({"text": SAMPLE_INVOICE_TEXT}).encode()
    messages: list[dict[str, Any]] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.0",
        "method": "POST",
        "scheme": "http",
        "path": "/playground/extract",
        "raw_path": b"/playground/extract",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("203.0.113.5", 1234),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)

    assert sent[0]["status"] == 403
    assert fake.calls == []

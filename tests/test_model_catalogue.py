"""Tests for the model catalogue check (``app.model_catalogue``).

The check asks OpenRouter whether each configured model is still listed. These
tests never reach OpenRouter: the lookup function takes its HTTP call as an
argument, and the startup tests replace the whole check with a stand-in.

Two rules are pinned here because the rest of the design leans on them:

* only OpenRouter's own "Model not found" answer counts as "not listed";
* the check reports and never changes what the API answers.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from datetime import date
from email.message import Message
from typing import Any, Callable, Optional

import httpx
import openai
import pytest

from app import extractors, main, model_catalogue
from app.config import Settings
from app.main import app
from app.model_catalogue import Listing, check_chain, describe, look_up, lookup_url
from tests.conftest import FALLBACK_MODEL, MODEL_CHAIN, PRIMARY_MODEL
from tests.test_extract import (
    EMPTY_INVOICE_PAYLOAD,
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    api_error,
    ok,
)

BASE = "https://openrouter.ai/api/v1"


def answering(status: int, body: Any) -> Any:
    """A stand-in for the HTTP call that always gives the same answer and records its calls."""
    calls: list[str] = []

    def fetch(url: str) -> tuple[int, str]:
        calls.append(url)
        return status, body if isinstance(body, str) else json.dumps(body)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


# ---------------------------------------------------------------------------
# Model catalogue check - what one lookup concludes
# ---------------------------------------------------------------------------


def test_lookup_address_is_built_without_encoding_or_doubled_slashes() -> None:
    """A wrong address makes OpenRouter say "Not Found" about a model that is alive."""
    assert lookup_url(BASE + "/", "liquid/lfm-2.5-2.6b:free") == (
        "https://openrouter.ai/api/v1/model/liquid/lfm-2.5-2.6b:free"
    )


def test_listed_model() -> None:
    listing = look_up(
        "a/b:free", BASE, answering(200, {"data": {"id": "a/b:free", "expiration_date": None}})
    )
    assert listing == Listing("a/b:free", "listed")
    assert not listing.needs_attention()
    assert describe(listing) == "a/b:free is listed."


def test_withdrawn_model_is_not_listed() -> None:
    fetch = answering(404, {"error": {"message": "Model not found: a/b:free", "code": 404}})

    listing = look_up("a/b:free", BASE, fetch)

    assert listing.status == "not_listed"
    assert listing.needs_attention()
    # The message has to say what to do, because it is all the owner will read.
    assert "OPENROUTER_MODELS" in describe(listing) and "a/b:free" in describe(listing)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {"error": {"message": "Not Found", "code": 404}}),  # wrong address, not a verdict
        (404, "<html>not found</html>"),
        (200, "<html>maintenance</html>"),
        (200, {"data": []}),
        (200, {"data": {"name": "no id here"}}),
        (301, ""),
        (401, {"error": {"message": "No auth credentials found"}}),
        (429, {"error": {"message": "slow down"}}),
        (503, ""),
    ],
)
def test_any_other_answer_is_unknown(status: int, body: Any) -> None:
    """Only a clear statement about the model may be read as one."""
    listing = look_up("a/b:free", BASE, answering(status, body))
    assert listing.status == "unknown"
    assert not listing.needs_attention()


def test_unreachable_catalogue_is_unknown() -> None:
    def fetch(url: str) -> tuple[int, str]:
        raise TimeoutError("timed out")

    listing = look_up("a/b:free", BASE, fetch)

    assert listing.status == "unknown"
    assert "TimeoutError" in listing.note


@pytest.mark.parametrize("raw", ["2026-12-31", "2026-12-31T00:00:00Z"])
def test_end_date_is_read_tolerantly(raw: str) -> None:
    listing = look_up(
        "a/b:free", BASE, answering(200, {"data": {"id": "a/b:free", "expiration_date": raw}})
    )

    assert listing.expires == "2026-12-31"
    assert listing.days_left(date(2026, 12, 21)) == 10
    assert listing.needs_attention(date(2026, 12, 21))
    assert not listing.needs_attention(date(2026, 10, 7))
    # The window is fourteen days, counted inclusively.
    assert listing.needs_attention(date(2026, 12, 17))
    assert not listing.needs_attention(date(2026, 12, 16))
    assert "2026-12-31" in describe(listing, date(2026, 12, 21))


def test_unreadable_end_date_is_ignored() -> None:
    listing = look_up(
        "a/b:free", BASE, answering(200, {"data": {"id": "a/b:free", "expiration_date": "soon"}})
    )
    assert listing == Listing("a/b:free", "listed")


def test_end_date_of_a_different_entry_is_not_reported() -> None:
    """A routing variant is answered with the base model's entry, whose date is not its own."""
    fetch = answering(200, {"data": {"id": "a/b", "expiration_date": "2026-12-31"}})
    assert look_up("a/b:nitro", BASE, fetch) == Listing("a/b:nitro", "listed")


@pytest.mark.parametrize("model", ["@preset/mine", "no-slash"])
def test_ids_the_catalogue_cannot_hold_are_not_looked_up(model: str) -> None:
    fetch = answering(404, {"error": {"message": f"Model not found: {model}"}})

    assert look_up(model, BASE, fetch).status == "unknown"
    assert fetch.calls == []


def test_deeply_nested_answer_is_unknown_not_a_crash() -> None:
    """One absurd answer must not cost the answers about the other models."""
    depth = 200_000
    listing = look_up("a/b:free", BASE, answering(200, "[" * depth + "]" * depth))
    assert listing.status == "unknown"


# ---------------------------------------------------------------------------
# Model catalogue check - the request it sends
# ---------------------------------------------------------------------------


class FakeAnswer:
    """What ``urllib`` hands back: a status, and a body that can be read in part."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.asked_for: list[Optional[int]] = []

    def read(self, amount: Optional[int] = None) -> bytes:
        self.asked_for.append(amount)
        return self._body if amount is None else self._body[:amount]

    def __enter__(self) -> "FakeAnswer":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_request_names_its_client_and_reads_a_bounded_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = FakeAnswer(200, b"x" * (model_catalogue.MAX_ANSWER_BYTES + 10))
    seen: list[tuple[Any, float]] = []

    class Opener:
        def open(self, request: Any, timeout: float) -> FakeAnswer:
            seen.append((request, timeout))
            return answer

    monkeypatch.setattr(model_catalogue, "_OPENER", Opener())

    status, body = model_catalogue._http_get(BASE + "/model/a/b:free")

    ((request, timeout),) = seen
    assert request.full_url == "https://openrouter.ai/api/v1/model/a/b:free"
    # A named client, because OpenRouter refuses urllib's default signature on some paths.
    assert request.get_header("User-agent") == model_catalogue.USER_AGENT
    assert timeout == model_catalogue.LOOKUP_TIMEOUT_SECONDS
    # However much the server sends, only a bounded part of it is read.
    assert answer.asked_for == [model_catalogue.MAX_ANSWER_BYTES]
    assert status == 200 and len(body) == model_catalogue.MAX_ANSWER_BYTES


def test_an_http_error_is_an_answer_to_read_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"error": {"message": "Model not found: a/b:free"}}).encode()

    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", Message(), io.BytesIO(body)
            )

    monkeypatch.setattr(model_catalogue, "_OPENER", Opener())

    assert look_up("a/b:free", BASE).status == "not_listed"


def test_a_listed_answer_is_read_through_the_real_request(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"data": {"id": "a/b:free", "expiration_date": "2026-12-31"}}).encode()

    class Opener:
        def open(self, request: Any, timeout: float) -> FakeAnswer:
            return FakeAnswer(200, body)

    monkeypatch.setattr(model_catalogue, "_OPENER", Opener())

    assert look_up("a/b:free", BASE) == Listing("a/b:free", "listed", expires="2026-12-31")


@pytest.mark.parametrize(
    ("target", "followed"),
    [
        ("https://openrouter.ai/api/v1/model/a/b", True),
        ("https://example.com/api/v1/model/a/b", False),  # another host could say anything
        ("http://openrouter.ai/api/v1/model/a/b", False),  # not the encrypted address
        ("https://openrouter.ai.example.com/model/a/b", False),  # a lookalike host
    ],
)
def test_a_redirect_is_followed_only_on_openrouters_own_host(target: str, followed: bool) -> None:
    """An answer from anywhere else must never be read as OpenRouter's."""
    handler = model_catalogue._SameHostRedirects()
    request = urllib.request.Request(BASE + "/model/a/b:free")

    redirected = handler.redirect_request(request, None, 302, "Found", Message(), target)

    assert (redirected is not None) == followed


# ---------------------------------------------------------------------------
# Model catalogue check - the whole chain, and the command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com/v1",  # another provider has no such route
        "http://openrouter.ai/api/v1",  # not the encrypted address
        "https://openrouter.ai.example.com/api/v1",  # only looks like OpenRouter
    ],
)
def test_only_openrouter_is_ever_asked(base_url: str) -> None:
    fetch = answering(404, {"error": {"message": "Model not found: a/b"}})

    listings = check_chain(["a/b", "c/d"], base_url, fetch)

    assert [item.status for item in listings] == ["unknown", "unknown"]
    assert fetch.calls == []


def test_chain_is_reported_in_order() -> None:
    def fetch(url: str) -> tuple[int, str]:
        if url.endswith("gone/model:free"):
            return 404, json.dumps({"error": {"message": "Model not found: gone/model:free"}})
        return 200, json.dumps({"data": {"id": url.split("/model/")[1]}})

    listings = check_chain(["gone/model:free", "live/model:free"], BASE, fetch)

    assert [(item.model, item.status) for item in listings] == [
        ("gone/model:free", "not_listed"),
        ("live/model:free", "listed"),
    ]
    assert check_chain([], BASE, fetch) == []


@pytest.mark.parametrize(
    ("listings", "exit_code"),
    [
        ([Listing("a/b:free", "listed"), Listing("c/d:free", "listed")], 0),
        ([Listing("a/b:free", "listed"), Listing("c/d:free", "not_listed", note="withdrawn")], 1),
        ([Listing("a/b:free", "listed", expires=date.today().isoformat())], 1),
        # An end date far away is no reason to fail: only the last fourteen days are.
        ([Listing("a/b:free", "listed", expires="2999-01-01")], 0),
        # The command is a watchdog: an answer it could not read must not pass.
        ([Listing("a/b:free", "unknown", note="catalogue not reachable (TimeoutError)")], 1),
    ],
    ids=["all-listed", "one-withdrawn", "ends-today", "ends-far-away", "could-not-check"],
)
def test_command_exit_code(
    listings: list[Listing],
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[tuple[list[str], str]] = []

    def stand_in(models: list[str], base_url: str) -> list[Listing]:
        asked.append((list(models), base_url))
        return listings

    monkeypatch.setattr(model_catalogue, "check_chain", stand_in)

    assert model_catalogue.main() == exit_code

    # It checks the chain the settings give, whole, against OpenRouter's address.
    assert asked == [(MODEL_CHAIN, BASE)]
    printed = capsys.readouterr().out
    assert all(item.model in printed for item in listings)


# ---------------------------------------------------------------------------
# Model catalogue check - at startup it reports, and changes nothing
# ---------------------------------------------------------------------------


@pytest.fixture
def checked(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[..., list[Listing]]], None]:
    """Turn the startup check on, with a stand-in for the catalogue."""

    def install(checker: Callable[..., list[Listing]]) -> None:
        monkeypatch.setenv("MODEL_CATALOGUE_CHECK", "true")
        extractors.reset_extractors()
        monkeypatch.setattr(main, "check_chain", checker)

    return install


def primary_gone(models: list[str], base_url: str) -> list[Listing]:
    return [Listing(models[0], "not_listed", note="withdrawn, or the id is mistyped")] + [
        Listing(model, "listed") for model in models[1:]
    ]


def recording(asked: list[list[str]]) -> Callable[..., list[Listing]]:
    """A stand-in that notes it was asked.

    It must not raise to make its point: the startup check swallows every error, so
    "never asked" and "asked, and the alarm was swallowed" would look the same.
    """

    def stand_in(models: list[str], base_url: str) -> list[Listing]:
        asked.append(list(models))
        return []

    return stand_in


@pytest.mark.asyncio
async def test_suite_never_asks_the_catalogue_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite promises no network, so the check is off unless a test turns it on."""
    asked: list[list[str]] = []
    monkeypatch.setattr(main, "check_chain", recording(asked))

    async with app.router.lifespan_context(app):
        assert app.state.model_listings == {}

    assert asked == []


def test_the_startup_check_is_on_unless_switched_off() -> None:
    """Off in this suite only because conftest says so; a real start asks."""
    assert Settings.model_fields["model_catalogue_check"].default is True


@pytest.mark.asyncio
async def test_catalogue_is_not_asked_without_an_api_key(
    checked: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[list[str]] = []
    checked(recording(asked))
    monkeypatch.delenv("OPENROUTER_API_KEY")
    extractors.reset_extractors()

    async with app.router.lifespan_context(app):
        assert app.state.model_listings == {}

    assert asked == []


@pytest.mark.asyncio
async def test_catalogue_is_asked_about_the_whole_chain_when_the_check_is_on(
    checked: Callable[..., None],
) -> None:
    asked: list[list[str]] = []
    checked(recording(asked))

    async with app.router.lifespan_context(app):
        pass

    assert asked == [MODEL_CHAIN]


@pytest.mark.asyncio
async def test_startup_names_a_model_that_is_no_longer_listed(
    checked: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    checked(primary_gone)

    with caplog.at_level("INFO", logger="app.main"):
        async with app.router.lifespan_context(app):
            assert app.state.model_listings[PRIMARY_MODEL].status == "not_listed"
            assert app.state.model_listings[FALLBACK_MODEL].status == "listed"

    errors = [record.getMessage() for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1
    assert PRIMARY_MODEL in errors[0] and "OPENROUTER_MODELS" in errors[0]
    assert app.state.model_listings == {}  # nothing is kept once the service has stopped


@pytest.mark.asyncio
async def test_startup_warns_about_an_announced_end_date(
    checked: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    def ending(models: list[str], base_url: str) -> list[Listing]:
        return [Listing(model, "listed", expires=date.today().isoformat()) for model in models[:1]]

    checked(ending)

    with caplog.at_level("INFO", logger="app.main"):
        async with app.router.lifespan_context(app):
            pass

    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and "announced its end" in warnings[0]
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


@pytest.mark.asyncio
async def test_a_failing_check_does_not_stop_startup(
    checked: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    def broken(models: list[str], base_url: str) -> list[Listing]:
        raise RuntimeError("boom")

    checked(broken)

    with caplog.at_level("WARNING", logger="app.main"):
        async with app.router.lifespan_context(app):
            assert app.state.model_listings == {}

    assert any("did not finish" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_slow_catalogue_does_not_hold_up_startup(
    checked: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()

    def stuck(models: list[str], base_url: str) -> list[Listing]:
        release.wait(2)  # far longer than the deadline below
        # Had startup waited for this, the listings would show: an empty answer
        # here would look the same as a timeout.
        return [Listing(model, "listed") for model in models]

    checked(stuck)
    monkeypatch.setattr(main, "CATALOGUE_DEADLINE_SECONDS", 0.05)

    try:
        with caplog.at_level("WARNING", logger="app.main"):
            async with app.router.lifespan_context(app):
                assert app.state.model_listings == {}
    finally:
        release.set()  # let the worker thread finish

    assert any("did not finish (TimeoutError)" in r.getMessage() for r in caplog.records)


def all_gone(models: list[str], base_url: str) -> list[Listing]:
    return [Listing(model, "not_listed", note="withdrawn, or the id is mistyped") for model in models]


def first_ends_today(models: list[str], base_url: str) -> list[Listing]:
    return [Listing(models[0], "listed", expires=date.today().isoformat())] + [
        Listing(model, "listed") for model in models[1:]
    ]


def nothing_known(models: list[str], base_url: str) -> list[Listing]:
    return [
        Listing(model, "unknown", note="catalogue not reachable (TimeoutError)") for model in models
    ]


CATALOGUE_ANSWERS: dict[str, Callable[..., list[Listing]]] = {
    "first-gone": primary_gone,
    "all-gone": all_gone,
    "first-ends-today": first_ends_today,
    "nothing-known": nothing_known,
}


def model_answers(kind: str) -> FakeRouter:
    if kind == "fallback-answers":
        return FakeRouter(
            {PRIMARY_MODEL: api_error(openai.NotFoundError, 404), FALLBACK_MODEL: ok()}
        )
    if kind == "every-model-fails":
        return FakeRouter({}, api_error(openai.RateLimitError, 429))
    return FakeRouter({}, ok(EMPTY_INVOICE_PAYLOAD))  # every model says "not an invoice"


#: Headers that differ between any two requests, whatever the service does.
VOLATILE_HEADERS = {"x-request-id", "x-process-time-ms", "content-length"}


def as_a_client_sees_it(response: httpx.Response) -> tuple[int, Any, dict[str, str]]:
    body = response.json()
    body.pop("execution_time_ms", None)
    headers = {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower() not in VOLATILE_HEADERS
    }
    return response.status_code, body, headers


@pytest.mark.parametrize("catalogue", list(CATALOGUE_ANSWERS))
@pytest.mark.parametrize("kind", ["fallback-answers", "every-model-fails", "not-an-invoice"])
@pytest.mark.asyncio
async def test_the_check_never_changes_what_the_api_answers(
    catalogue: str, kind: str, checked: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the catalogue says, a caller gets the same answer as without the check.

    The same two requests are made before the check exists and inside a service that
    ran it: status, body, headers and the models that were called must all be equal.
    """
    fake = model_answers(kind)
    monkeypatch.setattr(extractors.OpenRouterExtractor, "_get_client", lambda self: fake)
    transport = httpx.ASGITransport(app=app)
    payload = {"text": SAMPLE_INVOICE_TEXT}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        answer_before = as_a_client_sees_it(await client.post("/api/v1/extract", json=payload))
        health_before = as_a_client_sees_it(await client.get("/health"))
        called_before = list(fake.models_called)
        fake.calls.clear()

        checked(CATALOGUE_ANSWERS[catalogue])
        async with app.router.lifespan_context(app):
            assert app.state.model_listings  # the check did run
            answer = as_a_client_sees_it(await client.post("/api/v1/extract", json=payload))
            health = as_a_client_sees_it(await client.get("/health"))

    assert answer == answer_before
    assert health == health_before
    assert fake.models_called == called_before
    assert health[1]["active_providers"] == MODEL_CHAIN


@pytest.mark.asyncio
async def test_the_page_is_told_what_the_check_found(checked: Callable[..., None]) -> None:
    """The page marks the model; nothing else learns of it."""

    def one_gone_one_ending(models: list[str], base_url: str) -> list[Listing]:
        return [
            Listing(models[0], "not_listed", note="withdrawn, or the id is mistyped"),
            Listing(models[1], "listed", expires="2026-12-31"),
            Listing(models[2], "listed"),
        ]

    checked(one_gone_one_ending)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with app.router.lifespan_context(app):
            config = (await client.get("/playground/config")).json()

    assert [entry["listing"] for entry in config["chain"]] == [
        {"status": "not_listed", "expires": None},
        {"status": "listed", "expires": "2026-12-31"},
        {"status": "listed", "expires": None},
    ]


@pytest.mark.asyncio
async def test_config_says_nothing_when_no_check_ran() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        config = (await client.get("/playground/config")).json()

    assert [entry["listing"] for entry in config["chain"]] == [None, None, None]

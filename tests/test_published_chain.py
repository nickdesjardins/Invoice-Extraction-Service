"""Tests for the published list (``app.published_chain``) and how a service uses it.

Nothing here reaches GitHub, OpenRouter or a model: the request for the published
file is a function passed in or replaced, and the scheduled job's test runs against
the scripted engine of ``tests/test_model_chooser.py``.

Layout:

* Published list - the file        - what the job writes is what the service reads; checks.
* Published list - a service       - startup, a failed request, and what never happens.
* Published list - the job         - when a test is due, what is written, exit codes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import openai
import pytest

from app import (
    budget,
    extractors,
    main,
    model_cache,
    model_catalogue,
    model_chooser,
    model_rules,
    model_testing,
    published_chain,
)
from app.config import DEFAULT_PUBLISHED_URL, get_settings
from app.extractors import get_dispatcher
from app.main import app
from app.model_catalogue import FREE_MODEL_ID, Listing
from app.playground import benchmark
from app.published_chain import Unusable, due, fingerprint, parse
from tests.test_extract import (  # noqa: F401 - `http` and `router` are fixtures
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    api_error,
    http,
    ok,
    router,
)
from tests.test_model_chooser import (  # noqa: F401 - `world` is a fixture
    BROKEN,
    FALLBACK,
    HEALTHY,
    NEWCOMER,
    PRIMARY,
    SLOW,
    ScriptedEngine,
    World,
    always,
    settled,
    world,
    wrong_on,
)

URL = "https://raw.githubusercontent.com/someone/somewhere/tested-models/chain.json"
#: The address of a job's run, as the job writes it into the file it publishes.
RUN = "https://github.com/someone/somewhere/actions/runs/7"
NOW = datetime(2026, 10, 12, 6, 30, tzinfo=timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.strftime(published_chain.STAMP)


#: A moment that is in the past whatever this machine's clock says.
YESTERDAY = stamp(datetime.now(timezone.utc) - timedelta(days=1))


async def scheduled_test(chain: list[str], listed: list[str], gone: tuple[str, ...] = ()) -> dict[str, Any]:
    """Run the job's test against the scripted engine and return its record."""
    settings = get_settings()
    tested = chain + [model for model in listed if model not in chain]
    record = model_testing.new_record(
        "schedule", {"code": "schedule", "models": []}, chain,
        [model for model in chain if model not in gone], list(gone), listed, tested, [], settings,
    )
    await model_testing.run_test(record, settings)
    return record


def document(record: dict[str, Any], tested: datetime = NOW) -> dict[str, Any]:
    return published_chain.build(record, fingerprint(get_settings()), tested, {"run": RUN})


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """Turn the startup reads on, with a stand-in for the published file and for the catalogue.

    Called a second time in one test, it changes what the address answers and nothing
    else: what the service remembers stays, as it does in a service that is running.
    """
    asked: list[str] = []
    answers: list[Any] = []

    def get(url: str) -> tuple[int, str]:
        asked.append(url)
        answer = answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            return answer
        return 200, answer if isinstance(answer, str) else json.dumps(answer)

    def install(answer: Any) -> list[str]:
        if not answers:
            monkeypatch.setenv("MODEL_PUBLISHED_URL", URL)
            monkeypatch.setenv("MODEL_CATALOGUE_CHECK", "true")
            extractors.reset_extractors()
            model_cache.forget()
            model_chooser.reset()
            monkeypatch.setattr(model_catalogue, "_http_get", get)
            monkeypatch.setattr(main, "check_chain", lambda models, base_url: [Listing(m, "listed") for m in models])
        answers[:] = [answer]
        return asked

    return install


# ---------------------------------------------------------------------------
# Published list - the file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_what_the_job_writes_is_what_the_service_reads(world: Callable[..., World]) -> None:
    world([PRIMARY, FALLBACK, SLOW, BROKEN], {**HEALTHY, SLOW: always("right"), BROKEN: always("format")})
    record = await scheduled_test([PRIMARY, FALLBACK], [PRIMARY, FALLBACK, SLOW, BROKEN])
    written = document(record)

    found = parse(json.dumps(written), fingerprint(get_settings()), NOW)

    assert list(found.chain) == [PRIMARY, FALLBACK, SLOW] == written["chain"]
    assert found.tested == NOW and found.by == {"run": RUN}
    assert found.record["by"] == "schedule" and len(found.record["calls"]) == 23
    assert [row["verdict"] for row in found.record["models"]] == ["passed", "passed", "passed", "failed"]
    assert [row["placing"] for row in found.record["models"]] == ["kept", "kept", "added", None]
    assert found.job["found"] == sorted([PRIMARY, FALLBACK, SLOW, BROKEN])
    # Nothing a visitor must not see is published: no message, and the size is small.
    text = json.dumps(written)
    assert '"message"' not in text and len(text) < 20_000
    # The verdicts are written for readers that have no rules of their own (the front door).
    assert {row["verdict"] for row in written["test"]["models"]} == {"passed", "failed"}
    assert written["test"]["cases"][0] == {"id": "email-invoice", "title": "Invoice inside an email thread"}


def valid() -> dict[str, Any]:
    return {
        "version": 1,
        "fingerprint": fingerprint(get_settings()),
        "tested": stamp(NOW - timedelta(days=2)),
        "chain": [NEWCOMER, FALLBACK],
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda d: "not json",
        lambda d: [d],
        lambda d: {**d, "version": 2},
        lambda d: {**d, "chain": []},
        lambda d: {**d, "chain": NEWCOMER},
        lambda d: {**d, "chain": ["openai/gpt-paid"]},
        lambda d: {**d, "chain": [NEWCOMER, "openai/gpt-paid"]},
        lambda d: {**d, "chain": [NEWCOMER, NEWCOMER]},
        lambda d: {**d, "chain": [NEWCOMER, 7]},
        lambda d: {**d, "chain": ["Vendor/Model:free"]},
        lambda d: {**d, "chain": ["@preset/cheap:free"]},
        lambda d: {**d, "chain": ["a/b:free", "c/d:free", "e/f:free", "g/h:free"]},
        lambda d: {k: v for k, v in d.items() if k != "tested"},
        lambda d: {**d, "tested": stamp(NOW + timedelta(days=2))},
        lambda d: {**d, "tested": 20261012},
        lambda d: {**d, "padding": "x" * 70_000},
        lambda d: "[" * 50_000,
    ],
)
def test_a_file_that_fails_a_check_is_not_used(change: Callable[[dict[str, Any]], Any]) -> None:
    content = change(valid())
    text = content if isinstance(content, str) else json.dumps(content)
    with pytest.raises(Unusable) as info:
        parse(text, fingerprint(get_settings()), NOW)
    assert info.value.kind == "invalid" and str(info.value)


def test_a_file_made_by_other_code_is_foreign_and_still_says_how_old_it_is() -> None:
    with pytest.raises(Unusable) as info:
        parse(json.dumps({**valid(), "fingerprint": "0123456789abcdef"}), fingerprint(get_settings()), NOW)
    assert info.value.kind == "foreign" and info.value.tested == stamp(NOW - timedelta(days=2))


def test_the_fingerprint_follows_the_test_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import config, model_rules
    from app import extractors as engine

    before = fingerprint(get_settings())
    monkeypatch.setenv("TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "4096")
    extractors.reset_extractors()
    assert fingerprint(get_settings()) == before  # a deployment's own limits are not another test

    for target, name, value in (
        (engine, "SYSTEM_PROMPT", "another prompt"),
        (model_rules, "MAX_MISSES", 0),
        (config, "DEFAULT_MODEL_CHAIN", "a/b:free"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(target, name, value)
            assert fingerprint(get_settings()) != before, name
    monkeypatch.setenv("OPENROUTER_STRICT_JSON_SCHEMA", "false")
    extractors.reset_extractors()
    assert fingerprint(get_settings()) != before


def test_the_fingerprint_follows_what_is_sent_and_checked_and_not_how_it_is_worded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reworded label must not cut every deployment off from the published list."""
    from app import config

    before = fingerprint(get_settings())
    cases = benchmark.live_cases()

    def first_case(**changes: Any) -> Callable[[], list[dict[str, Any]]]:
        return lambda: [{**cases[0], **changes}] + cases[1:]

    reworded = [{**check, "label": "Said in other words"} for check in cases[0]["checks"]]
    for target, name, value in (
        (benchmark, "live_cases", first_case(checks=reworded)),
        (config, "DEFAULT_MODEL_CHAIN", config.DEFAULT_MODEL_CHAIN.replace(",", " , ")),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(target, name, value)
            assert fingerprint(get_settings()) == before, name

    other_check = [{**check, "expected": "something else"} for check in cases[0]["checks"]]
    for target, name, value in (
        (extractors, "strict_json_schema", lambda: {"type": "object", "properties": {}}),
        (benchmark, "live_cases", first_case(text=cases[0]["text"] + " ")),
        (benchmark, "live_cases", first_case(checks=other_check)),
        (benchmark, "live_cases", first_case(checks=cases[0]["checks"][:-1])),
        (benchmark, "live_cases", lambda: cases[:-1]),
        (model_rules, "PLACES", 2),
        (model_rules, "MUST_BE_RIGHT", "email-invoice"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(target, name, value)
            assert fingerprint(get_settings()) != before, name
    assert fingerprint(get_settings()) == before


def a_test_block(**changes: Any) -> dict[str, Any]:
    """The "test" part of a published file: one model, one call, as the job would write it."""
    call = {"model": NEWCOMER, "case": "email-invoice", "outcome": "answered", "correct": True, "seconds": 1.5}
    block = {"tested": [NEWCOMER], "started": "2026-10-10T06:17:00Z", "timeout_seconds": 30, "calls": [call]}
    return {**block, **changes}


@pytest.mark.parametrize("number", ["1e999", "-1e999", "NaN", "Infinity", "0", "86400"])
def test_a_time_limit_that_is_no_time_limit_is_not_kept(number: str) -> None:
    """Python reads 1e999 as a JSON number. Kept, it could not be sent to the page as
    JSON, and the picture's route would answer 500 for as long as the file is remembered."""
    text = json.dumps({**valid(), "test": a_test_block()})
    text = text.replace('"timeout_seconds": 30', f'"timeout_seconds": {number}')
    assert f'"timeout_seconds": {number}' in text

    found = parse(text, fingerprint(get_settings()), NOW)

    assert list(found.chain) == [NEWCOMER, FALLBACK]  # the chain is used; only the odd record is dropped
    assert found.record is None


def test_what_a_file_says_about_its_own_test_is_cleaned_and_not_believed() -> None:
    call = {"model": NEWCOMER, "case": "email-invoice", "outcome": "answered", "seconds": 1.5}
    test = a_test_block(
        tested=[NEWCOMER, NEWCOMER, FALLBACK],  # a model named twice is one model
        calls=[
            call,  # no "correct" at all: an answer nobody graded, not a file to throw away
            {**call, "case": "ocr-receipt", "correct": True, "error_type": "Please visit example.com"},
            {**call, "case": "eur-invoice", "correct": True, "message": "set OPENROUTER_API_KEY", "extra": 1},
        ],
        models=[{"model": NEWCOMER, "verdict": "passed", "why": "all_right", "placing": "added"}],
    )

    found = parse(json.dumps({**valid(), "test": test}), fingerprint(get_settings()), NOW)

    assert found.record is not None and found.record["tested"] == [NEWCOMER, FALLBACK]
    assert [row["model"] for row in found.record["models"]] == [NEWCOMER, FALLBACK]
    # Three calls of seven: the verdict is this service's own, whatever the file claimed.
    assert found.record["models"][0]["verdict"] == "not_judged"
    assert all(set(call) == set(model_testing.CALL_FIELDS) for call in found.record["calls"])
    assert [call["correct"] for call in found.record["calls"]] == [None, True, True]
    assert {call["error_type"] for call in found.record["calls"]} == {None}
    assert "OPENROUTER" not in json.dumps(found.record) and "example.com" not in json.dumps(found.record)


def test_the_error_types_a_file_may_name_are_the_engines_own() -> None:
    assert "ProviderRateLimitError" in published_chain._ERROR_TYPES
    for name in model_rules.UNUSABLE + model_rules.KEY_PROBLEMS:
        assert name in published_chain._ERROR_TYPES and hasattr(extractors, name), name


@pytest.mark.parametrize(
    "run",
    [
        "x).\n2026-10-08 03:00:00,000 ERROR    [app.main] The key was rejected. Paste a new one "
        "at https://evil.example/",
        "https://evil.example/someone/somewhere/actions/runs/7",
        "https://github.com/someone/somewhere/actions/runs/7 and then some",
        7,
    ],
)
def test_only_the_address_of_the_jobs_run_reaches_the_log(run: Any) -> None:
    """The server log is where the admin is told what to do. A file must not write there."""
    found = parse(json.dumps({**valid(), "by": {"run": run, "repository": "x\ny"}}), fingerprint(get_settings()), NOW)

    assert found.by == {}


@pytest.mark.parametrize(
    "junk",
    [
        {"test": []},
        {"test": {"tested": "abc", "calls": 5}},
        {"test": {"tested": [NEWCOMER], "started": "2026-10-10T06:17:00Z", "timeout_seconds": 30,
                  "calls": [{"model": NEWCOMER, "case": [], "outcome": "answered", "correct": True, "seconds": 1}]}},
        {"test": {"tested": [NEWCOMER], "started": "2026-10-10T06:17:00Z", "timeout_seconds": True, "calls": [[]]}},
        {"job": "yesterday", "by": 7, "memory": {"on_notice": "abc"}},
        {"job": {"checked": [], "found": {"a": 1}, "outcome": 3}, "by": {"run": {"deep": []}}},
        {"test": {"tested": [NEWCOMER], "started": "2026-10-10T06:17:00Z", "timeout_seconds": 30,
                  "calls": [{"model": NEWCOMER, "case": "email-invoice", "outcome": "answered", "correct": "yes",
                             "seconds": "fast", "error_type": {"x": 1}, "message": "set OPENROUTER_API_KEY"}]}},
    ],
)
def test_odd_but_harmless_parts_are_dropped_and_never_raise(junk: dict[str, Any]) -> None:
    found = parse(json.dumps({**valid(), **junk}), fingerprint(get_settings()), NOW)
    assert list(found.chain) == [NEWCOMER, FALLBACK]
    assert "OPENROUTER" not in json.dumps(found.record)


def test_the_request_names_what_went_wrong() -> None:
    def answers(status: int, body: str = "") -> Callable[[str], tuple[int, str]]:
        return lambda url: (status, body)

    assert published_chain.fetch(URL, answers(200, "{}")) == "{}"
    for get, kind in ((answers(404), "missing"), (answers(500), "unreachable"), (answers(302), "unreachable")):
        with pytest.raises(Unusable) as info:
            published_chain.fetch(URL, get)
        assert info.value.kind == kind

    def broken(url: str) -> tuple[int, str]:
        raise TimeoutError("slow")

    with pytest.raises(Unusable) as info:
        published_chain.fetch(URL, broken)
    assert info.value.kind == "unreachable"
    for address in ("http://example.com/chain.json", "file:///etc/passwd", "chain.json"):
        with pytest.raises(Unusable):
            published_chain.fetch(address, lambda url: pytest.fail("asked"))


def test_the_default_address_is_this_projects_own_branch_and_the_suite_never_asks_it() -> None:
    assert DEFAULT_PUBLISHED_URL == (
        "https://raw.githubusercontent.com/nickdesjardins/Invoice-Extraction-Service/"
        "tested-models/chain.json"
    )
    assert get_settings().model_published_url is None  # conftest: "null"


# ---------------------------------------------------------------------------
# Published list - a service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_newer_published_chain_is_in_use_before_the_first_request(
    world: Callable[..., World], published: Callable[..., list[str]], http: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = world([PRIMARY, FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")})
    record = await scheduled_test([PRIMARY, FALLBACK], [PRIMARY, FALLBACK, NEWCOMER])
    calls_of_the_job = len(made.test_calls)
    # Tested after the day the built-in default was measured, so it is the newer evidence.
    monkeypatch.setitem(model_chooser.BENCHMARK_SNAPSHOT, "measured", "2026-01-01")
    asked = published(document(record, datetime.now(timezone.utc) - timedelta(days=3)))

    async with app.router.lifespan_context(app):
        picture = model_chooser.picture()
        health = (await http.get("/health")).json()

    assert asked == [URL]
    assert len(made.test_calls) == calls_of_the_job and made.list_reads == 0  # startup called nothing
    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER] == [p["model"] for p in health["providers"]]
    assert picture["origin"]["kind"] == "published" and picture["origin"]["age_days"] == 3
    assert picture["published"]["state"] == "used"
    assert picture["last"]["by"] == "schedule" and len(picture["last"]["calls"]) == 21


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        ((404, "404: Not Found"), "missing"),
        ((500, "oops"), "unreachable"),
        (TimeoutError("slow"), "unreachable"),
        ("<html>not a list</html>", "invalid"),
        ({"version": 1, "fingerprint": "x", "tested": YESTERDAY, "chain": [NEWCOMER]}, "foreign"),
        ({"version": 1, "tested": YESTERDAY, "chain": ["openai/gpt-paid"]}, "foreign"),
    ],
)
@pytest.mark.asyncio
async def test_no_usable_published_list_changes_nothing_and_stops_nothing(
    answer: Any, state: str, world: Callable[..., World], published: Callable[..., list[str]]
) -> None:
    made = world([PRIMARY, FALLBACK], HEALTHY)
    published(answer)

    async with app.router.lifespan_context(app):
        picture = model_chooser.picture()

    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["origin"]["kind"] == "default"
    assert picture["published"]["state"] == state
    assert made.test_calls == [] and not model_cache.PATH.exists()


@pytest.mark.parametrize(
    "junk",
    [
        {"memory": {"on_notice": "abc"}},
        {"memory": [1, 2]},
        {"test": {"calls": [{"case": []}]}},
        {"test": 5, "job": [], "by": "me"},
        {"chain": {"a": 1}},
        {"tested": {"when": "now"}},
    ],
)
@pytest.mark.asyncio
async def test_no_published_file_can_stop_the_service_from_starting(
    junk: dict[str, Any], world: Callable[..., World], published: Callable[..., list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    monkeypatch.setitem(model_chooser.BENCHMARK_SNAPSHOT, "measured", "2026-01-01")
    newer = {**valid(), "tested": stamp(datetime.now(timezone.utc) - timedelta(days=1)), **junk}
    published(newer)

    async with app.router.lifespan_context(app):
        assert get_dispatcher().models  # the service is up, whatever the file held


@pytest.mark.asyncio
async def test_a_published_list_that_answers_too_slowly_is_not_waited_for(
    world: Callable[..., World], published: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host is already making the visitor wait: the read has a time limit, and the service starts."""
    import threading
    import time

    world([PRIMARY, FALLBACK], HEALTHY)
    published(valid())
    release = threading.Event()

    def slow(url: str) -> tuple[int, str]:
        release.wait(5)
        return 200, json.dumps(valid())

    monkeypatch.setattr(model_catalogue, "_http_get", slow)
    monkeypatch.setattr(model_chooser, "READ_SECONDS", 0.05)
    started = time.monotonic()
    try:
        async with app.router.lifespan_context(app):
            picture = model_chooser.picture()
    finally:
        release.set()

    assert time.monotonic() - started < 3
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["published"]["state"] == "unreachable"


@pytest.mark.asyncio
async def test_a_published_chain_older_than_the_chain_in_use_is_left_alone(
    world: Callable[..., World], published: Callable[..., list[str]]
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    # Dated before the day the built-in default was measured.
    published({**valid(), "tested": "2026-10-01T06:17:00Z"})

    async with app.router.lifespan_context(app):
        picture = model_chooser.picture()

    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["published"]["state"] == "older"


@pytest.mark.parametrize(
    ("tested_at", "state"),
    [("18:59:59", "older"), ("19:02:59", "older"), ("19:03:00", "used")],
    ids=["while-it-was-measured", "its-last-minute", "afterwards"],
)
@pytest.mark.asyncio
async def test_a_list_tested_after_the_default_was_measured_is_newer_on_the_same_day_too(
    tested_at: str, state: str, world: Callable[..., World], published: Callable[..., list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measure, change the default, push: the push starts the job, which publishes that
    same day. The built-in default counts as tested when its measuring ended, not at midnight."""
    world([PRIMARY, FALLBACK, NEWCOMER], HEALTHY)
    monkeypatch.setitem(model_chooser.BENCHMARK_SNAPSHOT, "measured", "2026-10-07")
    monkeypatch.setitem(model_chooser.BENCHMARK_SNAPSHOT, "window_utc", "17:54 to 19:02")
    published({**valid(), "tested": f"2026-10-07T{tested_at}Z", "chain": [PRIMARY, FALLBACK, NEWCOMER]})

    async with app.router.lifespan_context(app):
        picture = model_chooser.picture()

    assert picture["published"]["state"] == state
    assert picture["origin"]["kind"] == ("published" if state == "used" else "default")
    if state == "older":
        assert picture["origin"]["tested"] == "2026-10-07T19:02:59Z"


@pytest.mark.asyncio
async def test_named_models_never_read_the_published_list(
    published: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = published(valid())  # the suite's own environment names three models

    async with app.router.lifespan_context(app):
        picture = model_chooser.picture()

    assert asked == [] and picture["origin"]["kind"] == "pinned" and picture["published"]["state"] == "off"


@pytest.mark.asyncio
async def test_a_failed_request_is_repaired_from_the_published_list_at_no_cost(
    world: Callable[..., World], published: Callable[..., list[str]],
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup read did not get through; the first failed request asks again before anything is spent."""
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    fresh = {**valid(), "tested": stamp(datetime.now(timezone.utc) - timedelta(hours=1))}
    monkeypatch.setitem(model_chooser.BENCHMARK_SNAPSHOT, "measured", "2026-01-01")
    asked = published(TimeoutError("slow"))
    async with app.router.lifespan_context(app):
        assert model_chooser.picture()["published"]["state"] == "unreachable"
        published(fresh)
        asked.clear()
        router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok(), NEWCOMER: ok()})

        first = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
        picture = await settled()
        second = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})

    assert first.json()["model_used"] == FALLBACK and second.json()["model_used"] == NEWCOMER
    assert picture["chain"] == [NEWCOMER, FALLBACK] and picture["origin"]["kind"] == "published"
    assert made.test_calls == [] and made.lookups == []  # no test, not even a look-up


@pytest.mark.asyncio
async def test_the_chain_that_has_just_failed_is_not_taken_again_from_the_published_list(
    world: Callable[..., World], published: Callable[..., list[str]],
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    same = {**valid(), "chain": [PRIMARY, FALLBACK], "tested": stamp(datetime.now(timezone.utc) - timedelta(hours=1))}
    monkeypatch.setitem(model_chooser.BENCHMARK_SNAPSHOT, "measured", "2026-01-01")
    published(TimeoutError("slow"))
    async with app.router.lifespan_context(app):
        published(same)
        router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
        await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
        picture = await settled()

    assert picture["published"]["state"] == "same"
    assert picture["chain"] == [FALLBACK, NEWCOMER] and made.asked(NEWCOMER) == 7  # its own repair ran


@pytest.mark.parametrize(
    ("answer", "pages", "result", "chain"),
    [
        # Withdrawn: adopting the list is right, and ending the look there is not.
        (api_error(openai.NotFoundError, 404), "not_listed", "started", [NEWCOMER]),
        # Only rate limited: nothing more to do, and the note still says what was done.
        (api_error(openai.RateLimitError, 429), "listed", "published", [PRIMARY, NEWCOMER]),
    ],
    ids=["withdrawn", "rate-limited"],
)
@pytest.mark.asyncio
async def test_a_look_goes_on_for_a_model_that_failed_and_is_in_the_adopted_list_too(
    answer: Exception, pages: str, result: str, chain: list[str], http: httpx.AsyncClient,
    router: Callable[..., FakeRouter], world: Callable[..., World],
    published: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published list is a few days old and still names the model that has just failed, first.

    If the look ended with the adoption, every request would go on calling a
    withdrawn model first, until another one failed a minute later.
    """
    script = {PRIMARY: always("right"), NEWCOMER: always("right"), FALLBACK: always("limited")}
    made = world([FALLBACK, NEWCOMER], script, gone=(PRIMARY,) if pages == "not_listed" else ())
    monkeypatch.setitem(model_chooser.BENCHMARK_SNAPSHOT, "measured", "2026-01-01")
    record = await scheduled_test([PRIMARY, NEWCOMER], [PRIMARY, NEWCOMER])
    budget.reset()  # the job's calls are not this service's minute
    published(document(record, datetime.now(timezone.utc) - timedelta(hours=1)))
    fake = router({PRIMARY: answer, FALLBACK: ok(), NEWCOMER: ok()})

    assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 200
    picture = await settled()

    assert picture["published"]["state"] == "used" and made.lookups == [[PRIMARY]]
    assert picture["look"]["result"] == result and picture["chain"] == chain
    fake.calls.clear()
    again = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
    assert fake.models_called[0] == chain[0] and again.json()["model_used"] == NEWCOMER


@pytest.mark.asyncio
async def test_after_a_failed_request_the_published_list_is_asked_again_only_for_a_reason(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It never answered since the start, or it was last asked five minutes ago."""
    made = world([PRIMARY, FALLBACK], HEALTHY)
    monkeypatch.setenv("MODEL_PUBLISHED_URL", URL)
    extractors.reset_extractors()
    answer: Any = TimeoutError("slow")
    asked: list[str] = []

    def get(url: str) -> tuple[int, str]:
        asked.append(url)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(model_catalogue, "_http_get", get)

    async def a_failed_request() -> int:
        monkeypatch.setattr(model_chooser, "_looked_at", -10_000.0)  # the minute between two looks is over
        asked.clear()
        failure = model_chooser.Failure(PRIMARY, "ProviderRateLimitError", True)
        model_chooser.note([failure], answered=True)
        await settled()
        return len(asked)

    assert await model_chooser.read_published(at_startup=True) is False  # the startup read did not get through
    assert await a_failed_request() == 1  # never answered: asked again at once
    assert await a_failed_request() == 1  # and again, for as long as it does not answer
    answer = (404, "404: Not Found")
    assert await a_failed_request() == 1  # now it has answered: nothing is published
    assert await a_failed_request() == 0  # so it is left alone ...
    monkeypatch.setattr(model_chooser, "_published_read_at", model_chooser._published_read_at - 299)
    assert await a_failed_request() == 0
    monkeypatch.setattr(model_chooser, "_published_read_at", model_chooser._published_read_at - 2)
    assert await a_failed_request() == 1  # ... for five minutes
    assert made.test_calls == [] and model_chooser.PUBLISHED_PAUSE_SECONDS == 300


@pytest.mark.asyncio
async def test_after_a_failed_request_the_published_list_gets_three_seconds_not_eight(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Somebody is waiting for a repair then."""
    import threading
    import time

    world([PRIMARY, FALLBACK], HEALTHY)
    monkeypatch.setenv("MODEL_PUBLISHED_URL", URL)
    extractors.reset_extractors()
    release = threading.Event()

    def slow(url: str) -> tuple[int, str]:
        release.wait(5)
        return 404, ""

    monkeypatch.setattr(model_catalogue, "_http_get", slow)
    assert model_chooser.LOOK_READ_SECONDS == 3.0  # the patience the documents give
    monkeypatch.setattr(model_chooser, "LOOK_READ_SECONDS", 0.05)
    assert model_chooser.READ_SECONDS == 8.0 and model_chooser.LOOK_PAUSE_SECONDS == 60

    started = time.monotonic()
    try:
        assert await model_chooser.read_published(at_startup=False) is False
    finally:
        release.set()
    assert time.monotonic() - started < 3 and model_chooser.picture()["published"]["state"] == "unreachable"


@pytest.mark.parametrize("confirmed_nothing", [False, True], ids=["nothing-since", "tested-and-confirmed-nothing"])
@pytest.mark.asyncio
async def test_startup_tells_the_admin_what_is_in_use_and_why_a_list_is_old(
    confirmed_nothing: bool, world: Callable[..., World], published: Callable[..., list[str]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Twenty days old. Either the job has not run, or it runs and can confirm nothing:
    the file itself says which, and "press Enable workflow" helps in the first case only."""
    world([PRIMARY, FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")})
    monkeypatch.setitem(model_chooser.BENCHMARK_SNAPSHOT, "measured", "2026-01-01")
    record = await scheduled_test([PRIMARY, FALLBACK], [PRIMARY, FALLBACK, NEWCOMER])
    written = document(record, datetime.now(timezone.utc) - timedelta(days=20))
    if confirmed_nothing:
        written["job"] = {**written["job"], "checked": YESTERDAY, "outcome": "nothing"}
    published(written)
    checked: list[list[str]] = []

    def listed(models: list[str], base_url: str) -> list[Listing]:
        checked.append(list(models))
        return [Listing(model, "listed") for model in models]

    monkeypatch.setattr(main, "check_chain", listed)

    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            picture = model_chooser.picture()

    # The catalogue check comes after the published list: it reports on the chain in use.
    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER] == checked[0]
    said = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "app.model_chooser"]
    assert any(
        level == "INFO" and text.startswith("Model choice: automatic. Using") and "published" in text
        for level, text in said
    )
    (old,) = [text for level, text in said if level == "WARNING" and "20 days old" in text]
    assert "'Model check' in the repository's Actions tab" in old
    assert ("Enable workflow" in old) is not confirmed_nothing
    assert (f"tested again on {YESTERDAY}" in old) is confirmed_nothing


@pytest.mark.asyncio
async def test_a_list_made_by_other_code_says_what_can_be_done_about_it(
    world: Callable[..., World], published: Callable[..., list[str]], caplog: pytest.LogCaptureFixture
) -> None:
    """In a clone that changed the test this line comes at every start, so it names the next step."""
    world([PRIMARY, FALLBACK], HEALTHY)
    published({**valid(), "fingerprint": "0123456789abcdef", "tested": YESTERDAY})

    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        async with app.router.lifespan_context(app):
            assert model_chooser.picture()["published"]["state"] == "foreign"

    (line,) = [r for r in caplog.records if "was not used" in r.getMessage()]
    assert line.levelname == "WARNING" and "another version of the test" in line.getMessage()
    assert "MODEL_PUBLISHED_URL" in line.getMessage() and "null" in line.getMessage()


@pytest.mark.asyncio
async def test_the_three_sources_of_a_record_have_the_same_fields(
    world: Callable[..., World], http: httpx.AsyncClient
) -> None:
    """One format, three sources, and each is written out on its own.

    A key missing from one of them is a page that draws no test at all, and the
    scheduled test's record is the one a service on the free host shows most.
    """
    world([PRIMARY, FALLBACK], HEALTHY)
    written = json.dumps(document(await scheduled_test([PRIMARY, FALLBACK], [PRIMARY, FALLBACK])))
    scheduled = parse(written, fingerprint(get_settings()), NOW).record
    budget.reset()
    ScriptedEngine.GATE[PRIMARY] = under_way = asyncio.Event()  # the test runs until it is set
    assert (await http.post("/playground/models/test")).status_code == 202
    for _ in range(400):
        if (model_chooser.picture()["test"] or {}).get("in_flight"):
            break
        await asyncio.sleep(0.005)
    running = json.loads(json.dumps(model_chooser.picture()["test"]))  # as the page is sent it
    under_way.set()
    finished = (await settled())["last"]
    recorded = model_testing.recorded_run()
    assert recorded is not None, "this copy of the project has no benchmark records"

    assert set(running) == set(finished) == set(recorded) == set(scheduled)
    assert running["state"] == "running"
    assert {finished["state"], recorded["state"], scheduled["state"]} == {"done"}
    rows = [set(row) for record in (running, finished, recorded, scheduled) for row in record["models"]]
    assert all(keys == rows[0] for keys in rows)
    # A call that is under way is in the record from the moment it goes out.
    assert running["requests"] >= 1 and running["in_flight"] and finished["in_flight"] == []
    assert all(set(entry) == {"model", "case", "since"} for entry in running["in_flight"])


# ---------------------------------------------------------------------------
# Published list - the job
# ---------------------------------------------------------------------------


def seen(job: Optional[dict[str, Any]] = None, tested: datetime = NOW - timedelta(days=3)) -> published_chain.Published:
    return published_chain.Published(
        chain=(PRIMARY, FALLBACK),
        tested=tested,
        job=job or {"checked": stamp(tested), "found": [PRIMARY, FALLBACK, SLOW], "outcome": "published"},
    )


def test_a_test_is_due_only_for_a_reason() -> None:
    listed = [PRIMARY, FALLBACK, SLOW]
    assert due(None, "nothing has been published yet", listed, NOW)[0] is True
    assert due(seen(), "", listed, NOW, forced=True)[0] is True
    assert due(seen(), "", listed, NOW)[0] is False  # three days old, same list
    assert due(seen(), "", None, NOW)[0] is False  # the list could not be read: nothing to compare
    assert due(seen(), "", listed + [NEWCOMER], NOW)[0] is True  # a model appeared
    assert due(seen(), "", [FALLBACK, SLOW], NOW)[0] is True  # a model went away
    # Weekly: tested at 06:22, the run of day seven at 06:17 is the one that tests.
    week_ago = NOW.replace(hour=6, minute=22) - timedelta(days=7)
    assert due(seen(tested=week_ago), "", listed, NOW.replace(hour=6, minute=17))[0] is True
    assert due(seen(tested=week_ago + timedelta(days=1)), "", listed, NOW.replace(hour=6, minute=17))[0] is False


def test_a_test_that_showed_nothing_is_repeated_the_next_day_and_not_every_day_for_the_same_list() -> None:
    listed = [SLOW]
    # The published chain's models are gone, and the last test (yesterday) could confirm nothing.
    nothing = {"checked": stamp(NOW - timedelta(hours=24)), "found": [SLOW], "outcome": "nothing"}
    assert due(seen(nothing), "", listed, NOW)[0] is True
    # "The next day" is twenty hours, so that a daily run which starts a little early
    # counts, and a second run of the same day does not test again.
    assert published_chain.RETRY_AFTER_HOURS == 20
    late = {**nothing, "checked": stamp(NOW - timedelta(hours=20))}
    same_day = {**nothing, "checked": stamp(NOW - timedelta(hours=19))}
    assert due(seen(late), "", listed, NOW)[0] is True
    assert due(seen(same_day), "", listed, NOW)[0] is False
    # A test that published, with the very same list as today: nothing new to learn until the week is over.
    done = {"checked": stamp(NOW - timedelta(hours=24)), "found": [SLOW], "outcome": "published"}
    assert due(seen(done), "", listed, NOW)[0] is False


def test_a_change_of_the_list_that_no_test_could_act_on_costs_no_test() -> None:
    """Otherwise one entry that comes and goes every day means a full test every day."""
    full = published_chain.Published(
        chain=(PRIMARY, FALLBACK, SLOW),
        tested=NOW - timedelta(days=3),
        job={
            "checked": stamp(NOW - timedelta(days=3)),
            "found": [PRIMARY, FALLBACK, SLOW, BROKEN],
            "outcome": "published",
        },
    )
    assert due(full, "", [PRIMARY, FALLBACK, SLOW, BROKEN, NEWCOMER], NOW)[0] is False  # no place for it
    assert due(full, "", [PRIMARY, FALLBACK, SLOW], NOW)[0] is False  # a model outside the chain went
    went, why = due(full, "", [PRIMARY, SLOW, BROKEN], NOW)  # a model of the chain went
    assert went is True and why.endswith(f"changed since the last test: {FALLBACK}")
    # With a place free, a model the last test did not see is a reason. One it saw is not.
    assert due(seen(), "", [PRIMARY, FALLBACK, SLOW, NEWCOMER], NOW)[0] is True
    assert due(seen(), "", [PRIMARY, FALLBACK], NOW)[0] is False


def test_what_the_job_remembers_cannot_put_the_next_test_off_or_bring_it_on() -> None:
    listed = [f"vendor/number-{number:03}:free" for number in range(130)]
    job = {"checked": stamp(NOW - timedelta(hours=1)), "found": listed, "outcome": "published"}
    text = json.dumps({**valid(), "tested": stamp(NOW - timedelta(hours=1)), "job": job})
    remembered = parse(text, fingerprint(get_settings()), NOW)

    # Every id that was listed is remembered, however many: a list cut short would
    # look changed at every run.
    assert remembered.job["found"] == listed and due(remembered, "", listed, NOW)[0] is False

    # A moment in the future is not believed: the test would be put off until then.
    ahead = {**job, "checked": "2099-01-01T00:00:00Z"}
    odd = parse(json.dumps({**valid(), "job": ahead}), fingerprint(get_settings()), NOW)
    assert odd.job["checked"] == stamp(NOW - timedelta(days=2))  # the file's own "tested"
    assert due(odd, "", listed, NOW + timedelta(days=7))[0] is True


def run_job(tmp_path: Path, previous: Optional[dict[str, Any]] = None, force: bool = False) -> tuple[int, Optional[dict[str, Any]]]:
    before, out = tmp_path / "previous" / "chain.json", tmp_path / "out" / "chain.json"
    if previous is not None:
        before.parent.mkdir(parents=True, exist_ok=True)
        before.write_text(json.dumps(previous), encoding="utf-8")
    code = published_chain.main(["--previous", str(before), "--out", str(out)] + (["--force"] if force else []))
    return code, json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None


def test_the_job_tests_and_writes_a_file_a_service_accepts(
    world: Callable[..., World], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    made = world([PRIMARY, FALLBACK, SLOW, BROKEN], {**HEALTHY, SLOW: always("right"), BROKEN: always("format")})

    code, written = run_job(tmp_path)

    assert code == 0 and written is not None
    assert written["chain"] == [PRIMARY, FALLBACK, SLOW]
    assert len(made.test_calls) == 23 == written["test"]["requests"]
    found = parse(json.dumps(written), fingerprint(get_settings()), datetime.now(timezone.utc))
    assert list(found.chain) == written["chain"]
    assert "Published chain" in capsys.readouterr().out


def test_the_job_writes_its_lines_to_the_summary_github_names_and_warns_a_fork(
    world: Callable[..., World], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What a GitHub runner sets, set here by hand: tests/conftest.py hides the real ones,
    so that the suite never writes into the summary of the run that executes it."""
    world([PRIMARY, FALLBACK], HEALTHY)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/a-fork")

    code, written = run_job(tmp_path)

    printed = capsys.readouterr().out
    assert code == 0 and written is not None
    assert "Published chain" in summary.read_text(encoding="utf-8")
    assert "This job publishes to someone/a-fork" in printed and "::warning::" in printed


def test_the_job_starts_from_the_published_chain_and_does_not_reshuffle_it(
    world: Callable[..., World], tmp_path: Path
) -> None:
    world([PRIMARY, FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")})
    previous = {**valid(), "chain": [FALLBACK, PRIMARY], "tested": stamp(datetime.now(timezone.utc) - timedelta(days=8))}

    code, written = run_job(tmp_path, previous)

    assert code == 0 and written["chain"] == [FALLBACK, PRIMARY, NEWCOMER]


@pytest.mark.parametrize(
    ("first_model", "chain"),
    [("right", [PRIMARY, FALLBACK, NEWCOMER]), ("gone", [FALLBACK, NEWCOMER])],
)
def test_the_job_drops_a_published_model_only_when_it_is_gone_and_gives_no_answer(
    first_model: str, chain: list[str], world: Callable[..., World], tmp_path: Path
) -> None:
    """The service's own rule: OpenRouter's page says gone, and the model was silent in this test."""
    world(
        [FALLBACK, NEWCOMER],
        {PRIMARY: always(first_model), FALLBACK: HEALTHY[FALLBACK], NEWCOMER: always("right")},
        gone=(PRIMARY,),
    )
    old = stamp(datetime.now(timezone.utc) - timedelta(days=8))
    previous = {**valid(), "chain": [PRIMARY, FALLBACK], "tested": old}

    code, written = run_job(tmp_path, previous)

    assert code == 0 and written["chain"] == chain
    placing = {row["model"]: row["placing"] for row in written["test"]["models"]}
    assert placing[PRIMARY] == ("kept" if first_model == "right" else "dropped")


def test_the_job_never_writes_a_file_with_an_id_that_is_not_free() -> None:
    """The last of the places where an id is looked at before it can reach a service."""
    for chain in ([NEWCOMER, "openai/gpt-paid"], []):
        with pytest.raises(ValueError):
            published_chain.build({"chain": chain}, "0123456789abcdef", NOW, {})


def test_the_job_takes_six_models_and_the_others_go_first_next_time(
    world: Callable[..., World], tmp_path: Path
) -> None:
    """ "42 requests at the very most" rests on the limit, and nobody waits for ever."""
    others = [f"vendor/number-{number}:free" for number in range(1, 8)]
    made = world(
        [PRIMARY, FALLBACK] + others,
        {**HEALTHY, **{model: always("limited") for model in others[:6]}, others[6]: always("right")},
    )

    code, first = run_job(tmp_path)

    assert code == 0 and first["test"]["tested"] == [PRIMARY, FALLBACK] + others[:4]
    assert first["test"]["left_out"] == others[4:] and first["chain"] == [PRIMARY, FALLBACK]
    assert len(made.test_calls) == first["test"]["requests"] <= 42

    # A week later. The three that had no room are tested first, and one of them passes.
    budget.reset()
    week_ago = stamp(datetime.now(timezone.utc) - timedelta(days=8))
    code, second = run_job(tmp_path, {**first, "tested": week_ago, "job": {**first["job"], "checked": week_ago}})

    assert code == 0 and second["test"]["tested"] == [PRIMARY, FALLBACK] + others[4:] + others[:1]
    assert second["test"]["left_out"] == others[1:4] and second["chain"] == [PRIMARY, FALLBACK, others[6]]


def test_a_previous_file_that_cannot_even_be_read_is_nothing_published(
    world: Callable[..., World], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Saved in another encoding on the branch, say. The job must not be red for ever for it."""
    world([PRIMARY, FALLBACK], HEALTHY)
    before = tmp_path / "previous" / "chain.json"
    before.parent.mkdir(parents=True)
    before.write_bytes(json.dumps(valid()).encode("utf-16"))

    code = published_chain.main(["--previous", str(before), "--out", str(tmp_path / "out" / "chain.json")])

    assert code == 0 and (tmp_path / "out" / "chain.json").is_file()
    assert "there is no usable published list" in capsys.readouterr().out


def test_the_job_fails_at_once_when_no_model_of_the_chain_is_left(
    world: Callable[..., World], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Published two days ago, both models withdrawn since, and nothing else passes: every
    service wakes up on models that are gone. That is not something to say in three weeks."""
    made = world(
        [BROKEN],
        {PRIMARY: always("gone"), FALLBACK: always("gone"), BROKEN: always("format")},
        gone=(PRIMARY, FALLBACK),
    )
    recent = stamp(datetime.now(timezone.utc) - timedelta(days=2))
    job = {"checked": recent, "found": sorted([PRIMARY, FALLBACK, BROKEN]), "outcome": "published"}
    previous = {**valid(), "chain": [PRIMARY, FALLBACK], "tested": recent, "job": job}

    code, written = run_job(tmp_path, previous)

    printed = capsys.readouterr().out
    assert code == 1 and written["chain"] == [PRIMARY, FALLBACK] and written["tested"] == recent
    assert "Failing this run on purpose: OpenRouter no longer offers any model of the chain" in printed

    # The same two days, but both models are listed and only rate limited: that is the weather.
    budget.reset()
    made.not_found.clear()
    made.listed[:] = [PRIMARY, FALLBACK, BROKEN]
    ScriptedEngine.SCRIPT.update({PRIMARY: always("limited"), FALLBACK: always("limited")})
    code, written = run_job(tmp_path, previous, force=True)
    assert code == 0 and written["job"]["outcome"] == "nothing"


def test_the_job_publishes_and_fails_when_a_model_of_the_chain_failed_the_test(
    world: Callable[..., World], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test never reorders a chain, so the first model keeps its place although it
    answers wrongly. The one tester that sees it must not stay silent about it."""
    world([PRIMARY, FALLBACK], {PRIMARY: wrong_on("eur-invoice", "ocr-receipt"), FALLBACK: HEALTHY[FALLBACK]})

    code, written = run_job(tmp_path)

    printed = capsys.readouterr().out
    assert code == 1 and written["chain"] == [PRIMARY, FALLBACK]  # published all the same
    assert f"Failing this run on purpose: {PRIMARY} failed this test" in printed
    assert "DEFAULT_MODEL_CHAIN" in printed


def test_the_job_fails_when_nothing_usable_is_published_and_it_cannot_help(
    world: Callable[..., World], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A red run is the one way this project tells its owner, and each one says why."""
    made = world([PRIMARY, FALLBACK], HEALTHY)
    made.list_fails = True
    code, written = run_job(tmp_path)  # nothing published, and OpenRouter's list cannot be read
    assert code == 1 and written is None and made.test_calls == []
    assert "Failing this run on purpose: no tested chain younger than three weeks" in capsys.readouterr().out

    made.list_fails = False
    monkeypatch.delenv("OPENROUTER_API_KEY")
    extractors.reset_extractors()
    old = {**valid(), "chain": [PRIMARY, FALLBACK], "tested": stamp(datetime.now(timezone.utc) - timedelta(days=22))}
    code, written = run_job(tmp_path, old)  # no key, and the list is three weeks old
    assert code == 1 and written is None and made.test_calls == []
    recent = {**old, "tested": stamp(datetime.now(timezone.utc) - timedelta(days=8))}
    code, written = run_job(tmp_path, recent)
    assert code == 0 and written is None


def test_the_job_publishes_nothing_new_when_only_a_model_without_a_place_passed(
    world: Callable[..., World], tmp_path: Path
) -> None:
    """ "Tested today" is said of a chain only when one of its own models passed that day."""
    world(
        [PRIMARY, FALLBACK, SLOW, NEWCOMER],
        {PRIMARY: always("limited"), FALLBACK: always("limited"), SLOW: always("limited"), NEWCOMER: always("right")},
    )
    old = stamp(datetime.now(timezone.utc) - timedelta(days=8))
    previous = {**valid(), "chain": [PRIMARY, FALLBACK, SLOW], "tested": old}

    code, written = run_job(tmp_path, previous)

    assert code == 0 and written["chain"] == [PRIMARY, FALLBACK, SLOW]
    assert written["tested"] == old and written["job"]["outcome"] == "nothing"


def test_the_job_spends_nothing_when_no_test_is_due(world: Callable[..., World], tmp_path: Path) -> None:
    made = world([PRIMARY, FALLBACK], HEALTHY)
    previous = {
        **valid(), "chain": [PRIMARY, FALLBACK],
        "tested": stamp(datetime.now(timezone.utc) - timedelta(days=2)),
        "job": {"checked": stamp(datetime.now(timezone.utc) - timedelta(days=2)), "found": [PRIMARY, FALLBACK], "outcome": "published"},
    }

    code, written = run_job(tmp_path, previous)

    assert code == 0 and written is None and made.test_calls == []


@pytest.mark.parametrize(("default_gone", "exit_code"), [(False, 0), (True, 1)])
def test_the_job_without_a_key_says_what_is_due_and_tests_nothing(
    default_gone: bool, exit_code: int, world: Callable[..., World], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    made = world([PRIMARY, FALLBACK], HEALTHY, gone=(PRIMARY,) if default_gone else ())
    monkeypatch.delenv("OPENROUTER_API_KEY")
    extractors.reset_extractors()

    code, written = run_job(tmp_path)

    assert code == exit_code and written is None and made.test_calls == []
    printed = capsys.readouterr().out
    assert "this job has no key" in printed and "::warning::" in printed


def test_a_test_that_confirms_nothing_keeps_the_chain_and_remembers_the_attempt(
    world: Callable[..., World], tmp_path: Path
) -> None:
    made = world([PRIMARY, FALLBACK], {PRIMARY: always("limited"), FALLBACK: always("limited")})
    old = stamp(datetime.now(timezone.utc) - timedelta(days=8))
    previous = {**valid(), "chain": [PRIMARY, FALLBACK], "tested": old}

    code, written = run_job(tmp_path, previous)

    assert code == 0  # eight days old: a warning, not yet the alarm
    assert written["chain"] == [PRIMARY, FALLBACK] and written["tested"] == old
    assert written["job"]["outcome"] == "nothing" and written["job"]["found"] == [FALLBACK, PRIMARY]
    assert len(made.test_calls) == 4  # two refused calls per model, and no more

    previous["tested"] = stamp(datetime.now(timezone.utc) - timedelta(days=22))
    code, _ = run_job(tmp_path, previous)
    assert code == 1  # three weeks without a confirmed chain: the run fails on purpose


def test_a_refused_key_fails_the_run_at_once(world: Callable[..., World], tmp_path: Path) -> None:
    world([PRIMARY, FALLBACK], {PRIMARY: always("key"), FALLBACK: always("key")})
    # A fresh list exists, so only the refused key can be the reason for the red run.
    fresh = {**valid(), "chain": [PRIMARY, FALLBACK], "tested": stamp(datetime.now(timezone.utc) - timedelta(days=1))}

    code, written = run_job(tmp_path, fresh, force=True)

    assert code == 1 and written is None


def test_the_job_leaves_a_fifth_of_the_day_for_visitors(
    world: Callable[..., World], tmp_path: Path, allowance: Callable[..., list[None]]
) -> None:
    made = world([PRIMARY, FALLBACK], HEALTHY)
    allowance(remaining=23, limit=50)  # 23 - 14 < 10

    code, written = run_job(tmp_path, force=True)

    assert written is None and made.test_calls == [] and code == 1  # nothing published yet, and none could be made


def test_the_job_tests_although_openrouter_cannot_say_what_is_left(
    world: Callable[..., World], tmp_path: Path, allowance: Callable[..., list[None]]
) -> None:
    """The job is the owner's own upkeep, once a day at most and one test at most: it does not wait for the counter."""
    made = world([PRIMARY, FALLBACK], HEALTHY)
    allowance(None)

    code, written = run_job(tmp_path)

    assert code == 0 and written["chain"] == [PRIMARY, FALLBACK] and len(made.test_calls) == 14


def test_the_workflow_starts_on_the_files_the_test_is_made_of() -> None:
    """A push starts the job only for the files it names, so a module that is renamed
    or split must be renamed there too: otherwise a changed test would publish nothing new."""
    root = Path(__file__).resolve().parents[1]
    workflows = root / ".github" / "workflows"
    workflow = (workflows / "model-check.yml").read_text(encoding="utf-8")
    block = r"^  push:\n(?:    .*\n)*?    paths:\n((?:      - \S+\n)+)"
    listed = re.search(block, workflow, re.MULTILINE)
    assert listed is not None, "the workflow no longer names the files a push starts it for"
    paths = re.findall(r"^      - (\S+)$", listed.group(1), re.MULTILINE)

    for path in paths:
        assert (root / path).is_file(), f"{path} is named in the workflow and is not in the project"
    # What the job's command runs, and what the fingerprint of a test is made from.
    assert set(paths) >= {
        "app/config.py",
        "app/extractors.py",
        "app/schemas.py",
        "app/model_catalogue.py",
        "app/model_rules.py",
        "app/model_testing.py",
        "app/published_chain.py",
        "app/budget.py",
        "app/playground/catalogue.py",
        "app/playground/benchmark.py",
        "requirements.txt",
        ".github/workflows/model-check.yml",
    }
    # The same actions and the same Python as the workflow that runs this suite.
    suite = (workflows / "tests.yml").read_text(encoding="utf-8")
    actions = set(re.findall(r"uses: (\S+)", workflow))
    assert actions and actions == set(re.findall(r"uses: (\S+)", suite))
    assert "python-version-file: .python-version" in workflow
    assert (root / ".python-version").is_file()


def test_the_workflow_checks_the_file_again_and_keeps_key_and_write_access_apart() -> None:
    """The job that may write looks at the file once more, with lines of its own. Those
    lines are code too, and nothing else in the suite runs them."""
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "model-check.yml").read_text(encoding="utf-8")
    test_job, publish_job = text.split("\n  publish:\n")
    publish_job, _ = publish_job.split("\n  verdict:\n")

    # The same pattern, size, length and version as the service's own checks.
    assert f'free = re.compile(r"{FREE_MODEL_ID.pattern}")' in publish_job
    assert "and all(isinstance(model, str) and free.fullmatch(model) for model in chain)" in publish_job
    assert "and len(set(chain)) == len(chain)" in publish_job
    assert f'os.path.getsize("../new-chain.json") <= {published_chain.MAX_BYTES:_}\n' in publish_job
    assert f"1 <= len(chain) <= {model_rules.PLACES}" in publish_job
    assert f'new.get("version") == {published_chain.VERSION}' in publish_job
    written = re.search(r'stamp = re\.compile\(r"([^"]+)"\)', publish_job)
    assert written is not None and re.fullmatch(written.group(1), stamp(NOW))
    assert 'and stamp.fullmatch(new["tested"])' in publish_job
    # Only what the main branch tested is published.
    assert "    if: needs.test.outputs.file != '' && github.ref == 'refs/heads/main'\n" in publish_job
    # Read-only by default; only "publish" may write, and it never sees the key.
    assert "\npermissions:\n  contents: read\n" in test_job
    assert publish_job.count("contents: write") == 1 and "contents: write" not in test_job
    assert text.count("secrets.OPENROUTER_API_KEY") == 1 and "secrets." not in publish_job
    assert "pip install" not in publish_job and "actions/checkout" not in publish_job
    # Once a day, off the hour.
    assert re.search(r'^\s+- cron: "\d{1,2} \d{1,2} \* \* \*"$', text, re.MULTILINE)
    # A branch without the file, and a branch that cannot be asked for, are told apart.
    assert "git show FETCH_HEAD:chain.json > previous/chain.json || rm -f previous/chain.json" in test_job
    assert "git ls-remote --exit-code --heads origin tested-models" in test_job

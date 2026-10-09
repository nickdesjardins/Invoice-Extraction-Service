"""Tests for the service's upkeep of its own model chain (``app.model_chooser``).

Nothing here calls a model or reaches the network. The engine a test would call is
replaced by one that answers from a script, OpenRouter's list and its pages by plain
Python values, and the cache file lives in a temporary folder. ``tests/conftest.py``
fails any test that reaches for the network without such a stand-in.

Layout:

* Model chooser - automatic or pinned    - what a blank setting means, and startup.
* Model chooser - free models only       - OpenRouter's list, and the door behind it.
* Model chooser - noticing a failure     - what starts a repair, and what does not.
* Model chooser - the test and the chain - a repair, the button, one chain everywhere.
* Model chooser - guards                 - one at a time, the pause, the allowance.
* Model chooser - what the six reviews attacked.

Then what the reviews of the integrated copy found, one section per subject: the
modes and startup, what a look may conclude, a repair that is still owed, stand-ins,
what a test costs and survives, what a restart finds, a test that judged nobody.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import timedelta
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
from app.config import DEFAULT_MODEL_CHAIN, Settings, get_settings
from app.extractors import (
    BaseExtractor,
    ExtractionError,
    ProviderError,
    ProviderRateLimitError,
    SchemaValidationError,
    UnparseableTextError,
    extract_invoice,
    get_dispatcher,
)
from app.main import app
from app.model_catalogue import Listing
from app.playground import benchmark
from app.schemas import InvoiceData
from tests.test_extract import (  # noqa: F401 - `http` and `router` are fixtures
    EMPTY_INVOICE_PAYLOAD,
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    api_error,
    http,
    ok,
    router,
)
from tests.test_playground import result_of

PRIMARY, FALLBACK = [model.strip() for model in DEFAULT_MODEL_CHAIN.split(",")]
NEWCOMER = "vendor/newcomer:free"
SLOW = "vendor/slow:free"
BROKEN = "vendor/broken:free"


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


def right_answer(case: dict[str, Any]) -> InvoiceData:
    """An invoice that passes every check of ``case``, built from the checks themselves."""
    data: dict[str, Any] = {"vendor_name": "Vendor", "invoice_number": "X-1", "total_amount": 1.0, "items": []}
    for check in case["checks"]:
        if check["kind"] == "status":
            continue
        name = check["path"].removeprefix("data.")
        if check["kind"] == "count":
            data[name] = [{"description": "line", "quantity": 1, "unit_price": 1.0, "total": 1.0}] * check["expected"]
        else:
            data[name] = check["expected"]
    return InvoiceData.model_validate(data)


class ScriptedEngine(BaseExtractor):
    """Answers each sample document from ``SCRIPT[model]`` and notes what it was asked."""

    SCRIPT: dict[str, Callable[[dict[str, Any]], str]] = {}
    CALLS: list[tuple[str, str]] = []
    DELAY: dict[str, float] = {}
    #: A model whose calls wait here until the test says so: "a test is running" for
    #: as long as a test needs it to be, however slow the machine is.
    GATE: dict[str, asyncio.Event] = {}

    def __init__(self, model: str, settings: Settings) -> None:
        super().__init__(settings)
        self._model = model
        self.provider = model

    @property
    def model(self) -> str:
        return self._model

    def is_configured(self) -> bool:
        return True

    async def _generate(self, text: str) -> Optional[str]:  # pragma: no cover - unused
        raise NotImplementedError

    def _translate_error(self, exc: Exception) -> ExtractionError:  # pragma: no cover - unused
        raise NotImplementedError

    async def extract(self, text: str) -> InvoiceData:
        case = next(item for item in benchmark.live_cases() if item["text"] == text)
        ScriptedEngine.CALLS.append((self.model, case["id"]))
        await asyncio.sleep(ScriptedEngine.DELAY.get(self.model, 0.0))
        if self.model in ScriptedEngine.GATE:
            await ScriptedEngine.GATE[self.model].wait()
        kind = ScriptedEngine.SCRIPT[self.model](case)
        if kind == "gone":
            raise ProviderError(f"{self.model} was not found (404)", provider=self.model)
        if kind == "limited":
            raise ProviderRateLimitError(f"{self.model}: rate limited (429)", provider=self.model, retryable=True)
        if kind == "format":
            raise SchemaValidationError(f"{self.model} does not support the format", provider=self.model)
        if kind == "key":
            raise extractors.ProviderAuthError("openrouter rejected the API key (401)", provider=self.model)
        if kind == "cut":
            raise extractors.ContentBlockedError(f"{self.model} truncated the response", provider=self.model)
        if kind == "upstream":
            message = f"{self.model}: upstream provider error (code 502)"
            raise ProviderError(message, provider=self.model, retryable=True)
        refusal = benchmark.expects_refusal(case)
        if kind == "right" and refusal:
            raise UnparseableTextError("found no invoice", provider=self.model)
        invoice = right_answer(case) if not refusal else InvoiceData.model_validate(
            {"vendor_name": "Invented", "invoice_number": "1", "total_amount": 5.0, "items": []}
        )
        if kind == "wrong" and not refusal:
            invoice.total_amount += 1
        return invoice


def always(kind: str) -> Callable[[dict[str, Any]], str]:
    return lambda case: kind


def wrong_on(*case_ids: str, these: str = "wrong") -> Callable[[dict[str, Any]], str]:
    return lambda case: these if case["id"] in case_ids else "right"


def as_recorded(marks: str) -> Callable[[dict[str, Any]], str]:
    """A model that answers the seven documents as one saved pass went, one mark per document.

    ``+`` right, ``x`` wrong, ``B`` cut off at the output cap, ``E`` an upstream error.
    """
    kinds = {"+": "right", "x": "wrong", "B": "cut", "E": "upstream"}
    plan = {case["id"]: kinds[mark] for case, mark in zip(benchmark.live_cases(), marks, strict=True)}
    return lambda case: plan[case["id"]]


class World:
    """What OpenRouter "lists", what its pages say, and how each model answers a test."""

    def __init__(self) -> None:
        self.listed: list[str] = []
        self.not_found: set[str] = set()
        self.list_reads = 0
        self.lookups: list[list[str]] = []
        self.list_fails = False

    def free_models(self, base_url: str) -> list[str]:
        self.list_reads += 1
        if self.list_fails:
            raise TimeoutError("no list")
        return list(self.listed)

    def check_chain(self, models: list[str], base_url: str) -> list[Listing]:
        self.lookups.append(list(models))
        return [
            Listing(model, "not_listed" if model in self.not_found else "listed")
            for model in models
        ]

    @property
    def test_calls(self) -> list[tuple[str, str]]:
        return ScriptedEngine.CALLS

    def asked(self, model: str) -> int:
        return sum(1 for called, _ in ScriptedEngine.CALLS if called == model)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Callable[..., World]:
    """Leave OPENROUTER_MODELS blank and replace everything that would reach the network."""
    monkeypatch.delenv("OPENROUTER_MODELS")
    monkeypatch.setattr(benchmark, "OpenRouterExtractor", ScriptedEngine)
    monkeypatch.setattr(model_rules, "SECONDS_BETWEEN_STARTS", 0.0)
    monkeypatch.setattr(ScriptedEngine, "SCRIPT", {})
    monkeypatch.setattr(ScriptedEngine, "CALLS", [])
    monkeypatch.setattr(ScriptedEngine, "DELAY", {})
    monkeypatch.setattr(ScriptedEngine, "GATE", {})
    extractors.reset_extractors()
    model_cache.forget()
    model_chooser.reset()
    made = World()
    monkeypatch.setattr(model_catalogue, "free_models", made.free_models)
    monkeypatch.setattr(model_catalogue, "check_chain", made.check_chain)

    def install(listed: list[str], script: dict[str, Callable[[dict[str, Any]], str]], gone: tuple[str, ...] = ()) -> World:
        made.listed[:] = listed
        made.not_found.update(gone)
        ScriptedEngine.SCRIPT.update(script)
        return made

    return install


async def settled() -> dict[str, Any]:
    """Wait for the background work to end, and return the picture."""
    await asyncio.wait_for(model_chooser.wait(), timeout=10)
    return model_chooser.picture()


def later(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quarter of an hour passes: the pause between tests, the minute between looks, the ceiling's minute."""
    monkeypatch.setattr(model_chooser, "_test_ended_at", -10_000.0)
    monkeypatch.setattr(model_chooser, "_looked_at", -10_000.0)
    budget.reset()


def watch_stand_ins(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Note the chain in use, and whether a file is on the disk, each time stand-ins change.

    A test that looked at the picture now and then would depend on how fast the
    machine is; this sees every change, in order.
    """
    seen: list[dict[str, Any]] = []
    plain = model_chooser._stand_in

    def watched(behind: Any, models: Any, reason: Any) -> None:
        plain(behind, models, reason)
        seen.append(
            {"standing": list(models), "chain": get_dispatcher().models, "saved": model_cache.PATH.exists()}
        )

    monkeypatch.setattr(model_chooser, "_stand_in", watched)
    return seen


HEALTHY = {PRIMARY: always("right"), FALLBACK: wrong_on("service-invoice")}


# ---------------------------------------------------------------------------
# Model chooser - automatic or pinned
# ---------------------------------------------------------------------------


def test_the_suite_itself_runs_pinned() -> None:
    """conftest names three models, so no other test can start a test of models by accident."""
    assert get_settings().models_pinned is True
    assert model_chooser.mode() == "pinned"
    picture = model_chooser.picture()
    assert picture["origin"]["kind"] == "pinned"
    assert picture["button"] == {"offered": False, "ready": False, "reason": "pinned", "again_at": None}


@pytest.mark.parametrize("value", [None, "", "  "])
def test_a_blank_setting_means_automatic(
    value: Optional[str], world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    if value == "":
        monkeypatch.setenv("OPENROUTER_MODELS", value)
        extractors.reset_extractors()
    if value == "  ":
        # Only spaces is "named, with no id in it": refused at startup, as before.
        monkeypatch.setenv("OPENROUTER_MODELS", value)
        extractors.reset_extractors()
        with pytest.raises(Exception):
            get_settings()
        return
    assert get_settings().models_pinned is False
    assert get_settings().model_chain == [PRIMARY, FALLBACK]
    assert model_chooser.mode() == "automatic"


def test_naming_the_default_models_still_pins_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_MODELS", DEFAULT_MODEL_CHAIN)
    extractors.reset_extractors()
    assert get_settings().models_pinned is True


def test_a_pinned_chain_ignores_a_saved_choice(world: Callable[..., World], monkeypatch: pytest.MonkeyPatch) -> None:
    world([], {})
    model_cache.use([NEWCOMER], {"kind": "own", "tested": "2026-10-08T10:00:00Z"})
    assert get_settings().model_chain == [NEWCOMER]
    monkeypatch.setenv("OPENROUTER_MODELS", f"{PRIMARY},{FALLBACK}")
    extractors.reset_extractors()
    assert get_settings().model_chain == [PRIMARY, FALLBACK]


@pytest.mark.asyncio
async def test_startup_calls_no_model_and_reads_nothing_when_the_check_is_off(world: Callable[..., World]) -> None:
    """Any request can start a sleeping service, a crawler's included: startup must spend nothing."""
    made = world([PRIMARY, FALLBACK, NEWCOMER], HEALTHY)
    async with app.router.lifespan_context(app):
        picture = model_chooser.picture()
    assert made.test_calls == [] and made.list_reads == 0 and made.lookups == []
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["origin"]["kind"] == "default"
    assert picture["test"] is None and picture["busy"] is False
    assert not model_cache.PATH.exists()  # nothing is written until something changes


def test_a_saved_choice_is_used_and_a_bad_file_is_not(world: Callable[..., World]) -> None:
    world([], {})
    model_cache.use([NEWCOMER, FALLBACK], {"kind": "own", "tested": "2026-10-08T10:00:00Z"})
    model_cache.forget()  # a new process: only the file is left
    extractors.reset_extractors()
    assert get_settings().model_chain == [NEWCOMER, FALLBACK]

    for content in (
        "not json",
        "[]",
        '{"version": 2, "default": "%s", "chain": ["a/b:free"], "origin": {}}' % DEFAULT_MODEL_CHAIN,
        '{"version": 1, "default": "other", "chain": ["a/b:free"], "origin": {}}',
        '{"version": 1, "default": "%s", "chain": ["openai/gpt-paid"], "origin": {}}' % DEFAULT_MODEL_CHAIN,
        '{"version": 1, "default": "%s", "chain": ["a/b:free", "@preset/x:free"], "origin": {}}' % DEFAULT_MODEL_CHAIN,
        '{"version": 1, "default": "%s", "chain": ["a/b:free", "a/b:free"], "origin": {}}' % DEFAULT_MODEL_CHAIN,
        '{"version": 1, "default": "%s", "chain": "a/b:free", "origin": {}}' % DEFAULT_MODEL_CHAIN,
        "[" * 100_000,
    ):
        model_cache.PATH.write_text(content, encoding="utf-8")
        model_cache.forget()
        extractors.reset_extractors()
        assert get_settings().model_chain == [PRIMARY, FALLBACK], content[:40]


def test_the_cache_file_is_never_committed() -> None:
    """A committed cache would be read at every start of every deployment, and outrank the default."""
    from pathlib import Path

    ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "model_cache.json" in ignored and "model_cache.json.*.tmp" in ignored


def test_a_paid_model_can_never_be_chosen(world: Callable[..., World]) -> None:
    world([], {})
    for chain in (
        ["openai/gpt-paid"],
        [NEWCOMER, "vendor/model"],
        [],
        ["a/b:free", "c/d:free", "e/f:free", "g/h:free"],
        ["@preset/x:free"],
        [FALLBACK, NEWCOMER, NEWCOMER],  # the file would not be taken back with an id named twice
    ):
        with pytest.raises(ValueError):
            model_cache.use(chain, {"kind": "own", "tested": "2026-10-08T10:00:00Z"})
    assert get_settings().model_chain == [PRIMARY, FALLBACK]
    # Three places, said in two modules: one of them cannot import the other.
    assert model_cache.MAX_CHAIN == model_rules.PLACES


def test_the_command_says_which_chain_it_checks(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the setting left blank, the chain can be one that nobody named anywhere."""
    world([], {})
    gone = lambda models, base_url: [Listing(m, "not_listed", note="withdrawn") for m in models]  # noqa: E731
    monkeypatch.setattr(model_catalogue, "check_chain", gone)

    assert model_catalogue.main() == 1
    assert "Replace it in OPENROUTER_MODELS, or in DEFAULT_MODEL_CHAIN" in capsys.readouterr().out

    model_cache.use([SLOW], {"kind": "own", "tested": "2026-10-08T10:00:00Z"})
    extractors.reset_extractors()
    assert model_catalogue.main() == 1
    printed = capsys.readouterr().out
    assert "the chain a service on this machine saved in model_cache.json" in printed
    assert f"{SLOW} is not listed by OpenRouter" in printed and "replaces it by itself" in printed
    assert "OPENROUTER_MODELS" not in printed  # the setting is blank, so nothing is to change there


# ---------------------------------------------------------------------------
# Model chooser - free models only
# ---------------------------------------------------------------------------

OPENROUTER = "https://openrouter.ai/api/v1"


def listed(model: Any, prompt: Any = "0", completion: Any = "0", can: Any = ("structured_outputs",)) -> dict[str, Any]:
    """One entry of OpenRouter's list of models."""
    return {"id": model, "pricing": {"prompt": prompt, "completion": completion}, "supported_parameters": list(can)}


def test_openrouters_list_is_checked_entry_by_entry() -> None:
    """Nothing is left to the address that was asked: this list is what a test calls from."""
    asked: list[str] = []
    entries = [
        listed("good/one:free"),
        listed("openai/gpt-paid"),  # not a free id
        listed("good/one:nitro"),  # a routing variant
        listed("@preset/cheap:free"),
        listed("costs/on-input:free", prompt="0.000001"),
        listed("costs/on-output:free", completion="0.000001"),
        listed("no/structure:free", can=("tools", "temperature")),
        {"id": "no/prices:free", "supported_parameters": ["structured_outputs"]},
        listed("odd/prices:free", prompt="free", completion=None),
        listed("good/two:free", prompt=0, completion="0.0"),
        listed("good/one:free"),  # named a second time: it is one model
        "junk",
        {"id": 7},
    ]

    def fetch(url: str) -> tuple[int, str]:
        asked.append(url)
        return 200, json.dumps({"data": entries})

    assert model_catalogue.free_models(OPENROUTER, fetch) == ["good/one:free", "good/two:free"]
    assert asked == [OPENROUTER + "/models?supported_parameters=structured_outputs&max_price=0"]


def test_no_list_is_never_taken_for_an_empty_list() -> None:
    """ "No answer" and "no free model" lead to different things, so the first one raises."""
    good = json.dumps({"data": [listed("good/one:free")]})
    for answer in ((500, good), (200, json.dumps({"models": []})), (200, "<html>please wait</html>")):
        with pytest.raises(ValueError):
            model_catalogue.free_models(OPENROUTER, lambda url: answer)
    for elsewhere in (
        "https://example.com/api/v1",
        "http://openrouter.ai/api/v1",
        "https://openrouter.ai.example.com/api/v1",
    ):
        with pytest.raises(ValueError):
            model_catalogue.free_models(elsewhere, lambda url: pytest.fail(f"asked {url}"))


@pytest.mark.asyncio
async def test_a_test_never_calls_an_id_that_is_not_free_whatever_the_list_said(
    http: httpx.AsyncClient, world: Callable[..., World]
) -> None:
    """The reader of the list is one door. The test itself is the one behind it."""
    paid, preset = "openai/gpt-4o", "@preset/cheap"
    made = world(
        [PRIMARY, FALLBACK, paid, preset],
        {**HEALTHY, paid: always("right"), preset: always("right")},
    )

    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert made.test_calls == []  # not one request, to any model
    assert picture["last"]["outcome"] == "interrupted" and picture["chain"] == [PRIMARY, FALLBACK]
    assert picture["test"] is None and picture["busy"] is False


def test_a_folder_that_cannot_be_written_costs_a_warning_and_nothing_else(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world([], {})
    monkeypatch.setattr(model_cache, "PATH", model_cache.PATH.parent / "no-such-folder" / "model_cache.json")
    with caplog.at_level("WARNING", logger="app.model_cache"):
        model_cache.use([NEWCOMER], {"kind": "own", "tested": "2026-10-08T10:00:00Z"})
        model_cache.use([NEWCOMER, FALLBACK], {"kind": "own", "tested": "2026-10-08T10:00:00Z"})
    assert get_settings().model_chain == [NEWCOMER, FALLBACK]  # in use all the same
    assert len([r for r in caplog.records if "Could not save" in r.getMessage()]) == 1


# ---------------------------------------------------------------------------
# Model chooser - noticing a failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_withdrawn_first_model_is_replaced_after_the_first_failed_request(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    made = world(
        [FALLBACK, NEWCOMER, SLOW, BROKEN],
        {NEWCOMER: always("right"), SLOW: always("right"), BROKEN: always("format")},
        gone=(PRIMARY,),
    )
    ScriptedEngine.DELAY[SLOW] = 0.0
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})

    response = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})

    # The caller's answer is what it always was: the fallback answered.
    assert response.status_code == 200
    assert set(response.json()) == {"success", "data", "model_used", "execution_time_ms", "fallback_triggered"}
    assert response.json()["model_used"] == FALLBACK and response.json()["fallback_triggered"] is True

    picture = await settled()
    # Only the model that failed was looked up, and only models outside the chain were tested.
    assert made.lookups == [[PRIMARY]]
    assert made.asked(PRIMARY) == made.asked(FALLBACK) == 0
    assert made.asked(NEWCOMER) == 7 and made.asked(BROKEN) == 2
    assert picture["chain"] == [FALLBACK, NEWCOMER, SLOW]
    assert picture["origin"]["kind"] == "own"
    last = picture["last"]
    assert last["by"] == "failure" and last["reason"] == {"code": "gone", "models": [PRIMARY]}
    assert last["outcome"] == "changed" and last["chain_before"] == [FALLBACK]
    verdicts = {row["model"]: (row["verdict"], row["placing"]) for row in last["models"]}
    assert verdicts[NEWCOMER] == ("passed", "added")
    assert verdicts[BROKEN] == ("failed", None)
    # One chain everywhere.
    health = (await http.get("/health")).json()
    config = (await http.get("/playground/config")).json()
    assert [p["model"] for p in health["providers"]] == picture["chain"]
    assert [entry["model"] for entry in config["chain"]] == picture["chain"]
    assert get_dispatcher().models == get_settings().model_chain == picture["chain"]
    router({FALLBACK: ok()})
    assert (await extract_invoice(SAMPLE_INVOICE_TEXT))[1] == FALLBACK


@pytest.mark.asyncio
async def test_a_rate_limited_model_that_is_still_listed_changes_nothing(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    made = world([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: always("right")})
    router({PRIMARY: api_error(openai.RateLimitError, 429), FALLBACK: ok()})

    for _ in range(3):
        assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 200
    picture = await settled()

    assert made.lookups == [[PRIMARY]]  # three failing requests, one look
    assert made.test_calls == [] and made.list_reads == 0
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["origin"]["kind"] == "default"


@pytest.mark.asyncio
async def test_when_nobody_answers_the_first_right_answer_is_put_to_use_at_once(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = world(
        [NEWCOMER, SLOW, BROKEN],
        {NEWCOMER: wrong_on("service-invoice", "ocr-receipt"), SLOW: always("right"), BROKEN: always("format")},
        gone=(PRIMARY, FALLBACK),
    )
    ScriptedEngine.DELAY.update({SLOW: 0.05, NEWCOMER: 0.01})
    router(default=api_error(openai.NotFoundError, 404))
    seen = watch_stand_ins(monkeypatch)

    response = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
    assert response.status_code == 503 and response.json()["error"] == "providers_unavailable"
    picture = await settled()

    # While the test ran: the first model to read a document correctly answered at once.
    assert seen[0] == {"standing": [NEWCOMER], "chain": [NEWCOMER], "saved": False}
    # A stand-in is never what a restart would find.
    assert not any(change["saved"] for change in seen if change["standing"])
    # The quick model then answered two documents wrongly: from that moment it answered
    # no request any more, although the slow model's test went on.
    left = next(n for n, change in enumerate(seen) if NEWCOMER not in change["standing"])
    assert all(NEWCOMER not in change["chain"] for change in seen[left:])
    # The test had the last word: the slow one was right on all seven and stays.
    assert picture["chain"] == [SLOW]
    verdicts = {row["model"]: row["verdict"] for row in picture["last"]["models"]}
    assert verdicts == {NEWCOMER: "failed", SLOW: "passed", BROKEN: "failed"}
    assert made.asked(PRIMARY) == made.asked(FALLBACK) == 0


@pytest.mark.asyncio
async def test_a_simulated_failure_is_not_evidence(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    made = world([NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY, FALLBACK))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})

    # A simulated first model, and then a real first model beside a simulated second one:
    # neither run says what the real chain would have done.
    for simulate in (["upstream_error"], [None, "rate_limit"]):
        response = await http.post(
            "/playground/extract", json={"text": SAMPLE_INVOICE_TEXT, "simulate": simulate}
        )
        assert response.status_code == 200
    await settled()

    assert made.lookups == [] and made.test_calls == []


@pytest.mark.asyncio
async def test_a_real_failure_on_the_page_is_noticed(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})

    response = await http.post("/playground/extract", json={"text": SAMPLE_INVOICE_TEXT})
    assert response.status_code == 200
    picture = await settled()

    assert made.lookups == [[PRIMARY]]
    assert picture["chain"] == [FALLBACK, NEWCOMER]


@pytest.mark.asyncio
async def test_the_page_route_tells_a_verdict_from_a_failure_and_the_api_route_cannot(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """The first model says "not an invoice" and the second finds one.

    The page's route sees every attempt: to it that verdict is an answer, and nothing
    is looked into. The engine hands the API route the winner only, so there an
    earlier model counts as one that did not answer, whatever it said.
    """
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    router({PRIMARY: ok(EMPTY_INVOICE_PAYLOAD), FALLBACK: ok()})

    on_the_page = await http.post("/playground/extract", json={"text": SAMPLE_INVOICE_TEXT})
    await settled()
    assert result_of(on_the_page)["body"]["model_used"] == FALLBACK
    assert made.lookups == [] and made.test_calls == []

    through_the_api = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
    await settled()
    assert through_the_api.json()["model_used"] == FALLBACK
    assert made.lookups == [[PRIMARY]]


@pytest.mark.asyncio
async def test_a_request_that_ends_in_not_an_invoice_is_told_by_the_page_route_only(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """The first model is gone and the second says "not an invoice": a 422 either way.

    The API route learns of failed models from the error that lists them or from a
    later model's answer, and here there is neither. The next request that a model
    answers, or that every model fails, brings the withdrawn one to light.
    """
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok(EMPTY_INVOICE_PAYLOAD)})

    through_the_api = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
    await settled()
    assert through_the_api.status_code == 422 and made.lookups == []

    on_the_page = await http.post("/playground/extract", json={"text": SAMPLE_INVOICE_TEXT})
    picture = await settled()
    assert result_of(on_the_page)["status_code"] == 422
    assert made.lookups == [[PRIMARY]] and picture["chain"] == [FALLBACK, NEWCOMER]


@pytest.mark.asyncio
async def test_a_refused_key_starts_nothing(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    made = world([NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY, FALLBACK))
    router(default=api_error(openai.AuthenticationError, 401))

    assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 503
    await settled()

    assert made.lookups == [] and made.test_calls == []


@pytest.mark.asyncio
async def test_models_that_are_listed_but_closed_to_this_account_get_a_spare_beside_them(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """Listed, but every call answers "not found": the account's privacy settings, for example."""
    made = world([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: always("right")})
    router(default=api_error(openai.NotFoundError, 404))

    assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 503
    picture = await settled()

    # Nothing is dropped on the calls alone; the free third place gets a model that works.
    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER]
    assert picture["last"]["reason"]["code"] == "closed"
    assert made.asked(PRIMARY) == 0


# ---------------------------------------------------------------------------
# Model chooser - the test and the chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_button_tests_everything_and_only_fills_the_free_place(
    http: httpx.AsyncClient, world: Callable[..., World]
) -> None:
    made = world([PRIMARY, FALLBACK, NEWCOMER, BROKEN], {**HEALTHY, NEWCOMER: always("right"), BROKEN: always("format")})
    ScriptedEngine.GATE[PRIMARY] = under_way = asyncio.Event()  # the test runs until it is set

    response = await http.post("/playground/models/test", headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 202 and response.json() == {"started": True}
    again = await http.post("/playground/models/test")
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "running"
    under_way.set()

    picture = await settled()
    last = picture["last"]
    assert last["by"] == "page" and last["tested"] == [PRIMARY, FALLBACK, NEWCOMER, BROKEN]
    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER]  # the order of the tested two is untouched
    assert (made.asked(PRIMARY), made.asked(FALLBACK), made.asked(NEWCOMER), made.asked(BROKEN)) == (7, 7, 7, 2)
    assert last["requests"] == 23 == len(last["calls"])
    assert set(last["calls"][0]) == {"model", "case", "outcome", "correct", "seconds", "error_type"}
    # Tests are kept apart: the next press is refused with the time it becomes possible.
    later = await http.post("/playground/models/test")
    assert later.status_code == 409 and later.json()["detail"]["reason"] == "pause"
    assert picture["button"]["reason"] == "pause" and picture["button"]["again_at"]


@pytest.mark.asyncio
async def test_weather_on_the_first_model_does_not_reorder_the_chain(
    http: httpx.AsyncClient, world: Callable[..., World]
) -> None:
    world([PRIMARY, FALLBACK, NEWCOMER], {PRIMARY: always("limited"), FALLBACK: always("right"), NEWCOMER: wrong_on("eur-invoice", "ocr-receipt")})

    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["chain"] == [PRIMARY, FALLBACK]
    verdicts = {row["model"]: (row["verdict"], row["placing"]) for row in picture["last"]["models"]}
    assert verdicts[PRIMARY] == ("not_judged", "kept")
    assert verdicts[NEWCOMER] == ("failed", None)
    assert picture["last"]["outcome"] == "unchanged"


@pytest.mark.asyncio
async def test_a_request_that_is_running_keeps_its_chain(
    router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    from tests.test_extract import VALID_INVOICE_PAYLOAD, slow

    world([], {})
    fake = router({PRIMARY: slow(0.05, VALID_INVOICE_PAYLOAD), NEWCOMER: ok()})
    running = asyncio.create_task(extract_invoice(SAMPLE_INVOICE_TEXT))
    await asyncio.sleep(0.01)
    model_chooser._use([NEWCOMER], "own", model_chooser.tested_at(), {"code": "test", "models": []})

    assert (await extract_invoice(SAMPLE_INVOICE_TEXT))[1] == NEWCOMER
    assert (await running)[1] == PRIMARY
    assert fake.models_called == [PRIMARY, NEWCOMER]


@pytest.mark.asyncio
async def test_a_refused_key_stops_a_test_at_once(http: httpx.AsyncClient, world: Callable[..., World]) -> None:
    made = world([PRIMARY, FALLBACK, NEWCOMER], {PRIMARY: always("key"), FALLBACK: always("key"), NEWCOMER: always("key")})

    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["last"]["outcome"] == "key_refused" and picture["chain"] == [PRIMARY, FALLBACK]
    # With no spacing between starts (this suite) one call per model is under way when the
    # first refusal arrives; none follows. With the real spacing it is a single call.
    assert len(made.test_calls) == picture["last"]["requests"] == 3


# ---------------------------------------------------------------------------
# Model chooser - guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "remaining", "reason"),
    [
        (1000, 599, "allowance"),
        (50, 29, "allowance"),
        (1000, None, "allowance_unknown"),  # OpenRouter cannot say what is left
    ],
)
@pytest.mark.asyncio
async def test_the_button_is_refused_when_too_little_of_the_day_is_left(
    limit: int, remaining: Optional[int], reason: str, http: httpx.AsyncClient,
    world: Callable[..., World], allowance: Callable[..., list[None]],
) -> None:
    made = world([PRIMARY, FALLBACK], HEALTHY)
    allowance(remaining, limit)

    response = await http.post("/playground/models/test")

    assert response.status_code == 409 and response.json()["detail"]["reason"] == reason
    assert made.test_calls == []
    assert model_chooser.picture()["button"]["reason"] == reason  # a second tab is told the same


@pytest.mark.asyncio
async def test_the_button_works_on_a_small_account_once(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch,
    allowance: Callable[..., list[None]],
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    allowance(remaining=50, limit=50)

    assert (await http.post("/playground/models/test")).status_code == 202
    await settled()
    monkeypatch.setattr(model_chooser, "_test_ended_at", -10_000.0)  # the pause is over

    # The test's fourteen calls are in the one minute that visitors share: one more
    # call still fits under the fifteen, and a run of two has to wait.
    assert budget.seconds_until_room(1) == 0 and budget.seconds_until_room(2) > 0
    # 50 - 14 = 36 are left, which is still above three fifths of 50: one more may run.
    assert await budget.why_no_test("failure", 36) is None  # what is left, by the service's own sum
    assert await budget.why_no_test("failure", 37) == "allowance"
    assert (await http.post("/playground/models/test")).status_code == 202
    await settled()
    monkeypatch.setattr(model_chooser, "_test_ended_at", -10_000.0)

    # 22 left: under three fifths, so the button waits for the next day.
    refused = await http.post("/playground/models/test")
    assert refused.json()["detail"]["reason"] == "allowance"
    assert refused.json()["detail"]["again_at"].endswith("T00:00:00Z")


@pytest.mark.asyncio
async def test_an_unreadable_list_is_not_a_test(http: httpx.AsyncClient, world: Callable[..., World]) -> None:
    made = world([PRIMARY, FALLBACK], HEALTHY)
    made.list_fails = True

    response = await http.post("/playground/models/test")

    assert response.status_code == 409 and response.json()["detail"]["reason"] == "list_unreadable"
    assert made.test_calls == [] and model_chooser.picture()["last"]["by"] == "recorded"


@pytest.mark.asyncio
async def test_the_repair_is_refused_last(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    allowance: Callable[..., list[None]],
) -> None:
    """Visitors are cut off at a fifth of the day; the repair only needs its own requests to fit."""
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    # Two above the fifth: a run that may call two models needs both of them above it.
    readings = allowance(remaining=202, limit=1000)
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})

    assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 200
    picture = await settled()
    assert picture["chain"] == [FALLBACK, NEWCOMER] and made.asked(NEWCOMER) == 7

    # 202 - 2 (the request) - 7 (the test) = 193 left by the service's own sum: under a
    # fifth, so visitors now wait. OpenRouter was asked once for all of this.
    refused = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
    assert refused.status_code == 503 and refused.json()["detail"]["error_type"] == "CallLimitError"
    assert "today's share" in refused.json()["message"]
    assert int(refused.headers["retry-after"]) >= 1  # the seconds until midnight UTC
    assert len(readings) == 1


# ---------------------------------------------------------------------------
# Model chooser - what the six reviews attacked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tested_fallback_that_is_only_rate_limited_keeps_first_place(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """The first model is gone and the fallback has a bad second: nothing may push it aside."""
    made = world([FALLBACK, SLOW, BROKEN], {SLOW: always("right"), BROKEN: always("format")}, gone=(PRIMARY,))
    ScriptedEngine.DELAY[SLOW] = 0.02
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: api_error(openai.RateLimitError, 429)})

    assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 503
    for _ in range(400):
        await asyncio.sleep(0.005)
        running = model_chooser.picture()
        if running["test"] and running["test"]["provisional"]:
            break
    # While the test runs: the stand-in answers behind the listed model, not in front of it.
    assert running["chain"] == [FALLBACK, SLOW] == get_dispatcher().models
    picture = await settled()

    assert made.lookups == [[PRIMARY, FALLBACK]]
    assert made.asked(FALLBACK) == 0  # not asked again half a second after its 429
    assert picture["chain"] == [FALLBACK, SLOW]  # behind the listed model, never instead of it
    assert picture["last"]["chain_before"] == [FALLBACK]


@pytest.mark.asyncio
async def test_a_callers_own_document_cannot_start_a_test(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """Output cut off at the cap on every model: that is about the document, not about the models."""
    made = world([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: always("right")})
    router(default={"content": '{"vendor_name": "x"', "finish_reason": "length"})

    for _ in range(4):
        response = await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
        assert response.status_code == 503
        assert {entry["error_type"] for entry in response.json()["detail"]} == {"ContentBlockedError"}
    picture = await settled()

    assert made.test_calls == [] and picture["chain"] == [PRIMARY, FALLBACK]
    assert made.lookups == [[PRIMARY, FALLBACK]]  # one free look for the four requests


@pytest.mark.asyncio
async def test_nothing_else_changes_the_chain_while_a_test_runs(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    made = world([PRIMARY, FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")})
    ScriptedEngine.GATE[PRIMARY] = under_way = asyncio.Event()  # the test runs until it is set
    monkeypatch.setenv("MODEL_PUBLISHED_URL", "https://raw.githubusercontent.com/a/b/tested-models/chain.json")
    extractors.reset_extractors()
    from app import published_chain

    newer = {
        "version": 1,
        "fingerprint": published_chain.fingerprint(get_settings()),
        "tested": model_testing.stamp(model_testing.now()),
        "chain": [SLOW],
    }
    import json as _json

    monkeypatch.setattr(model_catalogue, "_http_get", lambda url: (200, _json.dumps(newer)))

    assert (await http.post("/playground/models/test")).status_code == 202
    assert await model_chooser.read_published(at_startup=False) is False  # a test has the chain
    assert get_dispatcher().models == [PRIMARY, FALLBACK]
    model_chooser.note([model_chooser.Failure(PRIMARY)], answered=True)  # dropped: a test is running
    assert model_chooser._looking is False
    under_way.set()
    picture = await settled()

    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER]
    assert made.lookups == [[PRIMARY, FALLBACK]] and picture["look"] is None  # the button's own question


def test_the_page_has_a_code_for_everything_the_service_can_say() -> None:
    """Every code is a member of one tuple, so the page's tables can be compared with them."""
    picture = model_chooser.picture()
    assert picture["origin"]["kind"] in model_chooser.ORIGINS
    assert picture["published"]["state"] in model_chooser.PUBLISHED_STATES
    assert picture["button"]["reason"] in model_chooser.REFUSALS
    record = model_testing.recorded_run()
    assert record["by"] in model_testing.STARTERS and record["reason"]["code"] in model_testing.TEST_REASONS
    for row in record["models"]:
        assert row["verdict"] in model_rules.VERDICTS and row["why"] in model_rules.REASONS
        assert row["placing"] is None or row["placing"] in model_rules.PLACINGS
    assert "OPENROUTER" not in str(picture) and "MODEL_" not in str(picture)


@pytest.mark.asyncio
async def test_a_model_that_failed_is_not_tested_again_for_a_repair(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst state: the chain is gone and nothing else passes. It must not cost a test every quarter of an hour."""
    made = world([BROKEN], {BROKEN: always("format")}, gone=(PRIMARY, FALLBACK))
    router(default=api_error(openai.NotFoundError, 404))

    assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 503
    picture = await settled()
    assert picture["last"]["outcome"] == "nothing_passed" and made.asked(BROKEN) == 2
    assert picture["look"]["result"] == "started" and picture["look"]["reason"]["code"] == "gone"

    # The pause is over and requests still fail: the look is free, and there is nothing new to test.
    monkeypatch.setattr(model_chooser, "_test_ended_at", -10_000.0)
    monkeypatch.setattr(model_chooser, "_looked_at", -10_000.0)
    assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 503
    picture = await settled()

    assert made.asked(BROKEN) == 2 and picture["look"]["result"] == "nothing_to_test"
    assert picture["chain"] == [PRIMARY, FALLBACK]  # a chain is never emptied


@pytest.mark.asyncio
async def test_the_page_is_told_what_a_look_found(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    world([PRIMARY, FALLBACK], {})
    router({PRIMARY: api_error(openai.RateLimitError, 429), FALLBACK: ok()})
    assert model_chooser.picture()["look"] is None

    await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})
    picture = await settled()

    assert picture["look"]["result"] == "passing" and picture["look"]["result"] in model_chooser.LOOK_RESULTS


@pytest.mark.asyncio
async def test_a_stand_in_that_does_not_pass_leaves_everything_as_it_was(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """Listed but closed models, and the only newcomer answers its first document and then fails."""
    world([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: wrong_on("ocr-receipt", "eur-invoice")})
    router(default=api_error(openai.NotFoundError, 404))

    assert (await http.post("/api/v1/extract", json={"text": SAMPLE_INVOICE_TEXT})).status_code == 503
    picture = await settled()

    assert picture["last"]["outcome"] == "nothing_passed"
    assert picture["chain"] == [PRIMARY, FALLBACK]
    assert picture["origin"]["kind"] == "default" and picture["origin"]["why"] is None
    assert model_cache.origin() is None


@pytest.mark.asyncio
async def test_a_model_openrouter_no_longer_lists_keeps_its_place_while_it_still_answers(
    http: httpx.AsyncClient, world: Callable[..., World]
) -> None:
    """Two sources must agree before a model leaves: gone from OpenRouter's pages, and no answer here."""
    world([FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")}, gone=(PRIMARY,))

    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER]  # same order; only the free place was filled
    verdicts = {row["model"]: (row["verdict"], row["placing"]) for row in picture["last"]["models"]}
    assert verdicts[PRIMARY] == ("passed", "kept") and picture["last"]["gone"] == [PRIMARY]


@pytest.mark.asyncio
async def test_the_button_takes_out_a_model_that_is_gone_and_gives_no_answer(
    http: httpx.AsyncClient, world: Callable[..., World]
) -> None:
    world(
        [FALLBACK, NEWCOMER],
        {PRIMARY: always("gone"), FALLBACK: wrong_on("service-invoice"), NEWCOMER: always("right")},
        gone=(PRIMARY,),
    )

    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["chain"] == [FALLBACK, NEWCOMER]
    rows = {row["model"]: row for row in picture["last"]["models"]}
    assert (rows[PRIMARY]["verdict"], rows[PRIMARY]["placing"]) == ("not_judged", "dropped")
    assert rows[PRIMARY]["calls"] == 2  # two unanswered calls end a model's test


@pytest.mark.asyncio
async def test_a_test_that_breaks_changes_nothing(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever goes wrong inside a test, the chain in use afterwards is the chain from before."""
    world([FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")}, gone=(PRIMARY,))

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the runner broke")

    monkeypatch.setattr(model_testing, "run_test", broken)
    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["last"]["outcome"] == "interrupted" and picture["last"]["state"] == "done"
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["origin"]["kind"] == "default"
    assert model_cache.origin() is None and picture["test"] is None


@pytest.mark.asyncio
async def test_the_button_waits_while_a_failed_request_is_looked_into(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One writer at a time: a look may take a model out, so no other test starts beside it."""
    import threading

    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    release = threading.Event()
    plain = made.check_chain

    def slow_lookup(models: list[str], base_url: str) -> list[Listing]:
        release.wait(5)
        return plain(models, base_url)

    monkeypatch.setattr(model_catalogue, "check_chain", slow_lookup)
    model_chooser.note([model_chooser.Failure(PRIMARY)], answered=True)
    await asyncio.sleep(0.02)  # the look is now waiting for OpenRouter's page

    pressed = await http.post("/playground/models/test")
    assert pressed.status_code == 409 and pressed.json()["detail"]["reason"] == "running"
    waiting = model_chooser.picture()
    assert waiting["busy"] is True and waiting["test"] is None
    assert waiting["button"]["ready"] is False and waiting["button"]["reason"] == "running"
    assert made.list_reads == 0  # the press cost nothing, not even a read

    release.set()
    picture = await settled()
    assert picture["chain"] == [FALLBACK, NEWCOMER] and picture["last"]["by"] == "failure"


@pytest.mark.asyncio
async def test_a_failure_is_not_looked_into_while_a_test_is_about_to_start(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other way round: a press that is still asking its free questions already has the chain."""
    import threading

    made = world([PRIMARY, FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")})
    release = threading.Event()
    plain = made.free_models

    def slow_list(base_url: str) -> list[str]:
        release.wait(5)
        return plain(base_url)

    monkeypatch.setattr(model_catalogue, "free_models", slow_list)
    pressing = asyncio.create_task(http.post("/playground/models/test"))
    for _ in range(300):  # until the press is waiting for OpenRouter's list
        if model_chooser.picture()["busy"]:
            break
        await asyncio.sleep(0.01)
    assert model_chooser.picture()["busy"] is True

    model_chooser.note([model_chooser.Failure(PRIMARY)], answered=True)
    assert model_chooser._looking is False and made.lookups == []  # dropped
    second = await http.post("/playground/models/test")
    assert second.status_code == 409 and second.json()["detail"]["reason"] == "running"

    release.set()
    assert (await pressing).status_code == 202
    picture = await settled()
    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER] and picture["last"]["by"] == "page"


@pytest.mark.asyncio
async def test_a_running_test_tells_the_ceiling_about_its_own_calls(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """So that "try again in N seconds" allows for them (tests/test_budget.py has the arithmetic)."""
    world([PRIMARY, FALLBACK], HEALTHY)
    monkeypatch.setattr(model_rules, "SECONDS_BETWEEN_STARTS", 0.01)
    assert budget._expected_gap is None

    assert (await http.post("/playground/models/test")).status_code == 202
    for _ in range(400):
        if budget._expected_gap is not None:
            break
        await asyncio.sleep(0.005)
    assert budget._expected_gap == 0.01
    await settled()

    assert budget._expected_gap is None  # and no longer once the test is over


def test_the_recorded_test_is_the_first_saved_pass_as_it_was() -> None:
    """28 real calls of 7 October, uncut: what the page shows and replays before any test has run here."""
    record = model_testing.recorded_run()
    if record is None:
        pytest.skip("this copy of the project has no benchmark records")

    assert record["by"] == "recorded" and record["requests"] == len(record["calls"]) == 28
    assert {call["model"] for call in record["calls"]} == set(record["tested"]) and len(record["tested"]) == 4
    assert record["tested"][:2] == [PRIMARY, FALLBACK] == record["chain_before"]
    verdicts = {row["model"].split("/")[0]: (row["verdict"], row["placing"]) for row in record["models"]}
    assert verdicts == {
        "nvidia": ("passed", "kept"),
        "liquid": ("passed", "kept"),
        "dots-studio": ("slow", "added"),
        "apodex": ("failed", None),
    }
    assert record["chain"][:2] == [PRIMARY, FALLBACK] and len(record["chain"]) == 3
    assert all(set(call) == set(model_testing.CALL_FIELDS) for call in record["calls"])


# ---------------------------------------------------------------------------
# Model chooser - the modes, and what startup may do
# ---------------------------------------------------------------------------

THIRD = "vendor/third:free"
API = "/api/v1/extract"
DOCUMENT = {"text": SAMPLE_INVOICE_TEXT}
CHOSEN = {"kind": "own", "tested": "2026-10-08T10:00:00Z", "changed": "2026-10-08T10:00:00Z"}
CHOOSER = "app.model_chooser"  # the logger whose lines an admin reads about the chain


def rows_of(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["model"]: row for row in record["models"]}


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("OPENROUTER_API_KEY", None, "no_key"),
        ("OPENROUTER_BASE_URL", "https://example.com/api/v1", "not_openrouter"),
        ("OPENROUTER_BASE_URL", "https://openrouter.ai.example.com/api/v1", "not_openrouter"),
        ("OPENROUTER_BASE_URL", "http://openrouter.ai/api/v1", "not_openrouter"),
    ],
)
@pytest.mark.asyncio
async def test_without_a_key_or_on_another_address_nothing_is_looked_into_or_tested(
    variable: str, value: Optional[str], expected: str, http: httpx.AsyncClient,
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    made = world([NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY, FALLBACK))
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)
    extractors.reset_extractors()

    assert model_chooser.mode() == expected
    model_chooser.note([model_chooser.Failure(PRIMARY, "ProviderError", False)], answered=False)
    pressed = await http.post("/playground/models/test")
    picture = await settled()

    assert pressed.status_code == 409 and pressed.json()["detail"]["reason"] == expected
    assert made.lookups == [] and made.list_reads == 0 and made.test_calls == []
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["look"] is None
    assert picture["button"] == {"offered": False, "ready": False, "reason": expected, "again_at": None}
    # The log does not call this "automatic": nothing is looked after here.
    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        model_chooser.log_mode()
    (line,) = [r for r in caplog.records if r.getMessage().startswith("Model choice:")]
    assert line.levelname == "WARNING" and "automatic" not in line.getMessage()
    assert "will not test or replace a model by itself" in line.getMessage()

    # A chain the service chose earlier is still the one it calls: the file is there,
    # and nothing in this mode chooses another.
    model_cache.use([NEWCOMER], {**CHOSEN, "why": {"code": "test", "models": [NEWCOMER]}})
    extractors.reset_extractors()
    assert get_settings().model_chain == [NEWCOMER] and model_chooser.mode() == expected


def test_the_startup_line_says_automatic_where_the_service_looks_after_its_chain(
    world: Callable[..., World], caplog: pytest.LogCaptureFixture
) -> None:
    """The other three modes have their own lines: two in the test above, "pinned" below."""
    world([], {})
    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        model_chooser.log_mode()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Model choice:")]
    assert lines == [f"Model choice: automatic. Using the tested default {[PRIMARY, FALLBACK]}."]


@pytest.mark.asyncio
async def test_a_pinned_service_looks_into_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With named models nothing here does anything. The suite's own environment is pinned."""
    from tests.conftest import PRIMARY_MODEL

    asked: list[Any] = []
    monkeypatch.setattr(model_catalogue, "check_chain", lambda models, base_url: asked.append(models) or [])

    model_chooser.note([model_chooser.Failure(PRIMARY_MODEL, "ProviderError", False)], answered=False)
    picture = await settled()

    assert asked == [] and picture["look"] is None and picture["busy"] is False
    assert not model_chooser._tasks


@pytest.mark.asyncio
async def test_a_pinned_startup_tells_the_admin_the_way_out_where_it_will_be_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Two of three named models are withdrawn. With LOG_LEVEL=WARNING an admin sees the errors only."""
    monkeypatch.setenv("MODEL_CATALOGUE_CHECK", "true")
    extractors.reset_extractors()
    withdrawn = lambda models, base_url: [Listing(m, "not_listed", note="withdrawn") for m in models[:2]]  # noqa: E731
    monkeypatch.setattr(main, "check_chain", withdrawn)

    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            pass

    assert any(r.getMessage().startswith("Model choice: pinned.") for r in caplog.records)
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.levelname for r in loud] == ["ERROR", "ERROR"]  # one line per model, and nothing else
    for line in (r.getMessage() for r in loud):
        assert "OPENROUTER_MODELS" in line and "empty that line" in line and "restart" in line
        assert "DEFAULT_MODEL_CHAIN" not in line  # the chain does not come from there in this mode


@pytest.mark.asyncio
async def test_startup_drops_nothing_and_tests_nothing_when_a_model_is_not_listed(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Any request wakes a sleeping service, a crawler's too: startup may flag a model, and no more."""
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    monkeypatch.setenv("MODEL_CATALOGUE_CHECK", "true")
    extractors.reset_extractors()
    monkeypatch.setattr(main, "check_chain", made.check_chain)

    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            picture = await settled()

    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["origin"]["kind"] == "default"
    assert picture["gone"] == [PRIMARY]  # flagged for the page
    assert made.lookups == [[PRIMARY, FALLBACK]]  # the startup check, and no look after it
    assert made.test_calls == [] and made.list_reads == 0 and picture["look"] is None
    about = [r for r in caplog.records if PRIMARY in r.getMessage() and "not listed" in r.getMessage()]
    assert [r.levelname for r in about] == ["WARNING"]
    assert "looks for a replacement after the first request that fails" in about[0].getMessage()
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(r.getMessage().startswith("Model choice: automatic.") for r in caplog.records)


@pytest.mark.asyncio
async def test_the_published_list_is_not_asked_for_at_startup_when_the_check_is_off(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MODEL_CATALOGUE_CHECK=false means no request at startup, and the log says what that leaves out."""
    world([PRIMARY, FALLBACK], HEALTHY)
    asked: list[str] = []
    monkeypatch.setenv("MODEL_PUBLISHED_URL", "https://raw.githubusercontent.com/a/b/tested-models/chain.json")
    extractors.reset_extractors()
    monkeypatch.setattr(model_catalogue, "_http_get", lambda url: asked.append(url) or (404, ""))

    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        async with app.router.lifespan_context(app):
            picture = model_chooser.picture()

    assert asked == [] and picture["published"]["state"] == "off"
    assert any("not read at startup: MODEL_CATALOGUE_CHECK is false" in r.getMessage() for r in caplog.records)


def test_a_cache_file_that_is_not_used_says_so_once(
    world: Callable[..., World], caplog: pytest.LogCaptureFixture
) -> None:
    """Four ids, no word of where the chain comes from, an origin that is no object, too large."""
    world([], {})
    origin = json.dumps(CHOSEN)
    for content in (
        '{"version": 1, "default": "%s", "chain": ["a/b:free", "c/d:free", "e/f:free", "g/h:free"], "origin": %s}'
        % (DEFAULT_MODEL_CHAIN, origin),
        '{"version": 1, "default": "%s", "chain": ["a/b:free"]}' % DEFAULT_MODEL_CHAIN,
        '{"version": 1, "default": "%s", "chain": ["a/b:free"], "origin": "me"}' % DEFAULT_MODEL_CHAIN,
        '{"version": 1, "default": "%s", "chain": ["a/b:free"], "origin": %s, "padding": "%s"}'
        % (DEFAULT_MODEL_CHAIN, origin, "x" * 260_000),
    ):
        model_cache.PATH.write_text(content, encoding="utf-8")
        model_cache.forget()
        extractors.reset_extractors()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app.model_cache"):
            assert get_settings().model_chain == [PRIMARY, FALLBACK], content[:90]
            assert model_chooser.picture()["origin"]["kind"] == "default"
        said = [r.getMessage() for r in caplog.records if "is there and was not used" in r.getMessage()]
        assert len(said) == 1 and "model_cache.json" in said[0]


def test_a_stand_in_lies_on_top_of_the_chosen_chain_and_is_never_saved(world: Callable[..., World]) -> None:
    world([], {})
    chosen = {**CHOSEN, "why": {"code": "gone", "models": [PRIMARY]}}
    model_cache.use([FALLBACK], chosen)
    standing = {**CHOSEN, "why": {"code": "first_answer", "models": [NEWCOMER]}}

    model_cache.use([FALLBACK, NEWCOMER], standing, save=False)
    assert model_cache.chain() == [FALLBACK, NEWCOMER] and model_cache.settled() == [FALLBACK]
    assert model_cache.origin()["why"]["code"] == "first_answer"
    model_cache.remember({"id": "a test", "finished": "2026-10-08T10:05:00Z"})  # this writes the file
    saved = json.loads(model_cache.PATH.read_text(encoding="utf-8"))
    assert saved["chain"] == [FALLBACK] and saved["origin"]["why"]["code"] == "gone"

    model_cache.stand_down()
    assert model_cache.chain() == [FALLBACK] and model_cache.origin() == chosen
    # A chain that is chosen ends a stand-in as well: what is chosen has been tested.
    model_cache.use([FALLBACK, NEWCOMER], standing, save=False)
    model_cache.use([FALLBACK, SLOW], chosen)
    assert model_cache.chain() == model_cache.settled() == [FALLBACK, SLOW]


# ---------------------------------------------------------------------------
# Model chooser - what a look may conclude
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_page_that_could_not_be_read_does_not_make_a_model_gone(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Only OpenRouter's own "Model not found" counts. An outage of its pages says nothing."""
    made = world([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: always("right")})

    def unknown(models: list[str], base_url: str) -> list[Listing]:
        made.lookups.append(list(models))
        return [Listing(model, "unknown", note="catalogue not reachable") for model in models]

    monkeypatch.setattr(model_catalogue, "check_chain", unknown)
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})

    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        assert (await http.post(API, json=DOCUMENT)).status_code == 200
        picture = await settled()

    assert made.lookups == [[PRIMARY]]
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["gone"] == []
    assert picture["look"]["result"] == "passing" and made.test_calls == []
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "could not be read, so nothing is concluded" in said and "still lists" not in said

    # The same when every model answers "not found" to the call: that is what a closed
    # chain looks like, and what two withdrawn models look like. Nobody knows which.
    later(monkeypatch)
    router(default=api_error(openai.NotFoundError, 404))
    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()
    assert picture["look"]["result"] == "passing" and made.test_calls == [] and made.list_reads == 0


@pytest.mark.asyncio
async def test_a_page_that_answers_too_late_is_no_error(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A slow answer is an ordinary thing. It must not read like a fault of the service."""
    world([PRIMARY, FALLBACK], HEALTHY)
    release = threading.Event()

    def late(models: list[str], base_url: str) -> list[Listing]:
        release.wait(3)
        return [Listing(model, "not_listed") for model in models]

    monkeypatch.setattr(model_catalogue, "check_chain", late)
    monkeypatch.setattr(model_chooser, "CATALOGUE_SECONDS", 0.05)
    try:
        with caplog.at_level(logging.INFO, logger="app.model_chooser"):
            model_chooser.note([model_chooser.Failure(PRIMARY, "ProviderRateLimitError", True)], answered=True)
            picture = await settled()
    finally:
        release.set()

    assert picture["look"]["result"] == "passing" and picture["chain"] == [PRIMARY, FALLBACK]
    assert picture["gone"] == []  # what came too late is not believed either
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]
    assert any("could not be read in time" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_outage_upstream_is_not_a_closed_chain(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """ "Closed" needs answers that waiting cannot change. A 503 from upstream is weather."""
    made = world([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: always("right")})
    router(default=api_error(openai.InternalServerError, 503))

    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()

    assert picture["look"]["result"] == "passing" and made.test_calls == [] and made.list_reads == 0


@pytest.mark.asyncio
async def test_a_document_every_model_rejects_is_not_a_closed_chain(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 is about the request that was sent (too long for the model, say), not about the account.

    Counted as "closed" it cost a test of the free models every quarter of an hour, to
    any caller who sent such a document, and with a full chain an alarm on the page.
    """
    made = world([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: always("right")})
    router(default=api_error(openai.BadRequestError, 400, "maximum context length is 8192 tokens"))

    for _ in range(3):
        later(monkeypatch)
        assert (await http.post(API, json=DOCUMENT)).status_code == 503
        assert (await settled())["look"]["result"] == "passing"
    assert made.test_calls == [] and made.list_reads == 0

    # A 403 is about the account, like a 404: this is what privacy settings look like.
    later(monkeypatch)
    router(default=api_error(openai.PermissionDeniedError, 403))
    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()
    assert picture["look"]["result"] == "started" and picture["last"]["reason"]["code"] == "closed"
    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER]


@pytest.mark.asyncio
async def test_a_full_chain_that_cannot_be_called_gets_no_test(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three listed models, all closed: nothing is dropped, nothing is tested, the admin is told."""
    made = world([PRIMARY, FALLBACK, SLOW, NEWCOMER], {NEWCOMER: always("right")})
    model_cache.use([PRIMARY, FALLBACK, SLOW], {**CHOSEN, "why": {"code": "test", "models": [SLOW]}})
    extractors.reset_extractors()
    router(default=api_error(openai.NotFoundError, 404))

    with caplog.at_level(logging.ERROR, logger="app.model_chooser"):
        assert (await http.post(API, json=DOCUMENT)).status_code == 503
        picture = await settled()

    assert picture["look"]["result"] == "no_place"
    assert picture["look"]["reason"] == {"code": "closed", "models": [PRIMARY, FALLBACK, SLOW]}
    assert picture["chain"] == [PRIMARY, FALLBACK, SLOW] and made.test_calls == [] and made.list_reads == 0
    assert any("no free place" in r.getMessage() and "OPENROUTER_MODELS" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_on_the_page_not_an_invoice_is_an_answer_and_a_refused_key_starts_nothing(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY, FALLBACK))

    router({PRIMARY: ok(EMPTY_INVOICE_PAYLOAD), FALLBACK: ok()})  # the first model says "not an invoice"
    assert (await http.post("/playground/extract", json=DOCUMENT)).status_code == 200
    await settled()
    assert made.lookups == [] and made.test_calls == []

    router(default=api_error(openai.AuthenticationError, 401))
    assert (await http.post("/playground/extract", json=DOCUMENT)).status_code == 200
    picture = await settled()
    assert made.lookups == [] and made.test_calls == [] and picture["look"] is None


@pytest.mark.asyncio
async def test_on_the_page_closed_models_get_a_spare_too(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """A page run that every model failed says so, and says what each model answered."""
    world([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: always("right")})
    router(default=api_error(openai.NotFoundError, 404))

    assert (await http.post("/playground/extract", json=DOCUMENT)).status_code == 200
    picture = await settled()

    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER] and picture["last"]["reason"]["code"] == "closed"


@pytest.mark.asyncio
async def test_the_picture_names_the_models_of_the_chain_that_are_gone_in_chain_order(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page's "No longer offered" chip and its "Models withdrawn" status rest on this list."""
    made = world([BROKEN], {BROKEN: always("format")}, gone=(PRIMARY, FALLBACK))
    plain = made.check_chain
    backwards = lambda models, base_url: list(reversed(plain(models, base_url)))  # noqa: E731
    monkeypatch.setattr(model_catalogue, "check_chain", backwards)
    router(default=api_error(openai.NotFoundError, 404))

    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["gone"] == [PRIMARY, FALLBACK]

    # A model that was taken out is no longer one of "the chain's models that are gone".
    made.not_found.discard(FALLBACK)
    model_chooser.reset()
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
    assert (await http.post(API, json=DOCUMENT)).status_code == 200
    picture = await settled()
    assert picture["chain"] == [FALLBACK] and picture["gone"] == []


@pytest.mark.asyncio
async def test_a_model_that_is_offered_again_is_no_longer_shown_as_withdrawn(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Withdrawn for a few days, listed again, tested, right seven times: its chip must go."""
    made = world([FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")}, gone=(PRIMARY,))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
    await http.post(API, json=DOCUMENT)
    assert (await settled())["chain"] == [FALLBACK, NEWCOMER]
    assert PRIMARY in model_chooser._gone

    made.not_found.clear()
    made.listed.insert(0, PRIMARY)
    later(monkeypatch)
    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    # Back in its measured place, and nothing on the page calls it withdrawn.
    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER] and picture["gone"] == []


@pytest.mark.asyncio
async def test_the_button_too_flags_a_model_openrouter_no_longer_offers(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model stays, because it answers, and the page says what OpenRouter says about it."""
    made = world([FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")}, gone=(PRIMARY,))

    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["chain"][0] == PRIMARY and picture["gone"] == [PRIMARY]

    # And no longer than OpenRouter says it: the next answer about the chain is believed too.
    made.not_found.clear()
    later(monkeypatch)
    assert (await http.post("/playground/models/test")).status_code == 202
    assert (await settled())["gone"] == []


@pytest.mark.asyncio
async def test_a_gone_model_that_answers_keeps_its_place_whatever_was_measured(
    http: httpx.AsyncClient, world: Callable[..., World]
) -> None:
    """Two sources must agree for any model of the chain, not only for those of the default."""
    world(
        [FALLBACK, NEWCOMER],
        {SLOW: always("right"), FALLBACK: always("right"), NEWCOMER: always("right")},
        gone=(SLOW,),
    )
    model_cache.use([SLOW, FALLBACK], {**CHOSEN, "why": {"code": "test", "models": [SLOW]}})
    extractors.reset_extractors()

    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["chain"] == [SLOW, FALLBACK, NEWCOMER] and picture["gone"] == [SLOW]
    assert rows_of(picture["last"])[SLOW]["placing"] == "kept"


# ---------------------------------------------------------------------------
# Model chooser - a repair that is still owed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gone_model_is_taken_out_and_saved_although_no_test_could_start(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The free part of a repair does not wait for the part that costs requests."""
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    made.list_fails = True
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})

    with caplog.at_level(logging.WARNING, logger="app.model_chooser"):
        assert (await http.post(API, json=DOCUMENT)).status_code == 200
        picture = await settled()

    assert picture["look"]["result"] == "list_unreadable" and made.test_calls == []
    wait = published_chain.moment(picture["look"]["again_at"]) - published_chain.moment(picture["now"])
    assert timedelta(seconds=50) < wait <= timedelta(seconds=60)
    assert picture["chain"] == [FALLBACK] and picture["origin"]["why"] == {"code": "gone", "models": [PRIMARY]}
    saved = json.loads(model_cache.PATH.read_text(encoding="utf-8"))
    assert saved["chain"] == [FALLBACK] and saved["origin"]["kind"] == "own"
    # In words, and no alarm: a model is left that answers.
    (line,) = [r for r in caplog.records if "No test of replacements can start now" in r.getMessage()]
    assert line.levelname == "WARNING" and "list of free models could not be read" in line.getMessage()


@pytest.mark.asyncio
async def test_a_repair_that_could_not_start_is_tried_again_after_the_next_failed_request(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The log says so, and the page tells the visitor so: it has to be true."""
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    made.list_fails = True  # OpenRouter's list cannot be read for a moment
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
    await http.post(API, json=DOCUMENT)
    picture = await settled()
    assert picture["chain"] == [FALLBACK] and picture["look"]["result"] == "list_unreadable"

    made.list_fails = False
    later(monkeypatch)
    router({FALLBACK: api_error(openai.RateLimitError, 429)})  # the next request that fails
    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()

    assert picture["chain"] == [FALLBACK, NEWCOMER] and picture["look"]["result"] == "started"
    assert picture["last"]["by"] == "failure"
    assert picture["last"]["reason"] == {"code": "gone", "models": [PRIMARY]}
    assert made.asked(FALLBACK) == 0  # still only the models outside the chain

    # The place is filled, so nothing is owed any more: the next failure is weather again.
    later(monkeypatch)
    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    assert (await settled())["look"]["result"] == "passing" and made.asked(NEWCOMER) == 7


@pytest.mark.asyncio
async def test_a_repair_in_which_nobody_could_be_judged_is_tried_again(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free models are rate limited in bursts: "nobody could be judged just now" is not the last word."""
    made = world([FALLBACK, NEWCOMER], {NEWCOMER: always("limited")}, gone=(PRIMARY,))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
    await http.post(API, json=DOCUMENT)
    picture = await settled()
    assert picture["last"]["outcome"] == "nothing_passed" and picture["chain"] == [FALLBACK]

    ScriptedEngine.SCRIPT[NEWCOMER] = always("right")  # the burst is over
    later(monkeypatch)
    router({FALLBACK: api_error(openai.RateLimitError, 429)})
    await http.post(API, json=DOCUMENT)
    picture = await settled()

    assert picture["chain"] == [FALLBACK, NEWCOMER] and made.asked(NEWCOMER) == 2 + 7


@pytest.mark.asyncio
async def test_a_repair_cut_off_by_a_restart_is_taken_up_again(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """On a machine that keeps its disk the shortened chain is what a restart finds."""
    world([FALLBACK, NEWCOMER, SLOW], {NEWCOMER: always("right"), SLOW: always("right")}, gone=(PRIMARY,))
    ScriptedEngine.GATE[SLOW] = asyncio.Event()  # never set: the test is still running at shutdown
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
    async with app.router.lifespan_context(app):
        await http.post(API, json=DOCUMENT)
        for _ in range(400):
            await asyncio.sleep(0.005)
            if (model_chooser.picture()["test"] or {}).get("provisional"):
                break
        assert model_chooser.picture()["chain"] == [FALLBACK, NEWCOMER]  # a stand-in answers
    # What the disk holds is the chain without the withdrawn model, and no stand-in.
    assert json.loads(model_cache.PATH.read_text(encoding="utf-8"))["chain"] == [FALLBACK]

    extractors.reset_extractors(), model_cache.forget(), model_chooser.reset(), budget.reset()  # a new process
    ScriptedEngine.GATE.clear()
    router({FALLBACK: api_error(openai.RateLimitError, 429)})
    async with app.router.lifespan_context(app):
        assert get_dispatcher().models == [FALLBACK]
        await http.post(API, json=DOCUMENT)
        picture = await settled()
    assert picture["chain"] == [FALLBACK, NEWCOMER, SLOW]


@pytest.mark.asyncio
async def test_the_quarter_of_an_hour_holds_the_services_own_repair_as_well(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """ "Whoever starts them". Without it a caller could have a test run after every failed request."""
    made = world([FALLBACK, BROKEN], {BROKEN: always("format"), NEWCOMER: always("right")}, gone=(PRIMARY,))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
    assert (await http.post(API, json=DOCUMENT)).status_code == 200
    picture = await settled()
    assert picture["chain"] == [FALLBACK] and picture["last"]["outcome"] == "nothing_passed"

    # A minute later the last model is withdrawn as well, and a good one has appeared.
    made.listed.append(NEWCOMER)
    made.not_found.add(FALLBACK)
    monkeypatch.setattr(model_chooser, "_looked_at", -10_000.0)
    router(default=api_error(openai.NotFoundError, 404))
    with caplog.at_level(logging.WARNING, logger="app.model_chooser"):
        assert (await http.post(API, json=DOCUMENT)).status_code == 503
        picture = await settled()

    assert picture["look"]["result"] == "pause" and made.asked(NEWCOMER) == 0
    wait = published_chain.moment(picture["look"]["again_at"]) - published_chain.moment(picture["now"])
    assert timedelta(minutes=14) < wait <= timedelta(minutes=15)
    assert model_chooser.PAUSE_SECONDS == 15 * 60 == picture["rules"]["pause_minutes"] * 60
    # Nothing answers until then, and the page sends visitors to the admin: the log
    # is where the admin reads what can be done.
    (alarm,) = [r for r in caplog.records if r.levelname == "ERROR" and r.name == CHOOSER]
    assert "the last test ended less than a quarter of an hour ago" in alarm.getMessage()
    assert "OPENROUTER_MODELS" in alarm.getMessage() and "restart" in alarm.getMessage()


@pytest.mark.asyncio
async def test_the_log_tells_the_admin_what_to_do_when_no_model_is_left(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """In these two states the page says "Please contact the admin", so the log has to say what for."""
    made = world([BROKEN], {BROKEN: always("format")}, gone=(PRIMARY, FALLBACK))
    router(default=api_error(openai.NotFoundError, 404))

    # Both models are gone and the one other model fails the test.
    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        assert (await http.post(API, json=DOCUMENT)).status_code == 503
        picture = await settled()
    assert picture["last"]["outcome"] == "nothing_passed"
    (alarm,) = [r.getMessage() for r in caplog.records if r.levelname == "ERROR" and r.name == CHOOSER]
    assert "found no free model that passes" in alarm and "HTTP 503" in alarm
    assert "name them in OPENROUTER_MODELS and restart" in alarm

    # Later nothing is on offer that has not failed here already.
    caplog.clear()
    later(monkeypatch)
    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        assert (await http.post(API, json=DOCUMENT)).status_code == 503
        picture = await settled()
    assert picture["look"]["result"] == "nothing_to_test" and made.asked(BROKEN) == 2
    (alarm,) = [r.getMessage() for r in caplog.records if r.levelname == "ERROR" and r.name == CHOOSER]
    assert "OpenRouter no longer offers any model of the chain" in alarm
    assert "lists no free model besides" in alarm and "name them in OPENROUTER_MODELS and restart" in alarm


# ---------------------------------------------------------------------------
# Model chooser - stand-ins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marks", ["+B++E++", "+++++B+"])
@pytest.mark.asyncio
async def test_a_stand_in_the_test_could_not_judge_goes_on_answering(
    marks: str, http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's own case, as two of the saved passes of 7 October went.

    Both default models are withdrawn. The one model that can do the job loses a call
    or two to the moment, and every answer it gives is right. "Not judged" is neither
    added nor dropped: it must not mean "back to two withdrawn models".
    """
    made = world([SLOW, BROKEN], {SLOW: as_recorded(marks), BROKEN: always("format")}, gone=(PRIMARY, FALLBACK))
    router({SLOW: ok()}, default=api_error(openai.NotFoundError, 404))

    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()

    row = rows_of(picture["last"])[SLOW]
    assert (row["verdict"], row["why"]) == ("not_judged", "unanswered") and row["correct"] == row["answered"] > 0
    assert picture["last"]["outcome"] == "nothing_passed" and picture["last"]["chain"] == [PRIMARY, FALLBACK]
    # It goes on answering ...
    assert picture["chain"] == [SLOW] == get_dispatcher().models and picture["test"] is None
    why = picture["origin"]["why"]
    assert (picture["origin"]["kind"], why["code"], why["models"]) == ("own", "first_answer", [SLOW])
    assert why["reason"] == {"code": "gone", "models": [PRIMARY, FALLBACK]}
    after = await http.post(API, json=DOCUMENT)
    assert after.status_code == 200 and after.json()["model_used"] == SLOW
    # ... and nothing of it is on the disk: a restart finds the chain the service chose.
    assert model_cache.settled() == []
    assert json.loads(model_cache.PATH.read_text(encoding="utf-8"))["chain"] == []

    # The repair is still owed. The next request that fails, a quarter of an hour
    # later, has the stand-in judged, and this time no call is lost.
    ScriptedEngine.SCRIPT[SLOW] = always("right")
    later(monkeypatch)
    router(default=api_error(openai.RateLimitError, 429))
    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()

    assert picture["look"]["result"] == "started"
    assert picture["last"]["reason"] == {"code": "gone", "models": [PRIMARY, FALLBACK]}
    assert picture["last"]["tested"] == [SLOW]  # the one that failed the first test is not asked again
    assert picture["chain"] == [SLOW] and picture["origin"]["why"] == {"code": "test", "models": [SLOW]}
    assert json.loads(model_cache.PATH.read_text(encoding="utf-8"))["chain"] == [SLOW]
    assert made.asked(PRIMARY) == made.asked(FALLBACK) == 0


@pytest.mark.asyncio
async def test_the_first_answer_decides_about_a_stand_in_whichever_document_it_was(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call lost on the first document must not keep a working model out until the test is over."""
    world(
        [SLOW, NEWCOMER],
        {SLOW: as_recorded("E++++++"), NEWCOMER: as_recorded("x++++++")},
        gone=(PRIMARY, FALLBACK),
    )
    router(default=api_error(openai.NotFoundError, 404))
    seen = watch_stand_ins(monkeypatch)

    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()

    assert seen[0]["standing"] == [SLOW]  # after its second call, the first one it answered
    # The other model's first answer was wrong. Six right answers after it do not make
    # a stand-in: it waits for the test, which lets it in with one document missed.
    assert not any(NEWCOMER in change["standing"] for change in seen)
    verdicts = {model: (row["verdict"], row["why"]) for model, row in rows_of(picture["last"]).items()}
    assert verdicts == {SLOW: ("passed", "one_missed"), NEWCOMER: ("passed", "one_missed")}
    assert picture["chain"] == [SLOW, NEWCOMER]  # fewest wrong answers first


@pytest.mark.asyncio
async def test_the_buttons_test_judges_a_stand_in_too_and_one_that_fails_stops_at_once(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gone = {PRIMARY: always("gone"), FALLBACK: always("gone")}
    world([SLOW], {**gone, SLOW: as_recorded("+B++E++")}, gone=(PRIMARY, FALLBACK))
    router(default=api_error(openai.NotFoundError, 404))
    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    assert (await settled())["chain"] == [SLOW]  # left answering by the repair

    ScriptedEngine.SCRIPT[SLOW] = wrong_on("ocr-receipt", "eur-invoice")
    later(monkeypatch)
    seen = watch_stand_ins(monkeypatch)
    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["last"]["by"] == "page" and picture["last"]["tested"] == [PRIMARY, FALLBACK, SLOW]
    assert rows_of(picture["last"])[SLOW]["verdict"] == "failed"
    # It stopped answering when its verdict fell, and the button put nobody else to use.
    assert [change["standing"] for change in seen] == [[SLOW], [], []]
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["origin"]["kind"] == "default"


@pytest.mark.asyncio
async def test_stand_ins_take_the_free_places_and_no_more(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three models read their first document correctly, and two places are free."""
    script = {NEWCOMER: always("right"), SLOW: always("right"), THIRD: always("right")}
    world([FALLBACK, NEWCOMER, SLOW, THIRD], script, gone=(PRIMARY,))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
    seen = watch_stand_ins(monkeypatch)

    assert (await http.post(API, json=DOCUMENT)).status_code == 200
    picture = await settled()

    standing = [change for change in seen if change["standing"]]
    assert [change["chain"] for change in standing] == [[FALLBACK, NEWCOMER], [FALLBACK, NEWCOMER, SLOW]]
    assert picture["last"]["outcome"] == "changed" and picture["chain"] == [FALLBACK, NEWCOMER, SLOW]


@pytest.mark.asyncio
async def test_the_buttons_test_puts_no_stand_in_to_use(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first right answer is a repair's business. A test nobody needed changes nothing until it ends."""
    world([PRIMARY, FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")})
    ScriptedEngine.GATE[PRIMARY] = under_way = asyncio.Event()
    seen = watch_stand_ins(monkeypatch)

    assert (await http.post("/playground/models/test")).status_code == 202
    for _ in range(400):  # until the newcomer has answered all seven, with the test still running
        if model_chooser.picture()["test"] and len(model_chooser.picture()["test"]["calls"]) >= 14:
            break
        await asyncio.sleep(0.005)
    running = model_chooser.picture()
    assert running["test"]["provisional"] == [] and running["chain"] == [PRIMARY, FALLBACK]
    assert running["origin"]["kind"] == "default" and running["doing"] == "testing"
    under_way.set()
    picture = await settled()

    assert picture["chain"] == [PRIMARY, FALLBACK, NEWCOMER] and picture["doing"] is None
    assert not any(change["standing"] for change in seen)


@pytest.mark.asyncio
async def test_a_stand_in_that_fails_gives_back_the_chain_the_service_had_chosen(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Exactly as it was" also when what was there was not the default."""
    world([FALLBACK, NEWCOMER], {NEWCOMER: wrong_on("ocr-receipt", "eur-invoice")}, gone=(PRIMARY,))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: api_error(openai.RateLimitError, 429)})
    seen = watch_stand_ins(monkeypatch)

    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()

    assert [change["chain"] for change in seen if change["standing"]] == [[FALLBACK, NEWCOMER]]
    assert picture["last"]["outcome"] == "nothing_passed" and picture["chain"] == [FALLBACK]
    assert picture["origin"]["kind"] == "own" and picture["origin"]["why"] == {"code": "gone", "models": [PRIMARY]}


@pytest.mark.asyncio
async def test_a_test_cut_off_by_shutdown_is_not_a_result(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """Stopping cancels at once, and what a half-run test saw changes and saves nothing."""
    world([NEWCOMER, SLOW], {NEWCOMER: always("right"), SLOW: always("right")}, gone=(PRIMARY, FALLBACK))
    ScriptedEngine.GATE[SLOW] = asyncio.Event()  # still on its first document when the service stops
    router(default=api_error(openai.NotFoundError, 404))

    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    for _ in range(400):
        await asyncio.sleep(0.005)
        if model_chooser.picture()["test"] and len(model_chooser.picture()["test"]["calls"]) == 7:
            break
    assert len(model_chooser.picture()["test"]["calls"]) == 7  # the quick model is through; it "passed"

    await asyncio.wait_for(model_chooser.stop(), timeout=2)
    await asyncio.sleep(0)

    assert not model_chooser._tasks and model_chooser.picture()["test"] is None
    assert not model_cache.PATH.exists() and model_cache.last() is None


@pytest.mark.asyncio
async def test_the_service_stops_its_background_work_when_it_shuts_down(
    http: httpx.AsyncClient, world: Callable[..., World]
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    ScriptedEngine.GATE[PRIMARY] = asyncio.Event()  # never set

    async with app.router.lifespan_context(app):
        assert (await http.post("/playground/models/test")).status_code == 202
        work = list(model_chooser._tasks)
        assert work

    assert all(task.done() for task in work)


# ---------------------------------------------------------------------------
# Model chooser - what a test costs, and what it survives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_test_takes_six_models_and_the_others_go_first_next_time(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "42 requests at the very most" rests on the limit, and nobody waits for ever."""
    others = [f"vendor/number-{n}:free" for n in range(1, 8)]
    world([PRIMARY, FALLBACK] + others, {**HEALTHY, **{model: always("limited") for model in others}})

    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()
    first = picture["last"]
    assert first["tested"] == [PRIMARY, FALLBACK] + others[:4] and first["left_out"] == others[4:]
    assert first["requests"] <= picture["rules"]["requests_at_most"] == 42

    later(monkeypatch)
    assert (await http.post("/playground/models/test")).status_code == 202
    second = (await settled())["last"]
    assert second["tested"] == [PRIMARY, FALLBACK] + others[4:] + others[:1]


@pytest.mark.parametrize(("by", "burst"), [("page", 1), ("failure", 3)])
@pytest.mark.asyncio
async def test_a_test_is_paced_and_only_a_repair_opens_with_a_burst(
    by: str, burst: int, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the runner is told. The scripted world sets the spacing to zero, so nothing else sees it."""
    world([PRIMARY, FALLBACK, NEWCOMER], HEALTHY)
    monkeypatch.setattr(model_rules, "SECONDS_BETWEEN_STARTS", 6.0)
    told: dict[str, Any] = {}

    async def runner(models: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        told.update(kwargs, models=list(models))
        return []

    monkeypatch.setattr(benchmark, "run", runner)
    tested = [PRIMARY, FALLBACK, NEWCOMER]
    chain = [PRIMARY, FALLBACK]
    record = model_testing.new_record(
        by, {"code": "asked", "models": []}, chain, chain, [], tested, tested, [], get_settings()
    )

    await model_testing.run_test(record, get_settings(), (lambda model: None) if by == "failure" else None)

    assert told["models"] == tested and told["passes"] == 1 and told["out"] is None
    assert told["seconds_between_starts"] == 6.0  # ten a minute
    assert told["give_up_after"] == 2 and told["burst"] == burst


@pytest.mark.asyncio
async def test_only_a_model_that_failed_is_barred_from_the_next_repair_and_only_until_it_passes(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate limit says something about the moment, so the model is asked again next time."""
    script = {
        PRIMARY: always("gone"), FALLBACK: always("gone"), NEWCOMER: always("limited"), BROKEN: always("format"),
    }
    made = world([NEWCOMER, BROKEN], script, gone=(PRIMARY, FALLBACK))
    router(default=api_error(openai.NotFoundError, 404))

    async def a_failed_request_after_the_pause() -> None:
        later(monkeypatch)
        assert (await http.post(API, json=DOCUMENT)).status_code == 503
        await settled()

    await a_failed_request_after_the_pause()
    assert (made.asked(NEWCOMER), made.asked(BROKEN)) == (2, 2)
    await a_failed_request_after_the_pause()
    assert (made.asked(NEWCOMER), made.asked(BROKEN)) == (4, 2)  # the one that failed is left out

    # The button tests everything. A model that passes there is a candidate for repairs again.
    ScriptedEngine.SCRIPT[BROKEN] = always("right")
    later(monkeypatch)
    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()
    assert picture["chain"] == [BROKEN] and BROKEN not in model_chooser._failed_here


@pytest.mark.asyncio
async def test_a_press_that_was_refused_is_answered_from_memory_until_it_can_change(
    http: httpx.AsyncClient, world: Callable[..., World], allowance: Callable[..., list[None]],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Any script can press the button. A press must not cost OpenRouter a read each time."""
    made = world([PRIMARY, FALLBACK, NEWCOMER], HEALTHY)
    readings = allowance(remaining=500, limit=1000)

    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        answers = [await http.post("/playground/models/test") for _ in range(20)]

    assert {(a.status_code, a.json()["detail"]["reason"]) for a in answers} == {(409, "allowance")}
    assert made.list_reads == 1 and len(made.lookups) == 1 and len(readings) == 1
    button = model_chooser.picture()["button"]
    assert (button["ready"], button["reason"]) == (False, "allowance") and button["again_at"].endswith("T00:00:00Z")
    # One line in the log for the twenty presses, in words.
    (line,) = [r for r in caplog.records if "A visitor asked for a test" in r.getMessage()]
    assert line.levelname == "INFO" and "too little of today's free allowance is left" in line.getMessage()

    # The time it named is over: the button is ready again without another press ...
    monkeypatch.setitem(model_chooser._refusal, "again_at", "2026-01-01T00:00:00Z")
    assert model_chooser.picture()["button"] == {"offered": True, "ready": True, "reason": None, "again_at": None}
    # ... and the next press asks again. A test that starts ends the memory of a refusal.
    allowance(remaining=1000, limit=1000)
    budget.reset()  # and OpenRouter is asked for the day's count again
    assert (await http.post("/playground/models/test")).status_code == 202
    assert model_chooser._refusal is None and made.list_reads == 2
    await settled()


@pytest.mark.asyncio
async def test_the_buttons_question_about_the_chain_has_a_time_limit_like_every_other_read(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    release = threading.Event()
    monkeypatch.setattr(model_chooser, "CATALOGUE_SECONDS", 0.1)
    monkeypatch.setattr(model_catalogue, "check_chain", lambda models, base_url: release.wait(3) and [])

    try:
        started = model_chooser.start("page", {"code": "asked", "models": []})
        code, _ = await asyncio.wait_for(started, timeout=1.5)
    finally:
        release.set()

    assert code == "started"  # nothing is known to be gone, and the test says the rest
    assert (await settled())["chain"] == [PRIMARY, FALLBACK]


@pytest.mark.asyncio
async def test_an_answer_the_engine_cannot_read_fails_that_model_and_not_the_test(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real engine raises AttributeError when a model sends its answer as a list of parts."""

    class Mixed(ScriptedEngine):
        """The one odd model goes through the real engine, the other through the script."""

        async def extract(self, text: str) -> InvoiceData:
            if self.model == BROKEN:
                return await extractors.OpenRouterExtractor(self.model, self.settings).extract(text)
            return await super().extract(text)

    world([NEWCOMER, BROKEN], {NEWCOMER: always("right")}, gone=(PRIMARY, FALLBACK))
    monkeypatch.setattr(benchmark, "OpenRouterExtractor", Mixed)
    parts = {"content": [{"type": "text", "text": "{}"}]}
    fake = router({BROKEN: parts}, default=api_error(openai.NotFoundError, 404))

    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()

    assert picture["last"]["outcome"] == "changed" and picture["chain"] == [NEWCOMER]
    verdicts = {model: (row["verdict"], row["why"]) for model, row in rows_of(picture["last"]).items()}
    assert verdicts == {NEWCOMER: ("passed", "all_right"), BROKEN: ("failed", "unusable_answers")}
    assert fake.models_called.count(BROKEN) == 2  # two calls end its test, as for any model


@pytest.mark.asyncio
async def test_when_a_test_breaks_nothing_of_it_goes_on_running(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One model's part of the test breaks: the others must not go on calling, or put a stand-in to use."""
    script = {NEWCOMER: always("right"), BROKEN: always("format")}
    made = world([NEWCOMER, BROKEN], script, gone=(PRIMARY, FALLBACK))
    ScriptedEngine.DELAY[NEWCOMER] = 0.05
    router(default=api_error(openai.NotFoundError, 404))
    plain = model_rules.judge
    broke: list[str] = []

    def breaks_once(tested: Any, calls: Any, *args: Any, **kwargs: Any) -> Any:
        # Once, and in one model's part only: the other model's part is in good order.
        if not broke and any(call["model"] == BROKEN for call in calls):
            broke.append("the first answer of the one model")
            raise RuntimeError("something nobody expected")
        return plain(tested, calls, *args, **kwargs)

    monkeypatch.setattr(model_rules, "judge", breaks_once)
    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()
    made_so_far = len(made.test_calls)
    await asyncio.sleep(0.3)
    afterwards = model_chooser.picture()

    assert broke and picture["last"]["outcome"] == "interrupted" and afterwards["test"] is None
    assert afterwards["chain"] == [PRIMARY, FALLBACK] and afterwards["origin"]["kind"] == "default"
    assert len(made.test_calls) == made_so_far  # no call after the test was over
    assert not [task for task in asyncio.all_tasks() if "lane" in repr(task.get_coro())]


@pytest.mark.asyncio
async def test_a_test_whose_result_cannot_be_put_to_use_still_ends(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whatever breaks after the last call, "a test is running" must not stay true until a restart."""
    world([PRIMARY, FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: always("right")})
    real = model_cache.remember

    def once(record: dict[str, Any]) -> None:
        monkeypatch.setattr(model_cache, "remember", real)
        raise RuntimeError("one unexpected error")

    monkeypatch.setattr(model_cache, "remember", once)
    with caplog.at_level(logging.ERROR, logger="app.model_chooser"):
        assert (await http.post("/playground/models/test")).status_code == 202
        picture = await settled()

    assert picture["busy"] is False and picture["test"] is None
    assert picture["button"]["reason"] == "pause"
    assert any("its result could not be put to use" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_tests_log_names_the_kind_of_error_and_never_the_engines_advice(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The engine's sentence is written for a model an admin chose. A candidate is none:

    "set OPENROUTER_STRICT_JSON_SCHEMA=false", followed for a model that is being ruled
    out, would switch the whole service to another mode and cut it off from the
    published list.
    """
    world([BROKEN], {}, gone=(PRIMARY, FALLBACK))
    monkeypatch.setattr(benchmark, "OpenRouterExtractor", extractors.OpenRouterExtractor)  # the real engine
    body = {"error": {"message": "response_format json_schema is not supported"}}
    rejects = api_error(openai.BadRequestError, 400, "response_format is not supported", body=body)
    router({BROKEN: rejects}, default=api_error(openai.NotFoundError, 404))

    with caplog.at_level(logging.INFO):
        assert (await http.post(API, json=DOCUMENT)).status_code == 503
        await settled()

    said = [r.getMessage() for r in caplog.records if r.name == "app.model_testing"]
    assert len(said) == 2 and all(line.endswith(" s (SchemaValidationError)") for line in said)
    for field in Settings.model_fields:
        assert not any(field.upper() in line for line in said), field


@pytest.mark.asyncio
async def test_a_visitors_test_is_no_warning_and_the_picture_says_what_the_service_is_doing(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    made = world([PRIMARY, FALLBACK], HEALTHY)
    assert model_chooser.picture()["doing"] is None
    release = threading.Event()
    plain = made.free_models

    def slow_list(base_url: str) -> list[str]:
        release.wait(5)
        return plain(base_url)

    monkeypatch.setattr(model_catalogue, "free_models", slow_list)
    with caplog.at_level(logging.INFO, logger="app.model_chooser"):
        pressing = asyncio.create_task(http.post("/playground/models/test"))
        for _ in range(300):  # until the press is waiting for OpenRouter's list
            if model_chooser.picture()["busy"]:
                break
            await asyncio.sleep(0.01)
        asking = model_chooser.picture()
        release.set()
        assert (await pressing).status_code == 202
        await settled()

    # "running" for every tab, and which of the three it is for a page that wants to say so.
    assert (asking["doing"], asking["button"]["reason"]) == ("starting", "running")
    assert asking["doing"] in model_chooser.DOING and "testing" in model_chooser.DOING
    started = [r for r in caplog.records if "started (page: asked)" in r.getMessage()]
    assert [r.levelname for r in started] == ["INFO"]


# ---------------------------------------------------------------------------
# Model chooser - what a restart finds, and which test the page is shown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_finds_the_chain_where_it_comes_from_and_the_last_test(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World]
) -> None:
    """The file is how a choice survives a restart on a machine that keeps its disk."""
    world([FALLBACK, NEWCOMER], {NEWCOMER: always("right")}, gone=(PRIMARY,))
    router({PRIMARY: api_error(openai.NotFoundError, 404), FALLBACK: ok()})
    await http.post(API, json=DOCUMENT)
    before = await settled()

    model_cache.forget()  # a new process: only the file is left
    model_chooser.reset()
    extractors.reset_extractors()
    after = model_chooser.picture()

    assert after["chain"] == before["chain"] == [FALLBACK, NEWCOMER]
    assert after["origin"]["kind"] == "own" and after["origin"]["why"] == {"code": "test", "models": [NEWCOMER]}
    assert after["origin"]["tested"] == before["origin"]["tested"]
    assert after["last"]["id"] == before["last"]["id"] and after["last"]["by"] == "failure"
    # The test's record is in the file as it ended, for the page of the next process.
    saved = json.loads(model_cache.PATH.read_text(encoding="utf-8"))
    assert saved["last"]["outcome"] == "changed" and saved["last"]["state"] == "done"


def test_the_newer_of_the_two_real_tests_is_the_one_shown(
    world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """This service's own test and the scheduled one, whichever ended later."""
    world([], {})
    own = {"id": "own", "by": "failure", "finished": "2026-10-09T10:00:00Z"}
    scheduled = {"id": "scheduled", "by": "schedule", "finished": "2026-10-08T06:20:00Z"}
    model_cache.remember(own)
    monkeypatch.setattr(model_chooser, "_published_record", scheduled)
    assert model_chooser.picture()["last"]["id"] == "own"
    monkeypatch.setattr(model_chooser, "_published_record", {**scheduled, "finished": "2026-10-10T06:20:00Z"})
    assert model_chooser.picture()["last"]["id"] == "scheduled"


# ---------------------------------------------------------------------------
# Model chooser - a test that judged nobody
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stand_in_leaves_when_the_test_that_put_it_to_use_judged_nobody(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is refused in the middle of a repair. The test stops, nobody is judged,
    and a model that had just joined on one right answer does not stay on that alone."""
    world([NEWCOMER, SLOW], {NEWCOMER: always("right"), SLOW: always("key")}, gone=(PRIMARY, FALLBACK))
    ScriptedEngine.DELAY[SLOW] = 0.05
    router(default=api_error(openai.NotFoundError, 404))
    seen = watch_stand_ins(monkeypatch)

    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    picture = await settled()

    assert picture["last"]["outcome"] == "key_refused"
    assert [change["standing"] for change in seen] == [[NEWCOMER], []]
    assert picture["chain"] == [PRIMARY, FALLBACK] and picture["origin"]["kind"] == "default"


@pytest.mark.asyncio
async def test_a_test_that_breaks_leaves_a_stand_in_that_was_answering_before_it(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], world: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "The chain stays as it is" is also true of a model that an earlier repair left answering."""
    world([SLOW], {SLOW: as_recorded("+B++E++")}, gone=(PRIMARY, FALLBACK))
    router({SLOW: ok()}, default=api_error(openai.NotFoundError, 404))
    assert (await http.post(API, json=DOCUMENT)).status_code == 503
    assert (await settled())["chain"] == [SLOW]

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the runner broke")

    monkeypatch.setattr(model_testing, "run_test", broken)
    later(monkeypatch)
    assert (await http.post("/playground/models/test")).status_code == 202
    picture = await settled()

    assert picture["last"]["outcome"] == "interrupted" and picture["chain"] == [SLOW]
    assert (await http.post(API, json=DOCUMENT)).json()["model_used"] == SLOW


@pytest.mark.asyncio
async def test_openrouters_list_gets_five_seconds_and_no_more(
    http: httpx.AsyncClient, world: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Somebody is waiting then: for a repair, or with a finger on the button."""
    made = world([PRIMARY, FALLBACK], HEALTHY)
    assert model_chooser.CATALOGUE_SECONDS == 5.0
    release = threading.Event()
    monkeypatch.setattr(model_chooser, "CATALOGUE_SECONDS", 0.05)
    monkeypatch.setattr(model_catalogue, "free_models", lambda base_url: release.wait(3) and [])

    try:
        pressed = await asyncio.wait_for(http.post("/playground/models/test"), timeout=1.5)
    finally:
        release.set()

    assert pressed.status_code == 409 and pressed.json()["detail"]["reason"] == "list_unreadable"
    assert made.test_calls == [] and model_chooser.picture()["busy"] is False

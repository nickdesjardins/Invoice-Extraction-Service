"""Tests for the benchmark command (``app.playground.benchmark``).

Nothing here calls a model. The grading and the tables are pure functions over
records, and the runner takes the engine it should use as an argument, so the
tests hand it one that answers from a script.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytest

from app.config import Settings, get_settings
from app.extractors import (
    BaseExtractor,
    ExtractionError,
    ProviderRateLimitError,
    UnparseableTextError,
)
from app.playground import benchmark
from app.playground.benchmark import (
    case_grid,
    check_passes,
    expects_refusal,
    grade,
    live_cases,
    load_records,
    print_report,
    summarise,
)
from app.schemas import InvoiceData
from tests.test_extract import VALID_INVOICE_PAYLOAD

EMAIL_ANSWER: dict[str, Any] = {
    **VALID_INVOICE_PAYLOAD,
    "vendor_name": "NORTHWIND TRADERS INC.",
}


@pytest.fixture(autouse=True)
def records_go_to_a_temporary_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test out of the real ``benchmarks/`` folder, which is committed.

    The tests that expect a refusal write nothing while the guard holds. When a
    guard is broken, which is what those tests are for, the command would create a
    file there before the test fails.
    """
    monkeypatch.setattr(benchmark, "RESULTS_DIR", tmp_path)


def case(case_id: str) -> dict[str, Any]:
    return next(item for item in live_cases() if item["id"] == case_id)


def call(
    model: str, case_id: str, outcome: str, correct: Optional[bool], seconds: float, **extra: Any
) -> dict[str, Any]:
    return {
        "type": "call",
        "model": model,
        "case": case_id,
        "outcome": outcome,
        "correct": correct,
        "seconds": seconds,
        "error_type": None,
        "message": None,
        "checks": [],
        **extra,
    }


# ---------------------------------------------------------------------------
# Benchmark - cases and grading
# ---------------------------------------------------------------------------


def test_the_live_cases_are_the_ones_that_call_a_real_model() -> None:
    cases = live_cases()

    assert [item["id"] for item in cases] == [
        "email-invoice",
        "ocr-receipt",
        "eur-invoice",
        "service-invoice",
        "discount-shipping",
        "prompt-injection",
        "not-an-invoice",
    ]
    assert not any(fault for item in cases for fault in item["simulate"])
    assert [item["id"] for item in cases if expects_refusal(item)] == ["not-an-invoice"]


def test_every_check_of_every_document_case_can_be_graded() -> None:
    """The page grades in JavaScript and this command in Python: keep the two in step."""
    for item in live_cases():
        if expects_refusal(item):
            continue
        for check in item["checks"]:
            if check["kind"] != "status":
                check_passes(check, {"data": EMAIL_ANSWER})  # must not raise


def test_an_unknown_check_kind_is_an_error_not_a_silent_pass() -> None:
    with pytest.raises(ValueError, match="cannot grade"):
        check_passes({"kind": "calls", "label": "x", "expected": 1}, {})


def test_a_fully_correct_answer() -> None:
    correct, checks = grade(case("email-invoice"), "answered", EMAIL_ANSWER)

    assert correct is True
    assert checks and all(check["passed"] for check in checks)
    # Every answer passes "Returns HTTP 200", so scoring it would only pad the total.
    assert not [check for check in checks if "HTTP" in check["label"]]


def test_one_wrong_field_makes_the_answer_wrong() -> None:
    correct, checks = grade(
        case("email-invoice"), "answered", {**EMAIL_ANSWER, "tax_amount": None}
    )

    assert correct is False
    assert [check["label"] for check in checks if not check["passed"]] == ["Tax is 28.00"]


INJECTION_ANSWER: dict[str, Any] = {
    **VALID_INVOICE_PAYLOAD,
    "vendor_name": "Vector Supply Co",
    "invoice_number": "VS-9001",
    "total_amount": 1000.0,
}


@pytest.mark.parametrize(
    ("case_id", "answer", "missed"),
    [
        (
            "email-invoice",
            {**EMAIL_ANSWER, "invoice_number": "NW-2024-0188"},
            "Invoice number is NW-2024-0187",
        ),
        ("email-invoice", {**EMAIL_ANSWER, "currency": "EUR"}, "Currency is USD"),
        (
            "email-invoice",
            {**EMAIL_ANSWER, "items": [*EMAIL_ANSWER["items"], EMAIL_ANSWER["items"][0]]},
            "Two line items",
        ),
        (
            "prompt-injection",
            {**INJECTION_ANSWER, "vendor_name": "HACKED"},
            "Vendor is still Vector Supply, not HACKED",
        ),
    ],
    ids=["text-field", "currency", "line-count", "vendor-contains"],
)
def test_every_kind_of_check_can_fail(case_id: str, answer: dict[str, Any], missed: str) -> None:
    """ "Fully correct" orders the chain, so a wrong text, count or vendor must cost it."""
    right = EMAIL_ANSWER if case_id == "email-invoice" else INJECTION_ANSWER
    assert grade(case(case_id), "answered", right)[0] is True  # the starting point is correct

    correct, checks = grade(case(case_id), "answered", answer)

    assert correct is False
    assert [check["label"] for check in checks if not check["passed"]] == [missed]


def test_amounts_are_compared_to_the_cent() -> None:
    check = {"kind": "field", "label": "total", "path": "data.total_amount", "expected": 377.98}

    assert check_passes(check, {"data": {"total_amount": 377.9800000001}})
    assert not check_passes(check, {"data": {"total_amount": 377.99}})
    assert not check_passes(check, {"data": {"total_amount": "377.98"}})


@pytest.mark.parametrize(
    ("case_id", "outcome", "expected"),
    [
        ("not-an-invoice", "refused", True),
        ("not-an-invoice", "answered", False),  # it invented an invoice
        ("email-invoice", "refused", False),  # it missed a real one
    ],
)
def test_refusals_are_graded_against_what_the_case_expects(
    case_id: str, outcome: str, expected: bool
) -> None:
    correct, checks = grade(case(case_id), outcome, EMAIL_ANSWER if outcome == "answered" else None)

    assert correct is expected
    assert len(checks) == 1 and checks[0]["passed"] is expected


def test_a_call_with_no_answer_is_not_graded() -> None:
    assert grade(case("email-invoice"), "no_answer", None) == (None, [])


# ---------------------------------------------------------------------------
# Benchmark - tables from saved records
# ---------------------------------------------------------------------------

RECORDS = [
    call("a/steady:free", "email-invoice", "answered", True, 6.0),
    call("a/steady:free", "not-an-invoice", "refused", True, 8.0),
    call("a/steady:free", "ocr-receipt", "answered", True, 21.5),
    call("b/flaky:free", "email-invoice", "answered", True, 3.0),
    call(
        "b/flaky:free",
        "service-invoice",
        "answered",
        False,
        5.0,
        checks=[{"label": "Tax is 0, because the email says none applies", "passed": False}],
    ),
    call(
        "b/flaky:free",
        "not-an-invoice",
        "no_answer",
        None,
        0.1,
        error_type="ProviderRateLimitError",
        message="b/flaky:free: rate limited (429)",
    ),
    call(
        "b/flaky:free",
        "ocr-receipt",
        "no_answer",
        None,
        0.2,
        error_type="ProviderRateLimitError",
        message="b/flaky:free: rate limited (429)",
    ),
]


def test_summary_separates_no_answer_from_wrong_answer() -> None:
    steady, flaky = summarise(RECORDS)

    assert (steady["model"], steady["calls"], steady["no_answer"]) == ("a/steady:free", 3, 0)
    assert (steady["answered"], steady["correct"]) == (3, 3)
    assert (steady["median_seconds"], steady["slowest_seconds"]) == (8.0, 21.5)

    assert (flaky["calls"], flaky["no_answer"], flaky["answered"], flaky["correct"]) == (4, 2, 2, 1)
    assert flaky["errors"] == {"ProviderRateLimitError": 2}
    # A rate-limited call returns at once; counting it would flatter the model's speed.
    assert (flaky["median_seconds"], flaky["slowest_seconds"]) == (4.0, 5.0)


def test_a_model_that_never_answered_has_no_times() -> None:
    (row,) = summarise([RECORDS[-1]])

    assert (row["answered"], row["median_seconds"], row["slowest_seconds"]) == (0, None, None)


def test_grid_shows_a_case_a_model_never_completed() -> None:
    grid = case_grid(RECORDS)

    assert grid["email-invoice"] == {"a/steady:free": "1/1/1", "b/flaky:free": "1/1/1"}
    assert grid["service-invoice"] == {"b/flaky:free": "0/1/1"}
    # Attempted once, never answered: "0/0/1", which a single total would hide.
    assert grid["not-an-invoice"]["b/flaky:free"] == "0/0/1"


def test_report_prints_the_table_the_grid_and_the_misses() -> None:
    out = io.StringIO()

    print_report(RECORDS, out)

    text = out.getvalue()
    assert "a/steady:free" in text and "3 of 3" in text
    assert "1 of 2" in text and "no answer x2: ProviderRateLimitError" in text
    assert "Per case: fully correct / answered / attempted" in text
    assert "flaky on service-invoice: Tax is 0" in text


def test_report_of_nothing_says_so() -> None:
    out = io.StringIO()
    print_report([], out)
    assert out.getvalue().strip() == "No call records."


def test_records_are_read_back_from_files(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    lines = [json.dumps({"type": "run", "models": ["a/steady:free"]})]
    lines += [json.dumps(record) for record in RECORDS[:2]]
    path.write_text("\n".join(lines) + "\n\n", encoding="utf-8")

    assert load_records([path]) == RECORDS[:2]


# ---------------------------------------------------------------------------
# Benchmark - running (with a scripted engine instead of a model)
# ---------------------------------------------------------------------------


class ScriptedEngine(BaseExtractor):
    """Answers from a script keyed by case text, so a run needs no network."""

    def __init__(self, model: str, settings: Settings) -> None:
        super().__init__(settings)
        self._model = model
        self.provider = model
        self.seen: list[str] = []

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
        self.seen.append(text)
        if "planning sync" in text:
            raise UnparseableTextError("found no invoice", provider=self.model)
        if "PALOMA" in text:
            raise ProviderRateLimitError(
                f"{self.model}: rate limited (429)",
                provider=self.model,
                detail="user_id=secret-account-id",
                retryable=True,
            )
        return InvoiceData.model_validate(EMAIL_ANSWER)


def run_scripted(passes: int = 1) -> tuple[list[dict[str, Any]], str]:
    out = io.StringIO()
    records = asyncio.run(
        benchmark.run(
            ["a/steady:free"],
            passes=passes,
            gap_minutes=0,
            out=out,
            settings=get_settings(),
            make_engine=ScriptedEngine,
            seconds_between_starts=0,
        )
    )
    return records, out.getvalue()


def test_request_starts_are_spaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The free tier allows 20 requests a minute, so starts keep a fixed distance."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    # A clock that stands still: every wait below is the pacer's own doing.
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(benchmark.asyncio, "sleep", fake_sleep)

    async def three_starts() -> None:
        pace = benchmark._Pace(3.5)
        for _ in range(3):
            await pace.wait()

    asyncio.run(three_starts())

    assert slept == [0.0, 3.5, 7.0]
    assert benchmark.SECONDS_BETWEEN_STARTS >= 3.0  # 60 s / 20 requests


def test_passes_are_separated_by_the_requested_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(benchmark.asyncio, "sleep", fake_sleep)

    asyncio.run(
        benchmark.run(
            ["a/steady:free"],
            passes=3,
            gap_minutes=0.5,
            out=io.StringIO(),
            settings=get_settings(),
            make_engine=ScriptedEngine,
            seconds_between_starts=0,
        )
    )

    # Two gaps between three passes, half a minute each; nothing else waits that long.
    assert [seconds for seconds in slept if seconds >= 1] == [30.0, 30.0]


def test_a_record_holds_the_documented_fields_and_no_others() -> None:
    """benchmarks/README.md lists these fields; a record must not carry anything else."""
    records, _ = run_scripted()

    assert {frozenset(record) for record in records} == {
        frozenset(
            {
                "type",
                "run",
                "pass",
                "started",
                "model",
                "case",
                "outcome",
                "correct",
                "checks",
                "seconds",
                "error_type",
                "message",
            }
        )
    }


def test_a_run_writes_a_header_and_one_record_per_call() -> None:
    records, written = run_scripted(passes=2)

    lines = [json.loads(line) for line in written.splitlines()]
    header, calls = lines[0], lines[1:]
    assert header["type"] == "run" and header["models"] == ["a/steady:free"]
    assert header["passes"] == 2 and len(header["cases"]) == 7
    assert (header["timeout_seconds"], header["max_output_tokens"]) == (5.0, 2048)
    assert calls == records and len(calls) == 14
    assert [record["pass"] for record in calls] == [1] * 7 + [2] * 7
    assert {record["run"] for record in calls} == {header["run"]}
    assert all(record["started"].endswith("Z") for record in calls)


def test_a_run_grades_each_outcome() -> None:
    records, _ = run_scripted()
    by_case = {record["case"]: record for record in records}

    assert (by_case["email-invoice"]["outcome"], by_case["email-invoice"]["correct"]) == (
        "answered",
        True,
    )
    # The scripted engine returns the email's invoice for every document, which is wrong here.
    assert by_case["eur-invoice"]["correct"] is False
    assert (by_case["not-an-invoice"]["outcome"], by_case["not-an-invoice"]["correct"]) == (
        "refused",
        True,
    )
    assert (by_case["ocr-receipt"]["outcome"], by_case["ocr-receipt"]["correct"]) == (
        "no_answer",
        None,
    )
    assert by_case["ocr-receipt"]["error_type"] == "ProviderRateLimitError"


def test_records_keep_the_sanitised_message_and_never_the_upstream_text() -> None:
    """The records are meant to be committed; upstream text can name the account."""
    records, written = run_scripted()

    limited = next(record for record in records if record["outcome"] == "no_answer")
    assert limited["message"] == "a/steady:free: rate limited (429)"
    assert "secret-account-id" not in written


# ---------------------------------------------------------------------------
# Benchmark - the command's guard rails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    ["nex-agi/nex-n2.5-mini", "openrouter/free", "openai/gpt-4o-mini", "a/b:nitro"],
)
def test_command_refuses_any_model_that_could_cost_money(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark, "run", lambda *args, **kwargs: pytest.fail("a model was called"))

    with pytest.raises(SystemExit, match="only calls free models"):
        benchmark.main(["a/b:free", model])


def test_command_refuses_a_run_that_would_drain_the_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark, "run", lambda *args, **kwargs: pytest.fail("a model was called"))

    # 5 models x 7 cases x 3 passes = 105 requests.
    with pytest.raises(SystemExit, match="105 requests"):
        benchmark.main([f"vendor/model-{n}:free" for n in range(5)] + ["--passes", "3"])


def test_command_refuses_to_run_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import extractors

    monkeypatch.delenv("OPENROUTER_API_KEY")
    extractors.reset_extractors()
    monkeypatch.setattr(benchmark, "run", lambda *args, **kwargs: pytest.fail("a model was called"))

    with pytest.raises(SystemExit, match="OPENROUTER_API_KEY"):
        benchmark.main(["a/b:free"])


def test_command_saves_dated_records_and_prints_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(benchmark, "OpenRouterExtractor", ScriptedEngine)
    monkeypatch.setattr(benchmark, "SECONDS_BETWEEN_STARTS", 0)

    assert benchmark.main(["a/steady:free", "a/steady:free"]) == 0  # a repeated id runs once

    (saved,) = tmp_path.glob("*/*.jsonl")
    assert len(load_records([saved])) == 7
    # One line ending on every system, so a saved file is the same bytes everywhere.
    assert b"\r" not in saved.read_bytes()
    printed = capsys.readouterr().out
    assert "Sending up to 7 free requests" in printed
    assert "a/steady:free" in printed and "Per case" in printed


def test_command_never_overwrites_an_earlier_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files are named to the second. A second run in that second must not wipe the first."""

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 10, 7, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(benchmark, "datetime", FrozenClock)
    monkeypatch.setattr(benchmark, "OpenRouterExtractor", ScriptedEngine)
    monkeypatch.setattr(benchmark, "SECONDS_BETWEEN_STARTS", 0)

    assert benchmark.main(["a/first:free"]) == 0
    (saved,) = tmp_path.glob("*/*.jsonl")
    first_run = saved.read_bytes()

    with pytest.raises(SystemExit, match="already exists"):
        benchmark.main(["a/second:free"])

    assert saved.read_bytes() == first_run
    assert list(tmp_path.glob("*/*.jsonl")) == [saved]


def test_report_mode_calls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "saved.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in RECORDS), encoding="utf-8")
    monkeypatch.setattr(benchmark, "run", lambda *args, **kwargs: pytest.fail("a model was called"))

    assert benchmark.main(["--report", str(path)]) == 0

    assert "3 of 3" in capsys.readouterr().out

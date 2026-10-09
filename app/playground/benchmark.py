"""Measure free models on the playground's live cases, and keep the records.

    python -m app.playground.benchmark MODEL [MODEL ...] [--passes N] [--gap-minutes M]
    python -m app.playground.benchmark --report FILE [FILE ...]

Every call goes through the service's own engine (prompt, schema, parsing and
validation), one model at a time with no fallback, so a result here is what the
service would get from that model on its own. Each call is written to a dated
file under ``benchmarks/`` as it finishes; ``--report`` prints the tables again
from saved files without calling anything.

What the numbers mean:

* **no answer**: the call ended without an answer (rate limit, timeout, rejected
  format, cut-off output). A fallback can cover these.
* **fully correct**: of the calls that did answer, how many got every checked
  field right, or refused the one document that is not an invoice. A fallback can
  NOT cover a wrong answer, because the chain only moves on when a call fails.

Guard rails, all deliberate:

* Free models only. An id that does not end in ``:free`` is refused, so this
  command can never spend credits.
* A run is refused when it would send more than ``MAX_REQUESTS`` requests, and
  requests start at least ``SECONDS_BETWEEN_STARTS`` apart across all models, to
  stay inside the free tier's limits.
* It proposes no chain and edits nothing. Rate limiting on free models swings
  within minutes, so one sitting cannot rank them; the README states the rule
  for reading the table.
* Records hold the error type and the sanitised message only, never the raw
  upstream text, which can carry account identifiers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, TextIO

from app.config import Settings, get_settings
from app.extractors import (
    BaseExtractor,
    ExtractionError,
    OpenRouterExtractor,
    UnparseableTextError,
    get_openrouter_client,
)
from app.playground.catalogue import scenarios_for

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "benchmarks"

#: Free models allow 20 requests a minute across the account; this keeps a run at 17.
SECONDS_BETWEEN_STARTS = 3.5
#: A run larger than this is refused rather than left to drain the daily allowance.
MAX_REQUESTS = 100

#: Builds the engine for one model. Tests pass a stand-in that calls nothing.
EngineFactory = Callable[[str, Settings], BaseExtractor]


# ---------------------------------------------------------------------------
# Cases and grading (pure functions)
# ---------------------------------------------------------------------------


def live_cases() -> list[dict[str, Any]]:
    """The playground scenarios that call a real model and simulate nothing."""
    return [
        scenario
        for scenario in scenarios_for(1)
        if scenario["live_calls"] and not any(scenario["simulate"])
    ]


def expects_refusal(case: dict[str, Any]) -> bool:
    """True for the case whose right answer is "this is not an invoice"."""
    return case["checks"][0]["expected"] == 422


def _dig(value: Any, path: str) -> Any:
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def check_passes(check: dict[str, Any], body: dict[str, Any]) -> bool:
    """One field check against a response body, by the same rules the page applies."""
    kind, expected = check["kind"], check["expected"]
    actual = _dig(body, check["path"]) if "path" in check else None
    if kind == "field":
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            # Money comes back as floats: compare to the cent, not to the last bit.
            return (
                isinstance(actual, (int, float))
                and not isinstance(actual, bool)
                and abs(actual - expected) < 0.005
            )
        return actual == expected
    if kind == "contains":
        return isinstance(actual, str) and expected.lower() in actual.lower()
    if kind == "count":
        return isinstance(actual, list) and len(actual) == expected
    raise ValueError(f"the benchmark cannot grade a check of kind {kind!r}")


def grade(
    case: dict[str, Any], outcome: str, data: Optional[dict[str, Any]]
) -> tuple[Optional[bool], list[dict[str, Any]]]:
    """Whether one answer is fully correct, and the result of each check behind that.

    ``outcome`` is ``answered``, ``refused`` or ``no_answer``. A call that gave no
    answer cannot be graded, so it returns ``(None, [])``. The "Returns HTTP 200"
    check is left out on purpose: every answer passes it, so it would only pad scores.
    """
    if outcome == "no_answer":
        return None, []
    if expects_refusal(case):
        refused = outcome == "refused"
        return refused, [{"label": "Refuses a text that is not an invoice", "passed": refused}]
    if outcome == "refused":
        return False, [{"label": "Finds the invoice in the document", "passed": False}]
    body = {"data": data}
    checks = [
        {"label": check["label"], "passed": check_passes(check, body)}
        for check in case["checks"]
        if check["kind"] != "status"
    ]
    return all(check["passed"] for check in checks), checks


# ---------------------------------------------------------------------------
# Reading records back (pure functions)
# ---------------------------------------------------------------------------


def load_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Every call record in the given files; run headers and blank lines are skipped."""
    records: list[dict[str, Any]] = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if entry.get("type") == "call":
                    records.append(entry)
    return records


def summarise(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per model, in the order the models first appear."""
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        row = rows.setdefault(
            record["model"],
            {
                "model": record["model"],
                "calls": 0,
                "no_answer": 0,
                "errors": {},
                "answered": 0,
                "correct": 0,
                "seconds": [],
            },
        )
        row["calls"] += 1
        if record["outcome"] == "no_answer":
            row["no_answer"] += 1
            row["errors"][record["error_type"]] = row["errors"].get(record["error_type"], 0) + 1
        else:
            row["answered"] += 1
            row["correct"] += 1 if record["correct"] else 0
            # Only calls that answered are timed: a refused request returns in a
            # fraction of a second and says nothing about the model's speed.
            row["seconds"].append(record["seconds"])

    for row in rows.values():
        seconds = row.pop("seconds")
        row["median_seconds"] = round(statistics.median(seconds), 1) if seconds else None
        row["slowest_seconds"] = round(max(seconds), 1) if seconds else None
    return list(rows.values())


def rank(rows: Sequence[dict[str, Any]], timeout_seconds: float) -> list[dict[str, Any]]:
    """The README's ordering rule, applied to rows of :func:`summarise`.

    Step 1 leaves out a model that never answered, or whose usual (median) answer
    takes more than half of the time limit. Steps 2 to 4 order the rest: fewest wrong
    answers, then fewest calls with no answer, and speed only breaks ties.
    """
    kept = [
        row for row in rows if row["answered"] > 0 and row["median_seconds"] <= timeout_seconds / 2
    ]
    return sorted(
        kept,
        key=lambda row: (row["answered"] - row["correct"], row["no_answer"], row["median_seconds"]),
    )


def case_grid(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """``grid[case][model]`` as "correct/answered/attempted", so a gap is visible.

    A model that was rate limited every time it met one case has never been tested
    on it, and a single total would hide that.
    """
    counts: dict[str, dict[str, list[int]]] = {}
    for record in records:
        cell = counts.setdefault(record["case"], {}).setdefault(record["model"], [0, 0, 0])
        cell[2] += 1
        if record["outcome"] != "no_answer":
            cell[1] += 1
            cell[0] += 1 if record["correct"] else 0
    return {
        case: {model: "/".join(str(n) for n in cell) for model, cell in models.items()}
        for case, models in counts.items()
    }


def _short(model: str) -> str:
    return model.split("/", 1)[-1].split(":", 1)[0]


def print_report(records: Sequence[dict[str, Any]], out: Optional[TextIO] = None) -> None:
    out = out or sys.stdout
    summary = summarise(records)
    if not summary:
        print("No call records.", file=out)
        return

    width = max(len(row["model"]) for row in summary)
    header = f"{'model':<{width}}  calls  no answer  fully correct  median  slowest"
    print(header, file=out)
    for row in summary:
        correct = f"{row['correct']} of {row['answered']}" if row["answered"] else "-"
        median = f"{row['median_seconds']} s" if row["median_seconds"] is not None else "-"
        slowest = f"{row['slowest_seconds']} s" if row["slowest_seconds"] is not None else "-"
        print(
            f"{row['model']:<{width}}  {row['calls']:>5}  {row['no_answer']:>9}  "
            f"{correct:>13}  {median:>6}  {slowest:>7}",
            file=out,
        )
    for row in summary:
        for error, count in sorted(row["errors"].items()):
            print(f"  {row['model']}: no answer x{count}: {error}", file=out)

    grid = case_grid(records)
    models = [row["model"] for row in summary]
    case_width = max(len(case) for case in grid)
    print(file=out)
    print("Per case: fully correct / answered / attempted", file=out)
    print(f"{'case':<{case_width}}  " + "  ".join(f"{_short(m):>24}" for m in models), file=out)
    for case, cells in grid.items():
        print(
            f"{case:<{case_width}}  " + "  ".join(f"{cells.get(m, '-'):>24}" for m in models),
            file=out,
        )

    misses: dict[tuple[str, str, str], int] = {}
    for record in records:
        for check in record.get("checks") or []:
            if not check["passed"]:
                key = (record["model"], record["case"], check["label"])
                misses[key] = misses.get(key, 0) + 1
    if misses:
        print(file=out)
        print("Checks that failed:", file=out)
        for (model, case, label), count in sorted(misses.items()):
            print(f"  {_short(model)} on {case}: {label} (x{count})", file=out)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


class _Pace:
    """Spaces request starts across all models, however fast the answers come back."""

    def __init__(self, gap: float, burst: int = 1) -> None:
        self._gap = gap
        self._next = 0.0
        #: Starts that go out at once: the first ``burst`` calls leave together. Each of
        #: them still uses up a slot, so a burst borrows from the pace and adds nothing
        #: to it: over any minute there are never more starts than ``60 / gap``.
        self._together = burst
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._together > 0:
                self._together -= 1
                start = now
                self._next = max(now, self._next) + self._gap
            else:
                start = max(now, self._next)
                self._next = start + self._gap
        await asyncio.sleep(start - now)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _one_call(
    engine: BaseExtractor,
    case: dict[str, Any],
    pace: _Pace,
    on_start: Optional[Callable[[str, str], None]] = None,
) -> dict[str, Any]:
    await pace.wait()
    if on_start is not None:
        on_start(engine.model, case["id"])
    record: dict[str, Any] = {
        "type": "call",
        "started": _utc_now(),
        "model": engine.model,
        "case": case["id"],
        "error_type": None,
        "message": None,
    }
    data: Optional[dict[str, Any]] = None
    began = time.perf_counter()
    try:
        invoice = await engine.extract(case["text"])
    except UnparseableTextError:
        record["outcome"] = "refused"
    except ExtractionError as exc:
        record["outcome"] = "no_answer"
        # The sanitised message only. exc.detail can carry account identifiers.
        record["error_type"], record["message"] = type(exc).__name__, exc.message
    except Exception as exc:  # noqa: BLE001 - one model's odd answer must not end the run for all
        # The engine could not even read what came back: a model that sends its answer
        # as a list of parts makes it raise AttributeError. That is this model's own
        # doing, like output that does not fit the schema, and it is recorded as such.
        record["outcome"] = "no_answer"
        record["error_type"] = "SchemaValidationError"
        record["message"] = f"{engine.model}: unexpected {type(exc).__name__}"
    else:
        record["outcome"] = "answered"
        data = invoice.model_dump(mode="json")
    record["seconds"] = round(time.perf_counter() - began, 2)
    record["correct"], record["checks"] = grade(case, record["outcome"], data)
    return record


async def run(
    models: Sequence[str],
    *,
    passes: int,
    gap_minutes: float,
    out: Optional[TextIO],
    settings: Settings,
    make_engine: Optional[EngineFactory] = None,
    seconds_between_starts: Optional[float] = None,
    give_up_after: Optional[int] = None,
    burst: int = 1,
    on_start: Optional[Callable[[str, str], None]] = None,
    on_call: Optional[Callable[[dict[str, Any]], None]] = None,
) -> list[dict[str, Any]]:
    """Run every case on every model ``passes`` times, writing each call as it ends.

    The optional arguments serve the service's own test of the free models
    (:mod:`app.model_testing`), which keeps no file and passes ``out=None``. With
    ``give_up_after``, a model is sent no further case in a pass once that many of
    its calls were not fully correct: the rest could not change what is concluded,
    and every call counts against the daily allowance. ``burst`` lets that many
    requests start at once before the spacing applies. ``on_start(model, case_id)``
    is called as each request goes out, and ``on_call(record)`` as each one ends.
    """
    make_engine = make_engine or OpenRouterExtractor
    if seconds_between_starts is None:
        seconds_between_starts = SECONDS_BETWEEN_STARTS
    cases = live_cases()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    header = {
        "type": "run",
        "run": run_id,
        "started": _utc_now(),
        "models": list(models),
        "passes": passes,
        "gap_minutes": gap_minutes,
        "cases": [case["id"] for case in cases],
        "timeout_seconds": settings.timeout_seconds,
        "max_output_tokens": settings.max_output_tokens,
        "strict_json_schema": settings.openrouter_strict_json_schema,
    }
    if out is not None:
        out.write(json.dumps(header) + "\n")
        out.flush()

    pace = _Pace(seconds_between_starts, burst)
    engines = [make_engine(model, settings) for model in models]
    records: list[dict[str, Any]] = []

    async def lane(engine: BaseExtractor, number: int) -> None:
        # One model's calls run one after another; the models run side by side.
        misses = 0
        for case in cases:
            call = await _one_call(engine, case, pace, on_start)
            record = {"run": run_id, "pass": number, **call}
            records.append(record)
            if out is not None:
                out.write(json.dumps(record) + "\n")
                out.flush()
            if on_call is not None:
                on_call(record)
            misses += record["correct"] is not True
            if give_up_after is not None and misses >= give_up_after:
                return

    for number in range(1, passes + 1):
        if number > 1 and gap_minutes:
            await asyncio.sleep(gap_minutes * 60)
        lanes = [asyncio.ensure_future(lane(engine, number)) for engine in engines]
        try:
            await asyncio.gather(*lanes)
        finally:
            # When one lane breaks, or the run is cancelled, the others would go on
            # calling models after the run is over: gather() does not stop them.
            for task in lanes:
                task.cancel()
    return records


def refuse_paid(models: Sequence[str]) -> None:
    """Stop before any call if a model could cost money."""
    paid = [model for model in models if not model.endswith(":free")]
    if paid:
        raise SystemExit(
            "Refusing to run: this command only calls free models, and these ids do not end "
            f"in ':free': {', '.join(paid)}"
        )


def planned_requests(models: Sequence[str], passes: int) -> int:
    return len(models) * len(live_cases()) * passes


async def _run_and_close(models: Sequence[str], args: argparse.Namespace, out: TextIO) -> None:
    try:
        await run(
            models,
            passes=args.passes,
            gap_minutes=args.gap_minutes,
            out=out,
            settings=get_settings(),
        )
    finally:
        # The engines share one HTTP client; close it while the event loop is alive.
        if get_openrouter_client.cache_info().currsize:
            await get_openrouter_client().close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.playground.benchmark",
        description="Measure free models on the playground's live cases and save the records.",
    )
    parser.add_argument("models", nargs="*", help="model ids to measure; each must end in ':free'")
    parser.add_argument("--passes", type=int, default=1, help="times to run every case (default 1)")
    parser.add_argument(
        "--gap-minutes",
        type=float,
        default=0,
        help="pause between passes; free-model rate limits change by the minute, so spread them out",
    )
    parser.add_argument(
        "--report",
        nargs="+",
        type=Path,
        metavar="FILE",
        help="print the tables for saved record files instead of calling any model",
    )
    args = parser.parse_args(argv)

    if args.report:
        print_report(load_records(args.report))
        return 0
    if not args.models:
        parser.error("name at least one model, or use --report FILE")
    if args.passes < 1:
        parser.error("--passes must be at least 1")

    models = list(dict.fromkeys(args.models))
    refuse_paid(models)
    planned = planned_requests(models, args.passes)
    if planned > MAX_REQUESTS:
        raise SystemExit(
            f"Refusing to run: {planned} requests is more than this command sends in one go "
            f"({MAX_REQUESTS}). Name fewer models or fewer passes."
        )
    if not get_settings().openrouter_enabled:
        raise SystemExit("Refusing to run: OPENROUTER_API_KEY is not set.")

    now = datetime.now(timezone.utc)
    path = RESULTS_DIR / now.strftime("%Y-%m-%d") / (now.strftime("%H%M%SZ") + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Mode "x" refuses a file that exists, so a second run started in the same
        # second stops here instead of wiping the first run's records. newline="\n"
        # gives the same bytes on every system.
        out = path.open("x", encoding="utf-8", newline="\n")
    except FileExistsError:
        raise SystemExit(
            f"Refusing to run: {path} already exists, so another run started in this same "
            "second. Start this one again."
        ) from None
    print(f"Sending up to {planned} free requests; they count against the daily allowance.")
    print(f"Records: {path}")

    with out:
        asyncio.run(_run_and_close(models, args, out))

    print()
    print_report(load_records([path]))
    return 0


if __name__ == "__main__":  # pragma: no cover - command-line entry
    sys.exit(main())

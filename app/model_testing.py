"""One test of the free models: send the sample documents, record every call, decide.

A test sends each model the playground's seven sample documents through the
service's own engine, using the benchmark's runner, grading and pacing
(:mod:`app.playground.benchmark`), and judges what comes back by the rules in
:mod:`app.model_rules`. Everything it learns goes into one plain dictionary, the
*record*. The page draws its board from that record, the scheduled job publishes it,
and the recorded measurement the page can replay is the same thing built from saved
calls. One format, three sources.

Two callers run tests: the running service (:mod:`app.model_chooser`), which puts the
result to use, and the scheduled job (:mod:`app.published_chain`), which publishes it.
Nothing here changes the chain in use.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from app import budget, model_rules
from app.config import DEFAULT_MODEL_CHAIN, Settings
from app.model_catalogue import is_free_model
from app.playground import benchmark
from app.playground.catalogue import BENCHMARK_SNAPSHOT

logger = logging.getLogger("app.model_testing")

STAMP = "%Y-%m-%dT%H:%M:%SZ"  #: how a moment is written for the page and in files (UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Who started a test, why, and how it ended. The page has words for every code (a
#: test compares these tuples with the page's tables).
STARTERS = ("failure", "page", "schedule", "recorded")
TEST_REASONS = ("gone", "closed", "asked", "schedule", "recorded")
OUTCOMES = ("changed", "unchanged", "nothing_passed", "key_refused", "interrupted")

#: The fields of one call that the page and the published file may hold. The engine's
#: sanitised message is not among them: it can name a setting, and visitors read this.
CALL_FIELDS = ("model", "case", "outcome", "correct", "seconds", "error_type")


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.strftime(STAMP)


def default_chain() -> list[str]:
    return [model.strip() for model in DEFAULT_MODEL_CHAIN.split(",") if model.strip()]


def new_record(
    by: str,
    reason: dict[str, Any],
    chain: Sequence[str],
    kept: Sequence[str],
    gone: Sequence[str],
    listed: Sequence[str],
    tested: Sequence[str],
    left_out: Sequence[str],
    settings: Settings,
) -> dict[str, Any]:
    """The record of a test that is about to run.

    ``chain`` is the chain in use, ``kept`` its models that OpenRouter still offers,
    ``gone`` the ones it does not, ``listed`` what OpenRouter lists, ``tested`` the
    models this test will call and ``left_out`` those it has no room for.
    """
    cases = benchmark.live_cases()
    started = now()
    return {
        "id": started.strftime("%Y%m%dT%H%M%SZ"),
        "by": by,
        "reason": reason,
        "state": "running",
        "started": stamp(started),
        "finished": None,
        "found": list(listed),
        "tested": list(tested),
        "left_out": list(left_out),
        "gone": list(gone),
        "cases": [{"id": case["id"], "title": case["title"]} for case in cases],
        "timeout_seconds": settings.timeout_seconds,
        "calls": [],
        "in_flight": [],
        "models": model_rules.judge(tested, [], [case["id"] for case in cases], settings.timeout_seconds),
        "provisional": [],
        "chain_before": list(chain),
        "kept": list(kept),
        "chain": list(chain),
        "outcome": None,
        "requests": 0,
    }


async def run_test(
    record: dict[str, Any],
    settings: Settings,
    on_first_right: Optional[Callable[[str], None]] = None,
    on_failed: Optional[Callable[[str], None]] = None,
) -> None:
    """Send the sample documents, judge, and decide the chain. Fills in ``record``.

    ``on_first_right(model)`` is called when the first answer a model gives is right.
    The running service uses it during a repair, to put that model to use while the
    test goes on; then the first round goes out all at once, because somebody is
    waiting. ``on_failed(model)`` is called when a model's verdict has become
    "failed", which nothing later in the test can change: a stand-in stops answering
    then, and not when the slowest model's test is over. The record's ``chain`` says
    what the chain should be when the test is over. Applying it is the caller's
    business.
    """
    if not all(is_free_model(model) for model in record["tested"]):
        # The last door before a model is called, as model_cache.use() is the last door
        # before one is chosen: whatever a list said, only free ids are sent a document.
        raise ValueError("only ids ending in ':free' are ever tested")
    cases = [case["id"] for case in record["cases"]]
    key_refused = False

    def started(model: str, case: str) -> None:
        budget.count()
        record["requests"] += 1
        record["in_flight"].append({"model": model, "case": case, "since": stamp(now())})

    def ended(call: dict[str, Any]) -> None:
        """One call has come back: a row of the record, a line in the log, and what follows."""
        nonlocal key_refused
        model = call["model"]
        record["in_flight"] = [
            entry
            for entry in record["in_flight"]
            if (entry["model"], entry["case"]) != (model, call["case"])
        ]
        record["calls"].append({key: call[key] for key in CALL_FIELDS})
        record["models"] = model_rules.judge(
            record["tested"], record["calls"], cases, record["timeout_seconds"]
        )
        row = next(row for row in record["models"] if row["model"] == model)
        # The error's type, never the engine's sentence: that one is written for a model
        # an admin chose ("set this", "raise that"), and a candidate is none.
        logger.info(
            "test %s: %s on %s: %s, %s s%s",
            record["id"],
            model,
            call["case"],
            {True: "right", False: "wrong", None: "no answer"}[call["correct"]],
            call["seconds"],
            f" ({call['error_type']})" if call["error_type"] else "",
        )
        if call["error_type"] in model_rules.KEY_PROBLEMS:
            key_refused = True
            runner.cancel()  # no model can fix a refused key: stop spending at once
        elif row["verdict"] == "failed":
            if on_failed:
                on_failed(model)
        elif on_first_right and call["correct"] is True and row["answered"] == 1:
            # The first answer this model gave, whichever document it was: a rate limit
            # on the first document must not keep a working model out for minutes.
            on_first_right(model)

    # The opening requests must fit into what is left of this minute. A caller who
    # keeps asking can keep the minute full, so the wait has an end: after a minute the
    # test goes at its pace, one request at a time, as it does for the button.
    burst = len(record["tested"]) if on_first_right else 1
    waited = 0
    while budget.seconds_until_room(burst, budget.MINUTE_LIMIT):
        if waited >= budget.MINUTE_SECONDS:
            burst = 1
            break
        await asyncio.sleep(1)
        waited += 1
    # From here on a visitor who is told "try again in N seconds" is told an N that
    # allows for this test's own calls.
    budget.expect_calls_every(model_rules.SECONDS_BETWEEN_STARTS)
    runner = asyncio.create_task(
        benchmark.run(
            record["tested"],
            passes=1,
            gap_minutes=0,
            out=None,  # the record is all a test keeps
            settings=settings,
            seconds_between_starts=model_rules.SECONDS_BETWEEN_STARTS,
            give_up_after=model_rules.GIVE_UP_AFTER,
            burst=burst,
            on_start=started,
            on_call=ended,
        )
    )
    try:
        await runner
    except asyncio.CancelledError:
        if not key_refused:
            runner.cancel()
            raise  # the service is stopping: a half-run test is not a result
    finally:
        budget.expect_calls_every(None)
    record["in_flight"] = []
    rows = model_rules.judge(
        record["tested"], record["calls"], cases, record["timeout_seconds"], finished=True
    )
    before = record["chain_before"]
    if key_refused:
        chain, outcome = list(before), "key_refused"
    else:
        # From the chain as it was when the test began, never from the stand-ins: a
        # model that was put to use after one document gets no benefit of the doubt.
        # Which of the chain's own models leave is for the rules to say (a model
        # OpenRouter no longer offers stays when it still answered here).
        chain = model_rules.next_chain(
            before, rows, record["gone"], record["timeout_seconds"], measured=default_chain()
        )
        passed = any(row["verdict"] in ("passed", "slow") for row in rows)
        outcome = "changed" if chain != list(before) else "unchanged" if passed else "nothing_passed"
    model_rules.place(rows, before, chain)
    record.update(
        models=rows, chain=chain, outcome=outcome, state="done", finished=stamp(now()), provisional=[]
    )


_recorded: Optional[dict[str, Any]] = None  #: the recorded run, built once ({} when there is none)


def recorded_run() -> Optional[dict[str, Any]]:
    """The first pass of the saved benchmark, told as a test. Nothing is called.

    It is what the page can show, and replay, before any test has run here: real
    calls, recorded on the day the default chain was measured, judged by the same
    rules a test uses. None when the records are not part of this deployment.
    """
    global _recorded
    if _recorded is None:
        _recorded = {}
        try:
            paths = [PROJECT_ROOT / name for name in BENCHMARK_SNAPSHOT["records"]]
            calls = [call for call in benchmark.load_records(paths) if call.get("pass") == 1]
            calls.sort(key=lambda call: call["started"])
            cases = benchmark.live_cases()
            chain = default_chain()
            tested = list(dict.fromkeys(call["model"] for call in calls))
            tested.sort(key=lambda model: chain.index(model) if model in chain else len(chain))
            limit = BENCHMARK_SNAPSHOT["timeout_seconds"]
            rows = model_rules.judge(tested, calls, [case["id"] for case in cases], limit, finished=True)
            after = model_rules.next_chain(chain, rows, [], limit, measured=chain)
            model_rules.place(rows, chain, after)
            _recorded = {
                "id": "recorded-" + BENCHMARK_SNAPSHOT["measured"],
                "by": "recorded",
                "reason": {"code": "recorded", "models": []},
                "state": "done",
                "started": calls[0]["started"],
                "finished": calls[-1]["started"],
                "found": tested,
                "tested": tested,
                "left_out": [],
                "gone": [],
                "cases": [{"id": case["id"], "title": case["title"]} for case in cases],
                "timeout_seconds": limit,
                "calls": [{key: call[key] for key in CALL_FIELDS} for call in calls],
                "in_flight": [],
                "models": rows,
                "provisional": [],
                "chain_before": chain,
                "kept": chain,
                "chain": after,
                "outcome": "changed" if after != chain else "unchanged",
                "requests": len(calls),
            }
        except Exception:  # noqa: BLE001 - no records, or unreadable ones: nothing to replay
            _recorded = {}
    return _recorded or None

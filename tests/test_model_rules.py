"""Tests for what a test of the free models concludes (``app.model_rules``).

Pure functions over made-up calls and over the calls saved on 7 October 2026. No
network, no clock, no file but the saved records.

Layout:

* Model rules - the verdict     - passed, slow, failed, not judged.
* Model rules - the chain       - repair, never rank.
* Model rules - the saved day   - every recorded pass keeps the hand-picked chain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from app import model_rules
from app.config import DEFAULT_MODEL_CHAIN
from app.model_rules import judge, next_chain, place
from app.playground.benchmark import live_cases, load_records, rank
from app.playground.catalogue import BENCHMARK, BENCHMARK_SNAPSHOT

CASES = [case["id"] for case in live_cases()]
LIMIT = 30.0
FIRST, SECOND = [model.strip() for model in DEFAULT_MODEL_CHAIN.split(",")]
NEW, OTHER, SLOW = "vendor/new:free", "vendor/other:free", "vendor/slow:free"
RECORDS = Path(__file__).resolve().parents[1] / BENCHMARK_SNAPSHOT["records"][0]


def calls(model: str, plan: str, seconds: float = 3.0) -> list[dict[str, Any]]:
    """One call per letter, in the order the documents are sent.

    ``r`` right, ``w`` wrong, ``l`` lost to a rate limit, ``u`` unusable output.
    """
    made = []
    for case, letter in zip(CASES, plan):
        answered = letter in "rw"
        made.append(
            {
                "model": model,
                "case": case,
                "outcome": "answered" if answered else "no_answer",
                "correct": {"r": True, "w": False}.get(letter),
                "seconds": seconds if answered else 0.2,
                "error_type": {"l": "ProviderRateLimitError", "u": "SchemaValidationError"}.get(letter),
            }
        )
    return made


def verdict(plan: str, seconds: float = 3.0, finished: bool = False) -> tuple[Optional[str], Optional[str]]:
    row = judge([NEW], calls(NEW, plan, seconds), CASES, LIMIT, finished=finished)[0]
    return row["verdict"], row["why"]


# ---------------------------------------------------------------------------
# Model rules - the verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ("rrrrrrr", ("passed", "all_right")),
        ("rrrwrrr", ("passed", "one_missed")),  # the tested fallback, in every recorded pass
        ("rlrrrrr", ("passed", "one_missed")),  # one document lost to a rate limit
        ("rrrrrwr", ("failed", "hidden_instructions")),  # it did what the document told it to
        ("rwrw", ("failed", "wrong_answers")),  # the second wrong answer ends its test
        ("uu", ("failed", "unusable_answers")),  # rejects the format: two calls, no more
        ("wu", ("failed", "wrong_answers")),
        ("ll", ("not_judged", "no_answers")),  # weather: nothing is held against it
        ("rlrl", ("not_judged", "unanswered")),
        ("rwrl", ("not_judged", "unanswered")),  # one wrong and one lost: it might still be good
        ("rrrrrlr", ("not_judged", "unanswered")),  # the guarded document got no answer
        ("lrrrrrl", ("not_judged", "unanswered")),  # two lost, the second on the last document
        ("rrr", (None, None)),  # still running
    ],
)
def test_the_verdict(plan: str, expected: tuple[Optional[str], Optional[str]]) -> None:
    assert verdict(plan) == expected


def test_a_slow_model_is_right_but_slow() -> None:
    assert verdict("rrrrrrr", seconds=LIMIT / 2) == ("passed", "all_right")
    assert verdict("rrrrrrr", seconds=LIMIT / 2 + 0.1) == ("slow", "all_right")
    # A refused request returns in a fraction of a second and says nothing about speed.
    assert verdict("rlrrrrr", seconds=21.6) == ("slow", "one_missed")


def test_a_test_that_was_cut_short_judges_nobody_by_what_is_missing() -> None:
    assert verdict("rrr", finished=True) == ("not_judged", "unanswered")
    assert verdict("", finished=True) == ("not_judged", "no_answers")
    # Six documents are not seven, although the guarded one is among the six.
    assert CASES.index(model_rules.MUST_BE_RIGHT) == 5
    assert verdict("rrrrrr", finished=True) == ("not_judged", "unanswered")


def test_output_that_was_cut_off_or_blocked_is_unusable_too() -> None:
    blocked = [{**call, "error_type": "ContentBlockedError"} for call in calls(NEW, "uu")]
    row = judge([NEW], blocked, CASES, LIMIT)[0]

    assert (row["verdict"], row["why"]) == ("failed", "unusable_answers")
    # The names the rules go by are the engine's own classes.
    from app import extractors

    for name in model_rules.UNUSABLE + model_rules.KEY_PROBLEMS:
        assert issubclass(getattr(extractors, name), extractors.ExtractionError), name


def test_the_numbers_bound_what_a_test_spends() -> None:
    assert model_rules.GIVE_UP_AFTER == model_rules.MAX_MISSES + 1 == 2
    assert model_rules.CANDIDATE_LIMIT * len(CASES) == 42  # the most one test can send
    assert model_rules.MUST_BE_RIGHT in CASES
    assert 60 / model_rules.SECONDS_BETWEEN_STARTS <= 10  # half of the 20 a minute OpenRouter allows


def test_an_opening_burst_borrows_from_the_pace_and_adds_nothing_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Six first documents at once, and then nothing until their six slots have passed.

    So a repair never sends more than ten requests in any minute, burst included, and a
    visitor's run still finds room under the ceiling while a test is going on.
    """
    import asyncio

    from app import budget
    from app.playground import benchmark

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    # A clock that stands still: every wait below is the pacer's own doing.
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(benchmark.asyncio, "sleep", fake_sleep)

    async def starts(burst: int, count: int) -> list[float]:
        slept.clear()
        pace = benchmark._Pace(model_rules.SECONDS_BETWEEN_STARTS, burst)
        for _ in range(count):
            await pace.wait()
        return list(slept)

    gap = model_rules.SECONDS_BETWEEN_STARTS
    burst = model_rules.CANDIDATE_LIMIT
    waits = asyncio.run(starts(burst, 12))
    assert waits[:burst] == [0.0] * burst
    assert waits[burst:] == [gap * n for n in range(burst, 12)]  # 36, 42, ... as if nobody had hurried
    assert sum(1 for wait in waits if wait < 60) <= 60 / gap == 10
    assert asyncio.run(starts(1, 3)) == [0.0, gap, 2 * gap]  # without a burst nothing changed
    # Ten a minute for a test leaves five of the fifteen that admit a visitor's run.
    assert budget.MINUTE_CEILING - 60 / gap >= 5 and budget.MINUTE_LIMIT < 20


# ---------------------------------------------------------------------------
# Model rules - the chain
# ---------------------------------------------------------------------------


def chain_after(
    current: list[str],
    plans: dict[str, tuple[str, float]],
    gone: tuple[str, ...] = (),
    measured: tuple[str, ...] = (),
) -> list[str]:
    made = [call for model, (plan, seconds) in plans.items() for call in calls(model, plan, seconds)]
    rows = judge(list(plans), made, CASES, LIMIT, finished=True)
    chain = next_chain(current, rows, list(gone), LIMIT, measured)
    place(rows, current, chain)
    return chain


GOOD, MISS, LOST, BAD, SLOWLY = ("rrrrrrr", 3.0), ("rrrwrrr", 3.0), ("ll", 3.0), ("rwrw", 3.0), ("rrrrrrr", 21.6)


@pytest.mark.parametrize(
    ("current", "plans", "gone", "expected"),
    [
        # Everything works: nothing moves, and the free place gets the newcomer.
        ([FIRST, SECOND], {FIRST: GOOD, SECOND: MISS, NEW: GOOD}, (), [FIRST, SECOND, NEW]),
        # The fallback did better than the first model in this one test: still nothing moves.
        ([FIRST, SECOND], {FIRST: MISS, SECOND: GOOD}, (), [FIRST, SECOND]),
        # Weather on the first model: it keeps first place.
        ([FIRST, SECOND], {FIRST: LOST, SECOND: GOOD}, (), [FIRST, SECOND]),
        # A model of the chain that answers wrongly keeps its place too: ranking is a person's job.
        ([FIRST, SECOND], {FIRST: BAD, SECOND: GOOD}, (), [FIRST, SECOND]),
        # Gone and silent: it leaves, the rest moves up, a newcomer goes behind.
        ([FIRST, SECOND], {FIRST: LOST, SECOND: MISS, NEW: GOOD}, (FIRST,), [SECOND, NEW]),
        # OpenRouter says "gone" but the model answers: the catalogue alone removes nothing.
        ([FIRST, SECOND], {FIRST: GOOD, SECOND: MISS}, (FIRST,), [FIRST, SECOND]),
        # Silent but still listed: it stays.
        ([FIRST, SECOND], {FIRST: LOST, SECOND: LOST, NEW: GOOD}, (), [FIRST, SECOND, NEW]),
        # A repair tests only the models outside the chain; the gone one was not asked again.
        ([FIRST, SECOND], {NEW: GOOD}, (FIRST,), [SECOND, NEW]),
        # A newcomer that failed, or could not be judged, gets nothing.
        ([FIRST, SECOND], {FIRST: GOOD, SECOND: MISS, NEW: BAD, OTHER: LOST}, (), [FIRST, SECOND]),
        # Quick models first, in the README's order; one slow model may take the last place.
        ([FIRST], {FIRST: GOOD, NEW: MISS, OTHER: GOOD, SLOW: SLOWLY}, (), [FIRST, OTHER, NEW]),
        ([FIRST, SECOND], {FIRST: GOOD, SECOND: MISS, SLOW: SLOWLY}, (), [FIRST, SECOND, SLOW]),
        ([FIRST], {FIRST: GOOD, SLOW: SLOWLY, OTHER: SLOWLY}, (), [FIRST, SLOW]),
        # A full chain takes nobody, however good.
        ([FIRST, SECOND, SLOW], {FIRST: GOOD, SECOND: MISS, SLOW: SLOWLY, NEW: GOOD}, (), [FIRST, SECOND, SLOW]),
        # The owner's case: every model of the chain is gone.
        ([FIRST, SECOND], {SLOW: SLOWLY, OTHER: ("uu", 3.0)}, (FIRST, SECOND), [SLOW]),
        # Nothing answers at all: nothing changes, and a chain is never emptied.
        ([FIRST, SECOND], {NEW: LOST}, (FIRST, SECOND), [FIRST, SECOND]),
        ([FIRST, SECOND], {FIRST: LOST, SECOND: LOST, NEW: LOST}, (), [FIRST, SECOND]),
    ],
)
def test_the_chain_is_repaired_and_never_reshuffled(
    current: list[str], plans: dict[str, tuple[str, float]], gone: tuple[str, ...], expected: list[str]
) -> None:
    chain = chain_after(current, plans, gone)
    assert chain == expected
    assert len(chain) == len(set(chain)) <= model_rules.PLACES
    # Whoever was in the chain and still is has kept the order it had.
    kept = [model for model in current if model in chain]
    assert [model for model in chain if model in kept] == kept


@pytest.mark.parametrize(
    ("current", "plans", "expected"),
    [
        # The first model was withdrawn for a few days and is back: it leads again.
        ([SECOND, SLOW], {FIRST: GOOD}, [FIRST, SECOND, SLOW]),
        # The second one comes back: behind the first, in front of everybody else.
        ([FIRST, SLOW], {SECOND: MISS}, [FIRST, SECOND, SLOW]),
        ([SLOW, OTHER], {SECOND: MISS}, [SECOND, SLOW, OTHER]),
        # Both come back in one test, the second with the better figures: the order is the measured one.
        ([SLOW], {SECOND: GOOD, FIRST: MISS}, [FIRST, SECOND, SLOW]),
        # One free place and two that passed: the measured model takes it, whatever one test's figures say.
        ([SECOND, SLOW], {NEW: GOOD, FIRST: MISS}, [FIRST, SECOND, SLOW]),
        ([SECOND], {NEW: GOOD, FIRST: MISS}, [FIRST, SECOND, NEW]),
        # Right but slow is good for the last place only, also for a model that was measured.
        ([SECOND, OTHER], {FIRST: SLOWLY}, [SECOND, OTHER, FIRST]),
        # Models that are in the chain are never moved, measured or not.
        ([SECOND, FIRST], {SECOND: GOOD, FIRST: GOOD, NEW: GOOD}, [SECOND, FIRST, NEW]),
        # A full chain takes nobody, and a model that did not pass gets no place.
        ([SECOND, SLOW, OTHER], {FIRST: GOOD}, [SECOND, SLOW, OTHER]),
        ([SECOND, SLOW], {FIRST: LOST}, [SECOND, SLOW]),
    ],
)
def test_a_measured_model_that_passes_goes_back_to_its_measured_place(
    current: list[str], plans: dict[str, tuple[str, float]], expected: list[str]
) -> None:
    """Not a ranking: the order is the one a person measured, and nobody else moves.

    Without it a first model that was withdrawn for a few days would come back as the
    last, the scheduled test would publish that order, and nothing short of a new
    default would put it first again.
    """
    assert chain_after(current, plans, measured=(FIRST, SECOND)) == expected
    # Told of no measured chain, a model that passes goes to the back like any other.
    if FIRST in plans and plans[FIRST] is GOOD and current == [SECOND, SLOW]:
        assert chain_after(current, plans) == [SECOND, SLOW, FIRST]


def test_models_outside_the_chain_are_taken_in_turn() -> None:
    """A test has room for a few. However many OpenRouter lists, each gets its turn."""
    outside = [f"vendor/number-{number:02}:free" for number in range(1, 16)]
    tested: list[str] = []
    left_out: list[str] = []
    had_a_turn: list[str] = []
    for _ in range(4):  # four tests with room for four models each
        order = model_rules.in_turn(outside, tested, left_out)
        tested, left_out = order[:4], order[4:]
        had_a_turn += tested

    assert had_a_turn[:15] == outside and set(had_a_turn) == set(outside)
    # Those that waited come first, in the order they waited; then a model nobody has
    # seen yet; then the ones the last test had room for.
    assert model_rules.in_turn(["a", "b", "c", "new"], ["a"], ["c", "b"]) == ["c", "b", "new", "a"]


def test_each_row_says_what_became_of_its_model() -> None:
    plans = {FIRST: LOST, SECOND: MISS, NEW: GOOD, OTHER: GOOD, SLOW: SLOWLY}
    made = [call for model, (plan, seconds) in plans.items() for call in calls(model, plan, seconds)]
    rows = judge(list(plans), made, CASES, LIMIT, finished=True)
    chain = next_chain([FIRST, SECOND], rows, [FIRST], LIMIT)
    place(rows, [FIRST, SECOND], chain)

    assert chain == [SECOND, NEW, OTHER]
    assert {row["model"]: row["placing"] for row in rows} == {
        FIRST: "dropped", SECOND: "kept", NEW: "added", OTHER: "added", SLOW: "no_place",
    }


def test_a_chain_is_seen_working_only_when_one_of_its_models_passed() -> None:
    rows = judge([FIRST, NEW], calls(FIRST, "ll") + calls(NEW, "rrrrrrr"), CASES, LIMIT, finished=True)
    assert model_rules.seen_working(rows, [FIRST, NEW]) is True
    assert model_rules.seen_working(rows, [FIRST]) is False


# ---------------------------------------------------------------------------
# Model rules - the saved day
# ---------------------------------------------------------------------------


def saved_pass(number: int) -> list[dict[str, Any]]:
    if not RECORDS.is_file():
        pytest.skip("this copy of the project has no benchmark records")
    return [record for record in load_records([RECORDS]) if record["pass"] == number]


@pytest.mark.parametrize("number", [1, 2, 3])
def test_every_recorded_pass_keeps_the_hand_picked_chain(number: int) -> None:
    """One pass alone, judged by these rules, never moves or drops a default model."""
    made = saved_pass(number)
    tested = [row["model"] for row in BENCHMARK]
    rows = judge(tested, made, CASES, LIMIT, finished=True)

    chain = next_chain([FIRST, SECOND], rows, [], LIMIT)

    assert chain[:2] == [FIRST, SECOND]
    by_model = {row["model"]: row for row in rows}
    assert by_model[FIRST]["verdict"] == "passed"
    assert by_model["apodex/apodex-1.1-mini:free"]["verdict"] == "failed"
    assert by_model["dots-studio/dots-3-note-preview:free"]["verdict"] in ("slow", "not_judged")


def test_the_readmes_ordering_rule_is_one_function() -> None:
    """rank() is the rule the README states; on the snapshot it gives the default chain."""
    assert [row["model"] for row in rank(BENCHMARK, BENCHMARK_SNAPSHOT["timeout_seconds"])] == [FIRST, SECOND]

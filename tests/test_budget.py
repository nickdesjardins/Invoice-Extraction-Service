"""Tests for the one ceiling on live model calls (``app.budget``).

Nothing here reaches OpenRouter or a model. The budget reads its clock through one
name, so a test moves time by hand; the day's count comes from the ``allowance``
fixture, or from a stand-in for the HTTP call when the reader itself is under test.

Layout:

* The per-minute ceiling   - 15 live calls in any 60 seconds, counted as they are made.
* The day's share          - visitors stop when a fifth of what OpenRouter counts is left,
                             and get twenty calls when OpenRouter cannot say.
* Reading the day's count  - the one request that carries the key, and what it keeps.
* Who else may spend       - the button, the scheduled job and the service's own repair.
* Through the service      - a refusal stays inside the public contract, the page's
                             stream mirrors it, and every live call is counted.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import Message
from typing import Any, Callable, Optional

import httpx
import openai
import pytest

from app import budget, extractors, model_catalogue, model_testing
from app.budget import (
    MINUTE_CEILING,
    UNKNOWN_LIVE_CALLS,
    Allowance,
    BudgetedDispatcher,
    CallLimitError,
    CountedExtractor,
    Run,
    read_allowance,
)
from app.config import get_settings
from app.extractors import ExtractionDispatcher, ExtractionError, get_dispatcher
from app.main import app
from tests.conftest import FALLBACK_MODEL, MODEL_CHAIN, PRIMARY_MODEL
from tests.test_extract import (  # noqa: F401 - `http`, `http_raw` and `router` are fixtures
    EMPTY_INVOICE_PAYLOAD,
    GARBAGE_TEXT,
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    api_error,
    http,
    http_raw,
    ok,
    router,
    timeout_error,
)
from tests.test_playground import events_of, result_of

#: The reader's opener as the service ships it. Taken at import, because conftest
#: replaces it for every test. ``read_allowance`` above is the real one for the same reason.
REAL_OPENER = budget._OPENER

NOON = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc).timestamp()
SECONDS_TO_MIDNIGHT_AT_NOON = 12 * 60 * 60

#: What OpenRouter answers to ``GET /key``, cut to the fields that matter here: the
#: three numbers, and the four fields that must never reach a log.
KEY_ANSWER: dict[str, Any] = {
    "data": {
        "label": "sk-or-v1-au7...890",
        "creator_user_id": "user_2dHFtVWx2n56w6HkM0000000000",
        "organization_id": "org_2dHFtVWx2n56w6HkM0000000001",
        "workspace_id": "wsp_2dHFtVWx2n56w6HkM0000000002",
        "is_free_tier": False,
        "free_model_daily_requests": {"used": 12, "limit": 50, "remaining": 38},
    }
}
#: Pieces of those fields, of the key and of OpenRouter's own error texts.
NEVER_LOGGED = ("sk-or-v1", "user_2dHF", "org_2dHF", "wsp_2dHF", "test-openrouter-key")

BUSY = (
    "The service is busy: it has reached its ceiling of live model calls per minute, "
    "counted for all visitors together. Please try again in {wait}."
)
DAY_USED = (
    "The service has used today's share of live model calls, counted for all visitors "
    "together. Please try again after midnight UTC."
)
DAY_UNKNOWN = (
    "The service cannot check today's free allowance right now, so it has stopped "
    "calling models for the moment. Please try again in 60 seconds."
)


class Clock:
    """A clock that a test moves by hand. It starts at noon UTC, far from midnight."""

    def __init__(self, now: float = NOON) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The budget's clock for one test."""
    clock = Clock()
    monkeypatch.setattr(budget, "_now", clock)
    return clock


def spend(calls: int) -> None:
    """Count ``calls`` model calls as made right now, whoever made them."""
    for _ in range(calls):
        budget.count()


async def one_call_run() -> None:
    """A visitor's run of one model: admitted, its call made, over."""
    run = Run(calls=1)
    await budget.admit_visitor(run)
    budget.count(run)
    budget.release(run)


async def refusal(calls: int = 1) -> CallLimitError:
    """The refusal a visitor's run of ``calls`` live calls gets right now."""
    with pytest.raises(CallLimitError) as info:
        await budget.admit_visitor(Run(calls=calls))
    return info.value


async def free_places() -> int:
    """How many places the minute has left, found the way a spender finds out: by asking."""
    for wanted in range(MINUTE_CEILING, 0, -1):
        run = Run(calls=wanted)
        try:
            await budget.admit_visitor(run)
        except CallLimitError:
            continue
        budget.release(run)
        return wanted
    return 0


# ---------------------------------------------------------------------------
# Budget - the per-minute ceiling
# ---------------------------------------------------------------------------


def test_the_ceiling_leaves_room_under_openrouters_own() -> None:
    """OpenRouter allows 20 a minute, and this process cannot see every call on the account."""
    assert MINUTE_CEILING == 15
    assert MINUTE_CEILING < budget.MINUTE_LIMIT < 20  # a test's opening burst stays under it too


@pytest.mark.asyncio
async def test_the_sixteenth_call_in_a_minute_is_refused(clock: Clock) -> None:
    for _ in range(MINUTE_CEILING):
        await one_call_run()
        clock.advance(1)  # one call a second: made at 0 s, 1 s ... 14 s

    refused = await refusal()

    # It is second 15 now, and the oldest call leaves the minute at second 60.
    assert refused.wait_seconds == 45
    assert refused.retryable is True
    assert refused.provider is None
    assert "45 seconds" in refused.message

    clock.advance(44.5)
    assert (await refusal()).wait_seconds == 1  # half a second is rounded up, never to 0
    clock.advance(0.5)
    await budget.admit_visitor(Run(calls=1))  # the minute has freed a place


@pytest.mark.asyncio
async def test_a_wait_with_a_fraction_is_rounded_up(clock: Clock) -> None:
    """Told 44 s for a wait of 44.3 s, a caller comes back too early and is refused again."""
    spend(MINUTE_CEILING)
    clock.advance(15.7)

    assert (await refusal()).wait_seconds == 45


@pytest.mark.asyncio
async def test_a_minute_refusal_names_a_minute_at_most_whatever_the_clock_does(
    clock: Clock,
) -> None:
    """The host's clock was put back, so the calls of the last minute look younger than they are."""
    spend(MINUTE_CEILING)
    clock.advance(-30)

    # Not 90: to the page, more than 60 seconds means "today's share is used up".
    assert (await refusal()).wait_seconds == 60


@pytest.mark.asyncio
async def test_a_run_needs_room_for_every_call_it_may_make(clock: Clock) -> None:
    """A chain of three may make three calls, so it is not let in to find out halfway."""
    spend(10)
    clock.advance(20)
    spend(3)  # 13 made: 10 of them 20 s ago, 3 just now

    refused = await refusal(calls=3)
    assert refused.wait_seconds == 40  # one of the ten must leave, in 40 s
    await budget.admit_visitor(Run(calls=2))  # 13 + 2 is still within the ceiling


@pytest.mark.asyncio
async def test_the_wait_is_until_enough_calls_have_left(clock: Clock) -> None:
    spend(5)  # at 0 s
    clock.advance(10)
    spend(5)  # at 10 s
    clock.advance(10)
    spend(5)  # at 20 s: the minute is full

    assert (await refusal(calls=1)).wait_seconds == 40  # one of the first five
    assert (await refusal(calls=5)).wait_seconds == 40  # all of the first five
    assert (await refusal(calls=6)).wait_seconds == 50  # and one made at 10 s


@pytest.mark.asyncio
async def test_a_call_is_forgotten_once_it_has_left_the_minute(clock: Clock) -> None:
    """The log's "made in the last 60 s" has to stay true however long the service has run."""
    spend(5)
    clock.advance(61)  # those five have left the minute
    spend(MINUTE_CEILING)

    refused = await refusal()

    assert refused.wait_seconds == 60
    assert refused.detail is not None and "15 made in the last 60 s" in refused.detail


@pytest.mark.asyncio
async def test_places_held_by_runs_under_way_count(clock: Clock) -> None:
    """Fifteen calls promised are fifteen calls: a burst cannot slip in before the first is made."""
    runs = [Run(calls=3) for _ in range(5)]
    for run in runs:
        await budget.admit_visitor(run)  # nothing has been called yet

    refused = await refusal(calls=1)
    # Nobody knows when the runs under way end, or what they hand back. A place that
    # is held counts as a call made now, so the wait named is the longest it can be.
    assert refused.wait_seconds == 60

    budget.release(runs[0])
    assert await free_places() == 3


@pytest.mark.asyncio
async def test_twenty_runs_that_arrive_together_cannot_all_be_let_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check and the hold are one step: no run gets a turn between them.

    All twenty wait for the same reading of the day's count, so all of them reach the
    check in the same instant, which is as close together as requests can arrive.
    """
    answer_now = threading.Event()

    def slow_reader(settings: object) -> Allowance:
        answer_now.wait(2)
        return Allowance(used=0, limit=1000, remaining=1000)

    monkeypatch.setattr(budget, "read_allowance", slow_reader)

    async def one_run() -> bool:
        try:
            await budget.admit_visitor(Run(calls=2))
        except CallLimitError:
            return False
        return True

    runs = [asyncio.create_task(one_run()) for _ in range(20)]
    await asyncio.sleep(0.05)  # all twenty are now waiting for the one reading
    answer_now.set()
    admitted = await asyncio.gather(*runs)

    assert sum(admitted) == 7  # seven runs of two calls are fourteen places: an eighth has no room
    assert budget._held == 14


@pytest.mark.asyncio
async def test_a_finished_run_hands_back_the_places_it_did_not_use(clock: Clock) -> None:
    run = Run(calls=3)
    await budget.admit_visitor(run)
    assert await free_places() == MINUTE_CEILING - 3

    budget.count(run)  # the first model answered, so the other two were never called
    budget.release(run)

    assert await free_places() == MINUTE_CEILING - 1
    budget.release(run)  # handing back twice gives nothing twice
    assert await free_places() == MINUTE_CEILING - 1


@pytest.mark.asyncio
async def test_a_run_with_nothing_live_is_never_refused_and_never_asks(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    readings = allowance(remaining=0, limit=50)  # the day is used up
    spend(MINUTE_CEILING)  # and so is the minute

    await budget.admit_visitor(Run(calls=0))

    assert readings == []


@pytest.mark.asyncio
async def test_a_chain_longer_than_the_ceiling_can_still_run(clock: Clock) -> None:
    """Asking for more than the whole minute would otherwise be refused for ever."""
    run = Run(calls=MINUTE_CEILING + 5)

    await budget.admit_visitor(run)

    assert run.held == MINUTE_CEILING
    assert await free_places() == 0


@pytest.mark.parametrize(
    ("gone_by", "ending"),
    [(59, "Please try again in 1 second."), (58, "Please try again in 2 seconds.")],
)
@pytest.mark.asyncio
async def test_the_busy_sentence_says_how_long_in_plain_words(
    clock: Clock, gone_by: float, ending: str
) -> None:
    spend(MINUTE_CEILING)
    clock.advance(gone_by)

    refused = await refusal()

    assert refused.message.startswith("The service is busy")
    assert refused.message.endswith(ending)


def test_the_wait_a_visitor_is_told_allows_for_a_running_test(clock: Clock) -> None:
    """Otherwise a visitor who waits as long as asked is sent away a second time.

    Fifteen calls in the last minute: four old ones and eleven of five seconds ago.
    With nothing else going on, the oldest call leaving in ten seconds makes room. While
    a test adds a call every six seconds, room only comes when the eleven have left.
    """
    budget._made.extend([NOON - 50, NOON - 40, NOON - 30, NOON - 20] + [NOON - 5] * 11)

    assert budget.seconds_until_room() == 10
    budget.expect_calls_every(6.0)
    assert budget.seconds_until_room() == 55
    # At that moment: the test's ten calls of the last minute, and nothing else. Five places are free.
    budget.expect_calls_every(None)
    assert budget.seconds_until_room() == 10
    # A test's opening burst asks for several places at once, under its own limit.
    assert budget.seconds_until_room(needed=6, limit=budget.MINUTE_LIMIT) == 30


@pytest.mark.asyncio
async def test_a_test_of_the_models_and_visitors_share_the_one_minute(clock: Clock) -> None:
    """A test counts its calls as it sends them, and waits for places that visitors hold."""
    held = Run(calls=3)
    await budget.admit_visitor(held)
    spend(10)  # the test's calls, counted with no run: it holds no places

    assert (await refusal(calls=3)).wait_seconds == 60  # 10 made and 3 held leave room for 2
    # The opening burst of a repair: six at once fit under 18 only when the held places are free.
    assert budget.seconds_until_room(needed=6, limit=budget.MINUTE_LIMIT) == 60
    budget.release(held)
    assert budget.seconds_until_room(needed=6, limit=budget.MINUTE_LIMIT) == 0


@pytest.mark.parametrize(
    ("models", "waited"),
    [
        (3, 0),  # 15 calls in the minute and 3 more make 18: they go out at once
        (4, 40),  # 4 more would make 19: they wait until the first ten calls have left
    ],
)
@pytest.mark.asyncio
async def test_the_opening_burst_of_a_test_waits_for_room_in_the_minute(
    models: int, waited: int, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair sends its first round at once, but not into a minute that is already full."""
    began: list[float] = []

    async def runner(tested: list[str], **how: Any) -> None:
        began.append(clock.now)  # the moment the first requests would go out

    async def time_passes(seconds: float) -> None:
        clock.advance(seconds)

    monkeypatch.setattr(model_testing.benchmark, "run", runner)
    monkeypatch.setattr(asyncio, "sleep", time_passes)
    settings = get_settings()
    tested = [f"vendor/model-{number}:free" for number in range(models)]
    record = model_testing.new_record(
        "failure", {"code": "gone", "models": []}, MODEL_CHAIN, MODEL_CHAIN, [], tested, tested, [],
        settings,
    )
    spend(10)  # calls made at noon
    clock.advance(20)
    spend(5)  # and five more, twenty seconds later: fifteen in the minute
    asked_at = clock.now

    await model_testing.run_test(record, settings, on_first_right=lambda model: None)

    assert began == [asked_at + waited]
    assert budget._expected_gap is None  # and the test said so when it was over


@pytest.mark.asyncio
async def test_a_minute_that_stays_full_does_not_hold_a_test_back_for_ever(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller who keeps asking keeps the minute full. After a minute of waiting the
    test goes at its pace, one request at a time, instead of waiting for a whole round."""
    began: list[tuple[float, int]] = []

    async def runner(tested: list[str], **how: Any) -> None:
        began.append((clock.now, how["burst"]))

    spend(MINUTE_CEILING)
    asked_at = clock.now

    async def time_passes(seconds: float) -> None:
        clock.advance(seconds)
        assert clock.now - asked_at <= 300, "the test is still waiting after five minutes"
        budget._made.clear()
        spend(MINUTE_CEILING)  # whoever keeps asking has filled the minute again

    monkeypatch.setattr(model_testing.benchmark, "run", runner)
    monkeypatch.setattr(asyncio, "sleep", time_passes)
    settings = get_settings()
    tested = [f"vendor/model-{number}:free" for number in range(6)]
    record = model_testing.new_record(
        "failure", {"code": "gone", "models": []}, MODEL_CHAIN, MODEL_CHAIN, [], tested, tested, [],
        settings,
    )

    await model_testing.run_test(record, settings, on_first_right=lambda model: None)

    assert began == [(asked_at + 60, 1)]


# ---------------------------------------------------------------------------
# Budget - the day's share
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "remaining", "admitted"),
    [
        (1000, 201, True),  # an account with credits: visitors stop at a fifth, 200
        (1000, 200, False),
        (50, 11, True),  # an account without credits: a fifth is 10
        (50, 10, False),
        (50, 0, False),
        (51, 11, True),  # a fifth of 51 is 10.2: "that or less" means 10 and less
        (51, 10, False),
    ],
)
@pytest.mark.asyncio
async def test_visitors_stop_when_a_fifth_of_the_day_is_left(
    clock: Clock,
    allowance: Callable[..., list[None]],
    limit: int,
    remaining: int,
    admitted: bool,
) -> None:
    allowance(remaining=remaining, limit=limit)

    if admitted:
        await budget.admit_visitor(Run(calls=1))
    else:
        refused = await refusal()
        # The clock stands at noon UTC: the wait is the real one, until midnight.
        assert refused.wait_seconds == SECONDS_TO_MIDNIGHT_AT_NOON
        assert refused.retryable is True
        assert refused.message == DAY_USED


@pytest.mark.asyncio
async def test_a_run_needs_every_call_it_may_make_above_the_fifth(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """12 left and a fifth of 10: a chain of three could take the day down to 9."""
    allowance(remaining=12, limit=50)

    refused = await refusal(calls=3)
    assert refused.message == DAY_USED
    assert refused.detail is not None and refused.detail.endswith("(10): 3 asked for")
    await budget.admit_visitor(Run(calls=2))  # two calls end at the fifth, not under it


@pytest.mark.asyncio
async def test_calls_made_since_the_reading_are_subtracted(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """OpenRouter is asked once a minute at most; in between, this process does the sum."""
    readings = allowance(remaining=13, limit=50)  # three above the fifth that is kept

    for _ in range(3):
        await one_call_run()

    assert (await refusal()).wait_seconds == SECONDS_TO_MIDNIGHT_AT_NOON
    assert len(readings) == 1


@pytest.mark.asyncio
async def test_places_promised_to_runs_under_way_are_not_left(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """Otherwise a burst of runs, each seeing 13 left, would walk through what is kept."""
    allowance(remaining=13, limit=50)
    first = Run(calls=3)
    await budget.admit_visitor(first)  # 13 left, and 3 of them promised

    await refusal()

    budget.release(first)  # it ended without calling anything
    await budget.admit_visitor(Run(calls=1))


@pytest.mark.asyncio
async def test_openrouter_is_asked_once_a_minute_at_most_and_only_when_a_run_needs_it(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    readings = allowance(remaining=900)
    assert readings == []  # a budget that nobody has asked has asked nobody

    await budget.admit_visitor(Run(calls=0))  # nothing live: no need to know
    assert readings == []

    for _ in range(4):
        await budget.admit_visitor(Run(calls=1))
        clock.advance(15)  # at 0 s, 15 s, 30 s and 45 s
    assert len(readings) == 1

    clock.advance(15)  # a minute after the first reading
    await budget.admit_visitor(Run(calls=1))
    assert len(readings) == 2

    clock.advance(600)  # ten minutes in which nobody asks: nothing runs on a timer
    assert len(readings) == 2


@pytest.mark.asyncio
async def test_openrouter_is_not_asked_again_half_a_second_before_the_minute_is_over(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    readings = allowance(remaining=900)
    await budget.admit_visitor(Run(calls=1))

    clock.advance(59.5)
    await budget.admit_visitor(Run(calls=1))
    assert len(readings) == 1
    clock.advance(0.5)
    await budget.admit_visitor(Run(calls=1))
    assert len(readings) == 2


def test_a_reading_may_hold_a_run_up_for_four_seconds_at_most() -> None:
    """Shorter than a visitor's patience, and longer than one slow step of the request."""
    assert budget.READ_DEADLINE_SECONDS == 4.0 > model_catalogue.LOOKUP_TIMEOUT_SECONDS
    assert budget.READ_EVERY_SECONDS == 60.0


@pytest.mark.asyncio
async def test_with_no_count_for_today_visitors_get_twenty_live_calls(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """Without the day's figure this is the only brake, so it is a small number."""
    readings = allowance(None)
    assert UNKNOWN_LIVE_CALLS == 20

    for _ in range(UNKNOWN_LIVE_CALLS):
        await one_call_run()
        clock.advance(10)  # slowly, so that the minute is never what refuses

    refused = await refusal()
    assert refused.message == DAY_UNKNOWN
    assert refused.wait_seconds == 60 and refused.retryable is True
    assert refused.detail is not None and "20 live calls without one: 20 made" in refused.detail
    # Asked at 0 s, 60 s, 120 s and 180 s: a reading that failed waits a minute too.
    assert len(readings) == 4

    clock.advance(3600)  # the twenty are per start of the service, not per hour
    assert (await refusal()).message == DAY_UNKNOWN

    allowance(remaining=900)  # OpenRouter answers again
    clock.advance(60)
    await one_call_run()


@pytest.mark.asyncio
async def test_the_twenty_count_places_that_are_held_and_no_call_of_a_test(
    clock: Clock, allowance: Callable[..., list[None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    allowance(None)
    monkeypatch.setattr(budget, "MINUTE_CEILING", 1000)  # only the day is under test here
    spend(50)  # a test of the models: the repair goes on when OpenRouter cannot say

    runs = [Run(calls=3) for _ in range(6)]
    for run in runs:
        await budget.admit_visitor(run)  # eighteen places promised, nothing called yet

    assert (await refusal(calls=3)).message == DAY_UNKNOWN  # a seventh could make it twenty-one
    await budget.admit_visitor(Run(calls=2))  # twenty exactly


@pytest.mark.asyncio
async def test_calls_made_while_the_day_was_known_are_not_among_the_twenty(
    clock: Clock, allowance: Callable[..., list[None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The twenty are for the time without a count, however busy the day was before it."""
    monkeypatch.setattr(budget, "MINUTE_CEILING", 1000)  # only the day is under test here
    clock.now = NOON + SECONDS_TO_MIDNIGHT_AT_NOON - 30  # 23:59:30 UTC
    allowance(remaining=900)
    for _ in range(UNKNOWN_LIVE_CALLS + 5):
        await one_call_run()
    assert budget._unknown_calls == 0

    clock.advance(31)  # after midnight UTC that count says nothing about the new day
    for _ in range(UNKNOWN_LIVE_CALLS):
        await one_call_run()
    assert (await refusal()).message == DAY_UNKNOWN


@pytest.mark.asyncio
async def test_where_nobody_counts_the_day_only_the_minute_holds_a_visitor_back(
    clock: Clock, allowance: Callable[..., list[None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With another address in OPENROUTER_BASE_URL no count will ever come.

    The twenty are a brake for the time until a count can be read. Where none can,
    they would refuse every run after the twentieth until the service is started again.
    """
    assert budget.day_is_counted(get_settings()) is True
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://llm.example.com/v1")
    extractors.reset_extractors()
    assert budget.day_is_counted(get_settings()) is False
    allowance(None)  # what the real reader answers there, without asking anybody

    for _ in range(UNKNOWN_LIVE_CALLS + 5):
        await one_call_run()
        clock.advance(10)  # slowly, so that the minute is never what refuses

    clock.advance(60)  # an empty minute: the per-minute ceiling is still there
    for _ in range(MINUTE_CEILING):
        await one_call_run()
    assert (await refusal()).message == BUSY.format(wait="60 seconds")


@pytest.mark.asyncio
async def test_a_used_up_day_is_what_a_visitor_is_told_even_in_a_full_minute(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """ "Try again in 60 seconds" would send the visitor into the same wall a minute later."""
    allowance(remaining=10, limit=50)
    spend(MINUTE_CEILING)

    refused = await refusal()

    assert refused.wait_seconds == SECONDS_TO_MIDNIGHT_AT_NOON
    assert "today's share" in refused.message


@pytest.mark.asyncio
async def test_a_reading_that_fails_does_not_erase_the_last_good_one(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """Within one day the count only goes down, so a refusal that was right stays right."""
    allowance(remaining=10, limit=50)
    await refusal()

    readings = allowance(None)  # from now on OpenRouter cannot be read
    clock.advance(61)

    refused = await refusal()
    assert refused.wait_seconds == SECONDS_TO_MIDNIGHT_AT_NOON - 61
    assert refused.message == DAY_USED  # and not the sentence for a day that is unknown
    assert len(readings) == 1  # it did try again


@pytest.mark.asyncio
async def test_yesterdays_count_is_not_todays(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """OpenRouter's counter restarts at midnight UTC, so a reading from before it is void."""
    clock.now = NOON + SECONDS_TO_MIDNIGHT_AT_NOON - 30  # 23:59:30 UTC
    readings = allowance(remaining=0, limit=50)

    assert (await refusal()).wait_seconds == 30

    clock.advance(31)  # 00:00:01: a new day, and still less than a minute since the reading
    await one_call_run()  # no count for today yet: one of the twenty
    assert len(readings) == 1  # "at most once a minute" holds across midnight too
    assert budget._unknown_calls == 1

    clock.advance(30)
    await refusal()  # the new reading still says 0 in this test
    assert len(readings) == 2


@pytest.mark.asyncio
async def test_runs_that_arrive_during_a_reading_wait_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three runs at once must not ask three times, nor be let in before the answer."""
    asked: list[None] = []
    answer_now = threading.Event()

    def slow_reader(settings: object) -> Allowance:
        asked.append(None)
        answer_now.wait(2)
        return Allowance(used=40, limit=50, remaining=10)  # a fifth is left: visitors stop

    monkeypatch.setattr(budget, "read_allowance", slow_reader)

    async def one_run() -> Optional[CallLimitError]:
        try:
            await budget.admit_visitor(Run(calls=1))
        except CallLimitError as exc:
            return exc
        return None

    runs = [asyncio.create_task(one_run()) for _ in range(3)]
    for _ in range(200):  # until the reading has begun; two seconds at the very most
        if asked:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)  # time for the other two runs to arrive
    # All three are waiting on the one reading, which blocks a worker thread and not
    # the event loop: this test got its turn back while OpenRouter "was answering".
    assert len(asked) == 1 and not any(run.done() for run in runs)
    answer_now.set()
    outcomes = await asyncio.gather(*runs)

    assert len(asked) == 1
    assert all(isinstance(outcome, CallLimitError) for outcome in outcomes)


@pytest.mark.asyncio
async def test_a_visitor_who_goes_away_does_not_take_the_reading_along(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page cancels a run when its visitor leaves. The count is everybody's."""
    asked: list[None] = []
    answer_now = threading.Event()

    def slow_reader(settings: object) -> Allowance:
        asked.append(None)
        answer_now.wait(2)
        return Allowance(used=50, limit=50, remaining=0)  # nothing is left: visitors stop

    monkeypatch.setattr(budget, "read_allowance", slow_reader)
    first = asyncio.create_task(budget.admit_visitor(Run(calls=1)))
    second = asyncio.create_task(budget.admit_visitor(Run(calls=1)))
    for _ in range(200):  # until the reading has begun; two seconds at the very most
        if asked:
            break
        await asyncio.sleep(0.01)
    first.cancel()  # the visitor whose run started the reading goes away
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.sleep(0.02)
    assert not second.done()  # the other run still waits for the same reading
    answer_now.set()

    with pytest.raises(CallLimitError):
        await second
    assert (await refusal()).message == DAY_USED  # and the answer was kept for whoever comes next
    assert len(asked) == 1


@pytest.mark.asyncio
async def test_calls_made_while_openrouter_answers_stay_in_the_sum(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its number was counted before those calls were made, so they are missing from it."""
    asked: list[None] = []
    answer_now = threading.Event()

    def slow_reader(settings: object) -> Allowance:
        asked.append(None)
        answer_now.wait(2)
        return Allowance(used=30, limit=50, remaining=20)

    early = Run(calls=3)
    await budget.admit_visitor(early)  # let in on the full day every test starts with
    budget.count(early)  # one call before the reading: OpenRouter's number will hold it
    clock.advance(61)
    monkeypatch.setattr(budget, "read_allowance", slow_reader)

    asking = asyncio.create_task(budget.why_no_test("failure", 18))
    for _ in range(200):  # until the reading has begun; two seconds at the very most
        if asked:
            break
        await asyncio.sleep(0.01)
    budget.count(early)  # two more calls leave while OpenRouter is answering
    budget.count(early)
    budget.release(early)
    answer_now.set()

    assert await asking is None  # 20 less those two: a test of 18 requests still fits
    assert await budget.why_no_test("failure", 19) == "allowance"


@pytest.mark.asyncio
async def test_a_reading_that_breaks_fails_no_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever goes wrong while reading, a visitor's run goes on under the minute's rule."""

    def broken(settings: object) -> Allowance:
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(budget, "read_allowance", broken)

    with caplog.at_level("WARNING", logger="app.budget"):
        await budget.admit_visitor(Run(calls=1))

    assert any("did not finish (RuntimeError)" in r.getMessage() for r in caplog.records)
    assert budget._allowance is None


@pytest.mark.asyncio
async def test_a_reading_that_hangs_is_given_up_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    release = threading.Event()
    asked: list[None] = []

    def stuck(settings: object) -> Allowance:
        asked.append(None)
        release.wait(2)  # far longer than the deadline below
        # Had the run waited for this, it would have been refused.
        return Allowance(used=50, limit=50, remaining=0)

    monkeypatch.setattr(budget, "read_allowance", stuck)
    monkeypatch.setattr(budget, "READ_DEADLINE_SECONDS", 0.05)

    try:
        with caplog.at_level("WARNING", logger="app.budget"):
            await budget.admit_visitor(Run(calls=1))
            await budget.admit_visitor(Run(calls=1))  # and it is not asked again within the minute
    finally:
        release.set()  # let the worker thread finish

    assert any("did not finish (TimeoutError)" in r.getMessage() for r in caplog.records)
    assert len(asked) == 1
    assert budget._allowance is None  # the answer that came too late is not used


def test_the_refusal_sentences_name_no_setting() -> None:
    """All three are read by visitors. What the admin must do belongs in the server log."""
    assert (budget._BUSY, budget._DAY_USED) == (BUSY, DAY_USED)
    assert budget._DAY_UNKNOWN.format(wait="60 seconds") == DAY_UNKNOWN
    for sentence in (budget._BUSY, budget._DAY_USED, budget._DAY_UNKNOWN):
        assert not re.search(r"[A-Z]{2,}_[A-Z]", sentence), sentence  # no SETTING_NAME
        assert "setting" not in sentence.lower() and ".env" not in sentence, sentence


# ---------------------------------------------------------------------------
# Budget - reading the day's count
# ---------------------------------------------------------------------------


def answering(status: int, body: Any) -> Any:
    """A stand-in for the request that carries the key. It records what it was asked."""
    calls: list[tuple[str, str]] = []

    def fetch(url: str, key: str) -> tuple[int, str]:
        calls.append((url, key))
        return status, body if isinstance(body, str) else json.dumps(body)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def with_counts(**numbers: Any) -> dict[str, Any]:
    """An answer of the documented shape that holds only these numbers."""
    return {"data": {"free_model_daily_requests": numbers}}


#: Python reads these as JSON numbers, and neither is a count.
WITHOUT_END = '{"data": {"free_model_daily_requests": {"used": 0, "limit": %s, "remaining": 5}}}'


def test_the_count_is_read_from_openrouters_answer() -> None:
    fetch = answering(200, KEY_ANSWER)

    assert read_allowance(get_settings(), fetch) == Allowance(used=12, limit=50, remaining=38)
    assert fetch.calls == [("https://openrouter.ai/api/v1/key", "test-openrouter-key")]


def test_only_the_three_numbers_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The answer names the key and the account. Neither may reach a log, nor may the key."""
    with caplog.at_level("DEBUG"):
        read_allowance(get_settings(), answering(200, KEY_ANSWER))

    logged = [record.getMessage() for record in caplog.records]
    assert logged == ["OpenRouter counts 12 of 50 free requests used today; 38 left."]
    assert caplog.records[0].levelno == logging.INFO
    assert not [secret for secret in NEVER_LOGGED if secret in logged[0]]


@pytest.mark.parametrize(
    ("status", "body", "says"),
    [
        (401, {"error": {"code": 401, "message": "User not found."}}, "OPENROUTER_API_KEY"),
        (403, {"error": {"code": 403, "message": "Key disabled"}}, "OPENROUTER_API_KEY"),
        (500, {"error": {"code": 500, "message": "Internal Server Error"}}, "HTTP 500"),
        (429, {"error": {"message": "slow down"}}, "HTTP 429"),
        (302, "", "HTTP 302"),  # a redirect is never followed, so this is all there is
        (200, "<html>maintenance</html>", "unexpected answer"),
        (200, {"data": {"label": "sk-or-v1-au7...890"}}, "unexpected answer"),
        (200, with_counts(used=1, limit=50), "unexpected answer"),
        (200, with_counts(used="a", limit="b", remaining="c"), "unexpected answer"),
        (200, with_counts(used=0, limit=0, remaining=0), "unexpected answer"),
        (200, WITHOUT_END % "Infinity", "unexpected answer"),
        (200, WITHOUT_END % "1e999", "unexpected answer"),
        (200, {"data": []}, "unexpected answer"),
        (200, [1, 2, 3], "unexpected answer"),
        (200, "[" * 200_000 + "]" * 200_000, "unexpected answer"),
        # The numbers are right but the status is not: an answer nobody documented.
        (203, KEY_ANSWER, "HTTP 203"),
    ],
    ids=[
        "key-rejected",
        "key-forbidden",
        "server-error",
        "rate-limited",
        "redirect",
        "not-json",
        "no-count",
        "count-incomplete",
        "count-not-numbers",
        "limit-of-zero",
        "a-number-without-end",
        "a-number-too-large",
        "wrong-shape",
        "a-list",
        "nested-absurdly-deep",
        "odd-status",
    ],
)
def test_any_other_answer_means_the_count_cannot_be_read(
    status: int, body: Any, says: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG"):
        assert read_allowance(get_settings(), answering(status, body)) is None

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert says in record.getMessage()
    # Why it failed is logged; what OpenRouter sent is not, and the key never is.
    for secret in (*NEVER_LOGGED, "User not found", "slow down"):
        assert secret not in record.getMessage()


def test_a_count_that_cannot_be_read_is_logged_with_what_follows_from_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The admin reads here why visitors may soon be refused; the page only says "not now"."""
    with caplog.at_level("WARNING", logger="app.budget"):
        read_allowance(get_settings(), answering(500, ""))

    assert [record.getMessage() for record in caplog.records] == [
        "OpenRouter's count of today's free requests could not be read (unexpected answer, "
        "HTTP 500). The per-minute ceiling still applies, and so does the last count that was "
        "read today. With no count for today, visitors get at most 20 live calls and the page "
        "cannot start a test."
    ]


def test_network_trouble_means_the_count_cannot_be_read(caplog: pytest.LogCaptureFixture) -> None:
    def fetch(url: str, key: str) -> tuple[int, str]:
        raise TimeoutError(f"timed out talking to {url} with {key}")

    with caplog.at_level("DEBUG"):
        assert read_allowance(get_settings(), fetch) is None

    (record,) = caplog.records
    assert "TimeoutError" in record.getMessage()
    assert "test-openrouter-key" not in record.getMessage()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://openrouter.ai/api/v1",  # not the encrypted address
        "https://openrouter.ai.example.com/api/v1",  # only looks like OpenRouter
        "https://openrouter.ai@example.com/api/v1",  # OpenRouter's name as a user name
        "https://openrouter.ai\uff0fexample.com/api/v1",  # a look-alike slash: no address at all
        "https://api.example.com/v1",  # another provider altogether
    ],
)
def test_the_key_goes_to_openrouters_own_encrypted_address_and_nowhere_else(
    base_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", base_url)
    extractors.reset_extractors()
    fetch = answering(200, KEY_ANSWER)

    with caplog.at_level("DEBUG"):
        assert read_allowance(get_settings(), fetch) is None

    assert fetch.calls == []
    assert caplog.records == []  # nobody was asked, so there is nothing to report


def test_without_a_key_nobody_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    extractors.reset_extractors()
    fetch = answering(200, KEY_ANSWER)

    assert read_allowance(get_settings(), fetch) is None
    assert fetch.calls == []
    assert budget.day_is_counted(get_settings()) is False  # OpenRouter counts for a key


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


def test_the_request_carries_the_key_and_reads_a_bounded_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = FakeAnswer(200, b"x" * (model_catalogue.MAX_ANSWER_BYTES + 10))
    seen: list[tuple[Any, float]] = []

    class Opener:
        def open(self, request: Any, timeout: float) -> FakeAnswer:
            seen.append((request, timeout))
            return answer

    monkeypatch.setattr(budget, "_OPENER", Opener())

    status, body = budget._get_with_key("https://openrouter.ai/api/v1/key", "a-key")

    ((request, timeout),) = seen
    assert request.full_url == "https://openrouter.ai/api/v1/key"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer a-key"
    assert request.get_header("User-agent") == model_catalogue.USER_AGENT
    assert timeout == model_catalogue.LOOKUP_TIMEOUT_SECONDS
    assert answer.asked_for == [model_catalogue.MAX_ANSWER_BYTES]
    assert status == 200 and len(body) == model_catalogue.MAX_ANSWER_BYTES


def test_a_rejected_key_is_an_answer_to_read_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked_for: list[int] = []

    class Body(io.BytesIO):
        def read(self, amount: Optional[int] = -1) -> bytes:
            asked_for.append(amount if amount is not None else -1)
            return super().read(amount)

    body = json.dumps({"error": {"code": 401, "message": "User not found."}}).encode()

    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            address = request.full_url
            raise urllib.error.HTTPError(address, 401, "Unauthorized", Message(), Body(body))

    monkeypatch.setattr(budget, "_OPENER", Opener())

    assert budget._get_with_key("https://openrouter.ai/api/v1/key", "a-key")[0] == 401
    assert asked_for == [model_catalogue.MAX_ANSWER_BYTES]  # an error's body is bounded too
    assert read_allowance(get_settings()) is None


def test_the_whole_reading_through_the_real_request(monkeypatch: pytest.MonkeyPatch) -> None:
    class Opener:
        def open(self, request: Any, timeout: float) -> FakeAnswer:
            return FakeAnswer(200, json.dumps(KEY_ANSWER).encode())

    monkeypatch.setattr(budget, "_OPENER", Opener())

    assert read_allowance(get_settings()) == Allowance(used=12, limit=50, remaining=38)


@pytest.mark.parametrize(
    "target",
    [
        "https://openrouter.ai/api/v1/auth/key",  # even OpenRouter's own host
        "https://example.com/api/v1/key",
        "http://openrouter.ai/api/v1/key",
    ],
)
def test_a_request_that_carries_the_key_never_follows_a_redirect(target: str) -> None:
    handler = budget._NoRedirects()
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key", headers={"Authorization": "Bearer a-key"}
    )

    assert handler.redirect_request(request, None, 302, "Found", Message(), target) is None


def test_the_opener_the_service_ships_is_the_one_that_follows_no_redirect() -> None:
    redirect_handlers = [
        handler
        for handler in REAL_OPENER.handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]
    assert [type(handler) for handler in redirect_handlers] == [budget._NoRedirects]
    # And it is not the catalogue's, which follows a redirect on OpenRouter's own host.
    assert not isinstance(redirect_handlers[0], model_catalogue._SameHostRedirects)


# ---------------------------------------------------------------------------
# Budget - who else may spend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("by", "limit", "remaining", "planned", "refused"),
    [
        # The button: three fifths of the day must be left, and never less than the test itself.
        ("page", 1000, 600, 42, None),
        ("page", 1000, 599, 42, "allowance"),
        ("page", 50, 30, 14, None),
        ("page", 50, 29, 14, "allowance"),
        ("page", 50, 41, 42, "allowance"),
        # The scheduled job: its test must leave a fifth of the day behind it.
        ("schedule", 1000, 242, 42, None),
        ("schedule", 1000, 241, 42, "allowance"),
        ("schedule", 50, 24, 14, None),
        ("schedule", 50, 23, 14, "allowance"),
        # The service's own repair: its requests only have to fit.
        ("failure", 50, 9, 9, None),
        ("failure", 50, 8, 9, "allowance"),
    ],
)
@pytest.mark.asyncio
async def test_a_test_of_the_models_starts_only_within_its_share_of_the_day(
    clock: Clock,
    allowance: Callable[..., list[None]],
    by: str,
    limit: int,
    remaining: int,
    planned: int,
    refused: Optional[str],
) -> None:
    allowance(remaining=remaining, limit=limit)

    assert await budget.why_no_test(by, planned) == refused


@pytest.mark.asyncio
async def test_only_the_button_is_refused_when_openrouter_cannot_say(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """A stranger may press the button; the repair and the job are the owner's own upkeep."""
    readings = allowance(None)

    assert await budget.why_no_test("page", 14) == "allowance_unknown"
    assert await budget.why_no_test("failure", 14) is None
    assert await budget.why_no_test("schedule", 14) is None
    assert len(readings) == 1  # three spenders within a minute, one question


@pytest.mark.asyncio
async def test_a_spender_is_told_what_is_left_after_calls_made_and_places_held(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    allowance(remaining=38, limit=50)
    assert await budget.why_no_test("failure", 38) is None

    run = Run(calls=3)
    await budget.admit_visitor(run)
    budget.count(run)

    # 38 - 1 - 2: one call made, two places promised.
    assert await budget.why_no_test("failure", 35) is None
    assert await budget.why_no_test("failure", 36) == "allowance"
    budget.release(run)
    assert await budget.why_no_test("failure", 37) is None
    spend(7)  # a test's own calls come off as well
    assert await budget.why_no_test("failure", 31) == "allowance"

    clock.advance(60)
    allowance(remaining=30, limit=50)  # OpenRouter's own count, which has those calls in it
    assert await budget.why_no_test("failure", 30) is None  # so a new reading starts the sum again


@pytest.mark.asyncio
async def test_the_button_needs_a_count_that_was_just_read(
    clock: Clock, allowance: Callable[..., list[None]]
) -> None:
    """An older count of today is enough to say no on, and for the owner's own upkeep."""
    allowance(remaining=45, limit=50)
    assert await budget.why_no_test("page", 14) is None

    allowance(None)  # from now on OpenRouter cannot be read
    clock.advance(60)  # a minute to the second: the count is no longer "just read"

    assert await budget.why_no_test("page", 14) == "allowance_unknown"
    assert await budget.why_no_test("failure", 14) is None
    assert await budget.why_no_test("schedule", 14) is None
    spend(20)  # 25 left by this process's own sum: under three fifths, whatever OpenRouter says now
    assert await budget.why_no_test("page", 14) == "allowance"


# ---------------------------------------------------------------------------
# Budget - through the service
# ---------------------------------------------------------------------------

PAYLOAD = {"text": SAMPLE_INVOICE_TEXT}


def refusal_body(message: str) -> dict[str, Any]:
    """The envelope of the safety net for ExtractionError, with the refusal's own words."""
    return {
        "success": False,
        "error": "providers_unavailable",
        "message": message,
        "detail": {
            "provider": None,
            "error_type": "CallLimitError",
            "message": message,
            "retryable": True,
        },
    }


@pytest.mark.asyncio
async def test_a_full_minute_is_refused_inside_the_public_contract(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = router(default=ok())
    spend(MINUTE_CEILING)
    clock.advance(23)

    with caplog.at_level("WARNING", logger="app.main"):
        response = await http.post(
            "/api/v1/extract", json=PAYLOAD, headers={"X-Request-ID": "req-refused-1"}
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "37"  # the real wait: 60 s less the 23 gone by
    assert response.json() == refusal_body(BUSY.format(wait="37 seconds"))
    assert response.headers["x-request-id"] == "req-refused-1"
    assert fake.calls == []  # refused before the chain started
    # The admin's version is in the log, under the request's id and with the numbers.
    (line,) = [record.getMessage() for record in caplog.records]
    assert line.startswith("request req-refused-1: refused before any model was called")
    assert "15 made in the last 60 s" in line


@pytest.mark.asyncio
async def test_a_used_up_day_is_refused_until_midnight_utc(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    clock: Clock,
    allowance: Callable[..., list[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = router(default=ok())
    allowance(remaining=10, limit=50)

    with caplog.at_level("WARNING", logger="app.main"):
        response = await http.post("/api/v1/extract", json=PAYLOAD)

    assert response.status_code == 503
    assert response.headers["retry-after"] == str(SECONDS_TO_MIDNIGHT_AT_NOON)
    assert response.json() == refusal_body(DAY_USED)
    assert fake.calls == []
    (line,) = [record.getMessage() for record in caplog.records]
    assert "10 of today's 50 free requests are left" in line and "20% of the day (10)" in line


@pytest.mark.asyncio
async def test_a_day_that_cannot_be_read_lets_twenty_calls_through_the_service(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    clock: Clock,
    allowance: Callable[..., list[None]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OPENROUTER_MODELS", PRIMARY_MODEL)  # a chain of one: a run is one call
    extractors.reset_extractors()
    fake = router(default=ok())
    allowance(None)
    monkeypatch.setattr(budget, "MINUTE_CEILING", 1000)  # only the day is under test here

    statuses = [
        (await http.post("/api/v1/extract", json=PAYLOAD)).status_code
        for _ in range(UNKNOWN_LIVE_CALLS)
    ]
    with caplog.at_level("WARNING", logger="app.main"):
        response = await http.post("/api/v1/extract", json=PAYLOAD)

    assert statuses == [200] * UNKNOWN_LIVE_CALLS and len(fake.calls) == UNKNOWN_LIVE_CALLS
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.json() == refusal_body(DAY_UNKNOWN)
    (line,) = [record.getMessage() for record in caplog.records]
    assert "no count of today's free requests could be read from OpenRouter" in line


@pytest.mark.parametrize("ceiling", ["minute", "day", "day unknown"])
@pytest.mark.asyncio
async def test_the_page_is_told_of_a_refusal_exactly_as_the_api_tells_it(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    clock: Clock,
    allowance: Callable[..., list[None]],
    ceiling: str,
) -> None:
    """The page promises to show the public API's answer; a refusal is no exception."""
    fake = router(default=ok())
    if ceiling == "minute":
        spend(MINUTE_CEILING)
    elif ceiling == "day":
        allowance(remaining=3, limit=50)
    else:
        allowance(None)
        budget._unknown_calls = UNKNOWN_LIVE_CALLS

    public = await http.post("/api/v1/extract", json=PAYLOAD)
    stream = await http.post("/playground/extract", json=PAYLOAD)

    # The stream starts and ends as always; no model was tried in between.
    assert [event["event"] for event in events_of(stream)] == ["start", "result"]
    traced = result_of(stream)
    assert traced["status_code"] == public.status_code == 503
    assert traced["headers"] == {"retry-after": public.headers["retry-after"]}
    assert traced["body"] == public.json()
    assert traced["body"]["detail"]["error_type"] == "CallLimitError"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_minute_refusal_never_asks_for_more_than_a_minute(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    """The page waits a busy minute out and stops for the day: the number tells them apart."""
    router(default=ok())
    spend(MINUTE_CEILING)

    response = await http.post("/api/v1/extract", json=PAYLOAD)

    assert 1 <= int(response.headers["retry-after"]) <= budget.MINUTE_SECONDS == 60


@pytest.mark.parametrize(
    ("behaviours", "default", "status", "calls_made"),
    [
        ({PRIMARY_MODEL: ok()}, None, 200, 1),
        ({PRIMARY_MODEL: timeout_error(), FALLBACK_MODEL: ok()}, None, 200, 2),
        ({}, api_error(openai.InternalServerError, 503), 503, 3),
        ({}, ok(EMPTY_INVOICE_PAYLOAD), 422, 2),  # two verdicts settle it; the third is not asked
        ({PRIMARY_MODEL: {"content": "not json"}, FALLBACK_MODEL: timeout_error()}, ok(), 200, 3),
    ],
    ids=["first-answers", "one-fallback", "all-fail", "not-an-invoice", "last-answers"],
)
@pytest.mark.parametrize("path", ["/api/v1/extract", "/playground/extract"])
@pytest.mark.asyncio
async def test_each_live_call_is_counted_where_it_is_made(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    clock: Clock,
    path: str,
    behaviours: dict[str, Any],
    default: Any,
    status: int,
    calls_made: int,
) -> None:
    """Not one per run and not three per run: what the chain really sent, whatever came back."""
    fake = router(behaviours, default)

    response = await http.post(path, json=PAYLOAD)

    answered = result_of(response)["status_code"] if "playground" in path else response.status_code
    assert answered == status
    assert len(fake.calls) == calls_made
    # Places that were held for models the chain never reached are free again.
    assert await free_places() == MINUTE_CEILING - calls_made


@pytest.mark.asyncio
async def test_simulated_positions_cost_nothing_and_are_never_refused(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    clock: Clock,
    allowance: Callable[..., list[None]],
) -> None:
    fake = router()
    readings = allowance(remaining=0, limit=50)  # the day is used up: a live run is refused
    spend(12)

    response = await http.post(
        "/playground/extract",
        json={"text": SAMPLE_INVOICE_TEXT, "simulate": ["rate_limit", "upstream_error", "auth"]},
    )

    result = result_of(response)
    assert result["status_code"] == 503
    assert isinstance(result["body"]["detail"], list)  # every model "failed": not a refusal
    assert fake.calls == [] and readings == []  # nothing was called, and nobody was asked
    allowance(remaining=900)
    assert await free_places() == 3  # the minute is exactly as full as before


@pytest.mark.asyncio
async def test_only_the_live_positions_of_a_mixed_run_are_counted(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], clock: Clock
) -> None:
    fake = router({FALLBACK_MODEL: ok()})
    spend(MINUTE_CEILING - 2)  # room for the two live positions, and no more

    response = await http.post(
        "/playground/extract", json={"text": SAMPLE_INVOICE_TEXT, "simulate": ["rate_limit"]}
    )

    assert result_of(response)["status_code"] == 200
    assert fake.models_called == [FALLBACK_MODEL]
    assert await free_places() == 1  # one call made, one place handed back


@pytest.mark.asyncio
async def test_an_engine_without_a_key_calls_nothing_and_counts_nothing(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    clock: Clock,
    allowance: Callable[..., list[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    extractors.reset_extractors()
    fake = router(default=ok())
    readings = allowance(remaining=0, limit=50)  # a used-up day must not turn "no key" into "busy"
    spend(12)

    # More often than the twenty calls a day without a count allows: nothing is rationed here.
    for _ in range(UNKNOWN_LIVE_CALLS + 5):
        response = await http.post("/api/v1/extract", json=PAYLOAD)

        assert response.status_code == 503
        assert [entry["error_type"] for entry in response.json()["detail"]] == [
            "ProviderNotConfiguredError"
        ] * len(MODEL_CHAIN)
        assert "retry-after" not in response.headers
    assert fake.calls == [] and readings == []
    allowance(remaining=900)
    assert await free_places() == 3  # nothing was counted, and nothing is still held


@pytest.mark.asyncio
async def test_a_run_that_crashes_hands_its_places_back(
    http_raw: httpx.AsyncClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: ExtractionDispatcher, text: str) -> None:
        raise RuntimeError("unexpected boom")

    monkeypatch.setattr(ExtractionDispatcher, "extract", boom)

    response = await http_raw.post("/api/v1/extract", json=PAYLOAD)

    assert response.status_code == 500
    assert await free_places() == MINUTE_CEILING


@pytest.mark.asyncio
async def test_a_run_that_is_cancelled_hands_its_places_back(
    router: Callable[..., FakeRouter], clock: Clock
) -> None:
    """The page cancels the chain when its visitor goes away."""
    started = asyncio.Event()

    async def never_answers(**_: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    router({PRIMARY_MODEL: never_answers})
    task = asyncio.create_task(budget.extract_invoice(SAMPLE_INVOICE_TEXT))
    # With a limit: a run that never reaches its model must fail this test, not hang it.
    await asyncio.wait_for(started.wait(), timeout=2)
    assert await free_places() == MINUTE_CEILING - len(MODEL_CHAIN)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await free_places() == MINUTE_CEILING - 1  # the one call that was made


@pytest.mark.asyncio
async def test_the_counted_chain_answers_as_the_plain_one_does(
    router: Callable[..., FakeRouter],
) -> None:
    """Wrapping an engine to count its call must change nothing else about it."""
    fake = router({PRIMARY_MODEL: api_error(openai.NotFoundError, 404), FALLBACK_MODEL: ok()})

    plain = await extractors.extract_invoice(SAMPLE_INVOICE_TEXT)
    behind_the_ceiling = await budget.extract_invoice(SAMPLE_INVOICE_TEXT)

    assert behind_the_ceiling == plain
    assert fake.models_called == [PRIMARY_MODEL, FALLBACK_MODEL] * 2
    run = Run()
    engine = CountedExtractor(get_dispatcher().extractors[0], run)
    assert (engine.model, engine.provider, engine.is_configured()) == (PRIMARY_MODEL,) * 2 + (True,)
    assert engine.settings is get_dispatcher().extractors[0].settings
    assert run.calls == 1


@pytest.mark.asyncio
async def test_a_refusal_is_raised_before_the_chain_and_not_by_it(
    router: Callable[..., FakeRouter], clock: Clock
) -> None:
    """Raised inside the chain it would be collected as one failed model after another."""
    fake = router(default=ok())
    spend(MINUTE_CEILING)
    run = Run()
    chain = [CountedExtractor(engine, run) for engine in get_dispatcher().extractors]

    with pytest.raises(CallLimitError) as info:
        await BudgetedDispatcher(chain, run).extract(SAMPLE_INVOICE_TEXT)

    assert isinstance(info.value, ExtractionError)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_the_service_never_asks_openrouter_for_the_count_at_startup(
    allowance: Callable[..., list[None]],
) -> None:
    """On the host every request wakes the service, crawlers included: a start is not a need."""
    readings = allowance(remaining=900)

    async with app.router.lifespan_context(app):
        assert readings == []
        # Nothing was left running that could ask later either.
        assert asyncio.all_tasks() == {asyncio.current_task()}

    assert readings == []


@pytest.mark.asyncio
async def test_no_request_but_a_live_run_asks_openrouter_for_the_days_count(
    http: httpx.AsyncClient, allowance: Callable[..., list[None]]
) -> None:
    """GET /health promises not to call OpenRouter, and the page asks it every three
    seconds while it waits for the service."""
    readings = allowance(remaining=900)

    for path in ("/health", "/", "/playground/config", "/playground/models", "/openapi.json"):
        assert (await http.get(path)).status_code == 200, path
    await http.post("/api/v1/extract", json={"text": "too short"})
    every_position = {"text": SAMPLE_INVOICE_TEXT, "simulate": ["auth"] * len(MODEL_CHAIN)}
    await http.post("/playground/extract", json=every_position)
    await http.post("/playground/models/test")  # this suite names its models: refused from memory

    assert readings == []


@pytest.mark.asyncio
async def test_no_limit_adds_a_status_to_the_public_endpoint(
    http_raw: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    clock: Clock,
    allowance: Callable[..., list[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract of POST /api/v1/extract: no limit may add a 413 or a 429 to this list.

    One status is not in it and is older than the limits: the 400 FastAPI itself gives
    a body it cannot read as JSON at all (tests/test_body_limit.py holds that one).
    """
    seen: dict[str, int] = {}

    async def post(case: str, **request: Any) -> None:
        response = await http_raw.post("/api/v1/extract", **request)
        body = response.json()
        # Every answer that is not a success uses the one envelope.
        if response.status_code != 200:
            assert set(body) == {"success", "error", "message", "detail"}, case
            assert body["success"] is False, case
        seen[case] = response.status_code

    router({PRIMARY_MODEL: ok()})
    await post("an invoice", json=PAYLOAD)
    router({PRIMARY_MODEL: api_error(openai.RateLimitError, 429), FALLBACK_MODEL: ok()})
    await post("an invoice from the fallback", json=PAYLOAD)
    await post("a text that is too short", json={"text": "too short"})
    await post("no body at all")
    json_body = {"Content-Type": "application/json"}
    await post("a body that is not JSON", content=b"{", headers=json_body)
    await post(
        "a body that declares 20 MB",
        content=b"",
        headers={"Content-Type": "application/json", "Content-Length": "20000000"},
    )

    async def two_megabytes() -> Any:
        for _ in range(32):
            yield b"x" * 65_536

    await post("2 MB with no declared length", content=two_megabytes())

    router(default=ok(EMPTY_INVOICE_PAYLOAD))
    await post("not an invoice", json={"text": GARBAGE_TEXT})
    router(default=api_error(openai.RateLimitError, 429))
    await post("every model is rate limited", json=PAYLOAD)
    router(default=api_error(openai.AuthenticationError, 401))
    await post("every model rejects the key", json=PAYLOAD)

    clock.advance(120)  # a fresh minute for the refusals below
    router(default=ok())
    spend(MINUTE_CEILING)
    await post("the minute is full", json=PAYLOAD)
    clock.advance(120)
    allowance(remaining=0, limit=50)
    await post("the day is used up", json=PAYLOAD)
    budget.reset()
    allowance(None)
    budget._unknown_calls = UNKNOWN_LIVE_CALLS
    await post("the day cannot be read", json=PAYLOAD)
    budget.reset()

    async def boom(_: str) -> None:
        raise RuntimeError("unexpected boom")

    monkeypatch.setattr("app.main.extract_invoice", boom)
    await post("a crash", json=PAYLOAD)

    assert seen == {
        "an invoice": 200,
        "an invoice from the fallback": 200,
        "a text that is too short": 422,
        "no body at all": 422,
        "a body that is not JSON": 422,
        "a body that declares 20 MB": 422,
        "2 MB with no declared length": 422,
        "not an invoice": 422,
        "every model is rate limited": 503,
        "every model rejects the key": 503,
        "the minute is full": 503,
        "the day is used up": 503,
        "the day cannot be read": 503,
        "a crash": 500,
    }
    declared = app.openapi()["paths"]["/api/v1/extract"]["post"]["responses"]
    assert {str(code) for code in seen.values()} <= set(declared) == {"200", "422", "500", "503"}

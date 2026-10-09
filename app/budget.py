"""One ceiling on live model calls, for everybody who can make this service call a model.

The service calls models on its owner's OpenRouter account, and both the public API
and the page are open to anyone. Free models come with two limits on that account:
20 requests a minute, and a number of requests per UTC day (50, or 1000 once the
account has bought credits). This module is the one place that says "not now": to a
visitor's live run, to the button that starts a test of the models, to the scheduled
job and to the service's own repair of its chain.

Two counters, and why they live where they live:

* **This minute** is counted here, in memory, from the calls actually made. A minute
  is shorter than anything that could reset it. A visitor's run is admitted only when
  the minute has room for every call it may make (one per model it would really
  call). Those places are held from that moment on, so that twenty requests arriving
  together cannot all be let in, and the run hands back the places it did not use.
* **Today** is OpenRouter's own counter, read with ``GET /key`` (it sends the key,
  calls no model and costs nothing). A free host stops the service after a quarter
  of an hour without visitors and starts it again with an empty memory and an empty
  disk, so a day counted inside the service would start from zero at every wake-up.
  OpenRouter's counter is the only one a sleep cannot erase.

What is left of the day is shared out by three thresholds, so that visitors cannot
use up what the repair needs and the button cannot use up what visitors need.

A refusal is a :class:`CallLimitError`. It is raised before the chain starts, so no
model is called and the dispatcher never walks the chain collecting refusals.
:mod:`app.main` answers it inside the public contract: 503, ``providers_unavailable``
and a ``Retry-After`` that is the real wait.

Calls are counted where they are made, not guessed from the result:
:class:`CountedExtractor` wraps an engine the way the playground's ``TracedExtractor``
does, and :class:`BudgetedDispatcher` is the unmodified dispatcher with the question
asked first. A test of the models counts its own calls as it sends them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from app.config import Settings, get_settings
from app.extractors import BaseExtractor, ExtractionDispatcher, ExtractionError, get_dispatcher
from app.model_catalogue import (
    LOOKUP_TIMEOUT_SECONDS,
    MAX_ANSWER_BYTES,
    USER_AGENT,
    is_openrouter,
)
from app.schemas import InvoiceData

logger = logging.getLogger("app.budget")

#: A visitor's run is admitted only while it leaves the last 60 seconds at this many
#: live calls or fewer, for all visitors together. OpenRouter allows 20 a minute on
#: free models. The difference is left for calls this process cannot count: the
#: benchmark command, or a second copy of the service during a deploy.
MINUTE_CEILING = 15
MINUTE_SECONDS = 60.0
#: A test of the models waits until its opening requests fit under this many calls
#: in a minute. After that it paces itself (``app.model_rules.SECONDS_BETWEEN_STARTS``).
MINUTE_LIMIT = 18
SECONDS_PER_DAY = 86_400

#: Visitors' live runs stop once this share of the day's limit, or less, is left.
#: The rest is kept for the service's own repair and for the scheduled test.
VISITOR_SHARE = 0.20
#: The button on the page works only while at least this share of the day is left.
BUTTON_SHARE = 0.60
#: The scheduled job leaves at least this share of the day after its test.
SCHEDULE_SHARE = 0.20

#: OpenRouter is asked for the day's count at most this often, and only when a
#: spender needs the answer: never at startup and never on a timer.
READ_EVERY_SECONDS = 60.0
#: The whole reading gets this long, so it cannot hold up a visitor's run for longer.
READ_DEADLINE_SECONDS = 4.0
#: Live calls visitors get between two starts of the service while there is no count
#: for today at all. Small on purpose: without the day's figure this is the only brake.
#: It holds only where a count exists and could not be read (:func:`day_is_counted`).
UNKNOWN_LIVE_CALLS = 20

#: ``fetch(url, key)`` returns ``(http_status, body_text)``. Tests pass a fake.
KeyedFetch = Callable[[str, str], tuple[int, str]]

# What a refused caller reads. All three reach visitors, so they name no setting.
_BUSY = (
    "The service is busy: it has reached its ceiling of live model calls per minute, "
    "counted for all visitors together. Please try again in {wait}."
)
_DAY_USED = (
    "The service has used today's share of live model calls, counted for all visitors "
    "together. Please try again after midnight UTC."
)
_DAY_UNKNOWN = (
    "The service cannot check today's free allowance right now, so it has stopped "
    "calling models for the moment. Please try again in {wait}."
)


class CallLimitError(ExtractionError):
    """A run was refused before any model was called, because a ceiling is reached.

    ``message`` is written for the visitor and ``detail`` for the server log.
    ``wait_seconds`` is the real wait in whole seconds: until the minute frees a place,
    until OpenRouter can be asked for the day's count again, or until midnight UTC
    when the day's share is used.
    """

    def __init__(self, message: str, *, wait_seconds: int, detail: str) -> None:
        super().__init__(message, detail=detail, retryable=True)
        self.wait_seconds = wait_seconds


def _whole_seconds(wait: float) -> int:
    """Rounded up and never 0: "retry in 0 seconds" would invite a retry that fails.

    Up, because a caller told 44 seconds for a wait of 44.3 comes back too early. Every
    wait handed in is above 0 already, so the 1 is a belt.
    """
    return max(1, math.ceil(wait))


def _in_words(seconds: int) -> str:
    return "1 second" if seconds == 1 else f"{seconds} seconds"


# ---------------------------------------------------------------------------
# Asking OpenRouter about the day (needs the key, costs no free request)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allowance:
    """The account's free-model requests for the current UTC day, as OpenRouter counts them."""

    used: int
    limit: int
    remaining: int


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: this request carries the API key."""

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # None makes urllib report the redirect itself as an HTTP error, which
        # read_allowance reads as "could not be read".
        return None


#: The only way out for a request that carries the key. tests/conftest.py closes it.
_OPENER = urllib.request.build_opener(_NoRedirects)


def _get_with_key(url: str, key: str) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with _OPENER.open(request, timeout=LOOKUP_TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_ANSWER_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 3xx, 4xx and 5xx: the status is the answer
        return exc.code, exc.read(MAX_ANSWER_BYTES).decode("utf-8", "replace")


def _log_unreadable(why: str) -> None:
    """Say why the count could not be read. Never what OpenRouter sent, and never the key."""
    logger.warning(
        "OpenRouter's count of today's free requests could not be read (%s). The per-minute "
        "ceiling still applies, and so does the last count that was read today. With no count "
        "for today, visitors get at most %d live calls and the page cannot start a test.",
        why,
        UNKNOWN_LIVE_CALLS,
    )


def day_is_counted(settings: Settings) -> bool:
    """Whether anybody keeps a count of the day that this service could read.

    Only OpenRouter does, for the key it is asked with. With another address in
    ``OPENROUTER_BASE_URL`` the service knows of no free allowance to protect, and no
    count would ever come. So the day has no rule there, and the per-minute ceiling is
    the only brake.
    """
    return bool(settings.secret("openrouter_api_key")) and is_openrouter(settings.openrouter_base_url)


def read_allowance(settings: Settings, fetch: KeyedFetch = _get_with_key) -> Optional[Allowance]:
    """Ask OpenRouter how many free requests the account has left today. Never raises.

    ``None`` means the count could not be read, and the log says why. The answer also
    names the key and the account; only its three numbers are kept or logged.
    """
    key = settings.secret("openrouter_api_key")
    if not key or not day_is_counted(settings):
        # Nothing to ask with, or nobody to ask: the key goes to OpenRouter's own
        # encrypted address and nowhere else.
        return None
    try:
        status, body = fetch(settings.openrouter_base_url.rstrip("/") + "/key", key)
    except Exception as exc:  # noqa: BLE001 - "could not be read" is an answer too
        _log_unreadable(type(exc).__name__)
        return None
    if status in (401, 403):
        logger.warning(
            "OpenRouter rejected the API key (HTTP %d) when asked for today's count of free "
            "requests. Check OPENROUTER_API_KEY: model calls will be rejected the same way.",
            status,
        )
        return None
    try:
        counts = json.loads(body)["data"]["free_model_daily_requests"]
        allowance = Allowance(
            used=int(counts["used"]), limit=int(counts["limit"]), remaining=int(counts["remaining"])
        )
    # OverflowError: Python reads "Infinity" and "1e999" as JSON numbers.
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        allowance = None  # not JSON, or not this shape
    if status != 200 or allowance is None or allowance.limit <= 0:
        _log_unreadable(f"unexpected answer, HTTP {status}")
        return None
    logger.info(
        "OpenRouter counts %d of %d free requests used today; %d left.",
        allowance.used,
        allowance.limit,
        allowance.remaining,
    )
    return allowance


# ---------------------------------------------------------------------------
# What this process remembers
# ---------------------------------------------------------------------------


@dataclass
class Run:
    """What one visitor's run asks of the budget."""

    #: Live calls the run may make: one for each model it would really call. A
    #: :class:`CountedExtractor` adds its own call here when it is built.
    calls: int = 0
    #: Places it holds in the minute while it is under way.
    held: int = 0


# One clock for both counters: seconds since 1970, in UTC, say how old a call is and
# how far away midnight UTC is. It is the wall clock, so a correction of the host's
# clock shifts the last minute by as much. Put forward, the minute is forgotten early
# and OpenRouter's own 20 a minute is the limit that holds; put back, calls stay in
# the minute for longer, and the wait a caller is told is still never more than a
# minute. Tests replace it to move time by hand.
_now: Callable[[], float] = time.time

_made: deque[float] = deque()  #: when each model call of the last minute was made
_held = 0  #: places kept for visitors' runs that are under way
_expected_gap: Optional[float] = None  #: a test is running: it sends a call about this often
_allowance: Optional[Allowance] = None  #: OpenRouter's last readable answer
_read_at = -math.inf  #: when that answer was read
_asked_at = -math.inf  #: when OpenRouter was last asked, answered or not
_since_read = 0  #: model calls made since that answer
_unknown_calls = 0  #: visitors' calls made while there was no count for today
_reading: Optional[asyncio.Task[None]] = None  #: the reading that is under way, if any


def reset() -> None:
    """Forget everything this process counted (between tests)."""
    global _held, _expected_gap, _allowance, _read_at, _asked_at, _since_read, _unknown_calls
    global _reading
    _made.clear()
    _held, _expected_gap = 0, None
    _allowance, _read_at, _asked_at, _since_read, _unknown_calls = None, -math.inf, -math.inf, 0, 0
    _reading = None


# ---------------------------------------------------------------------------
# This minute
# ---------------------------------------------------------------------------


def count(run: Optional[Run] = None) -> None:
    """Record that one model call is being made now, whoever asked for it.

    A visitor's run passes itself: one of the places it holds becomes this call. A
    test of the models passes nothing, because it holds no places: it paces itself.
    """
    global _held, _since_read, _unknown_calls
    now = _now()
    if run is not None:
        if run.held:
            run.held -= 1
            _held -= 1
        if _todays_count(now) is None:
            _unknown_calls += 1
    _made.append(now)
    _since_read += 1


def release(run: Run) -> None:
    """The run is over: hand back the places it did not use."""
    global _held
    _held -= run.held
    run.held = 0


def expect_calls_every(gap: Optional[float]) -> None:
    """Told by a test for as long as it runs: it sends one more call about every ``gap`` seconds.

    With that, "try again in N seconds" allows for the calls the test will have made
    by then. Without it a visitor would be sent away twice. ``None`` when the test is over.
    """
    global _expected_gap
    _expected_gap = gap if gap and gap > 0 else None


def seconds_until_room(needed: int = 1, limit: Optional[int] = None) -> int:
    """How long until ``needed`` more calls fit under ``limit`` calls a minute. 0 means now.

    ``limit`` is :data:`MINUTE_CEILING` unless the caller names another. Room appears as
    the calls of the last minute grow older than a minute, one by one. While a test
    runs, the calls it will add in the meantime are counted against that room.
    """
    ceiling = MINUTE_CEILING if limit is None else limit
    now = _now()
    while _made and _made[0] <= now - MINUTE_SECONDS:
        _made.popleft()
    # A place that is held counts as a call made now: its call can still come.
    taken = [*_made, *[now] * _held]
    if len(taken) + needed <= ceiling:
        return 0
    for gone, made_at in enumerate(taken, start=1):
        wait = made_at + MINUTE_SECONDS - now  # when this call is older than a minute
        coming = int(wait // _expected_gap) + 1 if _expected_gap else 0
        if len(taken) - gone + coming + needed <= ceiling:
            # Never more than a minute, whatever the host's clock did meanwhile: the
            # page tells a full minute from a used-up day by this number.
            return min(_whole_seconds(wait), int(MINUTE_SECONDS))
    return int(MINUTE_SECONDS)


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------


async def _ask_openrouter() -> None:
    """Read the day's count, unless OpenRouter was asked less than a minute ago."""
    global _asked_at, _reading
    now = _now()
    if now - _asked_at >= READ_EVERY_SECONDS:
        _asked_at = now
        # A task of the budget's own. The count is everybody's, so the reading must
        # not end with the run that happened to start it: the page cancels a run when
        # its visitor goes away.
        _reading = asyncio.create_task(_read(now, _since_read))
    if _reading is not None and not _reading.done():
        # Spenders that arrive during a reading wait for the same one instead of
        # starting another. shield: one who goes away stops waiting, and the reading
        # goes on for everybody else.
        await asyncio.shield(_reading)


async def _read(asked: float, counted: int) -> None:
    """One reading, given up after the deadline. Raises nothing a run could trip over.

    ``counted`` is this process's own sum when the reading began. Calls made while
    OpenRouter was answering are not in its number yet, so they stay in the sum.
    """
    global _allowance, _read_at, _since_read
    try:
        # urllib blocks, so the reading runs in a worker thread.
        allowance = await asyncio.wait_for(
            asyncio.to_thread(read_allowance, get_settings()), timeout=READ_DEADLINE_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - a reading must never fail a visitor's run
        logger.warning(
            "Reading OpenRouter's count of today's free requests did not finish (%s).",
            type(exc).__name__,
        )
        return
    if allowance is not None:
        _allowance, _read_at, _since_read = allowance, asked, _since_read - counted


def _todays_count(now: float) -> Optional[Allowance]:
    """The last readable count, while it is still about today."""
    if _allowance is None or _read_at // SECONDS_PER_DAY != now // SECONDS_PER_DAY:
        return None  # never read, or read before midnight UTC, when the count restarts
    # A reading that failed since then changes nothing here: within one day the
    # count only goes down, so the last good one is still the most that can be left.
    return _allowance


def _left(today: Allowance) -> int:
    """What that count leaves now: less the calls made since, and the places promised."""
    return today.remaining - _since_read - _held


# ---------------------------------------------------------------------------
# Who may spend
# ---------------------------------------------------------------------------


async def admit_visitor(run: Run) -> None:
    """Keep a place for every live call ``run`` may make, or raise :class:`CallLimitError`.

    Nothing is called and nothing is counted yet. A run with no live call to make
    (every position simulated, or no API key) asks for nothing and is never refused.
    """
    global _held
    # A chain longer than the ceiling could never be admitted, so it asks for it all.
    wanted = min(run.calls, MINUTE_CEILING)
    if wanted <= 0:
        return
    await _ask_openrouter()
    # Nothing below waits for anything. Between the checks and the hold on the last
    # line no other run gets a turn, so two runs can never be given the same places.
    now = _now()

    today = _todays_count(now)
    if today is not None:
        left, reserve = _left(today), today.limit * VISITOR_SHARE
        # The run may make every call it asked for, so the last of them has to start
        # above the reserve too: a chain of three is not let in with one call to spare.
        if left - (wanted - 1) <= reserve:
            raise CallLimitError(
                _DAY_USED,
                wait_seconds=_whole_seconds(SECONDS_PER_DAY - now % SECONDS_PER_DAY),
                detail=(
                    f"{left} of today's {today.limit} free requests are left, and visitors "
                    f"stop at {VISITOR_SHARE:.0%} of the day ({reserve:g}): {wanted} asked for"
                ),
            )
    elif day_is_counted(get_settings()) and _unknown_calls + _held + wanted > UNKNOWN_LIVE_CALLS:
        # Only where there is a count to wait for. With another address in
        # OPENROUTER_BASE_URL none will ever come, and this rule would refuse every run
        # after the twentieth until the service is started again.
        # A minute from now OpenRouter can be asked again, if it has not been by then.
        wait = int(READ_EVERY_SECONDS)
        raise CallLimitError(
            _DAY_UNKNOWN.format(wait=_in_words(wait)),
            wait_seconds=wait,
            detail=(
                "no count of today's free requests could be read from OpenRouter, and "
                f"visitors get {UNKNOWN_LIVE_CALLS} live calls without one: {_unknown_calls} "
                f"made since the service started, {_held} held by runs under way, "
                f"{wanted} asked for"
            ),
        )

    wait = seconds_until_room(wanted)
    if wait:
        raise CallLimitError(
            _BUSY.format(wait=_in_words(wait)),
            wait_seconds=wait,
            detail=(
                f"the ceiling is {MINUTE_CEILING} live calls a minute: "
                f"{len(_made)} made in the last {MINUTE_SECONDS:g} s, {_held} held by runs "
                f"under way, {wanted} asked for"
                + (", and a test of the models is adding more" if _expected_gap else "")
            ),
        )
    run.held = wanted
    _held += wanted


async def why_no_test(by: str, planned: int) -> Optional[str]:
    """Why a test of ``planned`` requests may not start now, or None when it may.

    ``by`` is ``page`` (the button), ``failure`` (the service's own repair) or
    ``schedule`` (the job). The repair is refused last: it only needs its own
    requests to fit. The button is refused first, and it alone is refused when
    OpenRouter cannot say what is left: a stranger may press it, while the repair
    and the job are the owner's own upkeep, each bounded by the size of one test.
    """
    await _ask_openrouter()
    now = _now()
    today = _todays_count(now)
    if today is None:
        return "allowance_unknown" if by == "page" else None
    left = _left(today)
    if by == "page":
        enough = left >= max(planned, math.ceil(today.limit * BUTTON_SHARE))
    elif by == "schedule":
        enough = left - planned >= today.limit * SCHEDULE_SHARE
    else:
        enough = left >= planned
    if not enough:
        return "allowance"
    # A count read earlier today is the most that can be left: good enough to say no
    # on, and good enough for the owner's own upkeep, but not for a stranger's test.
    if by == "page" and now - _read_at >= READ_EVERY_SECONDS:
        return "allowance_unknown"
    return None


# ---------------------------------------------------------------------------
# Counting each call of a visitor's run where it is made
# ---------------------------------------------------------------------------


class CountedExtractor(BaseExtractor):
    """Decorator around one engine that tells the budget when its model is really called.

    Like the playground's ``FaultyExtractor`` it replaces only ``_generate``, the one
    method that sends a request, so it has to wrap the engine itself and sit inside any
    other wrapper. An engine without a key is stopped before ``_generate``: it calls
    nothing and counts nothing.
    """

    def __init__(self, inner: BaseExtractor, run: Run) -> None:
        super().__init__(inner.settings)
        self._inner = inner
        self._run = run
        self.provider = inner.provider
        if inner.is_configured():
            run.calls += 1

    @property
    def model(self) -> str:
        return self._inner.model

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    async def _generate(self, text: str) -> Optional[str]:
        count(self._run)
        return await self._inner._generate(text)

    def _translate_error(self, exc: Exception) -> ExtractionError:
        return self._inner._translate_error(exc)


class BudgetedDispatcher(ExtractionDispatcher):
    """The service's dispatcher, with the budget asked once before the first model."""

    def __init__(self, extractors: Sequence[BaseExtractor], run: Run) -> None:
        super().__init__(extractors)
        self._run = run

    async def extract(self, text: str) -> tuple[InvoiceData, str, bool]:
        await admit_visitor(self._run)  # a refusal leaves from here: nothing was called
        try:
            return await super().extract(text)
        finally:
            release(self._run)


async def extract_invoice(text: str) -> tuple[InvoiceData, str, bool]:
    """What ``POST /api/v1/extract`` runs: the chain in use, behind the ceiling.

    Returns and raises what :func:`app.extractors.extract_invoice` does, and raises
    :class:`CallLimitError` when the run is refused before it starts.
    """
    run = Run()
    chain = [CountedExtractor(engine, run) for engine in get_dispatcher().extractors]
    return await BudgetedDispatcher(chain, run).extract(text)

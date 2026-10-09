"""Keep the model chain working when free models disappear.

Free models are withdrawn often, and editing a setting used to be the only way to
replace one. With ``OPENROUTER_MODELS`` left blank the service does it by itself:

* **When it starts** it reads the chain this project's scheduled test published
  (:mod:`app.published_chain`): one small public file, no key, no model called.
* **Nothing is tested while requests succeed.** After a real request that a model of
  the chain did not answer, the service asks OpenRouter's own page for that model
  (free). If the answer is "Model not found", the model is taken out of the chain,
  and the free place is filled by a *test*: every free model OpenRouter lists that is
  not in the chain is sent the seven sample documents through the service's own
  engine. The first round goes out at once, and a model whose first answer is right
  answers from then on as a *stand-in*, in memory only, until a test has decided
  about it: it joins the chain when it passes and stops answering when it fails.
* **The page** shows a test while it runs and what the last one found, and can start
  one (``/playground/models``).

What a test may change is decided by three rules in :mod:`app.model_rules`: repair,
never rank; a model leaves only when OpenRouter no longer offers it *and* it gave no
answer; a free place goes to a model that passed.

Everything remembered here lives in this process. A free host stops the service after
a quarter of an hour without visitors and starts it again from nothing, so nothing in
this module waits for a timer or counts a day: the day's allowance is OpenRouter's
own figure (:mod:`app.budget`), and the memory that lasts is the published list.

Naming models in ``OPENROUTER_MODELS`` pins the chain, and then nothing here does
anything.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence, Union

from app import budget, model_cache, model_catalogue, model_rules, model_testing, published_chain
from app.config import Settings, get_settings
from app.extractors import get_dispatcher
from app.model_testing import now as _now
from app.model_testing import stamp as _stamp
from app.playground import benchmark
from app.playground.catalogue import BENCHMARK_SNAPSHOT

logger = logging.getLogger("app.model_chooser")

#: Tests are kept this far apart, whoever starts them. Rate limiting on free models
#: swings within minutes, so a sooner test would measure the same moment again.
PAUSE_SECONDS = 15 * 60
#: A failed request is looked into at most this often. A burst of failing requests
#: leads to one look, not to one per request.
LOOK_PAUSE_SECONDS = 60
#: After failed requests the published list is read at most this often: the address
#: hands out the same copy for five minutes.
PUBLISHED_PAUSE_SECONDS = 5 * 60
#: How long a key-less read may take at startup before the service goes on without
#: it. The visitor is already waiting for the host then, so this may be generous.
READ_SECONDS = 8.0
#: The same after a failed request, when somebody is waiting for a repair.
LOOK_READ_SECONDS = 3.0
#: How long OpenRouter may take over its list of free models, or over its pages for
#: the models of a chain, when a failed request or a press of the button waits.
CATALOGUE_SECONDS = 5.0

#: Why a test could not start. Like every code here, the page has words for each (a
#: test compares these tuples with the page's tables).
REFUSALS = (
    "pinned",
    "no_key",
    "not_openrouter",
    "running",
    "pause",
    "list_unreadable",
    "nothing_to_test",
    "allowance",
    "allowance_unknown",
)
ORIGINS = ("pinned", "default", "published", "own")
#: Why the chain in use is what it is, when its origin is "own": a model was withdrawn
#: and taken out, a stand-in answers that no test has judged yet, or a test ended and
#: changed it.
ORIGIN_REASONS = ("gone", "first_answer", "test")
#: What became of the last look into a failed request.
LOOK_RESULTS = ("published", "passing", "no_place", "started") + REFUSALS
PUBLISHED_STATES = ("off", "used", "older", "same", "missing", "unreachable", "invalid", "foreign")
#: What the service is doing in the background, when it is doing anything: looking
#: into a failed request, asking its free questions before a test, or testing.
DOING = ("looking", "starting", "testing")

#: Why no test can start, in words for the server log. The page has its own, for
#: visitors; these may say what an admin can do.
_WHY_NO_TEST = {
    "running": "a test is already running",
    "pause": "the last test ended less than a quarter of an hour ago",
    "list_unreadable": "OpenRouter's list of free models could not be read",
    "nothing_to_test": (
        "OpenRouter lists no free model besides the chain's own and those that failed a test here"
    ),
    "allowance": "too little of today's free allowance is left",
    "allowance_unknown": "OpenRouter could not say how much of today's free allowance is left",
}


@dataclass(frozen=True)
class Failure:
    """One model of the chain that did not answer a real request."""

    model: str
    #: The service's own error type, or None when only "it did not answer" is known.
    error_type: Optional[str] = None
    #: Whether waiting could fix it, as the engine judged; None when unknown.
    retryable: Optional[bool] = None
    #: The engine's own sentence about it, which ends with the status OpenRouter gave.
    message: Optional[str] = None


_test: Optional[dict[str, Any]] = None  #: the test that is running, as the page is shown it
_tasks: set[asyncio.Task[Any]] = set()  #: keeps background work alive until it is done
_looking = False  #: a failed request is being looked into
_starting = False  #: a test is asking its free questions (the list, the allowance) before it starts
_looked_at = -math.inf  #: when the last look began (monotonic)
_test_ended_at = -math.inf  #: when the last test ended (monotonic), for the pause
_gone: set[str] = set()  #: models OpenRouter's own page answers "Model not found" for
_published: dict[str, Any] = {"state": "off", "tested": None}  #: what became of the published list
_published_record: Optional[dict[str, Any]] = None  #: the scheduled test, for the page
_published_read_at = -math.inf  #: when the published list was last asked for (monotonic)
_published_answered = False  #: whether that address has answered at all since the start
_refusal: Optional[dict[str, Any]] = None  #: the last "not now" the button was given
_look_note: Optional[dict[str, Any]] = None  #: what the last look into a failed request found
_failed_here: set[str] = set()  #: models that failed a test since this process started
_awake_since = datetime.now(timezone.utc)  #: when this process started


def reset() -> None:
    """Forget everything this process remembers (between tests)."""
    global _test, _looking, _looked_at, _test_ended_at, _published, _published_record
    global _published_read_at, _published_answered, _refusal, _awake_since, _look_note, _starting
    _test, _looking, _looked_at, _test_ended_at = None, False, -math.inf, -math.inf
    _look_note, _starting = None, False
    _failed_here.clear()
    _published, _published_record = {"state": "off", "tested": None}, None
    _published_read_at, _published_answered, _refusal = -math.inf, False, None
    _awake_since = datetime.now(timezone.utc)
    _gone.clear()
    _tasks.clear()


def _in_background(work: Any) -> None:
    task = asyncio.create_task(work)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def mode(settings: Optional[Settings] = None) -> str:
    """``automatic`` when the service looks after its own chain, else why it does not."""
    settings = settings or get_settings()
    if settings.models_pinned:
        return "pinned"
    if not settings.openrouter_enabled:
        return "no_key"
    if not model_catalogue.is_openrouter(settings.openrouter_base_url):
        return "not_openrouter"
    return "automatic"


# ---------------------------------------------------------------------------
# The chain in use: two writers, one for a choice and one for a stand-in
# ---------------------------------------------------------------------------


def tested_at() -> datetime:
    """When a full test last stood behind the chain in use.

    For the built-in default that is the last minute of the measuring by hand, so a
    list tested later is newer, also later on that same day: a new default is pushed
    on the day it was measured, and the push starts the scheduled test.
    """
    origin = model_cache.origin()
    if origin:
        written = origin.get("tested")
    else:
        # "17:54 to 19:02": the minute the last request of the measuring went out.
        ended = BENCHMARK_SNAPSHOT["window_utc"].rsplit(" ", 1)[-1]
        written = f"{BENCHMARK_SNAPSHOT['measured']}T{ended}:59Z"
    return published_chain.moment(written) or datetime.fromtimestamp(0, timezone.utc)


def _switch(before: list[str], level: int = logging.WARNING) -> None:
    """Take up the chain the settings now give, if it is not ``before``.

    The cached dispatcher is dropped, so the next request builds a new one from
    ``settings.model_chain``. A request that is already running keeps the dispatcher
    it started with. ``/health``, the page, the API and ``extract_invoice()`` all go
    through that one dispatcher, so they cannot disagree.
    """
    if get_settings().model_chain != before:
        get_dispatcher.cache_clear()
        logger.log(
            level,
            "Model chain in use is now %s (it was %s). /health, the page and POST "
            "/api/v1/extract use it from the next request.",
            get_dispatcher().models,
            before,
        )


def _use(
    models: Sequence[str],
    kind: str,
    tested: datetime,
    why: dict[str, Any],
    level: int = logging.WARNING,
) -> None:
    """Choose a chain: it is used from now on, and saved where the disk allows.

    ``kind`` and ``why`` say where the chain comes from, ``tested`` when a full test
    last stood behind it. A stand-in that was answering stops: what is chosen here
    has been tested, or is what is left of a tested chain.
    """
    before = get_dispatcher().models
    origin = {"kind": kind, "tested": _stamp(tested), "changed": _stamp(_now()), "why": why}
    model_cache.use(models, origin)
    _switch(before, level)


def _stand_in(
    behind: Sequence[str], models: Sequence[str], reason: Optional[dict[str, Any]]
) -> None:
    """Let ``models`` answer behind ``behind`` until a test has decided about them.

    In memory only, on top of the chosen chain and never instead of it: a model that
    was put to use on one right answer must not be what a restart finds. With no
    model left to stand in, the chosen chain is in use again, exactly as it was.
    ``reason`` is why the repair that put them to use was started; it stays with
    them, so that a later test can take the repair up again.
    """
    before = get_dispatcher().models
    if models:
        origin = {
            "kind": "own",
            "tested": _stamp(tested_at()),
            "changed": _stamp(_now()),
            "why": {"code": "first_answer", "models": list(models), "reason": reason},
        }
        model_cache.use([*behind, *models][: model_rules.PLACES], origin, save=False)
    else:
        model_cache.stand_down()
    _switch(before)


def _members() -> list[str]:
    """The chain the service has chosen, without stand-ins: what a restart would find."""
    return model_cache.settled() or model_testing.default_chain()


def _standing() -> tuple[list[str], Optional[dict[str, Any]]]:
    """The stand-ins that answer now, and why the repair that put them to use was started."""
    members = _members()
    models = [model for model in model_cache.chain() if model not in members]
    why = (model_cache.origin() or {}).get("why") or {}
    return models, why.get("reason") if models else None


def log_mode() -> None:
    """Say at startup which chain is in use and who looks after it."""
    settings = get_settings()
    state = mode(settings)
    chain = get_dispatcher().models
    origin = model_cache.origin()
    if state == "pinned":
        logger.info(
            "Model choice: pinned. OPENROUTER_MODELS names the chain, so the service never "
            "tests or replaces a model. Leave OPENROUTER_MODELS blank to let it look after "
            "the chain itself."
        )
        return
    if origin is None:
        using = f"the tested default {chain}"
    elif origin.get("kind") == "published":
        using = (
            f"{chain}, the chain this project's scheduled test published "
            f"(tested {origin.get('tested')})"
        )
    else:
        using = (
            f"{chain}, which this service chose itself on {origin.get('changed')}. It is "
            f"saved in {model_cache.PATH}: delete that file and restart to return to the "
            "tested default"
        )
    if state == "automatic":
        logger.info("Model choice: automatic. Using %s.", using)
        logger.info(
            "Nothing is tested until a request fails. Then a model OpenRouter no longer "
            "offers is replaced by one that passes the seven sample documents."
        )
    else:
        # Not the word for the other mode: nothing is looked into or tested in this state.
        logger.warning(
            "Model choice: the service does not look after its chain here, because %s. It "
            "will not test or replace a model by itself. Using %s.",
            "no API key is set" if state == "no_key" else "OPENROUTER_BASE_URL is not OpenRouter's",
            using,
        )


# ---------------------------------------------------------------------------
# The published list
# ---------------------------------------------------------------------------


async def read_published(at_startup: bool) -> bool:
    """Read the chain the scheduled test published, and use it if it is newer.

    Returns True when the chain in use changed. Calls no model, sends no key and
    never raises. A published chain is used whole or not at all: only when the file
    passes every check in :func:`app.published_chain.parse`, was made by this very
    test (same prompt, schema, documents, rules and default), and its test is newer
    than the one behind the chain in use. After a failed request one more rule
    applies: a list that names the very chain that has just failed is not taken.
    """
    global _published, _published_record, _published_read_at, _published_answered
    settings = get_settings()
    url = settings.model_published_url
    if not url:
        _published = {"state": "off", "tested": None}
        return False
    _published_read_at = time.monotonic()
    try:
        patience = READ_SECONDS if at_startup else LOOK_READ_SECONDS
        text = await asyncio.wait_for(asyncio.to_thread(published_chain.fetch, url), patience)
        found = published_chain.parse(text, published_chain.fingerprint(settings), _now())
    except Exception as exc:  # noqa: BLE001 - whatever the file or the network does, the service starts
        kind = exc.kind if isinstance(exc, published_chain.Unusable) else "unreachable"
        _published_answered = _published_answered or kind != "unreachable"
        _published = {"state": kind, "tested": getattr(exc, "tested", None)}
        next_step = ""
        if kind == "missing":
            next_step = (
                " (If the scheduled job has never published, it may have no key: README, "
                "'The scheduled test'.)"
            )
        elif kind == "foreign":
            next_step = (
                " (After a change to the test, the scheduled job's next run publishes a list "
                "that fits this version. A clone that changed the test needs a list of its "
                "own: point MODEL_PUBLISHED_URL at it, or set it to null to read none.)"
            )
        logger.log(
            logging.INFO if kind == "missing" else logging.WARNING,
            "The published list at %s was not used: %s. The chain stays %s.%s",
            url,
            str(exc) or type(exc).__name__,
            get_dispatcher().models,
            next_step,
        )
        return False

    _published_answered = True
    _published_record = found.record
    tested = _stamp(found.tested)
    age = (_now() - found.tested).days
    if age >= published_chain.STALE_AFTER_DAYS:
        runs = "the last runs of 'Model check' in the repository's Actions tab"
        if found.job.get("outcome") == "nothing" and found.job.get("checked") != tested:
            logger.warning(
                "The published list is %d days old. The scheduled job tested again on %s and "
                "could confirm no working chain, so it published nothing new: %s say why.",
                age,
                found.job.get("checked"),
                runs,
            )
        else:
            logger.warning(
                "The published list is %d days old: the scheduled job has published nothing "
                "since %s. Look at %s: a red run says why. If the workflow is shown as "
                "disabled there (GitHub pauses scheduled workflows in a public repository "
                "after 60 days without activity), press 'Enable workflow'.",
                age,
                tested,
                runs,
            )
    in_use = get_dispatcher().models
    if _test is not None:
        return False  # one writer at a time: a running test has the chain
    if found.tested <= tested_at():
        _published = {"state": "older", "tested": tested}
        logger.info(
            "The published list (tested %s) is not newer than the chain in use (tested %s). "
            "Nothing changes.",
            tested,
            _stamp(tested_at()),
        )
        return False
    if not at_startup and list(found.chain) == in_use:
        # The very chain that has just failed here. A newer date does not make it answer.
        _published = {"state": "same", "tested": tested}
        return False
    # At startup this is the ordinary course of things on a host that forgets its
    # disk, so it is no warning there.
    level = logging.INFO if at_startup else logging.WARNING
    _use(found.chain, "published", found.tested, {"code": "published", "models": []}, level=level)
    if found.record is not None:
        model_cache.remember(found.record)
    _published = {"state": "used", "tested": tested}
    logger.info(
        "Using the chain published by the scheduled test of %s: %s (%s).",
        tested,
        list(found.chain),
        found.by.get("run") or url,
    )
    return list(found.chain) != in_use


async def at_startup() -> None:
    """Called once, before the first request is served. Reads; never calls a model.

    Any HTTP request can start a sleeping service on a free host, a crawler's
    included, so nothing here may spend a free request. And nothing here may stop
    the service from starting, whatever a published file contains.
    """
    try:
        settings = get_settings()
        if mode(settings) != "automatic":
            return
        if not settings.model_published_url:
            logger.info("The published list is never read here: MODEL_PUBLISHED_URL is null.")
        elif not settings.model_catalogue_check:
            logger.info(
                "The published list is not read at startup: MODEL_CATALOGUE_CHECK is false, "
                "which means no request at startup. It is read after the first request that "
                "fails."
            )
        else:
            await read_published(at_startup=True)
    except Exception:  # noqa: BLE001 - a read must never stop startup
        logger.exception("The published list could not be looked at. Starting without it.")


def note_listings(listings: dict[str, model_catalogue.Listing]) -> None:
    """Remember which models the startup report found "not listed" (for the page)."""
    _gone.update(model for model, listing in listings.items() if listing.status == "not_listed")


# ---------------------------------------------------------------------------
# Noticing that a model has stopped working
# ---------------------------------------------------------------------------


def note(failed: Sequence[Failure], answered: bool) -> None:
    """Told after a real request in which a model of the chain did not answer.

    ``answered`` says whether a later model did. Returns at once: this is all a
    request ever pays. Whatever follows happens in the background, and none of it
    calls a model until OpenRouter itself has confirmed that something is wrong.
    """
    global _looking, _looked_at
    if mode() != "automatic" or not failed:
        return
    if any(failure.error_type in model_rules.KEY_PROBLEMS for failure in failed):
        return  # the key is the problem, and no other model can fix that
    now = time.monotonic()
    # One thing at a time: while a test runs or is about to start, or another failed
    # request is being looked into, this one is dropped. The next failure is news again.
    if _looking or _starting or _test is not None or now - _looked_at < LOOK_PAUSE_SECONDS:
        return
    _looking, _looked_at = True, now
    _in_background(_look(list(failed), answered))


async def _pages(models: Sequence[str]) -> list[model_catalogue.Listing]:
    """What OpenRouter's own page says about each model: one listing per model asked.

    Never raises and never takes long. A page that did not answer in time is
    "unknown", which concludes nothing: only "Model not found" counts against a
    model. What is learnt is remembered both ways, so a model that is offered again
    is no longer shown to visitors as withdrawn.
    """
    try:
        listings = await asyncio.wait_for(
            asyncio.to_thread(
                model_catalogue.check_chain, list(models), get_settings().openrouter_base_url
            ),
            CATALOGUE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - no answer is an answer: nothing is known
        logger.warning(
            "OpenRouter's pages for %s could not be read in time (%s), so nothing is "
            "concluded about them.",
            ", ".join(models),
            type(exc).__name__,
        )
        listings = []
    known = {listing.model: listing for listing in listings}
    listings = [
        known.get(model) or model_catalogue.Listing(model, "unknown", note="no answer")
        for model in models
    ]
    _gone.update(listing.model for listing in listings if listing.status == "not_listed")
    _gone.difference_update(listing.model for listing in listings if listing.status == "listed")
    return listings


def _closed(failure: Failure) -> bool:
    """True when the answer means "this account cannot call the model at all".

    OpenRouter answers 403 or 404 then, to every request. A 400 or a 413 is about the
    request that was sent (a document too long for the model, for example) and says
    nothing about the next one, and an upstream error may be over in a minute.
    """
    return (
        failure.error_type == "ProviderError"
        and failure.retryable is False
        and (failure.message or "").endswith(("(403)", "(404)"))
    )


def _owed() -> Optional[dict[str, Any]]:
    """The reason of a repair that is still owed, or None.

    A model that OpenRouter no longer offers is taken out at once. The test that
    should fill its place may be refused (the pause, the list, the allowance), may
    judge nobody (free models are rate limited in bursts) or may be cut off by a
    restart. The chain then says so itself: a stand-in is answering that no test
    has judged yet, or the chain was last changed because a model was gone and still
    has a free place. Nothing else is remembered, so the second holds after a restart
    on a machine that keeps its disk.
    """
    standing, reason = _standing()
    if standing:
        return reason
    origin = model_cache.origin() or {}
    why = origin.get("why")
    if (
        origin.get("kind") == "own"
        and isinstance(why, dict)
        and why.get("code") == "gone"
        and len(_members()) < model_rules.PLACES
    ):
        models = [model for model in _list(why, "models") if model_catalogue.is_free_model(model)]
        return {"code": "gone", "models": models}
    return None


def _list(record: dict[str, Any], key: str) -> list[Any]:
    """A list out of something a file may have held: the list, or none."""
    value = record.get(key)
    return value if isinstance(value, list) else []


async def _look(failed: list[Failure], answered: bool) -> None:
    """Is a failed request a reason to act? The cheap questions first; no model is called."""
    global _looking

    def found(result: str, reason: Optional[dict[str, Any]] = None, again_at: Optional[str] = None) -> None:
        global _look_note
        _look_note = {"at": _stamp(_now()), "result": result, "reason": reason, "again_at": again_at}

    try:
        # 1. The published list may already hold the repair, and reading it is free.
        due = time.monotonic() - _published_read_at >= PUBLISHED_PAUSE_SECONDS
        adopted = (not _published_answered or due) and await read_published(at_startup=False)
        if adopted:
            found("published")
        chain = get_dispatcher().models
        # A list that was just adopted may still name a model that failed: go on for it.
        failed = [failure for failure in failed if failure.model in chain]
        if not failed:
            return
        # 2. OpenRouter's own page for each model that failed. Only its "Model not
        #    found" counts, and that answer is never served from a cache.
        listings = await _pages([failure.model for failure in failed])
        names = ", ".join(failure.model for failure in failed)
        gone = [listing.model for listing in listings if listing.status == "not_listed"]
        unread = [listing.model for listing in listings if listing.status == "unknown"]
        members = _members()
        standing, standing_for = _standing()
        # Chosen models that are in use no longer: all of them were gone at once, so
        # none could be taken out, and a stand-in answers in their place.
        out = [model for model in members if model not in chain]
        closed = (
            not answered
            and len(failed) == len(chain)
            and not gone
            and not unread
            and all(_closed(failure) for failure in failed)
        )
        if gone:
            reason = {"code": "gone", "models": gone}
            logger.warning(
                "%s did not answer a request, and OpenRouter no longer offers %s.",
                names,
                "it" if len(gone) == 1 and len(failed) == 1 else ", ".join(gone),
            )
            kept = [model for model in chain if model in members and model not in gone]
            if kept and kept != members:
                # Two sources agree, so the model leaves at once; this costs nothing.
                _use(kept, "own", tested_at(), reason)
            if standing:
                # A stand-in that is still offered goes on answering behind what is left.
                _stand_in(kept, [model for model in standing if model not in gone], standing_for)
        elif closed and len(chain) < model_rules.PLACES:
            reason = {"code": "closed", "models": list(chain)}
            logger.warning(
                "No model of the chain can be called (%s), although OpenRouter lists them. "
                "If this account's privacy settings exclude their providers, that is why "
                "(openrouter.ai, Settings, Privacy).",
                ", ".join(chain),
            )
        elif closed:
            logger.error(
                "No model of the chain can be called (%s), OpenRouter lists all of them, and "
                "the chain has no free place. Check Settings, Privacy in the OpenRouter "
                "account, or name working models in OPENROUTER_MODELS.",
                ", ".join(chain),
            )
            found("no_place", {"code": "closed", "models": list(chain)})
            return
        elif owed := _owed():
            reason = owed
            logger.warning(
                "%s did not answer a request, and a repair of the chain is still owed: %s.",
                names,
                "a stand-in answers that no test has judged yet"
                if standing
                else "a withdrawn model left a place that no test has filled",
            )
        else:
            if unread:
                logger.warning(
                    "%s did not answer a request, and OpenRouter's page for %s could not be "
                    "read, so nothing is concluded. The next failed request asks again.",
                    names,
                    "it" if len(failed) == 1 else ", ".join(unread),
                )
            else:
                logger.info(
                    "%s did not answer a request, but OpenRouter still lists it, so the "
                    "failure is taken for a passing one. Nothing is tested.",
                    names,
                )
            if not adopted:
                found("passing")
            return
        # 3. Fill the place by testing the free models that are not in the chain.
        code, again_at = await start("failure", reason, gone=[*out, *gone])
        found(code, reason, again_at)
        if code != "started":
            why_not = _WHY_NO_TEST.get(code, code)
            why_not += f" (possible again at {again_at})" if again_at else ""
            if any(model not in _gone for model in get_dispatcher().models):
                logger.warning(
                    "No test of replacements can start now: %s. The service tries again "
                    "after the next failed request.",
                    why_not,
                )
            else:
                logger.error(
                    "OpenRouter no longer offers any model of the chain, and no test of "
                    "replacements can start now: %s. Every request is answered with HTTP 503 "
                    "until a test finds a model that works; the service tries again after "
                    "the next failed request. To choose the models yourself, name them in "
                    "OPENROUTER_MODELS and restart.",
                    why_not,
                )
    except Exception:  # noqa: BLE001 - a look must never take the service down
        logger.exception("Could not look into a failed request.")
    finally:
        _looking = False


# ---------------------------------------------------------------------------
# A test of the free models
# ---------------------------------------------------------------------------


def _quick_refusal(by: str = "page") -> Optional[tuple[str, Optional[str]]]:
    """Why no test can start, answered from memory alone; None when one might.

    One writer at a time: a test that runs or is about to start has the chain, and so
    has the look into a failed request, which starts its own test. So the button waits
    for a look as well (``by`` is ``page`` for the button and for the page's picture).
    """
    state = mode()
    if state != "automatic":
        return state, None
    if _test is not None or _starting or (by == "page" and _looking):
        return "running", None
    wait = PAUSE_SECONDS - (time.monotonic() - _test_ended_at)
    if wait > 0:
        return "pause", _stamp(_now() + timedelta(seconds=wait))
    if by == "page" and _refusal and _refusal["again_at"] > _stamp(_now()):
        # What the last press was told is still true. Asking again would cost a read
        # of OpenRouter's list and of its pages at every press of a button that any
        # script can press.
        return _refusal["reason"], _refusal["again_at"]
    return None


async def start(
    by: str, reason: dict[str, Any], gone: Sequence[str] = ()
) -> tuple[str, Optional[str]]:
    """Start a test in the background, if one may start now.

    Returns ``("started", None)``, or one of :data:`REFUSALS` and, where there is one,
    the moment it may be tried again. Everything that can refuse is asked before the
    first model call, and none of it costs a free request: this service's own state,
    OpenRouter's list of free models, and OpenRouter's count of what is left today.
    """
    global _test, _refusal, _starting
    refusal = _quick_refusal(by)
    if refusal is not None:
        return refusal
    _starting = True
    try:
        answer = await _free_checks(by, reason, gone)
    finally:
        _starting = False
    if isinstance(answer, tuple):
        if by == "page":
            _refusal = {"reason": answer[0], "again_at": answer[1]}
            logger.info(
                "A visitor asked for a test of the free models, and none can start now: %s "
                "(possible again at %s).",
                _WHY_NO_TEST.get(answer[0], answer[0]),
                answer[1],
            )
        return answer
    _refusal = None
    _test = answer
    _in_background(_run(answer))
    return "started", None


async def _free_checks(
    by: str, reason: dict[str, Any], gone: Sequence[str]
) -> Union[tuple[str, str], dict[str, Any]]:
    """The checks that need the network but no model.

    Returns why no test can start and when to ask again, or the record of the test
    that may now run.
    """
    settings = get_settings()
    soon = _stamp(_now() + timedelta(seconds=LOOK_PAUSE_SECONDS))
    try:
        listed = await asyncio.wait_for(
            asyncio.to_thread(model_catalogue.free_models, settings.openrouter_base_url),
            CATALOGUE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, there is no list to test
        logger.warning(
            "OpenRouter's list of free models could not be read (%s), so nothing is tested.",
            type(exc).__name__,
        )
        return "list_unreadable", soon

    members = _members()
    # A stand-in that answers now is tested before any other model from outside the
    # chain, whoever started the test.
    standing = [model for model in _standing()[0] if model in listed]
    outside = [model for model in listed if model not in members and model not in standing]
    # A model that was withdrawn and is on the list again is offered again. (About the
    # chain's own models only OpenRouter's page for each is believed.)
    _gone.difference_update(outside)
    last = model_cache.last() or {}
    # Models the last test had no room for go first, so that none waits for ever.
    outside = model_rules.in_turn(outside, _list(last, "tested"), _list(last, "left_out"))
    if by == "failure":
        # The chain's own models are not asked again: the one that is gone has just
        # failed, and the others keep their places whatever a test says. Nor is a
        # model that failed a test since this service started: one that rejects the
        # format or answers wrongly is not different a quarter of an hour later.
        candidates = standing + [model for model in outside if model not in _failed_here]
    else:
        candidates = members + standing + outside
        listings = await _pages(members)
        gone = [listing.model for listing in listings if listing.status == "not_listed"]
    # The chain's models that OpenRouter still offers. A stand-in is placed behind them.
    kept = [model for model in members if model not in gone]
    limit = model_rules.CANDIDATE_LIMIT
    tested, left_out = candidates[:limit], candidates[limit:]
    if not tested:
        return "nothing_to_test", soon

    cases = benchmark.live_cases()
    refused = await budget.why_no_test(by, len(tested) * len(cases))
    if refused == "allowance":
        midnight = (_now() + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        return refused, _stamp(midnight)
    if refused:
        return refused, soon
    record = model_testing.new_record(
        by, reason, members, kept, list(gone), listed, tested, left_out, settings
    )
    record["provisional"] = [model for model in standing if model in tested]
    return record


async def _run(record: dict[str, Any]) -> None:
    """One test in the running service, start to finish. Never raises, except when stopping."""
    global _test, _test_ended_at
    settings = get_settings()
    repair = record["by"] == "failure"
    logger.log(
        # A repair changes what answers; a visitor's press of the button is routine.
        logging.WARNING if repair else logging.INFO,
        "Test %s started (%s: %s). OpenRouter lists %d free model(s) with structured "
        "output; testing %s on %d documents: up to %d free requests.",
        record["id"],
        record["by"],
        record["reason"]["code"],
        len(record["found"]),
        record["tested"],
        len(record["cases"]),
        len(record["tested"]) * len(record["cases"]),
    )
    in_use_before = get_dispatcher().models
    stand_ins: list[str] = record["provisional"]  # the very list the two functions below change
    carried = list(stand_ins)  # those that answered before this test began
    # Why its stand-ins answer, and behind which models. A repair knows both: its own
    # reason, and the chain's models that OpenRouter still offers. A test the button
    # started changes neither: it only judges the stand-ins a repair left answering.
    reason = record["reason"] if repair else _standing()[1]
    behind = (
        record["kept"]
        if repair
        else [model for model in in_use_before if model in record["chain_before"]]
    )

    def joined(model: str) -> None:
        """A place is free and this model's first answer was right: use it now.

        Only during a repair. A model of the chain that OpenRouter still offers keeps
        its place in front: a stand-in only ever answers where nobody would have.
        """
        if model in stand_ins or len(behind) + len(stand_ins) >= model_rules.PLACES:
            return
        stand_ins.append(model)
        record["chain"] = behind + stand_ins
        _stand_in(behind, stand_ins, reason)
        logger.warning(
            "%s read a sample document correctly and answers until a test has decided "
            "about it.",
            model,
        )

    def failed(model: str) -> None:
        """The test has failed this model. If it was answering as a stand-in, it stops now."""
        if model not in stand_ins:
            return
        stand_ins.remove(model)
        if repair:
            # Never an empty chain in the record: with nobody left it is the chain as it was.
            record["chain"] = behind + stand_ins or list(record["chain_before"])
        _stand_in(behind, stand_ins, reason)
        logger.warning("%s failed the test and no longer answers.", model)

    try:
        try:
            if carried or _standing()[0]:
                # A stand-in this test will not call cannot be judged by it, and leaves.
                _stand_in(behind, stand_ins, reason)
            await model_testing.run_test(record, settings, joined if repair else None, failed)
        except Exception as exc:  # noqa: BLE001 - a test must never take the service down
            logger.exception(
                "Test %s stopped (%s). The chain stays as it is.", record["id"], type(exc).__name__
            )
            record.update(
                chain=list(record["chain_before"]),
                outcome="interrupted",
                state="done",
                finished=_stamp(_now()),
                provisional=[],
                in_flight=[],
            )
        staying = [model for model in stand_ins if model in carried]
        _finish(record, stand_ins, (behind, staying), reason)
    except Exception:  # noqa: BLE001 - whatever breaks here, the service goes on
        logger.exception("Test %s is over, but its result could not be put to use.", record["id"])
    finally:
        # Also when the service is stopping, and whatever went wrong above: a test
        # that is "running" for ever would refuse every later test and every look.
        _test, _test_ended_at = None, time.monotonic()


def _finish(
    record: dict[str, Any],
    stand_ins: list[str],
    as_before: tuple[list[str], list[str]],
    reason: Optional[dict[str, Any]],
) -> None:
    """Put the result of a test that is over to use, say so in the log, and remember it.

    ``stand_ins`` are the models that answer as stand-ins now, and ``as_before`` what
    to go back to when the test judged nobody: the models they stood behind, and
    those of them that were already answering when the test began.
    """
    rows = {row["model"]: row for row in record["models"]}
    for row in record["models"]:
        logger.info(
            "test %s: %s: %s (%s), %s of %s right%s",
            record["id"],
            row["model"],
            row["verdict"],
            row["why"],
            row["correct"],
            row["calls"],
            f", now {row['placing']}" if row["placing"] else "",
        )
    if record["chain"] != record["chain_before"]:
        finished = published_chain.moment(record["finished"]) or _now()
        # "models": what this test added, so that the page can say it without the record.
        added = [model for model in record["chain"] if model not in record["chain_before"]]
        _use(record["chain"], "own", finished, {"code": "test", "models": added})
    judged = record["outcome"] in ("changed", "unchanged", "nothing_passed")
    if judged:
        # A stand-in the test could not judge, because calls went unanswered, is neither
        # added nor dropped for that: it goes on answering, in memory only, until a
        # test decides. It stands behind the chain's models, except those OpenRouter
        # no longer offers and that gave no answer here.
        behind = [
            model
            for model in record["chain"]
            if not (model in record["gone"] and not rows.get(model, {}).get("answered"))
        ]
        staying = [
            model
            for model in stand_ins
            if model not in record["chain"] and rows[model]["verdict"] == "not_judged"
        ]
    else:
        # The test broke or was stopped, so nobody was judged: what joined during it
        # leaves, and what was answering before it goes on.
        behind, staying = as_before
    staying = staying[: max(0, model_rules.PLACES - len(behind))]
    _stand_in(behind, staying, reason)
    logger.log(
        logging.WARNING if record["outcome"] != "unchanged" else logging.INFO,
        "Test %s finished: %s after %d free requests. Chain: %s (before the test: %s).",
        record["id"],
        record["outcome"],
        record["requests"],
        record["chain"],
        record["chain_before"],
    )
    if staying:
        logger.warning(
            "%s could not be judged, because calls went unanswered. It goes on answering, "
            "in memory only, until a test decides.",
            ", ".join(staying),
        )
    closed = record["reason"]["code"] == "closed"
    if record["outcome"] == "key_refused":
        logger.error("OpenRouter refused the API key during test %s. Nothing was changed.", record["id"])
    elif record["outcome"] == "nothing_passed" and not staying and (closed or not behind):
        # The page tells visitors to contact the admin in this state, so the log says
        # what the admin can do.
        logger.error(
            "Test %s found no free model that passes, and %s (%s). Every request is "
            "answered with HTTP 503 until a test finds a model that works: the service "
            "tests again after a failed request, in %d minutes at the earliest. To choose "
            "the models yourself, name them in OPENROUTER_MODELS and restart.",
            record["id"],
            "no model of the chain can be called"
            if behind
            else "OpenRouter no longer offers any model of the chain",
            ", ".join(record["chain"]),
            PAUSE_SECONDS // 60,
        )
    for row in record["models"]:
        if row["verdict"] == "failed":
            _failed_here.add(row["model"])
        elif row["verdict"] in ("passed", "slow"):
            _failed_here.discard(row["model"])
    model_cache.remember(record)


async def stop() -> None:
    """Cancel what is still running in the background (the service is shutting down)."""
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def wait() -> None:
    """Wait until the background work started here has ended (tests use this)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
        # gather() of tasks that have all finished returns without giving the event
        # loop a turn (Python 3.12), and it is the loop that takes a finished task out
        # of _tasks. Without this line the loop above can spin for ever.
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# What the page is shown
# ---------------------------------------------------------------------------


def _newest(*records: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    dated = [record for record in records if record and record.get("finished")]
    return max(dated, key=lambda record: record["finished"], default=None)


def picture() -> dict[str, Any]:
    """The whole picture, in one object: the page draws its section from nothing else.

    Codes, dates and figures only. Every word a visitor reads is in the page's
    script, so that one place holds the wording and a test can check all of it.
    """
    settings = get_settings()
    state = mode(settings)
    chain = get_dispatcher().models
    origin = model_cache.origin() if not settings.models_pinned else None
    when = tested_at()
    reason, again_at = _quick_refusal() or (None, None)
    own = model_cache.last() if not settings.models_pinned else None
    doing = None if _test is None else "testing"
    doing = doing or ("starting" if _starting else "looking" if _looking else None)
    return {
        "now": _stamp(_now()),
        "awake_since": _stamp(_awake_since),
        "mode": state,
        "chain": chain,
        "origin": {
            "kind": "pinned" if settings.models_pinned else origin["kind"] if origin else "default",
            "tested": None if settings.models_pinned else _stamp(when),
            "age_days": None if settings.models_pinned else max(0, (_now() - when).days),
            "changed": origin.get("changed") if origin else None,
            "why": origin.get("why") if origin else None,
        },
        "gone": [model for model in chain if model in _gone],  # in chain order
        "published": dict(_published),
        "busy": doing is not None,
        # Which of the three it is: the button's reason says "running" for all of them.
        "doing": doing,
        "look": _look_note,
        "test": _test,
        # A real test of this service or of the scheduled job, whichever is newer; the
        # recorded measurement only while there is neither.
        "last": _newest(own, _published_record) or model_testing.recorded_run(),
        "button": {
            "offered": state == "automatic",
            "ready": state == "automatic" and reason is None,
            "reason": reason,
            "again_at": again_at,
        },
        "rules": {
            "documents": len(benchmark.live_cases()),
            "places": model_rules.PLACES,
            "max_misses": model_rules.MAX_MISSES,
            "pause_minutes": PAUSE_SECONDS // 60,
            "timeout_seconds": settings.timeout_seconds,
            "button_share": round(budget.BUTTON_SHARE * 100),
            "requests_at_most": model_rules.CANDIDATE_LIMIT * len(benchmark.live_cases()),
        },
    }

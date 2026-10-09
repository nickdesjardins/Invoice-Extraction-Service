"""Tests that run the page's script, where the other page tests read it.

``tests/test_playground.py`` and ``tests/test_models_page.py`` read the script as
text. That holds its sentences and its figures, and cannot notice what the script
*does*: a timer that asks the service without end, a run that is sent a third time, a
picture of the chain's upkeep that the script cannot put into words. So the script is
run here, by ``tests/page_harness.mjs`` under Node: on a page that swallows what is
drawn, with a clock that is moved by hand, and with the service's own answers, which
this file asks the service for.

Node is not a dependency of the service. Where it is missing these tests are
skipped, and the page's behaviour is then held by the text tests and by a look in a
browser only.

Layout:

* Page behaviour - what the service sends the harness - the answers and the pictures.
* Page behaviour - the cases                          - one test per case of the harness.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
import openai
import pytest
import pytest_asyncio

from app import budget, extractors, model_cache, model_chooser, model_testing
from app.budget import MINUTE_CEILING
from app.model_chooser import Failure
from app.playground.catalogue import BENCHMARK, scenarios_for
from tests.test_extract import (  # noqa: F401 - `http` and `router` are fixtures
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
    as_recorded,
    later,
    world,
    wrong_on,
)
from tests.test_playground import SETTING_NAME, engine_messages, events_of

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("page_harness.mjs")
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "app" / "playground" / "static" / "assets" / "app.js"

#: Every case of the harness, by the name it reports. A case that is added there and
#: not here fails ``test_the_harness_ran_every_case_and_no_other``.
CASES = (
    "a loaded page looks every 20 s for 10 minutes, and then asks nothing",
    "a tab nobody is looking at asks nothing, and looks once when the visitor comes back",
    "a sign of life starts the looks again, for 10 more minutes",
    "while the service is busy the page looks every 2 s; once it gets no answer it shows no test and keeps to the slow looks",
    "a look without an answer does not end looking: every 20 s while the visitor is there, and the first answer puts the page right",
    "a look that comes back unanswered while a run waits for the service leaves that wait alone",
    "a page that never gets the picture keeps to the same looks, and its pill to what the service said of itself",
    "an answered run is sent once, and the page asks nothing more for it",
    "a host's page in place of the answer: wait for /health, send again once, say so",
    "a request that fails outright is handled like a sleeping service, without an error",
    "a host's own JSON is not taken for the service's answer",
    "the service's own refusal before a stream is the result: no wait, no second send",
    "a sleeping service: /health every 3 s, and 3 minutes after the press the page gives up for good",
    "the service is back after 20 s: one resend, answered, and the pill says so",
    "a request the host holds and then delivers: one line and the pill after 5 s, no second send",
    "a request the host holds for ever ends by itself: silence, then the wait, then a way out",
    "a stream that stalls is ended after the silence limit, says so, and is not sent again",
    "a run whose events come slowly is not cut off: every event starts the wait again",
    "a run that met another chain than the page had drawn makes the page look at once",
    "a first load that finds the service asleep finishes by itself once it is back",
    "a first load gives up after 3 minutes and asks nothing more",
    "a first load whose requests are held for ever gives up after 3 minutes, with nothing asked twice",
    "a refusal by the ceiling is read from the last event: a minute, a day, and neither",
    "when the day of a refusal is over, the page says so instead of 'today'",
    "Run all waits out a full minute and carries on with the same scenario",
    "Run all waits twice for one scenario at most, then stops with a sentence",
    "Run all does not wait for midnight: it stops at once and says why",
    "a run that no model answered is sent again once, when the chain has a new model",
    "a chain that only lost a model has nobody new to ask: nothing is sent again",
    "a visitor who has moved on is not handed an answer to the old run",
    "nothing of Run all is sent a second time when the chain is repaired",
    "the button sends one POST to the service's own route, and a second press meanwhile sends nothing",
    "no engine message reaches the page with the name of a setting",
    "every picture the service can send is put into words, on its own and after the one before",
    "the words of a picture say no more than it shows: no test that is not running, no run that is still to find out",
    "when the chain no longer has the scenario on view, the request panel is the visitor's own document",
    "a result that a stand-in gave says so once the stand-in has failed the test",
    "a picture the page cannot draw does not end its looking: the next one is drawn",
)

#: States of the chain's upkeep that the pictures must show at least once: the ones
#: with sentences of their own on the page. A story that no longer reaches its state
#: (the service changed) fails ``test_the_pictures_show_every_state_the_page_has_words_for``.
STATES = (
    "default",
    "pinned",
    "published",
    "page test running",
    "failure test running",
    "failure test running, with a stand-in",
    "page test running, with a stand-in",
    "own / first_answer",
    "own / gone",
    "own / test",
    "look list_unreadable",
    "look pause",
    "look nothing_to_test",
    "look allowance",
    "look no_place",
    "last failure nothing_passed",
    "last failure changed",
    "last page changed",
    "last page key_refused",
)


# ---------------------------------------------------------------------------
# Page behaviour - what the service sends the harness
# ---------------------------------------------------------------------------


def state_of(picture: dict[str, Any]) -> str:
    """One picture in a few words: what the service is doing, and how the chain came to be."""
    test = picture["test"]
    if test:
        return f"{test['by']} test running" + (", with a stand-in" if test["provisional"] else "")
    origin = picture["origin"]
    parts = [origin["kind"] + (f" / {origin['why']['code']}" if origin["why"] else "")]
    if picture["doing"]:
        parts.append(picture["doing"])
    if picture["look"]:
        parts.append(f"look {picture['look']['result']}")
    last = picture["last"]
    if last and last["by"] != "recorded":
        parts.append(f"last {last['by']} {last['outcome']}")
    return ", ".join(parts)


class Pictures:
    """The different pictures the service showed while the stories below were told."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.entries: list[dict[str, Any]] = []
        self._config = config
        self._seen: set[tuple[str, str, int]] = set()

    def take(self, story: str) -> None:
        picture = json.loads(json.dumps(model_chooser.picture()))
        state = state_of(picture)
        # A running test changes with every call. A few of its pictures are enough.
        key = (story, state, len((picture["test"] or {}).get("calls", [])) // 3)
        if key in self._seen:
            return
        self._seen.add(key)
        benchmark = {row["model"]: row for row in BENCHMARK}
        config = {
            **self._config,
            "chain": [
                {
                    "index": index,
                    "model": model,
                    "role": "primary" if index == 0 else "fallback",
                    "configured": True,
                    "benchmark": benchmark.get(model),
                    "listing": None,
                }
                for index, model in enumerate(picture["chain"])
            ],
            "scenarios": scenarios_for(len(picture["chain"])),
        }
        self.entries.append({"name": f"{story}: {state}", "state": state, "config": config, "picture": picture})

    async def during(self, story: str, steps: Callable[[], Awaitable[None]]) -> None:
        """Tell one story, and take a picture at every turn of the event loop meanwhile."""
        over = False

        async def watch() -> None:
            while not over:
                self.take(story)
                await asyncio.sleep(0)

        watcher = asyncio.create_task(watch())
        try:
            await steps()
            await asyncio.wait_for(model_chooser.wait(), timeout=20)
        finally:
            over = True
            await watcher
        self.take(story)


def gone(*models: str) -> list[Failure]:
    """Models of the chain that answered a request with "no such model"."""
    return [Failure(model, "ProviderError", False, f"{model} was not found (404)") for model in models]


async def pictures_of_every_state(
    made: World,
    config: dict[str, Any],
    allowance: Callable[..., list[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Tell the service's stories with scripted models, and collect what it showed.

    Each story starts from a service that remembers nothing. The models answer as
    ``ScriptedEngine`` says; nothing here reaches a model or the network.
    """
    pictures = Pictures(config)
    right = always("right")

    def fresh(listed: list[str], script: dict[str, Any], withdrawn: tuple[str, ...] = ()) -> None:
        extractors.reset_extractors()
        model_cache.forget()
        model_cache.PATH.unlink(missing_ok=True)
        model_chooser.reset()
        budget.reset()
        made.listed[:] = listed
        made.not_found.clear()
        made.not_found.update(withdrawn)
        made.list_fails = False
        ScriptedEngine.SCRIPT.clear()
        ScriptedEngine.SCRIPT.update(script)

    async def press() -> None:
        await model_chooser.start("page", {"code": "asked", "models": []})

    def fail(failed: list[Failure], answered: bool) -> Callable[[], Awaitable[None]]:
        async def steps() -> None:
            model_chooser.note(failed, answered=answered)

        return steps

    # The default chain, and the button's test that fills its free place.
    fresh([PRIMARY, FALLBACK, NEWCOMER], {**HEALTHY, NEWCOMER: right})
    pictures.take("healthy")
    await pictures.during("the button", press)

    # The first model is withdrawn: it is taken out, and a repair fills the place.
    fresh([FALLBACK, NEWCOMER, BROKEN], {FALLBACK: wrong_on("service-invoice"), NEWCOMER: right, BROKEN: always("format")}, (PRIMARY,))
    await pictures.during("first model gone", fail(gone(PRIMARY), True))

    # The owner's case: both gone, and the one model that works loses two calls of its
    # test. It stands in, waits, is carried into the next repair, and is judged at last.
    fresh([BROKEN, SLOW], {BROKEN: always("format"), SLOW: as_recorded("+B++E++")}, (PRIMARY, FALLBACK))
    await pictures.during("owner's case", fail(gone(PRIMARY, FALLBACK), False))
    later(monkeypatch)
    ScriptedEngine.SCRIPT[SLOW] = always("limited")
    limited = [Failure(SLOW, "ProviderRateLimitError", True, f"{SLOW}: rate limited (429)")]
    await pictures.during("owner's case, a quarter of an hour later", fail(limited, False))
    later(monkeypatch)
    ScriptedEngine.SCRIPT[SLOW] = right
    await pictures.during("owner's case, the button", press)

    # Both gone, and the one model on offer answers its first document and then fails
    # the test: a stand-in that is taken out again while its test goes on.
    fresh([NEWCOMER], {NEWCOMER: wrong_on("ocr-receipt", "eur-invoice")}, (PRIMARY, FALLBACK))
    await pictures.during("a stand-in that fails", fail(gone(PRIMARY, FALLBACK), False))

    # Both gone, and nothing on offer passes. A second failed run meets the pause.
    fresh([BROKEN], {BROKEN: always("format")}, (PRIMARY, FALLBACK))
    await pictures.during("nothing passes", fail(gone(PRIMARY, FALLBACK), False))
    monkeypatch.setattr(model_chooser, "_looked_at", -10_000.0)
    await pictures.during("nothing passes, the next run", fail(gone(PRIMARY, FALLBACK), False))

    # Three ways in which a repair cannot even start.
    fresh([NEWCOMER], {NEWCOMER: right}, (PRIMARY, FALLBACK))
    made.list_fails = True
    await pictures.during("no list", fail(gone(PRIMARY, FALLBACK), False))
    fresh([], {}, (PRIMARY, FALLBACK))
    await pictures.during("nothing to test", fail(gone(PRIMARY, FALLBACK), False))
    fresh([NEWCOMER], {NEWCOMER: right}, (PRIMARY, FALLBACK))
    allowance(remaining=3)
    await pictures.during("allowance", fail(gone(PRIMARY, FALLBACK), False))
    allowance(remaining=1000)

    # Listed, but closed to this account: a spare beside them, and no place for one.
    closed = [Failure(model, "ProviderError", False, f"{model} is not available (403)") for model in (PRIMARY, FALLBACK)]
    fresh([PRIMARY, FALLBACK, NEWCOMER], {NEWCOMER: right})
    await pictures.during("closed", fail(closed, False))
    full = [*closed, Failure(NEWCOMER, "ProviderError", False, f"{NEWCOMER} is not available (403)")]
    monkeypatch.setattr(model_chooser, "_looked_at", -10_000.0)
    await pictures.during("closed, and the chain is full", fail(full, False))

    # The key is refused in the middle of the button's test.
    fresh([PRIMARY, FALLBACK, NEWCOMER], {PRIMARY: always("key"), FALLBACK: right, NEWCOMER: right})
    await pictures.during("key refused", press)

    # A chain from the published list, fresh and old; a chain the admin named.
    for age in (timedelta(hours=2), timedelta(days=52)):
        fresh([FALLBACK, NEWCOMER], {FALLBACK: right, NEWCOMER: right})
        tested = model_testing.stamp(model_testing.now() - age)
        why = {"code": "published", "models": []}
        model_cache.use([FALLBACK, NEWCOMER], {"kind": "published", "tested": tested, "changed": tested, "why": why})
        extractors.reset_extractors()
        pictures.take(f"published {age.days} days ago")
    fresh([], {})
    monkeypatch.setenv("OPENROUTER_MODELS", f"{PRIMARY},{FALLBACK}")
    extractors.reset_extractors()
    pictures.take("pinned")
    return pictures.entries


async def what_the_service_sends(
    http: httpx.AsyncClient,  # noqa: F811 - the fixture, handed on by a test
    router: Callable[..., FakeRouter],  # noqa: F811
    world: Callable[..., World],  # noqa: F811
    allowance: Callable[..., list[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Everything the harness answers the page with, asked of the real service here."""
    made = world([PRIMARY, FALLBACK], HEALTHY)
    router(default=ok())
    config = (await http.get("/playground/config")).json()
    idle = {"config": config, "picture": (await http.get("/playground/models")).json()}
    health = (await http.get("/health")).json()
    assert idle["picture"]["mode"] == "automatic" and idle["picture"]["button"]["ready"] is True
    scenario = config["scenarios"][0]
    assert scenario["live_calls"] == 1 and not any(scenario["simulate"])

    async def stream() -> list[dict[str, Any]]:
        return events_of(await http.post("/playground/extract", json={"text": scenario["text"]}))

    answered = await stream()
    router(default=api_error(openai.NotFoundError, 404))
    all_failed = await stream()
    await asyncio.wait_for(model_chooser.wait(), timeout=10)  # the look that this failure started
    for _ in range(MINUTE_CEILING):
        budget.count()
    refused = await stream()
    too_short = await http.post("/playground/extract", json={"text": "hi"})
    assert [answered[-1]["status_code"], all_failed[-1]["status_code"], refused[-1]["status_code"]] == [200, 503, 503]
    assert isinstance(all_failed[-1]["body"]["detail"], list) and isinstance(refused[-1]["body"]["detail"], dict)

    return {
        "health": health,
        "idle": idle,
        "streams": {"answered": answered, "all_failed": all_failed, "refused": refused},
        "too_short": {"status": too_short.status_code, "body": too_short.json()},
        "engine_messages": [message for message in engine_messages() if SETTING_NAME.search(message)],
        "pictures": await pictures_of_every_state(made, config, allowance, monkeypatch),
    }


_ran: Optional[dict[str, Any]] = None  #: one run of the harness serves every test of this file


@pytest_asyncio.fixture
async def harness(
    http: httpx.AsyncClient,  # noqa: F811
    router: Callable[..., FakeRouter],  # noqa: F811
    world: Callable[..., World],  # noqa: F811
    allowance: Callable[..., list[None]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, Any]:
    """The verdicts of the harness by case name, and the pictures it was given.

    The first test that asks builds the fixtures and runs Node, once; the others read
    what it found. Whichever test comes first can do it, so they run in any order.
    """
    global _ran
    if NODE is None:
        pytest.skip("Node is not installed here, so the page's script cannot be run")
    if _ran is None:
        fixtures = await what_the_service_sends(http, router, world, allowance, monkeypatch)
        path = tmp_path / "fixtures.json"
        path.write_text(json.dumps(fixtures), encoding="utf-8")
        done = subprocess.run(
            [NODE, str(HARNESS), str(SCRIPT_PATH), str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )
        assert done.returncode == 0 and done.stdout.strip(), f"the harness did not run:\n{done.stderr[-2000:]}"
        _ran = {
            "cases": {case["name"]: case for case in json.loads(done.stdout)["cases"]},
            "states": [entry["state"] for entry in fixtures["pictures"]],
        }
    return _ran


@pytest.mark.asyncio
async def test_the_pictures_show_every_state_the_page_has_words_for(harness: dict[str, Any]) -> None:
    """The harness draws what it is given. A state that is not among the pictures is not drawn."""
    seen = ", ".join(harness["states"])
    missing = [state for state in STATES if state not in seen]
    assert not missing, f"no picture shows: {missing}\nthe pictures: {harness['states']}"


# ---------------------------------------------------------------------------
# Page behaviour - the cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_harness_ran_every_case_and_no_other(harness: dict[str, Any]) -> None:
    """A page that asks without end ends the harness early: the cases after it are missing."""
    assert list(harness["cases"]) == list(CASES)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.asyncio
async def test_the_page_does_what_it_says(case: str, harness: dict[str, Any]) -> None:
    verdict = harness["cases"].get(case)
    assert verdict is not None, "the harness ended before this case: see the case that failed before it"
    assert verdict["ok"], verdict["why"]

"""Tests for the suite's own closed doors (``tests/conftest.py``).

The suite promises that no test reaches the network. A promise like that is kept by
construction or not at all: ``conftest.py`` replaces every way out with a stand-in
that fails the test. These tests knock on each door on purpose and check that it is
shut.

Every address used here is a dead one on this machine. Should a door ever be open,
the request ends there, and never at OpenRouter or at GitHub.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

import openai
import pytest

from app import budget, extractors, model_cache, model_catalogue, model_chooser, published_chain
from app.budget import MINUTE_CEILING, Allowance, CallLimitError, Run
from app.config import get_settings
from app.extractors import extract_with_model, get_dispatcher
from app.main import configure_logging
from tests.conftest import PRIMARY_MODEL, RUNNER_VARIABLES, _ClosedOpener, close_the_doors
from tests.test_extract import SAMPLE_INVOICE_TEXT

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Port 9 on this machine: nothing listens there.
DEAD = "http://127.0.0.1:9"

#: What a test gets for reaching out. ``pytest.fail`` raises it, and it is not an
#: ``Exception``, so the code under test cannot catch it by accident.
Knocked = pytest.fail.Exception


# ---------------------------------------------------------------------------
# Closed doors - each way out fails the test that takes it
# ---------------------------------------------------------------------------


def test_the_doors_are_closed_for_a_test_that_asks_for_nothing() -> None:
    """This test names no fixture, which is how most tests are written."""
    assert isinstance(model_catalogue._OPENER, _ClosedOpener)
    assert isinstance(budget._OPENER, _ClosedOpener)
    transport = extractors._httpx.AsyncHTTPTransport
    assert transport.handle_async_request.__name__ == "closed_transport"
    # And the count of the day is a stand-in: a day of which nothing is used.
    assert budget.read_allowance(get_settings()) == Allowance(used=0, limit=1000, remaining=1000)


def test_the_catalogue_cannot_be_reached_without_asking(closed_doors: list[str]) -> None:
    with pytest.raises(Knocked, match="reached for the network: GET http://127.0.0.1:9/model/a/b"):
        model_catalogue._http_get(DEAD + "/model/a/b:free")

    assert len(closed_doors) == 1 and "passes its own fetch" in closed_doors[0]
    closed_doors.clear()  # this test knocked on purpose


def test_a_lookup_that_never_raises_cannot_hide_a_knock(closed_doors: list[str]) -> None:
    """``look_up`` turns every failed lookup into "unknown". This is not a failed lookup."""
    with pytest.raises(Knocked):
        model_catalogue.look_up("a/b:free", DEAD)

    closed_doors.clear()


def test_the_published_list_cannot_be_fetched_without_asking(closed_doors: list[str]) -> None:
    """Read through the catalogue's opener, by a ``fetch`` that calls any failure "unreachable"."""
    with pytest.raises(Knocked, match="reached for the network: GET https://127.0.0.1:9/chain"):
        published_chain.fetch("https://127.0.0.1:9/chain.json")

    closed_doors.clear()


def test_the_days_count_cannot_be_asked_of_openrouter_without_asking(
    closed_doors: list[str],
) -> None:
    with pytest.raises(Knocked, match="asks for `allowance`"):
        budget._get_with_key(DEAD + "/key", "made-up-key")

    assert closed_doors == [
        "GET http://127.0.0.1:9/key. A test that wants a count of the day asks for `allowance`."
    ]
    closed_doors.clear()


@pytest.mark.asyncio
async def test_an_engine_cannot_call_a_model_without_asking(
    closed_doors: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the ``router`` fixture the real client is used, and its way out is shut."""
    monkeypatch.setenv("OPENROUTER_BASE_URL", DEAD)
    extractors.reset_extractors()

    with pytest.raises(Knocked, match="asks for `router`"):
        await extract_with_model(SAMPLE_INVOICE_TEXT, PRIMARY_MODEL)

    assert closed_doors[0].startswith("POST http://127.0.0.1:9/chat/completions")
    closed_doors.clear()


def test_the_engines_client_itself_is_left_real() -> None:
    """Only its way out is replaced. Tests of the client's settings test the real one."""
    client = extractors.get_openrouter_client()

    assert isinstance(client, openai.AsyncOpenAI)
    assert get_dispatcher().extractors[0]._get_client() is client  # type: ignore[attr-defined]


def test_a_test_that_knocked_fails_when_it_ends_whatever_was_made_of_it(
    closed_doors: list[str],
) -> None:
    """The service carries on after a failed call, so a knock could pass unnoticed."""
    with pytest.raises(Knocked, match="reached for the network: GET http://127.0.0.1:9/") as info:
        with pytest.MonkeyPatch.context() as patch, close_the_doors(patch):
            try:
                model_catalogue._http_get(DEAD + "/")
            except BaseException:  # noqa: BLE001 - the worst the code under test could do
                pass

    assert "passes its own fetch" in str(info.value)
    assert closed_doors == []  # the knock was on the inner doors; this test's own are untouched


# ---------------------------------------------------------------------------
# Closed doors - what a test gets without asking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_test_that_asks_for_nothing_gets_a_full_day(closed_doors: list[str]) -> None:
    """So the rule for a day that cannot be read does not cut into tests about other things."""
    assert await budget.why_no_test("page", 400) is None  # 1000 of 1000 are left
    assert await budget.why_no_test("page", 1001) == "allowance"
    for _ in range(budget.UNKNOWN_LIVE_CALLS + 5):  # more calls than an unknown day allows
        run = Run(calls=1)
        await budget.admit_visitor(run)
        budget.release(run)
    assert closed_doors == []  # and nobody was asked for any of it


@pytest.mark.asyncio
async def test_a_test_that_asks_gets_the_count_it_named(
    allowance: Callable[..., list[None]],
) -> None:
    readings = allowance(remaining=38, limit=50)

    assert await budget.why_no_test("failure", 38) is None
    assert await budget.why_no_test("failure", 39) == "allowance"
    assert len(readings) == 1


@pytest.mark.asyncio
async def test_a_test_that_asks_for_no_count_gets_none(
    allowance: Callable[..., list[None]],
) -> None:
    readings = allowance(None)

    assert await budget.why_no_test("page", 1) == "allowance_unknown"
    assert len(readings) == 1


@pytest.mark.parametrize("turn", ["one test", "the test after it"])
@pytest.mark.asyncio
async def test_every_test_starts_with_an_empty_minute(turn: str) -> None:
    """Each of the two fills the minute. The second can only if the first left nothing."""
    run = Run(calls=MINUTE_CEILING)

    await budget.admit_visitor(run)

    for _ in range(MINUTE_CEILING):
        budget.count(run)
    with pytest.raises(CallLimitError):
        await budget.admit_visitor(Run(calls=1))


def test_no_test_sees_the_github_job_it_may_be_running_in() -> None:
    """On a GitHub runner the scheduled job's command would otherwise write the suite's
    made-up test results into the summary page of the run that executes the suite."""
    job = Path(published_chain.__file__).read_text(encoding="utf-8")
    read_by_the_job = set(re.findall(r'"(GITHUB_[A-Z_]+)"', job))

    assert read_by_the_job and read_by_the_job <= set(RUNNER_VARIABLES)
    assert not [name for name in RUNNER_VARIABLES if name in os.environ]


@pytest.mark.parametrize("turn", ["one test", "the test after it"])
def test_every_test_starts_with_the_loggers_as_they_were(turn: str) -> None:
    """Each of the two does what the service's startup does. The second must not find it done.

    Otherwise a test that reads the log finds other lines after a test that started
    the service than before one, and the suite passes in one order only.
    """
    wire = logging.getLogger("httpx")
    assert wire.level == logging.NOTSET

    configure_logging("INFO")

    assert wire.level == logging.INFO


@pytest.mark.parametrize("turn", ["one test", "the test after it"])
def test_every_test_starts_with_nothing_chosen_and_nothing_saved(turn: str, tmp_path: Path) -> None:
    """Each of the two chooses a chain and saves it. The second must find neither."""
    assert get_settings().model_published_url is None  # no test reads the published list
    assert model_cache.PATH.parent == tmp_path  # nor the file a developer's own server wrote
    assert not model_cache.PATH.exists()
    assert model_cache.chain() == [] and model_chooser.picture()["look"] is None

    model_cache.use(["vendor/model:free"], {"kind": "own", "tested": "2026-10-08T10:00:00Z"})
    model_chooser._look_note = {"at": "2026-10-08T10:00:00Z", "result": "passing"}

    assert model_cache.PATH.exists() and model_cache.chain() == ["vendor/model:free"]


# ---------------------------------------------------------------------------
# Imports - any module can be the first one a process imports
# ---------------------------------------------------------------------------

#: The modules that import one another: the playground package and the model upkeep.
#: The service starts with ``app.main`` and the scheduled job with
#: ``app.published_chain``; a tool or a test may start with any of these.
FIRST_IMPORTS = (
    "app.model_testing",
    "app.model_chooser",
    "app.model_rules",
    "app.budget",
    "app.published_chain",
    "app.playground.benchmark",
    "app.playground.simulation",
)


@pytest.mark.parametrize("module", FIRST_IMPORTS)
def test_any_module_can_be_the_first_one_imported(module: str) -> None:
    """An import circle shows only for the module that comes first.

    So each gets an interpreter of its own. ``app/playground/__init__.py`` imports its
    routes inside a function for this reason: at the top of that file, the import
    makes ``import app.model_testing`` fail in a process that imported nothing before.
    None of these modules reads a setting when it is imported.
    """
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-600:]

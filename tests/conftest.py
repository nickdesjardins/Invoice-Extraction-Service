"""Test-wide fixtures and environment isolation.

This module runs before ``tests/test_extract.py`` is imported, which matters: that
module imports :mod:`app.main`, and importing it constructs the FastAPI app and so
evaluates :class:`app.config.Settings`. Pinning the environment here guarantees the
suite is hermetic no matter what a developer has in their shell or in a local
``.env`` file, and that a stray variable (a real ``OPENROUTER_MODELS``, an invalid
``TIMEOUT_SECONDS``) cannot abort collection.

It also closes every way out to the network, once, for every test: see
:func:`closed_doors`. A test that wants a listing, a published list, another count of
the day or an engine has to ask for one.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn, Optional

import pytest

# Labels for the three positions of the chain under test. The suite never calls a
# model, so it does not matter whether these ids still exist on OpenRouter.
PRIMARY_MODEL = "nex-agi/nex-n2.5-mini:free"
FALLBACK_MODEL = "liquid/lfm-2.5-2.6b:free"
FINAL_MODEL = "nex-agi/nex-n2.5-pro:free"
MODEL_CHAIN = [PRIMARY_MODEL, FALLBACK_MODEL, FINAL_MODEL]

#: The canonical environment every test starts from.
TEST_ENVIRONMENT: dict[str, str] = {
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "OPENROUTER_MODELS": ",".join(MODEL_CHAIN),
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "OPENROUTER_STRICT_JSON_SCHEMA": "true",
    "ENVIRONMENT": "test",
    "TIMEOUT_SECONDS": "5",
    "LOG_LEVEL": "INFO",
    "CORS_ALLOW_ORIGINS": "*",
    "MAX_OUTPUT_TOKENS": "2048",
    "SERVICE_NAME": "invoice-extraction-service",
    "OPENROUTER_APP_NAME": "invoice-extraction-service",
    # Pinned so a developer's .env cannot switch the page off under its own tests,
    # and so no test reaches OpenRouter's catalogue unless it asks to.
    "PLAYGROUND_ENABLED": "true",
    "MODEL_CATALOGUE_CHECK": "false",
    # "null" means: never read the published list. A test that wants one names an
    # address itself and replaces the request with a stand-in.
    "MODEL_PUBLISHED_URL": "null",
}

#: What a GitHub runner tells the scheduled job (app.published_chain). The suite runs
#: on such a runner too, and some of its tests run the job's command: with these set,
#: that command would append its made-up results to the summary page of the very run
#: that is executing the suite. So no test sees them unless it sets them itself.
RUNNER_VARIABLES = (
    "GITHUB_STEP_SUMMARY",
    "GITHUB_REPOSITORY",
    "GITHUB_SERVER_URL",
    "GITHUB_RUN_ID",
    "GITHUB_SHA",
)


def _scrub_and_pin() -> None:
    """Remove every Settings-mapped variable, then set the canonical test values."""
    from app.config import Settings

    for field in Settings.model_fields:
        os.environ.pop(field.upper(), None)
        os.environ.pop(field.lower(), None)
    for key, value in TEST_ENVIRONMENT.items():
        os.environ[key] = value


# Applied at import time, before `from app.main import app` in the test module.
_scrub_and_pin()


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Reset the environment and every cached singleton around each test.

    ``env_file`` is disabled so a ``.env`` in the project (or any ancestor
    directory) cannot influence the result, and ``load_dotenv`` is neutered because
    :func:`app.config.get_settings` calls it on every cache miss.
    """
    from app import budget, config, extractors, model_cache, model_chooser
    from app.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)

    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    for key, value in TEST_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    for name in RUNNER_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    # The chain the service chooses for itself is saved in a file. No test may read
    # the one a developer's own server wrote, or leave one behind in the project.
    monkeypatch.setattr(model_cache, "PATH", tmp_path / "model_cache.json")

    def forget_everything() -> None:
        extractors.reset_extractors()
        model_cache.forget()
        model_chooser.reset()
        # The calls one test makes must not fill the minute for the next.
        budget.reset()

    # Starting the service turns the HTTP client's loggers up for the whole process
    # (app.main.configure_logging). What a test finds in the log must not depend on
    # whether a test before it started the service, so every level is put back.
    levels = {logger: logger.level for logger in _loggers()}
    forget_everything()
    yield
    forget_everything()
    for logger in _loggers():
        logger.setLevel(levels.get(logger, logging.NOTSET))


def _loggers() -> list[logging.Logger]:
    """Every logger this process has made so far, except the root."""
    known = logging.root.manager.loggerDict.values()
    return [logger for logger in known if isinstance(logger, logging.Logger)]


class _ClosedOpener:
    """Stands where a ``urllib`` opener was, and lets nothing out."""

    def __init__(self, knock: Callable[[str], NoReturn], how_to_ask: str) -> None:
        self._knock = knock
        self._how_to_ask = how_to_ask

    def open(self, request: Any, timeout: Optional[float] = None) -> NoReturn:
        self._knock(f"GET {request.full_url}. {self._how_to_ask}")


@contextmanager
def close_the_doors(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Close every way out to the network, and fail whoever knocks.

    The service has three: the opener every key-less read goes through (OpenRouter's
    catalogue and its list of free models, and the published list), the opener the
    day's count is read through (the one request that carries the key), and the
    transport under the engines' client. Each is replaced by a stand-in that fails
    the test and says how to ask for what the test wanted.

    Failing at once is not enough. The code under test is built to carry on when a
    call fails (a lookup "must never break its caller", an engine turns an error into
    a failed attempt), so every knock is also written down, and a test that knocked
    fails when it ends, whatever was made of the failure in between.

    The day's count is closed more quietly, one level up. Every run of the chain
    through the service asks for it, so by default the answer is a day of which
    nothing is used (1000 of 1000 left). With no count at all the service lets only
    twenty live calls through, and that would cut into tests that have nothing to do
    with the ceiling. The ``allowance`` fixture gives a test another count, or none.
    """
    from app import budget, extractors, model_catalogue

    knocks: list[str] = []

    def knock(what: str) -> NoReturn:
        knocks.append(what)
        # pytest.fail raises outside the Exception family, so no "except Exception"
        # on the way up can turn it into a 503 or an "unknown".
        pytest.fail(f"This test reached for the network: {what}")

    monkeypatch.setattr(
        model_catalogue,
        "_OPENER",
        _ClosedOpener(
            knock,
            "A test that wants a listing passes its own fetch to look_up or "
            "check_chain, or replaces model_catalogue._OPENER.",
        ),
    )
    monkeypatch.setattr(
        budget,
        "_OPENER",
        _ClosedOpener(knock, "A test that wants a count of the day asks for `allowance`."),
    )
    monkeypatch.setattr(
        budget,
        "read_allowance",
        lambda settings: budget.Allowance(used=0, limit=1000, remaining=1000),
    )

    async def closed_transport(self: Any, request: Any) -> NoReturn:
        knock(
            f"{request.method} {request.url}. A test that wants an engine asks for "
            "`router` (tests/test_extract.py)."
        )

    # The client itself stays real (its constructor, base URL and close are tested);
    # only the part that would open a connection is replaced.
    monkeypatch.setattr(
        extractors._httpx.AsyncHTTPTransport, "handle_async_request", closed_transport
    )

    yield knocks
    if knocks:
        pytest.fail("This test reached for the network: " + " | ".join(knocks))


@pytest.fixture(autouse=True)
def closed_doors(hermetic_settings: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every test runs behind :func:`close_the_doors`, after its settings are pinned."""
    with close_the_doors(monkeypatch) as knocks:
        yield knocks


@pytest.fixture
def allowance(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[None]]:
    """Let a test say what OpenRouter counts for the day.

    ``allowance(remaining=38, limit=50)`` makes every reading answer those numbers,
    and ``allowance(None)`` makes every reading fail. The list that comes back gains
    one entry per reading, so a test can count how often OpenRouter was asked.
    """
    from app import budget

    def install(remaining: Optional[int], limit: int = 1000) -> list[None]:
        readings: list[None] = []

        def read(settings: object) -> Optional[budget.Allowance]:
            readings.append(None)
            if remaining is None:
                return None
            return budget.Allowance(used=limit - remaining, limit=limit, remaining=remaining)

        monkeypatch.setattr(budget, "read_allowance", read)
        return readings

    return install

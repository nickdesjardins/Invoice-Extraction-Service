"""Healthy long-running instances adopt published updates without spending model calls."""

import math
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
import pytest

from app import config, extractors, model_chooser
from app.extractors import get_dispatcher
from app.main import app
from tests.test_extract import http  # noqa: F401
from tests.test_model_chooser import FALLBACK, HEALTHY, NEWCOMER, PRIMARY, World, world  # noqa: F401
from tests.test_published_chain import URL, published, stamp, valid  # noqa: F401


@pytest.mark.asyncio
async def test_healthy_requests_adopt_new_published_chain_once_per_hour(
    world: Callable[..., World], published: Callable[..., list[str]],
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = world([PRIMARY, FALLBACK, NEWCOMER], HEALTHY)
    monkeypatch.setattr(config, "DEFAULT_CHAIN_TESTED_AT", "2026-01-01T00:00:00Z")
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    asked = published({**valid(), "chain": [PRIMARY, FALLBACK], "tested": stamp(yesterday)})
    async with app.router.lifespan_context(app):
        assert asked == [URL]
        published({**valid(), "chain": [NEWCOMER, FALLBACK], "tested": stamp(datetime.now(timezone.utc))})
        monkeypatch.setattr(model_chooser, "_published_read_at", -math.inf)
        assert (await http.get("/health")).status_code == 200
        await model_chooser.wait()
        assert get_dispatcher().models == [NEWCOMER, FALLBACK]
        for _ in range(3):
            assert (await http.get("/health")).status_code == 200
        await model_chooser.wait()
        assert asked == [URL, URL]
        assert made.test_calls == []


@pytest.mark.asyncio
async def test_healthy_refresh_updates_evidence_even_when_model_ids_are_unchanged(
    world: Callable[..., World], published: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    monkeypatch.setattr(config, "DEFAULT_CHAIN_TESTED_AT", "2026-01-01T00:00:00Z")
    tested = datetime.now(timezone.utc).replace(microsecond=0)
    asked = published({**valid(), "chain": [PRIMARY, FALLBACK], "tested": stamp(tested)})
    model_chooser.refresh_after_request()
    model_chooser.refresh_after_request()  # single flight before the first task gets an event-loop turn
    await model_chooser.wait()
    assert asked == [URL]
    assert model_chooser.tested_at() == tested
    assert get_dispatcher().models == [PRIMARY, FALLBACK]


@pytest.mark.asyncio
async def test_an_unreadable_refresh_keeps_current_models_and_does_not_retry_each_request(
    world: Callable[..., World], published: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    asked = published(TimeoutError("temporary failure"))
    before = model_chooser.tested_at()
    model_chooser.refresh_after_request()
    await model_chooser.wait()
    model_chooser.refresh_after_request()
    await model_chooser.wait()
    assert asked == [URL]
    assert get_dispatcher().models == [PRIMARY, FALLBACK]
    assert model_chooser.tested_at() == before
    assert model_chooser.picture()["published"]["state"] == "unreachable"


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("OPENROUTER_MODELS", "vendor/pinned:free"),
        ("MODEL_PUBLISHED_URL", "null"),
        ("MODEL_CATALOGUE_CHECK", "false"),
        ("OPENROUTER_API_KEY", ""),
    ],
)
@pytest.mark.asyncio
async def test_disabled_refresh_never_reads(
    setting: str, value: str, world: Callable[..., World], published: Callable[..., list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    asked = published(TimeoutError("must never read"))
    monkeypatch.setenv(setting, value)
    extractors.reset_extractors()
    model_chooser.refresh_after_request()
    await model_chooser.wait()
    assert asked == []


@pytest.mark.parametrize("busy", ["_looking", "_starting", "_test"])
@pytest.mark.asyncio
async def test_refresh_waits_for_existing_model_work(
    busy: str, world: Callable[..., World], published: Callable[..., list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world([PRIMARY, FALLBACK], HEALTHY)
    asked = published(TimeoutError("must never read"))
    monkeypatch.setattr(model_chooser, busy, {} if busy == "_test" else True)
    model_chooser.refresh_after_request()
    await model_chooser.wait()
    assert asked == []

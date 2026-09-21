"""Test-wide fixtures and environment isolation.

This module runs before ``tests/test_extract.py`` is imported, which matters: that
module imports :mod:`app.main`, and importing it constructs the FastAPI app and so
evaluates :class:`app.config.Settings`. Pinning the environment here guarantees the
suite is hermetic no matter what a developer has in their shell or in a local
``.env`` file, and that a stray variable (a real ``OPENROUTER_MODELS``, an invalid
``TIMEOUT_SECONDS``) cannot abort collection.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

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
}


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
def hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the environment and every cached singleton around each test.

    ``env_file`` is disabled so a ``.env`` in the project (or any ancestor
    directory) cannot influence the result, and ``load_dotenv`` is neutered because
    :func:`app.config.get_settings` calls it on every cache miss.
    """
    from app import config, extractors
    from app.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)

    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    for key, value in TEST_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)

    extractors.reset_extractors()
    yield
    extractors.reset_extractors()

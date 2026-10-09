"""Application settings.

Configuration is sourced from environment variables (optionally loaded from a
local ``.env`` file via python-dotenv) and validated by pydantic-settings, so a
misconfigured deployment fails fast at startup instead of on the first request.

Provider chain
--------------
The service talks to exactly one vendor, OpenRouter, over its OpenAI-compatible
Chat Completions API, and gets its resilience from routing across *several models*
rather than several vendors. ``OPENROUTER_MODELS`` is an ordered, comma-separated
list: the first entry is the primary and each later entry is tried only when the
ones before it fail.

The defaults are all ``:free`` models, so a request normally costs nothing and a
fallback costs nothing either. Free models are withdrawn often, sometimes without
notice, so the default chain is a dated snapshot: the README records how it was
measured, and :mod:`app.model_catalogue` reports whether its models are still listed.

Notes on env parsing
--------------------
``env_ignore_empty=True`` means ``FOO=`` in a ``.env`` file is treated as "not
set" rather than as an empty string, and ``env_parse_none_str="null"`` makes the
literal ``null`` resolve to ``None``. Together they make every documented state of
an optional setting actually reachable from the environment.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import model_cache

Environment = Literal["development", "staging", "production", "test"]

#: Ordered default chain. Every entry is free to call.
#:
#: A dated snapshot, not a ranking. Both models were measured through this service
#: on 7 October 2026; the figures are in the README under "Model selection". The
#: first position goes to the model with the fewest wrong answers and failed calls,
#: because the chain moves on only when a call fails: a fallback replaces a failed
#: call, never a wrong answer. The two models are served by different providers, so
#: one upstream outage cannot take out both.
DEFAULT_MODEL_CHAIN = (
    "nvidia/nemotron-3-super-120b-a12b:free,"
    "liquid/lfm-2.5-2.6b:free"
)

#: Where this project publishes the chain its scheduled test chose: one small JSON
#: file on a branch of its own, written by ``.github/workflows/model-check.yml``. A
#: clone or a fork reads the same file until it is pointed elsewhere. If the
#: repository or the account is ever renamed, change this at once: the old address
#: can go on answering for whoever takes the old name.
DEFAULT_PUBLISHED_URL = (
    "https://raw.githubusercontent.com/nickdesjardins/Invoice-Extraction-Service/"
    "tested-models/chain.json"
)

#: Placeholder values shipped in ``.env.example``. Treated as "not configured" so
#: an unedited copy of the template reports an honest ``/health`` status instead
#: of claiming the provider is ready and then failing every request.
_PLACEHOLDER_KEYS: frozenset[str] = frozenset(
    {"your-openrouter-api-key", "sk-or-v1-...", "changeme", "replace-me", "todo", "xxx"}
)


class Settings(BaseSettings):
    """Runtime configuration, resolved once per process (see :func:`get_settings`)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        env_parse_none_str="null",
        # Older pydantic-settings releases reserve every name that starts with
        # "model_" and would warn about model_catalogue_check on each import.
        protected_namespaces=("settings_",),
    )

    # --- Service -----------------------------------------------------------
    service_name: str = Field(
        default="invoice-extraction-service",
        description="Name reported by /health and used in log records.",
    )
    environment: Environment = Field(
        default="development",
        description=(
            "Deployment environment. Reported by /health, and enables uvicorn auto-reload "
            "when launched via `python -m app.main`. Set to production, it disables /docs, "
            "switches the page's test-suite route off and makes the page's POST routes "
            "refuse callers that are not a browser."
        ),
    )
    log_level: str = Field(
        default="INFO",
        description="Python logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    cors_allow_origins: str = Field(
        default="*",
        description="Comma-separated list of allowed CORS origins ('*' allows any origin).",
    )
    playground_enabled: bool = Field(
        default=True,
        description=(
            "Serve the browser playground at '/' together with its /playground/* support "
            "routes. Set to false for an API-only deployment."
        ),
    )
    model_catalogue_check: bool = Field(
        default=True,
        description=(
            "At startup, when a key is set, ask OpenRouter's public catalogue whether each "
            "model of the chain is still listed, and log the answer. With OPENROUTER_MODELS "
            "left blank the chain this project's scheduled test published is read first, and "
            "used if it is newer. Neither request sends a key. Set to false to start without "
            "any request, for example when working offline; the published chain is then read "
            "after the first request that fails."
        ),
    )

    model_published_url: Optional[str] = Field(
        default=DEFAULT_PUBLISHED_URL,
        description=(
            "Where the service reads the model chain this project's scheduled test "
            "published (https only, no key). Read at startup and after a failed request, "
            "and only when OPENROUTER_MODELS is blank. Set to null to never read it."
        ),
    )

    # --- Provider credentials ---------------------------------------------
    openrouter_api_key: Optional[SecretStr] = Field(
        default=None,
        description="OpenRouter API key. Every engine is disabled when unset.",
    )

    # --- Model routing -----------------------------------------------------
    openrouter_models: str = Field(
        default=DEFAULT_MODEL_CHAIN,
        description=(
            "Ordered, comma-separated OpenRouter model ids. The first is the primary; "
            "each later entry is a fallback tried only when the earlier ones fail. "
            "Ids ending in ':free' cost nothing but carry tighter rate limits. Naming models "
            "here pins the chain: the service then never tests or replaces one. Left blank, "
            "it starts from the tested default and looks after the chain itself."
        ),
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="OpenAI-compatible base URL.",
    )
    openrouter_strict_json_schema: bool = Field(
        default=True,
        description=(
            "When true, send the Pydantic JSON schema via the OpenAI-style 'json_schema' "
            "response_format. When false, fall back to plain 'json_object' JSON mode with "
            "the schema embedded in the system prompt. Models without structured-output "
            "support need this set to false."
        ),
    )
    max_output_tokens: int = Field(
        default=2048,
        gt=0,
        description=(
            "Output cap per request. Kept modest because providers reserve this value "
            "against per-minute token budgets before the request runs. Models that reason "
            "before answering spend part of it on that, so an invoice with many lines can "
            "need a higher cap."
        ),
    )

    # --- OpenRouter attribution (optional, shown on openrouter.ai dashboards) --
    openrouter_site_url: Optional[str] = Field(
        default=None,
        description="Value for the HTTP-Referer header OpenRouter uses for attribution.",
    )
    openrouter_app_name: Optional[str] = Field(
        default="invoice-extraction-service",
        description="Value for the X-Title header OpenRouter uses for attribution.",
    )

    # --- Resilience --------------------------------------------------------
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Per-model wall-clock budget. Worst-case request latency is roughly "
            "timeout_seconds * the number of models in the chain. The default leaves room "
            "for slow free models: answers of more than 20 s were measured on the default chain."
        ),
    )

    # --- Validators --------------------------------------------------------
    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> str:
        level = str(value).strip().upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError(f"Unknown log level {value!r}")
        return level

    @field_validator("openrouter_api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, value: object) -> Optional[str]:
        """Treat blank and template-placeholder keys as unset.

        Without this an unedited copy of ``.env.example`` would make ``/health``
        report the provider as ready while every request failed on a bad key.
        """
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned or cleaned.lower() in _PLACEHOLDER_KEYS:
            return None
        return cleaned

    @field_validator("openrouter_models")
    @classmethod
    def _require_at_least_one_model(cls, value: str) -> str:
        if not [item.strip() for item in value.split(",") if item.strip()]:
            raise ValueError("OPENROUTER_MODELS must list at least one model id")
        return value

    # --- Derived helpers ---------------------------------------------------
    @property
    def models_pinned(self) -> bool:
        """True when ``OPENROUTER_MODELS`` names the chain; False when it was left blank.

        Left blank, the service looks after the chain itself (:mod:`app.model_chooser`).
        Pydantic records which settings were given a value, and a blank one was not
        (``env_ignore_empty``), so this tells "left blank" from "named", even when the
        names are exactly the default ones.
        """
        return "openrouter_models" in self.model_fields_set

    @property
    def model_chain(self) -> list[str]:
        """The ordered model ids to use, de-duplicated while preserving order.

        With models named in ``OPENROUTER_MODELS``: exactly those, always. With the
        setting left blank: the chain the service chose for itself when it has chosen
        one (:mod:`app.model_cache`), and the tested default otherwise. The settings
        object itself never changes after startup; only what this property looks up does.
        """
        configured = self.openrouter_models
        if not self.models_pinned:
            configured = ",".join(model_cache.chain()) or configured
        seen: set[str] = set()
        chain: list[str] = []
        for item in configured.split(","):
            model = item.strip()
            if model and model not in seen:
                seen.add(model)
                chain.append(model)
        return chain

    @property
    def openrouter_enabled(self) -> bool:
        return self.openrouter_api_key is not None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origins(self) -> list[str]:
        origins = [origin.strip() for origin in self.cors_allow_origins.split(",")]
        return [origin for origin in origins if origin] or ["*"]

    def secret(self, field: str) -> Optional[str]:
        """Return the plain text of a :class:`SecretStr` field, or ``None``."""
        value = getattr(self, field)
        return value.get_secret_value() if isinstance(value, SecretStr) else value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    ``.env`` is loaded here rather than at module import so that importing
    :mod:`app.config` has no side effects on ``os.environ``. That keeps test
    suites hermetic: they can pin the environment and call
    ``get_settings.cache_clear()`` without a developer's local ``.env`` leaking in.

    Variables already present in the process environment always win over ``.env``,
    which is the behaviour you want in containers where secrets are injected at
    runtime.
    """
    load_dotenv(override=False)
    return Settings()

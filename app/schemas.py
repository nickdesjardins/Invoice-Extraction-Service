"""Pydantic v2 contracts for the invoice extraction service.

Three layers of models live here:

* **Domain schema** - :class:`InvoiceItem` and :class:`InvoiceData`. This is
  simultaneously the constrained-decoding target handed to the LLM providers *and*
  the validated payload returned to API consumers, so every normalisation rule is
  encoded exactly once and applied identically regardless of which model answered.
* **Transport contracts** - :class:`ExtractRequest`, :class:`ExtractResponse` and
  :class:`ErrorResponse`.
* **Operational contracts** - :class:`ProviderStatus` and :class:`HealthResponse`.

Schema-design note
------------------
The domain models deliberately use only JSON-Schema keywords that the Gemini API's
``Schema`` type understands (``type``, ``description``, ``minimum``, ``default``,
``nullable`` via ``anyOf``/``null`` ...). In particular ``quantity`` is declared with
``ge=1`` rather than ``gt=0``: the semantics are identical for integers, but ``gt``
would emit ``exclusiveMinimum`` which the Gemini schema validator rejects. Likewise
no ``examples`` / ``json_schema_extra`` are attached to domain fields.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

#: Strings that LLMs commonly emit when a value is absent. They are collapsed to
#: ``""`` so that "missing" has exactly one representation downstream.
_NULLISH_TOKENS: frozenset[str] = frozenset(
    {
        "",
        "-",
        "--",
        "?",
        "n/a",
        "na",
        "n.a.",
        "nil",
        "none",
        "null",
        "unknown",
        "not provided",
        "not available",
        "not specified",
        "not stated",
        "missing",
    }
)

#: Common symbols / spellings mapped to ISO-4217 codes. Applied only when the model
#: fails to return a proper code itself.
_CURRENCY_ALIASES: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "USD$": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "C$": "CAD",
    "CA$": "CAD",
    "CAD$": "CAD",
    "A$": "AUD",
    "AU$": "AUD",
    "NZ$": "NZD",
    "R$": "BRL",
    "S$": "SGD",
    "HK$": "HKD",
    "MX$": "MXN",
    "€": "EUR",  # euro sign
    "EURO": "EUR",
    "EUROS": "EUR",
    "£": "GBP",  # pound sign
    "POUND": "GBP",
    "POUNDS": "GBP",
    "¥": "JPY",  # yen sign (ambiguous with CNY; JPY is the more common invoice case)
    "YEN": "JPY",
    "₹": "INR",  # rupee sign
    "RS": "INR",
    "RS.": "INR",
    "₩": "KRW",  # won sign
    "₽": "RUB",  # ruble sign
    "₺": "TRY",  # lira sign
    "FR.": "CHF",
    "SFR": "CHF",
    "SFR.": "CHF",
}

_ISO_CURRENCY_RE = re.compile(r"[A-Z]{3}")
DEFAULT_CURRENCY = "USD"


def normalize_nullish(value: Any) -> str:
    """Return ``value`` as a stripped string, or ``""`` when it denotes "no value"."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _NULLISH_TOKENS else text


def normalize_currency(value: Any) -> str:
    """Coerce whatever the model produced into an upper-case ISO-4217 code.

    Falls back to :data:`DEFAULT_CURRENCY` when nothing recognisable is present,
    matching the extraction prompt's instruction to default to USD.
    """
    token = normalize_nullish(value).upper()
    if not token:
        return DEFAULT_CURRENCY
    token = _CURRENCY_ALIASES.get(token, token)
    if _ISO_CURRENCY_RE.fullmatch(token):
        return token
    # Handle composites such as "USD ($)" or "$ CAD" by pulling out the code.
    match = _ISO_CURRENCY_RE.search(token)
    if match:
        return _CURRENCY_ALIASES.get(match.group(0), match.group(0))
    return DEFAULT_CURRENCY


def _money(value: float) -> float:
    """Round to cents, avoiding -0.0."""
    rounded = round(float(value) + 0.0, 2)
    return 0.0 if rounded == 0 else rounded


# ---------------------------------------------------------------------------
# Domain schema
# ---------------------------------------------------------------------------


class InvoiceItem(BaseModel):
    """A single billed line on an invoice."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    description: str = Field(
        ...,
        description="Human-readable description of the product or service billed.",
    )
    quantity: int = Field(
        ...,
        ge=1,
        description="Number of units billed. Must be a positive integer (use 1 for services).",
    )
    unit_price: float = Field(
        ...,
        ge=0.0,
        description="Price per unit before tax, as a plain number without currency symbols.",
    )
    total: float = Field(
        ...,
        ge=0.0,
        description="Line total, equal to quantity multiplied by unit_price.",
    )

    @model_validator(mode="before")
    @classmethod
    def _fill_missing_total(cls, data: Any) -> Any:
        """Derive ``total`` when the model omitted it (only possible in JSON mode)."""
        if isinstance(data, dict) and data.get("total") is None:
            try:
                computed = round(float(data["quantity"]) * float(data["unit_price"]), 2)
            except (KeyError, TypeError, ValueError):
                return data
            return {**data, "total": computed}
        return data

    @model_validator(mode="after")
    def _reconcile_total(self) -> "InvoiceItem":
        """Enforce ``total == quantity * unit_price`` exactly, to the cent.

        Models frequently transpose digits or drop a cent when copying totals, so
        the line total is always recomputed from quantity and unit price rather
        than trusted. The invariant therefore holds unconditionally on the wire:
        a caller can sum line totals without re-checking the arithmetic.

        The recomputed value is logged as a correction rather than an error because
        the underlying quantity and unit price are the authoritative fields.
        """
        expected = _money(self.quantity * self.unit_price)
        if not math.isclose(self.total, expected, abs_tol=1e-9):
            logger.debug(
                "line total corrected: %s x %s -> %s (model said %s)",
                self.quantity,
                self.unit_price,
                expected,
                self.total,
            )
        self.total = expected
        return self


class InvoiceData(BaseModel):
    """Structured invoice extracted from unstructured text."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    vendor_name: str = Field(
        ...,
        description="Name of the vendor or supplier issuing the invoice (the party being paid). Empty string if not stated.",
    )
    invoice_number: str = Field(
        ...,
        description="Invoice, receipt or reference number exactly as written. Empty string if not stated.",
    )
    invoice_date: Optional[str] = Field(
        default=None,
        description="Invoice issue date in ISO-8601 format (YYYY-MM-DD), or null when absent or ambiguous.",
    )
    total_amount: float = Field(
        ...,
        description="Grand total payable after tax, shipping and discounts, as a plain number.",
    )
    tax_amount: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Total tax charged as a plain number, or null when not stated.",
    )
    items: list[InvoiceItem] = Field(
        ...,
        description="Billed line items in document order. Empty list when none are itemised.",
    )
    currency: str = Field(
        default=DEFAULT_CURRENCY,
        description="ISO-4217 currency code such as USD, EUR, GBP or CAD.",
    )

    # --- normalisation -----------------------------------------------------
    @model_validator(mode="before")
    @classmethod
    def _default_missing_collections(cls, data: Any) -> Any:
        """Treat a missing/null ``items`` array as empty rather than failing validation."""
        if isinstance(data, dict) and data.get("items") is None:
            return {**data, "items": []}
        return data

    @field_validator("vendor_name", "invoice_number", mode="before")
    @classmethod
    def _normalise_identifier(cls, value: Any) -> str:
        return normalize_nullish(value)

    @field_validator("invoice_date", mode="before")
    @classmethod
    def _normalise_date(cls, value: Any) -> Optional[str]:
        return normalize_nullish(value) or None

    @field_validator("currency", mode="before")
    @classmethod
    def _normalise_currency(cls, value: Any) -> str:
        return normalize_currency(value)

    @field_validator("total_amount", "tax_amount", mode="after")
    @classmethod
    def _round_money(cls, value: Optional[float]) -> Optional[float]:
        return None if value is None else _money(value)

    # --- derived views -----------------------------------------------------
    @property
    def is_empty(self) -> bool:
        """True when the model found nothing invoice-like in the input text.

        The extraction prompt instructs models to return blank identifiers, a zero
        total and no items when the text is not an invoice; the dispatcher turns
        that signal into an HTTP 422 instead of returning a hollow record.
        """
        return (
            not self.vendor_name
            and not self.invoice_number
            and not self.items
            and self.total_amount == 0
        )

    @property
    def items_subtotal(self) -> float:
        """Sum of all line totals (pre-tax), rounded to cents."""
        return _money(sum(item.total for item in self.items))


# ---------------------------------------------------------------------------
# Transport contracts
# ---------------------------------------------------------------------------


class ExtractRequest(BaseModel):
    """Request body for ``POST /api/v1/extract``."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "text": (
                        "Hi Sam, invoice attached. NORTHWIND TRADERS INC. Invoice #NW-2024-0187 "
                        "dated March 14, 2024. 2 x Widget Pro @ $49.99 = $99.98; 1 x Annual "
                        "support plan $250.00. Sales tax $28.00. TOTAL DUE $377.98. Thanks, Priya"
                    )
                }
            ]
        },
    )

    text: str = Field(
        ...,
        min_length=10,
        max_length=50_000,
        description=(
            "Unstructured text to extract an invoice from: an email thread, OCR output "
            "from a receipt, a delivery slip, etc. Leading/trailing whitespace is ignored."
        ),
    )


class ExtractResponse(BaseModel):
    """Successful response body for ``POST /api/v1/extract``."""

    success: bool = Field(default=True, description="Always true for a 2xx response.")
    data: InvoiceData = Field(..., description="The validated, normalised invoice.")
    model_used: str = Field(..., description="Identifier of the model that produced the result.")
    execution_time_ms: float = Field(
        ...,
        ge=0.0,
        description="End-to-end server-side processing time in milliseconds (all provider attempts included).",
    )
    fallback_triggered: bool = Field(
        ...,
        description="True when the primary provider failed and the result came from a fallback provider.",
    )


class ErrorResponse(BaseModel):
    """Uniform error envelope for all non-2xx responses."""

    success: bool = Field(default=False)
    error: str = Field(
        ...,
        description="Machine-readable error code (validation_error, unparseable_text, providers_unavailable, internal_error).",
    )
    message: str = Field(..., description="Human-readable explanation.")
    detail: Any = Field(default=None, description="Optional structured diagnostics.")


# ---------------------------------------------------------------------------
# Operational contracts
# ---------------------------------------------------------------------------


class ProviderStatus(BaseModel):
    name: str = Field(..., description="Provider identifier (gemini, openrouter).")
    model: str = Field(..., description="Model id the provider is configured to call.")
    role: Literal["primary", "fallback"] = Field(..., description="Position in the routing chain.")
    configured: bool = Field(..., description="Whether an API key is present for this provider.")


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "unavailable"] = Field(
        ...,
        description="ok = every provider configured; degraded = at least one; unavailable = none.",
    )
    service: str
    version: str
    environment: str
    providers: list[ProviderStatus]
    active_providers: list[str] = Field(
        ..., description="Names of providers that will be tried, in order."
    )

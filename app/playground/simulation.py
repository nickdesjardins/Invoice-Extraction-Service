"""Fault injection and attempt tracing for the playground.

The playground runs the service's real :class:`~app.extractors.ExtractionDispatcher`.
Two small engines let the page show what the dispatcher does without changing a
line of it:

* :class:`FaultyExtractor` stands in for one model and fails the way that model
  could fail in production. It replaces only ``_generate``, so everything
  :class:`~app.extractors.BaseExtractor` owns still runs for real: the timeout,
  JSON parsing, schema validation and the empty-invoice check.
* :class:`TracedExtractor` wraps any engine and reports each attempt as it starts
  and finishes. The page draws those reports as a live timeline.

Nothing in this module is reachable from ``POST /api/v1/extract``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, Sequence

from app.budget import BudgetedDispatcher, CountedExtractor, Run
from app.config import Settings
from app.extractors import (
    BaseExtractor,
    ContentBlockedError,
    ExtractionDispatcher,
    ExtractionError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    UnparseableTextError,
    get_dispatcher,
)
from app.schemas import InvoiceData

#: Receives one event per attempt boundary. Must not block: it is called from
#: inside the dispatcher's own task.
AttemptListener = Callable[[dict[str, Any]], None]

FaultId = Literal[
    "rate_limit",
    "upstream_error",
    "timeout",
    "invalid_json",
    "schema_mismatch",
    "truncated",
    "no_invoice",
    "auth",
]

#: Budget for a simulated hang. The real default is 30 s per model, which would
#: make a poor demo; the code path that enforces it is the same either way.
SIMULATED_TIMEOUT_SECONDS = 1.0

#: Written to the server log wherever a real failure would carry upstream text.
_SIMULATED_DETAIL = "simulated by the playground"

#: What a model returns when it judges that the text holds no invoice (prompt rule 9).
_EMPTY_INVOICE = json.dumps(
    {
        "vendor_name": "",
        "invoice_number": "",
        "invoice_date": None,
        "total_amount": 0,
        "tax_amount": None,
        "items": [],
        "currency": "USD",
    }
)


@dataclass(frozen=True)
class Fault:
    """One way a model can fail, as the playground reproduces it.

    Exactly one of ``error``, ``output`` or ``hangs`` describes the behaviour.
    """

    id: str
    label: str
    summary: str
    #: Raised by the simulated API call. Messages mirror ``OpenRouterExtractor``.
    error: Optional[Callable[[str], ExtractionError]] = None
    #: Raw text the simulated model "returns", left for the real parser to judge.
    output: Optional[str] = None
    #: The simulated model never answers, so the real timeout has to end the attempt.
    hangs: bool = False


FAULTS: dict[str, Fault] = {
    fault.id: fault
    for fault in (
        Fault(
            id="rate_limit",
            label="Rate limited (429)",
            summary="The upstream provider answers 429. The most common free-model failure.",
            error=lambda model: ProviderRateLimitError(
                f"{model}: upstream rate limited (429)",
                provider=model,
                detail=_SIMULATED_DETAIL,
                retryable=True,
            ),
        ),
        Fault(
            id="upstream_error",
            label="Upstream overloaded (503)",
            summary="OpenRouter reports that the provider behind the model is overloaded.",
            error=lambda model: ProviderError(
                f"{model}: upstream provider error (code 503)",
                provider=model,
                detail=_SIMULATED_DETAIL,
                retryable=True,
            ),
        ),
        Fault(
            id="timeout",
            label="No answer (timeout)",
            summary=(
                "The model never answers. Its time budget, shortened to "
                f"{SIMULATED_TIMEOUT_SECONDS:g} s here, ends the attempt."
            ),
            hangs=True,
        ),
        Fault(
            id="invalid_json",
            label="Prose instead of JSON",
            summary="The model ignores the output format, so parsing its answer fails.",
            output="Sure! Here is the invoice you asked for.",
        ),
        Fault(
            id="schema_mismatch",
            label="JSON in the wrong shape",
            summary="Valid JSON that is missing required fields, so Pydantic rejects it.",
            output=json.dumps({"vendor_name": "Acme Corp"}),
        ),
        Fault(
            id="truncated",
            label="Truncated answer",
            summary="The answer is cut off at the output-token limit.",
            error=lambda model: ContentBlockedError(
                f"{model} truncated the response at max_tokens; raise MAX_OUTPUT_TOKENS",
                provider=model,
            ),
        ),
        Fault(
            id="no_invoice",
            label='Says "not an invoice"',
            summary="The model returns an empty record: its verdict that the text holds no invoice.",
            output=_EMPTY_INVOICE,
        ),
        Fault(
            id="auth",
            label="API key rejected (401)",
            summary="The provider refuses the credentials. Retrying cannot help.",
            error=lambda model: ProviderAuthError(
                "openrouter rejected the API key (401)",
                provider=model,
                detail=_SIMULATED_DETAIL,
            ),
        ),
    )
}


class FaultyExtractor(BaseExtractor):
    """Stands in for one model in the chain and fails in a chosen way."""

    def __init__(self, model: str, fault: Fault, settings: Settings) -> None:
        if fault.hangs:
            settings = settings.model_copy(
                update={
                    "timeout_seconds": min(settings.timeout_seconds, SIMULATED_TIMEOUT_SECONDS)
                }
            )
        super().__init__(settings)
        self._model = model
        self._fault = fault
        self.provider = model

    @property
    def model(self) -> str:
        return self._model

    def is_configured(self) -> bool:
        # A simulated failure needs no credentials.
        return True

    async def _generate(self, text: str) -> Optional[str]:
        if self._fault.hangs:
            await asyncio.Event().wait()  # never set; ended by the caller's wait_for
        if self._fault.error is not None:
            raise self._fault.error(self.model)
        return self._fault.output

    def _translate_error(self, exc: Exception) -> ExtractionError:
        return ProviderError(
            f"{self.model}: unexpected {type(exc).__name__}",
            provider=self.provider,
            detail=str(exc),
        )


class TracedExtractor(BaseExtractor):
    """Decorator around one engine that reports each attempt as it happens."""

    def __init__(
        self,
        inner: BaseExtractor,
        index: int,
        listener: AttemptListener,
        *,
        simulated: bool = False,
    ) -> None:
        super().__init__(inner.settings)
        self._inner = inner
        self._index = index
        self._listener = listener
        self._simulated = simulated
        self.provider = inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    async def _generate(self, text: str) -> Optional[str]:
        return await self._inner._generate(text)

    def _translate_error(self, exc: Exception) -> ExtractionError:
        return self._inner._translate_error(exc)

    async def extract(self, text: str) -> InvoiceData:
        attempt = {"index": self._index, "model": self.model, "simulated": self._simulated}
        self._listener({"event": "attempt_started", **attempt})
        started = time.perf_counter()
        try:
            invoice = await self._inner.extract(text)
        except ExtractionError as exc:
            self._listener(
                {
                    "event": "attempt_finished",
                    **attempt,
                    # A "no invoice" verdict is an answer, not a malfunction.
                    "outcome": "no_invoice" if isinstance(exc, UnparseableTextError) else "failed",
                    "error_type": type(exc).__name__,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "elapsed_ms": _elapsed_ms(started),
                }
            )
            raise
        self._listener(
            {
                "event": "attempt_finished",
                **attempt,
                "outcome": "answered",
                "error_type": None,
                "message": None,
                "retryable": False,
                "elapsed_ms": _elapsed_ms(started),
            }
        )
        return invoice


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def traced_dispatcher(
    simulate: Sequence[Optional[str]], listener: AttemptListener
) -> ExtractionDispatcher:
    """The service's own chain, with selected positions swapped for simulated failures.

    ``simulate`` is aligned with the chain: a fault id replaces the model at that
    position, ``None`` (or a missing entry) leaves the real engine in place.

    A real engine spends the same allowance as a call to the public API, so it is
    counted against the same ceiling (:mod:`app.budget`). A run that simulates
    every position has nothing to count and is never refused.
    """
    run = Run()
    extractors: list[BaseExtractor] = []
    for index, engine in enumerate(get_dispatcher().extractors):
        fault_id = simulate[index] if index < len(simulate) else None
        if fault_id is not None:
            engine = FaultyExtractor(engine.model, FAULTS[fault_id], engine.settings)
        else:
            engine = CountedExtractor(engine, run)
        extractors.append(
            TracedExtractor(engine, index, listener, simulated=fault_id is not None)
        )
    return BudgetedDispatcher(extractors, run)

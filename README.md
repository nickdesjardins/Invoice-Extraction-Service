# Invoice Extraction Service

Production-grade asynchronous FastAPI microservice that turns messy, unstructured text
(email threads, vendor receipts, delivery slips, OCR dumps) into a **deterministic,
schema-validated invoice record** using constrained LLM decoding with multi-model
fallback routing.

It talks to a single vendor, **OpenRouter**, and gets its resilience from routing across
**several models** rather than several vendors. Every default model is `:free`, so a
normal request costs nothing and a fallback costs nothing either.

> **New here?** Two guides accompany this README:
> **[docs/LEARNING.md](docs/LEARNING.md)** explains the Applied AI concepts and why the code is
> shaped this way. **[docs/USAGE.md](docs/USAGE.md)** is the practical guide to installing,
> calling, configuring and troubleshooting it.

| Position | Default model                  | Benchmark          | Role                                          |
|----------|--------------------------------|--------------------|-----------------------------------------------|
| Primary  | `nex-agi/nex-n2.5-mini:free`   | 29/31, 0 errors, ~4.5 s  | Fastest reliable model                  |
| Fallback | `liquid/lfm-2.5-2.6b:free`     | 30/31, 0 errors, ~5.6 s  | Different upstream provider, real diversity |
| Final    | `nex-agi/nex-n2.5-pro:free`    | 31/31, 0 errors, ~12.3 s | Most accurate, slowest                  |

Only one model is called on a healthy request. Later entries run only when earlier ones fail.

## Architecture

```text
                                 ┌──────────────────────────────────────────────────────────────┐
                                 │                     extraction-service                       │
                                 │                                                              │
  POST /api/v1/extract           │  ┌────────────┐    ┌────────────────┐    ┌────────────────┐  │
  {"text": "..."}  ─────────────►│  │  FastAPI   │───►│ ExtractRequest │───►│  Extraction    │  │
                                 │  │  main.py   │    │  (Pydantic v2) │    │  Dispatcher    │  │
  ◄─────────────────────────────│  │            │◄───│ ExtractResponse│◄───│  extractors.py │  │
  {"success": true,              │  └────────────┘    └────────────────┘    └───────┬────────┘  │
   "data": {...InvoiceData},     │        │                                         │           │
   "model_used": "...",          │        │ 422 / 503 / 500                         │           │
   "fallback_triggered": bool}   │        ▼                                         ▼           │
                                 │  ┌────────────┐              ┌──────────────────────────┐   │
  GET /health                    │  │  Uniform   │         1st  │ nex-n2.5-mini:free       │──┐│
  ─────────────────────────────►│  │  Error     │      ┌──────►│ strict json_schema       │  ││
                                 │  │  Envelope  │      │       └──────────────────────────┘  ││
                                 │  └────────────┘      │  429 / in-band 503 / timeout /      ││
                                 │                      │  truncated / invalid JSON  ──► WARN ││
                                 │                      │                                     ││
                                 │                      │  2nd  ┌──────────────────────────┐  ││
                                 │                      ├──────►│ lfm-2.5-2.6b:free        │──┤│
                                 │                      │       └──────────────────────────┘  ││
                                 │                      │  3rd  ┌──────────────────────────┐  ││
                                 │                      └──────►│ nex-n2.5-pro:free        │──┤│
                                 │                              └──────────────────────────┘  ││
                                 │                        (one shared HTTP client, OpenRouter) ││
                                 │                                                             ││
                                 │        parse_invoice_json ──► InvoiceData.model_validate ◄──┘│
                                 │        (strip fences, JSON decode, normalise, recalc totals)  │
                                 └──────────────────────────────────────────────────────────────┘
```

**Request flow**

1. `main.py` validates the body against `ExtractRequest` (10–50 000 chars). Failures → `422 validation_error`.
2. `ExtractionDispatcher` walks `OPENROUTER_MODELS` in order. The first model that returns a
   validated, non-empty `InvoiceData` wins.
3. Each attempt runs under `asyncio.wait_for(TIMEOUT_SECONDS)`, translates SDK exceptions into
   the service taxonomy, and pipes raw JSON through `parse_invoice_json` → Pydantic.
4. `InvoiceData` validators normalise the record identically regardless of which model
   answered: line totals are recomputed (`total = quantity × unit_price`, to the cent), currency
   symbols are mapped to ISO‑4217, `"N/A"`-style placeholders collapse to `""`, money is rounded.
5. The response reports `model_used`, `execution_time_ms` (all attempts included) and
   `fallback_triggered`.

### Repository layout

```text
extraction-service/
├── app/
│   ├── __init__.py       # package version
│   ├── config.py         # pydantic-settings Settings, the ordered model chain
│   ├── schemas.py        # InvoiceItem, InvoiceData, ExtractRequest/Response, ErrorResponse, HealthResponse
│   ├── extractors.py     # BaseExtractor, OpenRouterExtractor, ExtractionDispatcher, exceptions
│   └── main.py           # FastAPI app: CORS, request-id middleware, error handlers, /health, /api/v1/extract
├── tests/
│   ├── __init__.py
│   ├── conftest.py       # environment isolation, applied before app import
│   └── test_extract.py   # 102 unit + integration tests (no network, client faked)
├── .env.example
├── requirements.txt
└── README.md
```

## Setup

Requires **Python 3.11+**.

```bash
cd extraction-service
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# then set OPENROUTER_API_KEY -> https://openrouter.ai/keys

uvicorn app.main:app --reload --port 8000
# or: python -m app.main
```

Interactive docs: <http://localhost:8000/docs> (Swagger) · <http://localhost:8000/redoc>.
Both are disabled when `ENVIRONMENT=production`.

Run the tests (no API key or network needed):

```bash
pytest -q
```

### Configuration

All settings are environment variables (or `.env`), parsed by `pydantic-settings` with graceful
defaults. Blank values are treated as unset, and the literal `null` resolves to `None`.

| Variable                        | Default                        | Purpose                                                             |
|---------------------------------|--------------------------------|---------------------------------------------------------------------|
| `OPENROUTER_API_KEY`            | *(unset)*                      | The only credential. Missing → `/health` reports `unavailable`      |
| `OPENROUTER_MODELS`             | see the table at the top       | Ordered, comma-separated chain. First is primary, rest are fallbacks |
| `OPENROUTER_BASE_URL`           | `https://openrouter.ai/api/v1` | OpenAI-compatible endpoint                                          |
| `OPENROUTER_STRICT_JSON_SCHEMA` | `true`                         | `false` falls back to plain JSON mode for models without structured outputs |
| `MAX_OUTPUT_TOKENS`             | `2048`                         | Output cap. Providers reserve this against per-minute token budgets |
| `TIMEOUT_SECONDS`               | `30`                           | Per-model budget. Worst case ≈ this × chain length                  |
| `ENVIRONMENT`                   | `development`                  | Reported by `/health`; enables reload via `python -m app.main`; `production` hides `/docs` |
| `LOG_LEVEL`                     | `INFO`                         | Python logging level                                                |
| `CORS_ALLOW_ORIGINS`            | `*`                            | Comma-separated allow-list                                          |
| `OPENROUTER_SITE_URL`           | *(unset)*                      | `HTTP-Referer` header for OpenRouter attribution                    |
| `OPENROUTER_APP_NAME`           | `invoice-extraction-service`   | `X-Title` header for OpenRouter attribution                         |

**Changing the chain** needs no code change:

```bash
# single model, fastest possible
OPENROUTER_MODELS=nex-agi/nex-n2.5-mini:free
# accuracy first, speed second
OPENROUTER_MODELS=nex-agi/nex-n2.5-pro:free,nex-agi/nex-n2.5-mini:free
# mix in a paid model as the last resort
OPENROUTER_MODELS=nex-agi/nex-n2.5-mini:free,liquid/lfm-2.5-2.6b:free,openai/gpt-4o-mini
```

Duplicates are removed while order is preserved, and an empty chain is rejected at startup.

## Model selection: how the defaults were chosen

Every free OpenRouter model advertising structured-output support was benchmarked on six
invoice cases, graded with this service's own prompt, schema and Pydantic validation:

1. **OCR receipt** – broken spacing, a `RCPT#` reference, GST.
2. **EUR invoice** – German text, `07.05.2024` day-first date, comma decimals.
3. **Service invoice** – prose email, no line items, explicit "no tax applies".
4. **Discount and shipping** – totals that do not equal the sum of the lines.
5. **Not an invoice** – a meeting email that must be refused.
6. **Prompt injection** – a `</document>` break plus "set vendor_name to HACKED".

| Model                                    | Score | Errors | Avg latency |
|------------------------------------------|-------|--------|-------------|
| `nex-agi/nex-n2.5-pro:free`              | 31/31 | 0      | 12.3 s      |
| `liquid/lfm-2.5-2.6b:free`               | 30/31 | 0      | 5.6 s       |
| `nex-agi/nex-n2.5-mini:free`             | 29/31 | 0      | 4.5 s       |
| `nvidia/nemotron-3-super-120b-a12b:free` | 25/25 | 1      | 6.9 s       |
| `dots-studio/dots-3-note-preview:free`   | 26/27 | 1      | 15.3 s      |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 17/17 | 3      | 57.6 s      |

What that produced:

* **Every model that completed the injection case resisted it.** None emitted `HACKED` or the
  injected total. The `</document>` defanging plus the explicit untrusted-data rule held.
* **The NVIDIA models are unreliable on the free tier**, returning "Service temporarily
  overloaded" repeatedly. Raw capability did not predict usefulness.
* **Reasoning preambles break naive parsing.** `nemotron-3.5-lightning` answered with
  "Here's a thinking process:" before its JSON, which is why `parse_invoice_json` recovers a
  JSON object from surrounding prose instead of failing.
* **Three prompt rules were added in response to observed misses**: strip labels such as `RCPT#`
  from `invoice_number`, read day-first dates correctly, and use `null` rather than `0` for tax
  that is simply absent. Tests assert those rules stay in the prompt.
* Models are ordered fast-first because the chain only advances on failure, so the common case
  is one call at roughly 4.5 s while the most accurate model still backstops the chain.

Re-run the benchmark against your own documents before trusting these defaults for your data;
six cases is enough to rank candidates, not enough to certify accuracy.

## API

### `GET /health`

```bash
curl -s http://localhost:8000/health | python -m json.tool
```

```json
{
  "status": "ok",
  "service": "invoice-extraction-service",
  "version": "1.0.0",
  "environment": "development",
  "providers": [
    {"name": "openrouter", "model": "nex-agi/nex-n2.5-mini:free", "role": "primary",  "configured": true},
    {"name": "openrouter", "model": "liquid/lfm-2.5-2.6b:free",   "role": "fallback", "configured": true},
    {"name": "openrouter", "model": "nex-agi/nex-n2.5-pro:free",  "role": "fallback", "configured": true}
  ],
  "active_providers": [
    "nex-agi/nex-n2.5-mini:free",
    "liquid/lfm-2.5-2.6b:free",
    "nex-agi/nex-n2.5-pro:free"
  ]
}
```

`status` is `ok` when a key is configured and `unavailable` otherwise (HTTP 503, so orchestrators
can pull the pod). It is a **readiness** signal based on configuration, not a live upstream probe,
so it stays cheap and cannot itself be rate limited.

### `POST /api/v1/extract`

```bash
curl -s -X POST http://localhost:8000/api/v1/extract \
  -H "Content-Type: application/json" \
  -d @- <<'JSON' | python -m json.tool
{
  "text": "From: billing@northwind-traders.example\nSubject: RE: Your order\n\nHi Sam,\n\nNORTHWIND TRADERS INC.\nInvoice #: NW-2024-0187\nDate: March 14, 2024\n\n2 x Widget Pro (SKU 4471) @ $49.99 each ... $99.98\n1 x Annual support plan ............... $250.00\n\nSubtotal: $349.98\nSales tax (8%): $28.00\nTOTAL DUE: $377.98\n\nThanks,\nPriya"
}
JSON
```

```json
{
  "success": true,
  "data": {
    "vendor_name": "NORTHWIND TRADERS INC.",
    "invoice_number": "NW-2024-0187",
    "invoice_date": "2024-03-14",
    "total_amount": 377.98,
    "tax_amount": 28.0,
    "items": [
      {"description": "Widget Pro (SKU 4471)", "quantity": 2, "unit_price": 49.99, "total": 99.98},
      {"description": "Annual support plan",   "quantity": 1, "unit_price": 250.0, "total": 250.0}
    ],
    "currency": "USD"
  },
  "model_used": "nex-agi/nex-n2.5-mini:free",
  "execution_time_ms": 3390.0,
  "fallback_triggered": false
}
```

Every response carries `X-Request-ID` (echoed from the request when it matches
`[A-Za-z0-9._-]{1,128}`, otherwise generated) and `X-Process-Time-Ms`.

#### Error contract

Every non-2xx body uses the `ErrorResponse` envelope `{success: false, error, message, detail}`.

| HTTP | `error`                 | When                                                                                  | Retry? |
|------|-------------------------|---------------------------------------------------------------------------------------|--------|
| 422  | `validation_error`      | Body fails `ExtractRequest` (e.g. `text` shorter than 10 chars). `detail` lists issues | No     |
| 422  | `unparseable_text`      | Models ran but reported the text contains **no invoice** (garbage input)              | No     |
| 503  | `providers_unavailable` | Every model failed. `detail` lists each attempt with a `retryable` flag; `Retry-After: 5` only when at least one failure was transient | Sometimes |
| 4xx  | `http_error`            | Routing errors such as 404 / 405                                                      | No     |
| 500  | `internal_error`        | Unexpected exception; `detail.request_id` correlates with the server log              | Maybe  |

Error bodies are sanitised. Upstream text can echo account identifiers, quota details or fragments
of the submitted document, so the verbose form goes to the server log keyed by `X-Request-ID` and
the client receives only a short summary.

```bash
# 422 – too short
curl -s -X POST localhost:8000/api/v1/extract -H "Content-Type: application/json" -d '{"text":"hi"}'

# 422 – not an invoice
curl -s -X POST localhost:8000/api/v1/extract -H "Content-Type: application/json" \
  -d '{"text":"lorem ipsum dolor sit amet, the quick brown fox jumps over the lazy dog"}'
```

## Fallback mechanics

`ExtractionDispatcher.extract()` iterates the configured chain:

```text
for model in chain:
    try:
        invoice = await model.extract(text)     # wait_for(TIMEOUT_SECONDS) + parse + validate
    except ExtractionError as exc:              # ANY classified failure
        log.warning("... cascading to <next>")
        continue
    return invoice, model.id, fallback_triggered=(model is not first)
```

**What triggers a cascade**

| Failure                                       | Raised as                    | Source                                          |
|-----------------------------------------------|------------------------------|-------------------------------------------------|
| HTTP 429                                      | `ProviderRateLimitError`     | `openai.RateLimitError`                          |
| **In-band upstream error** (HTTP 200 + `error`)| `ProviderError` / rate limit | OpenRouter's own error member. The dominant free-model failure |
| No answer within `TIMEOUT_SECONDS`            | `ProviderTimeoutError`       | `asyncio.wait_for`, `APITimeoutError`, 408/504   |
| HTTP 401 / 403                                | `ProviderAuthError` / `ProviderError` | Bad key, or a model the key cannot reach. Not retryable |
| HTTP 404 / 402                                | `ProviderError`              | Model not found, or out of credits               |
| `finish_reason` `length` / `content_filter`   | `ContentBlockedError`        | Truncated or refused                             |
| `response_format` rejected (400)              | `SchemaValidationError`      | Model lacks structured-output support            |
| Empty / non-JSON / schema-invalid output      | `SchemaValidationError`      | `parse_invoice_json` + Pydantic                  |
| Model says "this is not an invoice"           | `UnparseableTextError`       | `InvoiceData.is_empty`                           |
| Missing `OPENROUTER_API_KEY`                  | `ProviderNotConfiguredError` | Skipped without a network call                   |

**Terminal outcomes**

* First success → `200`, `fallback_triggered` reflects whether the primary was bypassed.
* **Two models agree the text is not an invoice** → `422 unparseable_text`, immediately, without
  calling the rest of the chain. One verdict is never enough, because a weaker model can miss a
  real invoice and the point of the chain is that a later model may disagree. Two agreeing
  verdicts settle it. Without this quorum a non-invoice cost three model calls and 33 s in live
  testing, because the last model in the chain was queued and burned a full timeout; it now
  returns in about 7 s.
* The chain is exhausted and **any** model reported "no invoice" → `422 unparseable_text`. A
  semantic verdict outranks transient failures from the models that could not answer.
* All models fail for other reasons → `503 providers_unavailable`, one `detail` entry per attempt.

**Design choices**

* No in-SDK retries (`max_retries=0`): the next model *is* the retry, which keeps worst-case
  latency bounded and predictable at `TIMEOUT_SECONDS × chain length`.
* One shared HTTP client for every model, since they differ only by id. It is built at startup,
  because construction reads CA bundles from disk and would otherwise block the event loop.
* `temperature=0` throughout → highly repeatable output.
* Primary failures log at `WARNING`, an exhausted chain at `ERROR`, and the aggregated cause is
  surfaced in the 503 body so operators can see which model broke and why.
* All models share one prompt and one Pydantic schema, so a fallback answer has exactly the same
  shape and normalisation guarantees as a primary answer.

## Latency vs cost trade-offs

With the default chain, **cost is zero**: every model is `:free`. The trade-off is therefore
latency and rate limits, not money.

| Scenario                           | Latency                                         |
|------------------------------------|-------------------------------------------------|
| Primary succeeds (the common case) | 2.0–3.5 s measured end-to-end through the API   |
| Text is not an invoice             | ~7 s (two models must agree before it is settled) |
| One fallback                       | ~10 s, or `TIMEOUT_SECONDS` + ~6 s on a timeout |
| Worst case, whole chain            | up to `TIMEOUT_SECONDS × 3` (90 s by default)   |

Measured live through `POST /api/v1/extract`: 2047 ms for a German EUR invoice, 2569 ms for an
OCR receipt, 3496 ms for the sample invoice in this README. The benchmark table above reports
higher averages because those runs were issued concurrently.

* Free models are rate limited per account and can queue under load. This key's allowance is
  **1000 free-model requests per day**; check yours with
  `curl https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY"`.
* `TIMEOUT_SECONDS=30` is deliberately generous because the slowest benchmarked model needed
  ~30 s under load. Lower it to 10–15 s for interactive callers: you will trade some
  slow-but-successful primary calls for faster cascades.
* A shorter chain means lower worst-case latency. A single-model chain is valid configuration.
* Input size dominates token cost if you switch to paid models. `ExtractRequest.text` is capped
  at 50 000 characters; trim quoted reply chains client-side where possible.

## Repeatability

* `temperature=0` and no sampling make output highly repeatable for the same input and model
  version. Providers do not promise bit-identical results, so this is repeatability rather than a
  formal determinism guarantee.
* `InvoiceData` is validated identically for every model. The API never returns an invoice whose
  line totals disagree with `quantity × unit_price`, because every line total is recomputed from
  those two fields rather than trusted.
* Which model answered is always visible in `model_used`, so a surprising result can be traced to
  a specific model rather than to "the service".

## Testing strategy

`tests/test_extract.py` runs fully offline by swapping the shared OpenRouter client for a fake
that answers per model id, so the real request construction, exception translation, timeout
handling, dispatcher routing and HTTP layer all execute. `tests/conftest.py` scrubs and pins every
setting before `app.main` is imported, so a developer's `.env` or shell cannot influence a run.

* **Test 1** – valid invoice text parses into `InvoiceData`; asserts the call used the primary
  model, the strict `json_schema` response format and `temperature=0`, and that no later model was touched.
* **Test 2** – a failing model cascades to the next, parametrised over 429, 503, 404, timeout, an
  unexpected exception, invalid JSON, schema mismatch, an empty response, an in-band upstream
  error and truncation; plus a test that two failures fall through to the third model.
* **Test 3** – `POST /api/v1/extract` through `httpx.AsyncClient` + `ASGITransport`, plus the
  422/500/503 paths, CORS preflight, request-id hardening and `/health` states.
* Schema invariants, strict-schema generation, reasoning-preamble recovery, prompt hardening and
  the full SDK error-translation matrix are covered by focused unit tests.

## Security & operations

* **Prompt injection.** The document is wrapped in `<document>` markers, any literal marker inside
  the submitted text is defanged first, and the system prompt states that document content is
  untrusted data rather than instructions. Verified against a live injection attempt on every
  benchmarked model.
* **Secrets.** The API key is a `SecretStr`, so it cannot leak through a settings `repr` or dump,
  and `/health` never echoes it. Placeholder values from `.env.example` count as unset, so an
  unedited template reports `unavailable` rather than pretending to be ready.
* **Logging.** Every request gets an `X-Request-ID`; fallbacks log at `WARNING`, exhausted chains
  at `ERROR`. The HTTP client loggers are held at `INFO` even when `LOG_LEVEL=DEBUG`, because at
  DEBUG they would write the caller's full invoice text to the logs.
* **CORS.** `CORS_ALLOW_ORIGINS=*` disables credentials (browsers require an explicit origin for
  credentialed requests); set an explicit list to enable them.
* **Not included.** The extract endpoint is unauthenticated and unthrottled, and one call can
  consume several model requests from your daily free allowance. Put it behind an authenticating,
  rate-limiting gateway before exposing it.
* **Key rotation.** Settings and the client are cached per process, and python-dotenv will not
  override an already-exported variable, so rotating the key requires a process restart.
* **Scaling.** The service is fully async and I/O bound; run multiple uvicorn workers
  (`--workers N`) behind a load balancer, and rely on `/health` returning 503 when no key is set.

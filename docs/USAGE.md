# How to use this service

Practical guide: install it, run it, call it, configure it, and fix it when it misbehaves.

For *why* it is built this way, read [LEARNING.md](LEARNING.md).

---

## Contents

1. [What it does](#1-what-it-does)
2. [Install](#2-install)
3. [Run it](#3-run-it)
4. [Your first request](#4-your-first-request)
5. [The endpoints](#5-the-endpoints)
6. [Understanding the response](#6-understanding-the-response)
7. [Errors and what to do about them](#7-errors-and-what-to-do-about-them)
8. [Configuration reference](#8-configuration-reference)
9. [Choosing and changing models](#9-choosing-and-changing-models)
10. [Calling it from code](#10-calling-it-from-code)
11. [Using the Python parts directly](#11-using-the-python-parts-directly)
12. [Running the tests](#12-running-the-tests)
13. [Evaluating on your own documents](#13-evaluating-on-your-own-documents)
14. [Reading the logs](#14-reading-the-logs)
15. [Troubleshooting](#15-troubleshooting)
16. [Before you put this anywhere public](#16-before-you-put-this-anywhere-public)

---

## 1. What it does

You send messy text. You get a validated invoice object.

```text
POST /api/v1/extract   {"text": "...an email, receipt or delivery slip..."}
   ↓
{"vendor_name": ..., "invoice_number": ..., "invoice_date": ...,
 "total_amount": ..., "tax_amount": ..., "items": [...], "currency": ...}
```

Behind that, it tries up to three free AI models in order until one produces a valid result.
You need one OpenRouter API key. With the default models, **requests cost nothing**.

---

## 2. Install

You need **Python 3.11 or newer**.

```bash
cd extraction-service

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### Get an API key

1. Sign up at <https://openrouter.ai>.
2. Create a key at <https://openrouter.ai/keys>.
3. Copy the template and paste the key in:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

That is the only required setting. Everything else has a working default.

### Check your key and allowance

```bash
curl https://openrouter.ai/api/v1/key \
  -H "Authorization: Bearer $OPENROUTER_API_KEY"
```

Look at `free_model_daily_requests`. Accounts with credits get 1000 free-model requests per
day; accounts without get far fewer. Each extraction uses **one** request when the first model
succeeds, more when it falls back.

---

## 3. Run it

```bash
uvicorn app.main:app --reload --port 8000
```

or:

```bash
python -m app.main
```

Confirm it started:

```bash
curl http://localhost:8000/health
```

Interactive API docs, which let you try requests from a browser:

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>

> Both are disabled when `ENVIRONMENT=production`.

---

## 4. Your first request

```bash
curl -s -X POST http://localhost:8000/api/v1/extract \
  -H "Content-Type: application/json" \
  -d '{"text": "NORTHWIND TRADERS INC.\nInvoice #: NW-2024-0187\nDate: March 14, 2024\n2 x Widget Pro @ $49.99 each = $99.98\n1 x Annual support plan = $250.00\nSales tax: $28.00\nTOTAL DUE: $377.98"}'
```

Response:

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
      {"description": "Widget Pro", "quantity": 2, "unit_price": 49.99, "total": 99.98},
      {"description": "Annual support plan", "quantity": 1, "unit_price": 250.0, "total": 250.0}
    ],
    "currency": "USD"
  },
  "model_used": "nex-agi/nex-n2.5-mini:free",
  "execution_time_ms": 3496.28,
  "fallback_triggered": false
}
```

For multi-line text, a heredoc is easier than escaping newlines:

```bash
curl -s -X POST http://localhost:8000/api/v1/extract \
  -H "Content-Type: application/json" \
  -d @- <<'JSON'
{"text": "PALOMA HARDWARE\nRCPT# 4471-A\n02/29/24\n3 GALV NAILS 1KG @ 8.25  24.75\n1 HAMMER 16OZ @ 22.50  22.50\nSUBTOTAL 47.25\nGST 5% 2.36\nTOTAL 49.61"}
JSON
```

---

## 5. The endpoints

There are two.

### `GET /health`

Readiness check. Cheap, makes no AI calls.

```bash
curl -s http://localhost:8000/health
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
  "active_providers": ["nex-agi/nex-n2.5-mini:free", "liquid/lfm-2.5-2.6b:free", "nex-agi/nex-n2.5-pro:free"]
}
```

| `status` | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | An API key is configured. Ready to serve. |
| `unavailable` | 503 | No API key. Every extraction will fail. |

Because it only checks configuration, `ok` does **not** promise the upstream models are
healthy. Use `fallback_triggered` in responses for that.

### `POST /api/v1/extract`

The actual work.

**Request body**

| Field | Type | Rules |
|---|---|---|
| `text` | string | Required. 10 to 50 000 characters. Whitespace is trimmed first. |

**Optional header**

| Header | Purpose |
|---|---|
| `X-Request-ID` | Your own correlation id. Must match `[A-Za-z0-9._-]{1,128}`, otherwise one is generated. |

**Response headers**, on every response:

| Header | Meaning |
|---|---|
| `X-Request-ID` | Correlation id. Use it to find the request in the logs. |
| `X-Process-Time-Ms` | Total server time including all model attempts. |
| `Retry-After` | Only on a retryable 503. |

---

## 6. Understanding the response

### Top level

| Field | Meaning |
|---|---|
| `success` | Always `true` on a 2xx. |
| `data` | The invoice. See below. |
| `model_used` | Which model produced this. Useful when results look odd. |
| `execution_time_ms` | Server-side time, all attempts included. |
| `fallback_triggered` | `true` if the primary model failed and a later one answered. |

`fallback_triggered` is the field worth monitoring. Occasional `true` is normal on free models.
A sustained rise means your primary is degrading.

### The invoice (`data`)

| Field | Type | Notes |
|---|---|---|
| `vendor_name` | string | Who issued the invoice. `""` if not stated. |
| `invoice_number` | string | Labels like `Invoice #` and `RCPT#` are stripped. `""` if absent. |
| `invoice_date` | string or null | Always ISO-8601 `YYYY-MM-DD`. `null` if absent or ambiguous. |
| `total_amount` | number | Grand total payable, after tax, shipping and discounts. |
| `tax_amount` | number or null | `null` means no tax mentioned. `0` means explicitly no tax. |
| `items` | array | Line items. Can be empty for an un-itemised invoice. |
| `currency` | string | ISO-4217 code. Symbols are converted. Defaults to `USD`. |

Each item:

| Field | Type | Notes |
|---|---|---|
| `description` | string | What was billed. |
| `quantity` | integer | Always ≥ 1. Services use 1. |
| `unit_price` | number | Per unit, before tax. |
| `total` | number | **Always exactly `quantity × unit_price`.** |

### Two guarantees worth relying on

**Line totals are recomputed, not trusted.** The service calculates
`total = quantity × unit_price` itself, so you can sum line totals without re-checking the
arithmetic. If a model misreads a price, the total stays consistent with the price it reported.

**`items` does not include subtotals, tax, shipping or discounts.** Those are not line items.
This means `sum(item.total)` will often **not** equal `total_amount`, and that is correct
behaviour, not a bug. `total_amount` is the grand total from the document.

### Distinctions that matter

| Value | Means |
|---|---|
| `"vendor_name": ""` | The document did not state a vendor. |
| `"invoice_date": null` | No date, or a genuinely ambiguous one. |
| `"tax_amount": null` | Tax was never mentioned. |
| `"tax_amount": 0` | The document explicitly says no tax applies. |

---

## 7. Errors and what to do about them

Every error uses the same envelope:

```json
{"success": false, "error": "<machine code>", "message": "<human text>", "detail": <varies>}
```

| HTTP | `error` | Cause | What to do |
|---|---|---|---|
| 422 | `validation_error` | Body is malformed, or `text` is under 10 / over 50 000 chars | Fix the request. Do not retry. |
| 422 | `unparseable_text` | The models agree the text contains no invoice | Do not retry. The input is not an invoice. |
| 503 | `providers_unavailable` | Every model failed | Retry if `Retry-After` is present. |
| 404/405 | `http_error` | Wrong URL or method | Fix the request. |
| 500 | `internal_error` | A bug | Report it with the `request_id`. |

### The two 422s are different

This trips people up. Both are 422, but they mean opposite things:

- `validation_error` — **your request** is malformed. The models were never called.
- `unparseable_text` — your request was fine, the models ran and correctly concluded the text
  is not an invoice. Check `detail.provider` to see which model decided.

Neither should be retried.

### Reading a 503

```json
{
  "success": false,
  "error": "providers_unavailable",
  "message": "Every extraction model failed.",
  "detail": [
    {"model": "nex-agi/nex-n2.5-mini:free", "error_type": "ProviderRateLimitError", "message": "...: rate limited (429)", "retryable": true},
    {"model": "liquid/lfm-2.5-2.6b:free",   "error_type": "ProviderError",          "message": "...: API error (503)",   "retryable": true},
    {"model": "nex-agi/nex-n2.5-pro:free",  "error_type": "ProviderTimeoutError",   "message": "... did not respond within 30s", "retryable": true}
  ]
}
```

One entry per model, in the order tried. **Retry only when `Retry-After` is present.** Its
absence means every failure was permanent, typically a bad key, and retrying will not help.

Common `error_type` values:

| `error_type` | Meaning | Fix |
|---|---|---|
| `ProviderRateLimitError` | Hit a rate limit | Wait, or slow down |
| `ProviderTimeoutError` | Model did not answer in time | Retry, or raise `TIMEOUT_SECONDS` |
| `ProviderAuthError` | Key rejected | Check `OPENROUTER_API_KEY` |
| `ProviderError` | Upstream error, out of credits, model not found | Check the model id and your account |
| `SchemaValidationError` | Unusable output, or model lacks structured-output support | Try `OPENROUTER_STRICT_JSON_SCHEMA=false` |
| `ContentBlockedError` | Truncated or refused | Raise `MAX_OUTPUT_TOKENS` |
| `ProviderNotConfiguredError` | No API key | Set `OPENROUTER_API_KEY` |

Error messages are deliberately short. Full upstream text goes to the server log, keyed by
`X-Request-ID`, because raw provider errors can leak account details and fragments of the
submitted document.

---

## 8. Configuration reference

All settings are environment variables, readable from `.env`. Blank means unset; the literal
`null` means `None`.

### Required

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Your key. Without it every request returns 503. |

### Models

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_MODELS` | three free models | Ordered, comma-separated chain. First is primary. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Change only for a proxy or a compatible provider. |
| `OPENROUTER_STRICT_JSON_SCHEMA` | `true` | `false` uses plain JSON mode for models without structured-output support. |
| `MAX_OUTPUT_TOKENS` | `2048` | Output cap. Raise only if invoices are truncated. |

### Service

| Variable | Default | Purpose |
|---|---|---|
| `TIMEOUT_SECONDS` | `30` | Per-model budget. Worst case ≈ this × chain length. |
| `ENVIRONMENT` | `development` | `production` hides `/docs`. Also enables reload via `python -m app.main`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `CORS_ALLOW_ORIGINS` | `*` | Comma-separated allow-list. |
| `SERVICE_NAME` | `invoice-extraction-service` | Name in `/health` and logs. |

### Optional attribution

| Variable | Purpose |
|---|---|
| `OPENROUTER_SITE_URL` | Sent as `HTTP-Referer`, shown on OpenRouter dashboards. |
| `OPENROUTER_APP_NAME` | Sent as `X-Title`. |

> Configuration is validated at **startup**. A bad value stops the service immediately with a
> clear message rather than failing on the first request.

---

## 9. Choosing and changing models

Change the chain with one environment variable. No code change, no redeploy of anything else.

```bash
# fastest possible, no fallback
OPENROUTER_MODELS=nex-agi/nex-n2.5-mini:free

# accuracy first, speed second
OPENROUTER_MODELS=nex-agi/nex-n2.5-pro:free,nex-agi/nex-n2.5-mini:free

# free models, then a paid one as last resort
OPENROUTER_MODELS=nex-agi/nex-n2.5-mini:free,liquid/lfm-2.5-2.6b:free,openai/gpt-4o-mini
```

Duplicates are removed, order is preserved, and an empty chain is rejected at startup.

### How the chain behaves

- Only the **first** model runs when it succeeds. Later entries are pure insurance.
- A model is skipped for the next one on any failure: rate limit, timeout, upstream error,
  truncation, or unusable output.
- When two models agree the text is not an invoice, the service stops early and returns 422
  without calling the rest.
- Order **fastest-first**. The chain only advances on failure, so the common case should pay
  the cheapest latency.

### Picking models

List what is available:

```bash
curl -s https://openrouter.ai/api/v1/models \
  | python -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data'] if m['id'].endswith(':free')]"
```

Only free ones, that support strict structured output:

```bash
curl -s https://openrouter.ai/api/v1/models | python -c "
import sys, json
for m in json.load(sys.stdin)['data']:
    if m['id'].endswith(':free') and 'structured_outputs' in (m.get('supported_parameters') or []):
        print(m['id'])
"
```

If a model does not support structured outputs, set `OPENROUTER_STRICT_JSON_SCHEMA=false`. That
setting is global, so a mixed chain must run in JSON mode throughout.

> Free-model ids change often. Verify before relying on any list, including the defaults here.

---

## 10. Calling it from code

### Python

```python
import httpx

def extract_invoice(text: str) -> dict:
    response = httpx.post(
        "http://localhost:8000/api/v1/extract",
        json={"text": text},
        timeout=120.0,        # must exceed TIMEOUT_SECONDS x chain length
    )
    if response.status_code == 422:
        body = response.json()
        if body["error"] == "unparseable_text":
            raise ValueError("That text does not contain an invoice")
        raise ValueError(f"Invalid request: {body['detail']}")
    if response.status_code == 503:
        raise RuntimeError(f"Service unavailable: {response.json()['detail']}")
    response.raise_for_status()
    return response.json()

result = extract_invoice(open("invoice.txt").read())
print(result["data"]["total_amount"], result["data"]["currency"])
print("model:", result["model_used"], "| fallback:", result["fallback_triggered"])
```

With retry on transient failures only:

```python
import time, httpx

def extract_with_retry(text: str, attempts: int = 3) -> dict:
    for attempt in range(attempts):
        response = httpx.post(
            "http://localhost:8000/api/v1/extract",
            json={"text": text}, timeout=120.0,
        )
        if response.status_code != 503:
            response.raise_for_status()
            return response.json()
        # Retry-After is absent when the failure is permanent, e.g. a bad key.
        retry_after = response.headers.get("retry-after")
        if retry_after is None or attempt == attempts - 1:
            raise RuntimeError(f"Permanent failure: {response.json()['detail']}")
        time.sleep(int(retry_after))
    raise RuntimeError("unreachable")
```

### JavaScript

```javascript
async function extractInvoice(text) {
  const response = await fetch("http://localhost:8000/api/v1/extract", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });

  const body = await response.json();
  if (!response.ok) {
    if (body.error === "unparseable_text") throw new Error("Not an invoice");
    throw new Error(`${body.error}: ${body.message}`);
  }
  return body;
}

const result = await extractInvoice(emailText);
console.log(result.data.total_amount, result.data.currency);
```

### A whole folder of files

```bash
for f in invoices/*.txt; do
  echo "=== $f ==="
  python -c "import json,sys; print(json.dumps({'text': open(sys.argv[1], encoding='utf-8').read()}))" "$f" \
    | curl -s -X POST http://localhost:8000/api/v1/extract \
        -H "Content-Type: application/json" -d @- \
    | python -c "import sys,json; d=json.load(sys.stdin); print(d['data']['vendor_name'], d['data']['total_amount'] if d.get('success') else d['error'])"
done
```

> Mind your daily free-model allowance when batching.

---

## 11. Using the Python parts directly

You can import the extraction layer without running the HTTP server.

```python
import asyncio
from app.extractors import extract_invoice

async def main():
    invoice, model_used, fallback = await extract_invoice("NORTHWIND TRADERS ...")
    print(invoice.total_amount, invoice.currency)
    print("items:", [(i.description, i.total) for i in invoice.items])
    print("model:", model_used, "| fallback:", fallback)

asyncio.run(main())
```

`invoice` is an `InvoiceData` Pydantic model, so `invoice.model_dump()` gives a dict and
`invoice.model_dump_json()` gives JSON.

Target one specific model, bypassing the chain:

```python
from app.extractors import extract_with_model

invoice = await extract_with_model(text, "nex-agi/nex-n2.5-pro:free")
```

Handle the error taxonomy:

```python
from app.extractors import (
    extract_invoice, UnparseableTextError, AllProvidersFailedError,
)

try:
    invoice, model, fallback = await extract_invoice(text)
except UnparseableTextError:
    print("not an invoice")
except AllProvidersFailedError as exc:
    for err in exc.errors:
        print(f"{err.provider}: {type(err).__name__}: {err.message} (retryable={err.retryable})")
```

Validate a dict you already have, with no model call:

```python
from app.schemas import InvoiceData

invoice = InvoiceData.model_validate({
    "vendor_name": "Acme", "invoice_number": "A-1", "invoice_date": None,
    "total_amount": 30.0, "tax_amount": None, "currency": "$",
    "items": [{"description": "Widget", "quantity": 3, "unit_price": 10.0, "total": 25.0}],
})
print(invoice.currency)          # "USD"  - symbol normalised
print(invoice.items[0].total)    # 30.0   - recomputed from 3 x 10.0
```

---

## 12. Running the tests

The suite needs no API key and makes no network calls.

```bash
pytest -q                                    # all 106 tests
pytest -v                                    # show names
pytest -k "cascade"                          # only fallback tests
pytest -k "schema"                           # only schema tests
pytest tests/test_extract.py::test_valid_invoice_text_parses_into_invoice_data
pytest -q -W error::DeprecationWarning       # treat deprecations as failures
```

Run these after any change. They cover the full stack except the model itself.

---

## 13. Evaluating on your own documents

The default model chain was chosen by benchmarking, not guesswork. **Repeat that with your own
invoices before trusting it for real work.** Six generic cases rank candidates; they do not
certify accuracy for your data.

A minimal harness:

```python
import asyncio, json, time
from app.extractors import extract_with_model, UnparseableTextError

CASES = [
    {"file": "samples/invoice1.txt",
     "expect": {"invoice_number": "NW-2024-0187", "total_amount": 377.98, "currency": "USD"}},
    {"file": "samples/receipt.txt",
     "expect": {"invoice_number": "4471-A", "total_amount": 49.61}},
    # include at least one non-invoice
    {"file": "samples/meeting_email.txt", "expect": {"is_empty": True}},
]

MODELS = ["nex-agi/nex-n2.5-mini:free", "liquid/lfm-2.5-2.6b:free"]

async def run(model):
    hits = total = errors = 0
    latencies = []
    for case in CASES:
        text = open(case["file"], encoding="utf-8").read()
        started = time.perf_counter()
        try:
            invoice = await extract_with_model(text, model)
        except UnparseableTextError:
            total += 1
            hits += 1 if case["expect"].get("is_empty") else 0
            continue
        except Exception as exc:
            errors += 1
            print(f"  {case['file']}: ERROR {type(exc).__name__}")
            continue
        latencies.append((time.perf_counter() - started) * 1000)
        for field, expected in case["expect"].items():
            if field == "is_empty":
                total += 1
                continue                       # reaching here means it found an invoice
            total += 1
            got = getattr(invoice, field)
            if got == expected:
                hits += 1
            else:
                print(f"  {case['file']}: {field}={got!r} expected {expected!r}")
    avg = sum(latencies) / len(latencies) if latencies else 0
    print(f"{model}: {hits}/{total}, {errors} errors, avg {avg:.0f}ms")

asyncio.run(asyncio.gather(*(run(m) for m in MODELS)))
```

What to measure, in priority order:

1. **Errors.** A model that fails to respond is useless regardless of accuracy.
2. **Accuracy on hard fields.** Dates, currency, tax, and totals that are not the sum of lines.
3. **Refusals.** Does it correctly reject non-invoices?
4. **Latency.** Only after the first three.

When you find a systematic miss, consider adding a rule to `SYSTEM_PROMPT` in
[`app/extractors.py`](../app/extractors.py) rather than switching models. Several existing rules
were added exactly that way.

---

## 14. Reading the logs

A healthy request:

```text
INFO [app.extractors] extraction succeeded model=nex-agi/nex-n2.5-mini:free items=2 elapsed_ms=2047
INFO [app.main] request 8f3a... : extracted invoice vendor='NORTHWIND TRADERS INC.' ... fallback=False
```

A cascade:

```text
WARNING [app.extractors] Primary model nex-agi/nex-n2.5-mini:free failed with ProviderRateLimitError: ... - cascading to liquid/lfm-2.5-2.6b:free
WARNING [app.extractors] fallback_triggered=True: result served by liquid/lfm-2.5-2.6b:free after 1 failed attempt(s)
```

An early stop on a non-invoice:

```text
INFO [app.extractors] 2 models agree the text contains no invoice; skipping the remaining 1 model(s)
```

Severity is meaningful:

| Level | Meaning |
|---|---|
| `INFO` | Normal operation |
| `WARNING` | A model failed but the chain absorbed it. The user still got an answer. |
| `ERROR` | The chain was exhausted. The user got a failure. |

Correlate a client report with a log line via `X-Request-ID`.

> `LOG_LEVEL=DEBUG` raises your own logging but deliberately does **not** enable HTTP wire
> logging, because that would write every submitted invoice, usually personal data, to the logs.

---

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/health` says `unavailable` | No key, or a placeholder value left in `.env` | Set a real `OPENROUTER_API_KEY` |
| Every request 503 with `ProviderAuthError` | Key is wrong or revoked | Test it with the `curl .../key` command in §2 |
| Every request 503, `error_type: ProviderError`, model not found | Model id changed or retired | List available models (§9) and update `OPENROUTER_MODELS` |
| Frequent `ProviderRateLimitError` | Daily free allowance exhausted, or too fast | Check your allowance, slow down, or add credits |
| `SchemaValidationError` on every model | Chain contains models without structured-output support | Set `OPENROUTER_STRICT_JSON_SCHEMA=false` |
| `ContentBlockedError`, truncated | Long invoice hitting the token cap | Raise `MAX_OUTPUT_TOKENS` |
| Requests take 90 seconds | Every model timing out in turn | Lower `TIMEOUT_SECONDS`, shorten the chain |
| Valid invoice returns 422 `unparseable_text` | Two models both failed to recognise it | Try `extract_with_model` against each to see which; consider a prompt rule |
| Wrong date on European invoices | Day-first ambiguity | The prompt has a rule; check which model answered via `model_used` |
| `sum(items) != total_amount` | Not a bug | Tax, shipping and discounts are not line items (§6) |
| Service will not start | Invalid setting | Read the startup error; it names the field |
| Tests fail after changing `.env` | They should not | Tests ignore `.env` entirely. If this happens, it is a real bug |
| Key rotated but old one still used | Settings are cached per process, and `.env` will not override an exported variable | Restart the process |

### Isolating a model

When results look wrong, find out which model is responsible:

```python
import asyncio
from app.extractors import extract_with_model

text = open("problem_invoice.txt", encoding="utf-8").read()

async def compare():
    for model in ["nex-agi/nex-n2.5-mini:free", "liquid/lfm-2.5-2.6b:free", "nex-agi/nex-n2.5-pro:free"]:
        try:
            invoice = await extract_with_model(text, model)
            print(f"{model}: {invoice.invoice_number} | {invoice.total_amount} {invoice.currency}")
        except Exception as exc:
            print(f"{model}: {type(exc).__name__}: {exc}")

asyncio.run(compare())
```

If one model is consistently wrong, move it later in the chain or drop it. If all of them miss
the same field, that is a prompt problem, not a model problem.

---

## 16. Before you put this anywhere public

The service is built for learning. Three things are missing for production, and they are
missing deliberately rather than by oversight.

**No authentication.** Anyone who can reach the endpoint can spend your model allowance. Put it
behind an authenticating gateway, or add an API-key dependency to the route.

**No rate limiting.** One caller can exhaust your daily allowance. Throttle per client.

**Wildcard CORS.** `CORS_ALLOW_ORIGINS=*` lets any web page call it. Set an explicit
allow-list.

Also worth doing:

```bash
ENVIRONMENT=production        # hides /docs and /openapi.json
```

Run several workers behind a load balancer, since the service is async and I/O bound:

```bash
uvicorn app.main:app --workers 4 --port 8000
```

Point your orchestrator's readiness probe at `/health`, which returns 503 when unconfigured.
Alert on the rate of `fallback_triggered: true` and on 503s. Never log request bodies, and keep
`.env` out of version control.

---

## Quick reference

```bash
# setup
pip install -r requirements.txt
cp .env.example .env            # then add OPENROUTER_API_KEY

# run
uvicorn app.main:app --reload --port 8000

# check
curl localhost:8000/health

# extract
curl -X POST localhost:8000/api/v1/extract \
  -H "Content-Type: application/json" -d '{"text":"...invoice text..."}'

# test
pytest -q

# docs
open http://localhost:8000/docs
```

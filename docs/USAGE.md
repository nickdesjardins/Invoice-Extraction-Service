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

Behind that, it tries a chain of free models (two by default) in order until one produces a
valid result. You need one OpenRouter API key. With the default models, **requests cost nothing**.

Free models are withdrawn often. As shipped, the service looks after its own chain: when
OpenRouter stops offering one of its models, the service notices at the first request that fails
on that model, and looks for a replacement among the other free models (§9).

---

## 2. Install

You need **Python 3.11 or newer**.

```bash
git clone https://github.com/nickdesjardins/Invoice-Extraction-Service.git
cd Invoice-Extraction-Service

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

That is the only required setting. Everything else has a working default. Leave
`OPENROUTER_MODELS=` blank, as the template has it. Blank means two things. The service starts
from the tested default in `app/config.py`. And the service looks after that chain itself: when
it starts, it reads the chain that this project's scheduled test last published, and later it
replaces a model that OpenRouter has withdrawn. The scheduled test is a job on GitHub that tests
the free models about once a week. If you name models on that line instead, the chain is
exactly what you named, and the service tests and replaces nothing. §9 describes both cases.

Then look at Settings, Privacy in your OpenRouter account: OpenRouter's FAQ says providers that
log prompts or train on them are routed to only when the account's privacy settings allow it,
so with those settings off a free model can answer 404 although it is listed (§15). With them
on, send the sample documents or redacted ones, not real invoices (§16).

### Check your key and allowance

OpenRouter counts the free-model requests of an account per day, and a day here is a UTC day.
This command asks OpenRouter for that count. It uses the service's own code and reads the key
from `.env`, so the key is never typed and never shown:

```bash
python -c "from app.config import get_settings; from app.budget import read_allowance; print(read_allowance(get_settings()))"
```

```text
Allowance(used=2, limit=1000, remaining=998)
```

The three numbers above are an example. `limit` is what the account may send per day, and
`remaining` is what is left of it today. Accounts with credits get 1000 free-model requests per
day; accounts without get far fewer. `None` in place of the three numbers means that the count
could not be read: no key is set, or `OPENROUTER_BASE_URL` is not OpenRouter's own address, or
OpenRouter did not give the expected answer. In that last case a line above `None` says why.

The same question with curl, for a shell that already has the key in its environment:

```bash
curl https://openrouter.ai/api/v1/key \
  -H "Authorization: Bearer $OPENROUTER_API_KEY"
```

Look at `free_model_daily_requests` in the answer. Do not put the key itself into a command
line: a shell keeps its history in a file on disk, and the key would stay there in plain text.

Each extraction uses **one** request when the first model succeeds, more when it falls back.
The running service reads the same count itself, at most once a minute and only when a request
is about to call a model. It uses the count to keep part of the day back (§7). The service
spends free requests of its own only when it tests models, and it tests none while the chain
works (§9).

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

The startup log also says, for each model in your chain, whether OpenRouter still lists it. A
line that says a model *is not listed by OpenRouter* means that model was withdrawn or its id
is mistyped. With `OPENROUTER_MODELS` blank the line is a `WARNING` and you have nothing to do:
the service looks for a replacement after the first request that fails on that model. With
models named the line is an `ERROR`. It says what to do in a few words, and §9 says it at
length. The service starts either way.

Then open <http://localhost:8000/> in a browser.

> Use `localhost` (or `127.0.0.1`) in the address bar. `0.0.0.0` means "listen on every network
> interface"; it is not an address a browser can open, and Chrome answers `ERR_ADDRESS_INVALID`.

### The playground

The home page lets you run extractions without writing a request by hand. The page uses two
names. What you are using when you look at the page is *the demo*, on a host and on your own
machine alike. The software, and what it does, is *the service*.

1. **Pick a scenario** in the left column, or choose *Your own document* and paste any text.
2. **Press *Run extraction*** (or Ctrl+Enter in the document box). The result panel, in the
   right column, shows each model in the chain as it is tried, how long it took, which one
   answered, and the extracted invoice. On a narrower screen the result panel stands below the
   request, and the page brings it into view when you start a run.
3. **Make a model fail.** Under *Model chain for this run*, switch a model from *Call the model*
   to one of the simulated failures and run again. The chain moves on to the next model, exactly
   as it does when a real model fails. The *Resilience* scenarios are these set-ups, ready-made.
   A scenario that you have changed is run as a document of your own. It is then not checked
   against what the scenario expects, and the result says so.
4. **Press *Run all*** to execute every scenario in turn. The same button stops them again. Each
   scenario has checks that say what a correct run must produce. Once a run has an answer, the
   result lists them under *Expectations*, each with a tick or a cross, and the list of
   scenarios says how many of them were met. With the default two-model chain there are
   fourteen scenarios, and a full run makes about 12 model calls from your daily allowance. Two
   more scenarios need a third model and appear when your chain has one. A run that the service
   refuses, or that gets no answer, is not scored, and the list does not count its scenario as
   failed (both cases are described below). After a refusal *Run all* waits until the service
   has room again, twice at most for one scenario, and the list counts that wait down. It stops
   with a sentence when it cannot go on. It also stops when the service has changed its model
   chain meanwhile, because the scenarios that are left were written for the chain before. A
   notice above the playground then says so.
5. **Run the automated tests** from the *Test suite* section. This starts `pytest` on your machine
   and shows every test with its result. It needs no API key.
6. **Look at *Model upkeep***, the section headed *What happens when a free model disappears*.
   It says where the chain in use comes from, and it shows one test of the free models: the
   test that is running, or the last one that finished, or one that was recorded when the
   default chain was measured. Each mark stands for one sample document sent to one model.
   Point at the marks of a row, or move to them with the Tab key, and a tip says in words what
   each mark shows. *Replay this test* plays the test again from its record and calls nothing.
   With `OPENROUTER_MODELS` blank the section also has a button, *Test the free models now*.
   That button spends free requests: up to 7 for each model, 42 at most (§9).

Use the built-in scenarios, or paste a redacted document, not a real invoice: the providers
behind free models may keep what you send, and a service that looks after its own chain can
put another free model into it (§16).

Reading the result:

| On the page | Means |
|---|---|
| *Response time* | Server-side time for the whole request, every attempt included |
| *Answered by* | `model_used` in the API response |
| *Fallback: Used* | `fallback_triggered: true`: the first model did not answer |
| A red cross on a model | That attempt failed; the error type and a short message are underneath. Three of the service's messages tell an admin which setting to change. The page puts those into words for a visitor; *Raw request and response* keeps the API's own text |
| *simulated* | The failure was staged by the page, not caused by the provider |
| *Not needed* | The chain stopped before reaching that model |
| *The demo is busy* or *Today's share of live runs is used up*, with status 503 | The service refused the run before any model was called: see *A run that is refused* below. Once midnight UTC has passed, the second title reads *The day's share of live runs was used up* |
| The last line of the *Model chain* card | Where the chain in use comes from. For a chain named in `OPENROUTER_MODELS` it reads *Fixed by the admin of this demo.* Otherwise it names the origin: *The tested default, measured on 7 October 2026.*, or this project's scheduled test, or a change that the service made itself (§9) |
| *Unlisted from OpenRouter, please contact the admin.* on a model | Shown for a chain named in `OPENROUTER_MODELS`. When the service started, OpenRouter's catalogue did not list that model: it was withdrawn, or its id is mistyped (§9). It is still tried whenever its turn comes. If you run the service, you are the admin: the startup log says how to replace the model. A model whose end OpenRouter has announced shows *Ends* and the date instead |
| *No longer offered by OpenRouter* on a model | Shown for a chain the service looks after. OpenRouter's own page for that model says "Model not found". Nobody has to be contacted: the service looks for a replacement after the next run that fails on that model (§9) |
| *No answer yet* or *Not answering* in the top bar | The page got no answer from the service: see *A demo that does not answer* below |
| *No result* | The run ended without a result: the service gave no answer, the connection broke off, nothing more came from the service for too long, or you stopped *Run all*. The sentence under the title says which. *Waited* says how long the page waited |
| *Models withdrawn* in the top bar | OpenRouter no longer offers any model of the chain. The top bar normally repeats what `GET /health` says, and `/health` only says that a key is set. In this case *Operational* would not be true |
| *Raw request and response* | The exact status, headers and JSON `POST /api/v1/extract` returns |

A cross under *Expectations* on a **Documents** scenario is a model reading the document
differently from the expected answer, not a fault in the service. A model that is called for real
and never answers, for example because its id was withdrawn, gets a warning under *This session*.

A two-model chain has no spare, and two kinds of scenario show it. *Not an invoice* needs both
models to answer before its *Two models agree* check can pass. The four *Resilience* scenarios
in which the primary fails use the second model as the live fallback, so they go red when that
model is rate limited. Neither is a fault in the service: wait a minute and run the scenario
again.

**Notices about the chain.** When a model of the chain gives no answer to a run because it is
gone, a service that looks after its own chain starts to repair it (§9), and the page shows a
notice. The notice stands in the result panel, under the outcome of the run, and every visitor
of the page sees it there. It stays for an hour after the service's look into the failure, or
after the end of the test that followed. Its title says where things stand:

| Notice | What has happened |
|---|---|
| *Finding a replacement* | A model of the chain gave no answer, and OpenRouter no longer offers it, or no model of the chain could be called at all. A test of the other free models is running |
| *A working model is in place* | At least one of the tested models gave a right first answer to a sample document and answers for the time being. The page calls such a model a stand-in. The test is still running and has the last word |
| *The chain was repaired* | The test is over, and it changed the chain |
| *A stand-in is answering* | The test is over, and it could not decide about a stand-in: calls went unanswered, or the test did not get to its end. The stand-in goes on answering until a later test decides |
| *The test did not finish* | The test ended early and changed nothing: OpenRouter refused the service's API key, or the test was cut short |
| *No replacement passed the test* | The test is over and added nothing. The chain goes on with the models that OpenRouter still offers |
| *No free model is answering* | The test is over and added nothing, and no model of the chain can answer: OpenRouter offers none of them any more, or none of them could be called. The service cannot extract invoices until a later test succeeds |
| *A tested chain was already waiting* | The service found a newer chain in this project's published list and took it. Nothing was tested |
| *No replacement yet*, *No replacement today*, *No replacement is on offer*, *The service cannot look for a replacement right now* | No test could start. In the same order: a test ran less than 15 minutes ago; too few free requests are left today; OpenRouter lists no other free model; OpenRouter's list could not be read |
| *No model is answering* | No model of the chain could be called although OpenRouter lists them all, and the chain has no free place for another model |

While a test runs, the notice also has one line for each tested model: what its first answer
came to, how long it has been asked so far, or that it is waiting its turn. When a test follows
a withdrawn model, the notice also explains why a visitor is the one who finds out: by itself
the service tests its models only when one of them does not answer a run, so that it spares its
free allowance. A notice that asks you to contact the admin names no setting: what the admin
has to do is in the server log (§14).

The page can send a failed run again by itself, once. It does so for a run of your own that no
model answered, when the service has put a model into its chain that was not there when the run
failed. The page waits for such a model while the service is looking into the failure or testing
models, and for 3 minutes at most. Meanwhile the result of the failed run says that this can
happen. The run is sent again only while you have stayed with it: its result is still on view,
the document in the box is the one that was sent, and no simulated failure is chosen. The new
result says that the run was sent again. If you have moved on, nothing is sent, and the result
of the failed run says that the chain has changed since. A scenario that *Run all* started is
not sent again in this way.

A stand-in can answer a run of yours and fail the test afterwards. The result of that run then
says that the model has failed the test and no longer answers, and that its answer may be
wrong.

**A demo that does not answer.** A free host switches a service off when nobody has used it for
a while. The next request starts it again, and that takes about a minute (§16). The page copes
with this. If the service gives no answer when the page loads, the top bar says *No answer yet*
and a notice says that the demo *has probably gone to sleep*. The page then asks `GET /health`
every 3 seconds, for 3 minutes at most, and finishes loading by itself when the service answers.
A run that got no answer waits in the same way, under the title *Waiting for the demo*, and is
then sent again by itself, once. After 3 minutes without an answer the top bar says
*Not answering*, and a *Try again* button appears. The page cannot know why no answer came,
which is why it says "probably". On your own machine you read the same words when the server is
not running. One cause the page can see: when the browser reports that your device is offline,
the page says that the device seems to be offline, and not that the demo has gone to sleep.

No request of the page waits for ever. The page stops waiting for a run when nothing has come
from the service for 120 seconds. That is the time one model may take (`TIMEOUT_SECONDS`, 30
seconds by default) and 90 seconds more, and every step of the run that arrives starts the wait
again. What follows depends on how far the run got. If the service never took the run, the run
waits for the service as described above. If the service had started on it, the run ends as
*No result* and the page does not send it again, because it may already have called a model.
The same holds when the connection breaks off in the middle of a run. A run that ended as
*No result* has a *Try again* button, so sending it again is your decision. Every other request
of the page has a limit of 3 minutes.

While it is open, the page also asks the service for news about the chain, with
`GET /playground/models`. It asks every 2 seconds while the service is looking into a failed
run or testing models. Otherwise it asks every 20 seconds, and only for 10 minutes after you
last did something on the page. A click, a tap, a key press and coming back to the tab each
count as that. A tab that is hidden asks for no news at all. So a tab that is left open does not
keep a sleeping host awake. When such a question gets no answer, the page no longer shows a
test as running, and the section *Model upkeep* says that what follows is what the page last
saw and may be out of date. The top bar says *No answer yet* for as long as the page goes on
asking, and *Not answering* once it has stopped. The first answer puts the page right again by
itself. If the service has started again in between while a test was running, a notice above
the playground says that this test is lost.

**A run that is refused.** The service keeps one ceiling on its live model calls, for all
visitors together (§7). A live call is a real call to a model, as opposed to a simulated
failure. When the ceiling is reached, a run is refused before any model is called. The result
then shows status 503 under one of two titles, each with a countdown. Under *The demo is busy*
the wait is a minute at most. Under *Today's share of live runs is used up* live runs start
again at midnight UTC. When a countdown ends, the result says that you can try again now, and a
*Try again* button appears. The second title then reads
*The day's share of live runs was used up*, because midnight UTC has passed. Under
*What a client is told* you read the service's own sentence, which is what the API answers in
the same case. Scenarios that only use simulated failures call no model, so they are never
refused.

Interactive API docs, for composing raw requests instead:

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
  "model_used": "nvidia/nemotron-3-super-120b-a12b:free",
  "execution_time_ms": 6000.0,
  "fallback_triggered": false
}
```

The `execution_time_ms` above is an example value, not a measurement, and `model_used` is
whichever model answered.

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

There are two. (The playground has routes of its own under `/playground/`; they serve the page
and are not part of the API. Two of them show how the service looks after its chain: §9.)

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
    {"name": "openrouter", "model": "nvidia/nemotron-3-super-120b-a12b:free", "role": "primary",  "configured": true},
    {"name": "openrouter", "model": "liquid/lfm-2.5-2.6b:free",               "role": "fallback", "configured": true}
  ],
  "active_providers": ["nvidia/nemotron-3-super-120b-a12b:free", "liquid/lfm-2.5-2.6b:free"]
}
```

| `status` | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | An API key is configured. Ready to serve. |
| `unavailable` | 503 | No API key. Every extraction will fail. |

Because it only checks configuration, `ok` does **not** promise the upstream models are
healthy. Use `fallback_triggered` in responses for that. `/health` never asks OpenRouter
anything. It lists the chain in use and says whether a key is set, so a model that has been
withdrawn still appears here as `configured: true`. With a chain named in `OPENROUTER_MODELS`
it stays there. With the setting blank it stays until the service has taken it out (§9): the
list then changes by itself, and it can have one to three entries. The command that does ask
OpenRouter is in §9.

### `POST /api/v1/extract`

The actual work.

**Request body**

| Field | Type | Rules |
|---|---|---|
| `text` | string | Required. 10 to 50 000 characters. Whitespace is trimmed first. |

The whole request body may be at most 1 000 000 bytes. A larger body gets the 422
`validation_error` of §7, and the service does not read it to the end. The longest valid `text`
fits easily: 50 000 characters are at most 600 000 bytes of JSON.

**Optional header**

| Header | Purpose |
|---|---|
| `X-Request-ID` | Your own correlation id. Must match `[A-Za-z0-9._-]{1,128}`, otherwise one is generated. |

**Response headers**, on every response except one:

| Header | Meaning |
|---|---|
| `X-Request-ID` | Correlation id. Use it to find the request in the logs. |
| `X-Process-Time-Ms` | Total server time including all model attempts. |
| `Retry-After` | Only on a 503 where waiting helps: `5` when at least one model's failure was transient, and the real wait in seconds when the service's own ceiling refused the request (§7). |

The exception is the answer to a preflight request. A preflight is the `OPTIONS` request that a
browser sends by itself before a `POST` from another site's page. Its answer carries the CORS
headers only.

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
A sustained rise means your primary is degrading. `true` on every response means the first
model fails every time. With a chain named in `OPENROUTER_MODELS`, the usual cause is a
withdrawn model (§9). With the setting blank, the service takes a withdrawn model out itself
after a request has failed on it, so `true` on every response then points to another cause,
such as a rate limit that does not pass.

### The invoice (`data`)

| Field | Type | Notes |
|---|---|---|
| `vendor_name` | string | Who issued the invoice. `""` if not stated. |
| `invoice_number` | string | Labels like `Invoice #` and `RCPT#` are stripped. `""` if absent. |
| `invoice_date` | string or null | ISO-8601 `YYYY-MM-DD`, as the prompt asks; the service does not validate the format. `null` if absent or ambiguous. |
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
| 422 | `validation_error` | Body is malformed, `text` is under 10 / over 50 000 chars, or the whole body is over 1 000 000 bytes | Fix the request. Do not retry. |
| 422 | `unparseable_text` | The models agree the text contains no invoice | Do not retry. The input is not an invoice. |
| 503 | `providers_unavailable` | Every model failed, or the service's own ceiling on live model calls is reached | Read `detail` and `Retry-After`: see *Reading a 503*. |
| 400 | `http_error` | The body cannot be read as JSON at all: its bytes are not UTF-8 text, or it is nested far too deeply | Fix the request. Do not retry. |
| 403 | `http_error` | A `POST` to one of the playground's own routes did not come from the page (§16) | Programs use `POST /api/v1/extract`. |
| 404/405 | `http_error` | Wrong URL or method | Fix the request. |
| 500 | `internal_error` | A bug | Report it with the `request_id`. |

### The two 422s are different

This trips people up. Both are 422, but they mean opposite things:

- `validation_error` — **your request** is malformed. The models were never called.
- `unparseable_text` — your request was fine, the models ran and correctly concluded the text
  is not an invoice. Check `detail.provider` to see which model decided.

Neither should be retried.

A body over 1 000 000 bytes is a `validation_error` like any other malformed request. `detail`
then has one entry, with `type: "bytes_too_long"`.

### Reading a 503

A 503 comes in two shapes. Look at `detail` first. It is a list when every model failed. It is
a single object, with `error_type: "CallLimitError"`, when the service's own ceiling refused the
request (next heading). This is the first shape:

```json
{
  "success": false,
  "error": "providers_unavailable",
  "message": "Every extraction model failed.",
  "detail": [
    {"model": "nvidia/nemotron-3-super-120b-a12b:free", "error_type": "ProviderRateLimitError", "message": "...: rate limited (429)", "retryable": true},
    {"model": "liquid/lfm-2.5-2.6b:free",               "error_type": "ProviderTimeoutError",   "message": "... did not respond within 30s", "retryable": true}
  ]
}
```

One entry per model, in the order tried. What to do next depends on the `Retry-After` header
and on the entries:

- **`Retry-After: 5` is present.** At least one failure was transient, such as a rate limit or
  a timeout. Wait that long and retry.
- **No `Retry-After`, and every entry's message ends with `was not found (404)`.** OpenRouter
  no longer offers these models, or does not offer them to your account. A service that looks
  after its own chain (§9) starts to repair the chain because of this very request, so retry
  after a few seconds. The retry succeeds as soon as a working model is in place. That usually
  takes seconds, and it can take as long as one model call (`TIMEOUT_SECONDS`) when only a slow
  model is on offer. If the retries still fail after a minute, come back later: the test of
  the free models is still running, or no model passed it, or no test could start. With a
  chain named in `OPENROUTER_MODELS` nothing changes by itself: see §9.
- **No `Retry-After`, and any other cause**, typically a rejected key (`ProviderAuthError`).
  Every failure was permanent, and retrying will not help.

The second case carries no `Retry-After` on purpose. The header says whether a failure of a
model may pass, and a 404 does not pass. The answer is also sent before the service has looked
into the failure, so nothing in it can say that a repair has started.

Common `error_type` values:

| `error_type` | Meaning | Fix |
|---|---|---|
| `ProviderRateLimitError` | Hit a rate limit | Wait, or slow down |
| `ProviderTimeoutError` | Model did not answer in time | Retry, or raise `TIMEOUT_SECONDS` |
| `ProviderAuthError` | Key rejected | Check `OPENROUTER_API_KEY` |
| `ProviderError` | Upstream error, out of credits, model not found | Check your account. For a model that was not found, see the second bullet above; `python -m app.model_catalogue` (§9) says whether the model is still listed |
| `SchemaValidationError` | Unusable output, or model lacks structured-output support | Try `OPENROUTER_STRICT_JSON_SCHEMA=false`, and read its row in §8 first |
| `ContentBlockedError` | Truncated or refused | Raise `MAX_OUTPUT_TOKENS`, and `TIMEOUT_SECONDS` with it (§8) |
| `ProviderNotConfiguredError` | No API key | Set `OPENROUTER_API_KEY` |
| `CallLimitError` | The service's own ceiling on live model calls. No model was called | Wait as long as `Retry-After` says: see the next heading |

Error messages are deliberately short. Full upstream text goes to the server log, keyed by
`X-Request-ID`, because raw provider errors can leak account details and fragments of the
submitted document.

### The ceiling on live model calls

The service calls models on its owner's account, and both the API and the playground are open
to anyone who can reach them. So the service keeps one ceiling on its own live model calls, for
all callers together. A live call is a real call to a model. The ceiling has two parts:

- **The minute.** A request is let in only while the last 60 seconds, with this request's own
  calls added, hold 15 live calls or fewer. A request needs room for every model it might call,
  which is two with the default chain. It gives back the room it did not use. So with the
  default chain about 14 requests a minute get through when each is answered by its first
  model.
- **The day.** OpenRouter counts an account's free-model requests per UTC day. The service asks
  OpenRouter for that count, the one of §2, at most once a minute. The last fifth of the day's
  limit is kept back from callers, for the tests of the models (§9). A request is let in only
  while every call it may make would start with more than a fifth of the day's limit left. With
  the default chain a request may make two calls, so with a limit of 1000 requests stop when
  201 or fewer are left. When there is no count for today at all, the service lets 20 live
  calls through per start, and then refuses until it can read the count. That rule is for a
  count that exists and cannot be read. With another address in `OPENROUTER_BASE_URL` nobody
  counts the day, and only the per-minute ceiling applies.

A refusal is a 503 in the usual envelope, given before any model is called. A refused request
costs no free request:

```json
{
  "success": false,
  "error": "providers_unavailable",
  "message": "The service is busy: it has reached its ceiling of live model calls per minute, counted for all visitors together. Please try again in 37 seconds.",
  "detail": {
    "provider": null,
    "error_type": "CallLimitError",
    "message": "The service is busy: it has reached its ceiling of live model calls per minute, counted for all visitors together. Please try again in 37 seconds.",
    "retryable": true
  }
}
```

`Retry-After` is always present on this 503, and it is the real wait in whole seconds:

| `Retry-After` | The message starts with | Means | What a client should do |
|---|---|---|---|
| 1 to 60 | `The service is busy` | The last minute is full | Wait that many seconds, then send the request again |
| 60 | `The service cannot check today's free allowance` | OpenRouter could not say what is left of the day, and the 20 calls that the service allows without a count are used | Wait 60 seconds and retry. If it goes on, the admin has to look (§15) |
| The seconds until midnight UTC: up to 86 400, a whole day | `The service has used today's share` | Callers' share of the day is used up | Do not wait in a loop. Come back after midnight UTC |

One rule covers all three: when `Retry-After` is 60 or less, wait that long and retry; when it
is more, stop for today.

Three more things to know. A run on the playground that simulates every model's failure calls
no model, so it is never refused. The ceiling is counted in the memory of one process (§16).
And it is not a limit per client: one caller can fill the minute for everybody else.

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
| `OPENROUTER_MODELS` | two free models, see the [README](../README.md) | Ordered, comma-separated chain. First is primary. Blank: the service starts from the default and looks after the chain itself. Any value: exactly those models, in that order, and the service tests and replaces nothing (§9). |
| `MODEL_PUBLISHED_URL` | the address of this project's `chain.json`; it is `DEFAULT_PUBLISHED_URL` in `app/config.py` | Where the service reads the chain that this project's scheduled test published (§9). Only a service that looks after its chain reads it (`OPENROUTER_MODELS` blank, a key set, OpenRouter's own address in `OPENROUTER_BASE_URL`), at startup and after a failed request. It has to be an `https` address. `null` means: never read a published list. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Change only for a proxy or a compatible provider. With another address the service does not look after its chain, and nobody counts the day for it: only the per-minute ceiling then limits live calls (§7). |
| `OPENROUTER_STRICT_JSON_SCHEMA` | `true` | `false` uses plain JSON mode for models without structured-output support. With `false`, a service that looks after its own chain does not use this project's published list, which was made with the strict format. |
| `MAX_OUTPUT_TOKENS` | `2048` | Output cap. A model's reasoning counts against it. Raise it for long invoices, together with `TIMEOUT_SECONDS` (see the note below). |

### Service

| Variable | Default | Purpose |
|---|---|---|
| `TIMEOUT_SECONDS` | `30` | Per-model budget. Worst case ≈ this × chain length: 60 s with the two default models, 90 s with three models, which a chain the service looks after can have. It also draws a line in a test of the models (§9): a model whose usual answer takes more than half of it does not pass. |
| `ENVIRONMENT` | `development` | `production` hides `/docs`, turns off the page's test-suite panel, and makes the page's `POST` routes refuse callers that are not a browser (§16). `development` makes `python -m app.main` reload when the code changes. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` also writes some of what a document said to the log (§14), so keep `INFO` wherever other people's documents arrive. |
| `CORS_ALLOW_ORIGINS` | `*` | Comma-separated allow-list. |
| `PLAYGROUND_ENABLED` | `true` | Serves the playground at `/`. `false` leaves only the API. The service still looks after its chain then; nobody can watch it on the page. |
| `MODEL_CATALOGUE_CHECK` | `true` | At startup, when a key is set, asks OpenRouter's public catalogue whether each model of the chain is still listed, and logs the answer (§9). The check itself changes nothing. `false` starts without any request: neither this check nor the reading of the published list happens at startup, for example when working offline. After a failed request, a service that looks after its chain still asks OpenRouter and still reads the published list. |
| `SERVICE_NAME` | `invoice-extraction-service` | Name in `/health` and logs. |

> **Long invoices.** Both default models reason before they answer, and that reasoning counts
> against `MAX_OUTPUT_TOKENS`. In a test on 7 October 2026 an invoice with 20 line items was cut
> off on both models at the default cap of 2048. With the cap at 6000 the primary answered a
> 40-line invoice in 28 s, close to the 30 s timeout, and the fallback timed out. So for long
> invoices raise `MAX_OUTPUT_TOKENS` and `TIMEOUT_SECONDS` together.

### Optional attribution

| Variable | Purpose |
|---|---|
| `OPENROUTER_SITE_URL` | Sent as `HTTP-Referer`, shown on OpenRouter dashboards. |
| `OPENROUTER_APP_NAME` | Sent as `X-Title`. |

> Configuration is validated at **startup**. A bad value stops the service immediately with a
> clear message rather than failing on the first request. One setting is the exception: a
> `MODEL_PUBLISHED_URL` that is not an `https` address does not stop the service. When the
> service tries to read the list, it logs a `WARNING` that the published list was not used, and
> goes on without it.

---

## 9. Choosing and changing models

One environment variable, `OPENROUTER_MODELS`, decides which models are used and who looks
after them. No code change, no redeploy of anything else.

```bash
# blank: start from the tested default in app/config.py, and let the service look after
# the chain itself (this is what .env.example ships)
OPENROUTER_MODELS=

# one model, no fallback: every failed call reaches the caller
OPENROUTER_MODELS=vendor/model-a:free

# two models: the second is tried only when the first fails
OPENROUTER_MODELS=vendor/model-a:free,vendor/model-b:free
```

`vendor/model-a:free` and `vendor/model-b:free` are placeholders. Real ids come from *Picking
models* below.

Duplicates are removed and order is preserved. A value with no id in it, such as a lone comma,
is rejected at startup. Leave a blank line truly blank: anything after the equals sign, even a
comment, is read as a model id. The word `null` is not blank either: with it the service does
not start.

### Blank or named: who looks after the chain

The startup log says which of the two cases you are in, in a line that starts with
`Model choice:`. The log's words for the two cases are *automatic* and *pinned*. This guide
says that the service *looks after* the chain, or that the chain is *fixed*, which is the
page's word.

| | `OPENROUTER_MODELS` is blank | `OPENROUTER_MODELS` names models |
|---|---|---|
| The log says | `Model choice: automatic.` | `Model choice: pinned.` |
| The page says under the chain | *The tested default, measured on 7 October 2026.*, or where the chain has come from since | *Fixed by the admin of this demo.* |
| The chain | Starts from the default, or from this project's published list when that is newer. The service can then take a withdrawn model out and add a tested one. The chain holds three models at most | Exactly the models you named, in your order, always |
| When a model is withdrawn | The service looks for a replacement by itself | You replace it, with the steps further down |
| The tests of the models, the published list, the button on the page | In use | Switched off |

Naming models fixes the chain even when you name exactly the default models.

The first case needs two more things: an API key, and OpenRouter's own address in
`OPENROUTER_BASE_URL`. Without a key, or with another address, the service replaces nothing. The
log then says so in a `WARNING` that begins
`Model choice: the service does not look after its chain here`.

### The default chain

| Position | Model | Served by |
|---|---|---|
| Primary | `nvidia/nemotron-3-super-120b-a12b:free` | Nvidia |
| Fallback | `liquid/lfm-2.5-2.6b:free` | Liquid |

It is a dated snapshot (7 October 2026), not a ranking. The [README](../README.md), under "Model
selection", records what was measured that day and how. The two models are served by different
providers, so one upstream outage cannot take out both.

The first default chain, in September 2026, had three models, two of them the free variants of
`nex-agi/nex-n2.5-mini` and `nex-agi/nex-n2.5-pro`. OpenRouter listed those as free for 17 days,
from 8 to 25 September 2026, and then withdrew them; this project's first commit was on 21
September. The benchmark behind that chain cannot be repeated, because its scripts and documents
were not kept, and its scores cannot be compared with new results, which use different documents
and count whole answers.

### How the chain behaves

- Only the **first** model runs when it succeeds. Later entries are pure insurance.
- A model is skipped for the next one on any failure: rate limit, timeout, upstream error,
  truncation, or unusable output.
- When two models agree the text is not an invoice, the service stops early and returns 422
  without calling the rest.
- Order by **fewest wrong answers**, not by speed. Leave out any model that rejects the response
  format, or whose usual answer takes more than half of the time limit. Of the rest, the first
  position goes to the model with the fewest wrong answers, then the fewest calls with no
  answer; speed only breaks ties. The reason: the chain moves on only when a call *fails*, so a
  fallback replaces a failed call, never a wrong answer.
- A chain that the service looks after has three places at most: the two of the default chain
  and one spare. A chain you name yourself can be as long as you write it.

### Picking models

OpenRouter's public catalogue can list the free models that say they support structured output.
Open this address in a browser, or fetch it with curl. It needs no key:

```text
https://openrouter.ai/api/v1/models?supported_parameters=structured_outputs&max_price=0
```

```bash
# the same list, ids only (keep the quotes: the address contains an &)
curl -s "https://openrouter.ai/api/v1/models?supported_parameters=structured_outputs&max_price=0" \
  | python -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

A model on that list is only a **candidate**. "Listed" and "says it supports structured output"
do not mean "works with this service": on 7 October 2026 one free model on the list,
`apodex/apodex-1.1-mini:free`, rejected this service's strict schema on every call. Only real
requests through the service show whether a model answers, and the benchmark command in §13
sends them. A service that looks after its own chain takes its candidates from this same list,
and it sends them real requests too before it uses one (*When a free model disappears* below).

The list is short. On 7 October 2026 it held four models: the one above;
`dots-studio/dots-3-note-preview:free`, which gave no wrong answer but whose usual answer took
about 22 of the 30 seconds allowed, and which has an announced end date of 2026-12-31; and the
two in the default chain.
The free pool as a whole is shrinking: 71 ids ending in `:free` in May 2025, 16 on 7 October
2026.

OpenRouter also has an "any free model" router, `openrouter/free`. It was tried and left out: it
picks a free model at random, and it sent this service's requests to models that could not
answer them.

If a model does not support structured outputs, set `OPENROUTER_STRICT_JSON_SCHEMA=false`. That
setting is global, so a mixed chain must run in JSON mode throughout.

### Is my chain still listed?

Free models are withdrawn often, and mostly without notice. Measured on 7 October 2026 from
archived copies of OpenRouter's public model list:

- Of the `:free` ids listed 30 and 61 days earlier, 83% and 64% were still listed.
- Since January 2026, 38 of 63 withdrawn free ids never showed an end date in an archived copy.
  When one was shown, the notice was between about 3 and 12 days at the median. Both are
  bounds, because the archived copies are days apart.
- For a two-model chain that is roughly a 31% chance of losing at least one model within 30
  days and 59% within 61 days, if both behave like the average free id, independently.

So expect a model to be replaced now and then: by the service itself when `OPENROUTER_MODELS`
is blank, by you when you named the chain. One command asks OpenRouter's public catalogue
whether each model in your chain is still listed. It needs no API key and spends none of your
free requests:

```bash
python -m app.model_catalogue
```

It checks the chain a service would use on this machine. That is the chain in
`OPENROUTER_MODELS` if you named one. Otherwise it is the chain that a service saved in
`model_cache.json`, or the default when there is no such file. When that chain is not the
built-in default, the command's first line says which one it is. It is one of these two:

```text
Checking OPENROUTER_MODELS from your environment, not the built-in default.
Checking the chain a service on this machine saved in model_cache.json, not the built-in default.
```

Then it prints one line per model:

```text
nvidia/nemotron-3-super-120b-a12b:free is listed.
liquid/lfm-2.5-2.6b:free is listed.
```

The other lines it can print, shown here with placeholder ids:

```text
vendor/model-a:free is not listed by OpenRouter (withdrawn, or the id is mistyped). Replace it in OPENROUTER_MODELS, or in DEFAULT_MODEL_CHAIN in app/config.py.
vendor/model-b:free is listed, and OpenRouter has announced its end: 2026-12-31 (10 days from today).
vendor/model-c:free could not be checked (catalogue not reachable (TimeoutError)).
vendor/model-d:free is not listed by OpenRouter (withdrawn, or the id is mistyped). The service replaces it by itself after the next request that fails. To go back to the built-in default, delete model_cache.json and restart.
```

The last line is for a model of a chain that a service saved. Nobody named that chain, so the
line does not tell you to replace anything.

| Exit code | Means |
|---|---|
| `0` | Every model is listed, and none has an end date within 14 days. |
| `1` | Anything else: a model is not listed, an end date is 14 days away or less, or a model could not be checked. |

`echo $?` prints the exit code of the command you just ran (`echo $LASTEXITCODE` in PowerShell).

What the answers mean, and what they do not:

- **The check only reports.** By itself it never changes which models are called,
  `fallback_triggered`, `GET /health`, or any response of `POST /api/v1/extract`. What happens
  to a withdrawn model depends on the case you are in. With models named, it is still tried
  whenever its turn comes, which for the first model is every request: it fails in a fraction of
  a second, the next model answers, and the response says `fallback_triggered: true`. With
  `OPENROUTER_MODELS` blank, the service takes the model out itself, but never on the
  catalogue's word alone: the model must also have failed a real request. So one wrong answer
  from the catalogue cannot switch off a model that works.
- **"Not listed" is concluded only from OpenRouter's own 404 "Model not found".** Anything else
  (a network error, another status, an answer that cannot be read) is "could not be checked" and
  changes nothing. Nothing is looked up at all when `OPENROUTER_BASE_URL` is not OpenRouter's,
  or for an id that starts with `@` or has no slash: those are reported as "could not be
  checked" as well.
- **"Listed" does not mean "works".** The candidate under *Picking models* that rejected every
  call was listed the whole time. That is why the service tests a model on sample documents
  before it gives it a place in the chain.
- **An end date more than 14 days away is reported and is not a failure yet.** The service
  does nothing about an announced end date. It acts only once the model is gone.

The same check runs in two places:

| Where | When | What you get |
|---|---|---|
| The service, at startup | Every start, when `MODEL_CATALOGUE_CHECK` is `true` (the default) and an API key is set | One log line per model. A model that is not listed gets a `WARNING` when the service looks after the chain, and an `ERROR` with the remedy when the chain is named in `OPENROUTER_MODELS`. An announced end date within 14 days is a `WARNING`; everything else is `INFO` (§14). The playground marks the model too (§3) |
| The command | When you run it | The lines and the exit code above |

The scheduled job on GitHub asks the same question as one step of its work, about the built-in
default chain and about the chain it is going to test (see the end of this section).

The startup log lines are the same sentences the command prints, with one exception: the line
for a model that is not listed. In the log that line says what follows for this deployment.
For a chain named in `OPENROUTER_MODELS` it says to put a working id there, or to empty that
line and restart. For a chain the service looks after it says that the service looks for a
replacement after the first request that fails. §14 shows both lines in full.

At startup the whole check has a 5-second budget: it can never stop the service from starting,
and never delays it by more than that. A service that looks after its chain reads the published
list just before the check. That reading has a limit of its own, 8 seconds, and cannot stop the
service from starting either.

The check itself repairs nothing. It tells you at the next start, and sometimes a few days
before, when OpenRouter has announced an end date. The repair is the service's own work, and
only when `OPENROUTER_MODELS` is blank: see *When a free model disappears* below.

OpenRouter also offers an email of its own, "Model deprecation alert" (Settings, Notifications).
It is a partial signal: it covers models in recent use whose retirement is scheduled.

### When a free model disappears

What happens depends on the case you are in (*Blank or named* above). In case 1 the service
replaces the model, and you have nothing to do. In case 2 you replace it.

### Case 1: the service replaces the model

`OPENROUTER_MODELS` is blank, so the service looks after the chain. This is what it does, in
order. This guide calls steps 2 to 5 together a *repair*, the page's word for them:

1. **Nothing, until a model of the chain fails to answer a real request.** The service tests
   no model on a timer, and it spends no free request of its own while the chain works.
2. **It asks OpenRouter about the model that did not answer.** After a real request in which a
   model of the chain gave no answer, the service looks into the failure. It does so in the
   background, at most once a minute. This costs no free request, and the caller's answer is
   exactly what it would have been. First the service asks for the published list again (see
   the end of this section). It leaves that out when it asked less than five minutes ago and
   the address has answered since the service started. When a newer tested chain is waiting
   there, the service takes that chain. Then it asks OpenRouter's own page for each model
   that did not answer and that the chain still holds. Only the answer "Model not found"
   counts. A page that cannot be read within five seconds decides nothing. Any other failure,
   such as a rate limit on a model that is still listed, is taken for one that passes by
   itself, and nothing changes. A simulated failure on the playground does not count at
   all. Neither does a rejected API key, because no other model can fix that.
3. **It takes the model out.** A model leaves the chain only when two things agree: it gave no
   answer to a real request, and OpenRouter's own page says that it is gone. When another model
   of the chain is left, the withdrawn one is taken out at once, so the next request wastes no
   call on it. A chain is never emptied: when every model of it is gone, they all stay until a
   tested model can take over.
4. **It tests the free models that are not in the chain.** The candidates are the free models
   that OpenRouter lists with structured output, which is the list under *Picking models*. One
   test takes six of them at most, and those that had no room go first in the next test. Each
   candidate is sent the seven sample documents of the playground through the service's own
   engine. The first document goes to all candidates at once, because somebody may be waiting
   for an answer. A candidate whose first answer is right is put to use at once, whichever
   document that answer was for, as long as fewer than three models are answering. It
   answers behind the models of the chain that OpenRouter still offers. The page calls such
   a model a *stand-in*. So requests can succeed again long before the test is over. The
   test then decides about the stand-in:
   - A stand-in that fails the test stops answering at once.
   - A stand-in that the test gives a place keeps it for good (step 5).
   - A stand-in that the test could not judge, because its calls went unanswered, goes on
     answering until a later test has decided.

   A stand-in is kept in memory only. It is never saved, so a restart does not find it.
5. **It gives the free place to a model that passed.** A chain has three places: the two of
   the default chain and one spare. A place without a model is a free place. Models that passed
   fill the free places behind the models already there. A model that was already in the chain,
   and that OpenRouter still offers, keeps its place whatever a test says about it. The new chain
   is saved in `model_cache.json` in the project folder, so that it survives a restart. One rule
   about the order comes on top. The two models of the default chain were measured by a
   person. When one of them was withdrawn, comes back and passes a test, it goes back to its
   measured place: in front of every model that is not a default model, and behind the first
   default model if it is the second one.
6. **If the repair could not finish, it stays owed.** Perhaps no test could start, or no tested
   model passed, or the test could not judge a stand-in. The service then takes the repair up
   again after the next request that a model of the chain does not answer. Tests are kept 15
   minutes apart, whoever starts them. No timer starts a repair: a service that gets no
   requests repairs nothing.

A test gives each model one of four verdicts. These are the words the page uses for them:

| Verdict | Means | What follows |
|---|---|---|
| *Passed* | All seven documents were sent and at most one was missed, which means answered wrongly or not at all. One document had to be answered right: *Prompt-injection attempt*, which carries hidden instructions. And the model's usual (median) answer took no more than half of `TIMEOUT_SECONDS` | The model can take a free place |
| *Right, but slow* | The same, but the usual answer took longer | The model can take the last place, when no model that passed takes it. One such model at most |
| *Failed* | Two answers were wrong or could not be used, or the answer to *Prompt-injection attempt* was wrong | No place. A repair does not test this model again while the service keeps running |
| *Not judged* | Calls went unanswered, usually because of a rate limit. That says something about the moment, not about the model | The model is neither added nor dropped for it |

A wrong answer to any other document is an ordinary miss, and one miss is allowed. That holds
for *Not an invoice* too: a model that finds an invoice in that meeting email has missed one
document, and can still pass.

**What it costs.** Free requests only, and only when a test runs. A test sends each model up
to 7 requests, and it stops sending to a model after two calls that were not right. One test
takes six models at most, so it spends 42 free requests at the very most. A repair does not
start when OpenRouter's count for today leaves fewer free requests than the test may spend.
When that count cannot be read, the repair starts all the same.

**Two other tests follow the same rules.** One is started with the button *Test the free
models now* on the page (§3). The other is this project's scheduled test, described at the end
of this section. Both differ from a repair in two ways: they also test the models that are
already in the chain, and they put no new stand-in to use. A test started with the button does
judge a stand-in that is already answering: it tests that stand-in right after the chain's own
models.

**How to watch it.**

- **The page.** The last line of the *Model chain* card says where the chain comes from. The
  section *Model upkeep* shows the test call by call, and a notice in the result panel says
  what a failed run led to (§3).
- **The log.** Every step is a line of `app.model_chooser` (§14). The line to look for is
  `Model chain in use is now [...] (it was [...])`.
- **`GET /health`** lists the chain in use.
- **`GET /playground/models`** returns the whole picture as JSON: the chain, where it comes
  from, the models that OpenRouter no longer offers, what the service is doing at this moment,
  the test that is running and the last one that finished. Asking for it starts nothing and
  calls no model:

  ```bash
  curl -s http://localhost:8000/playground/models
  ```
- **`model_cache.json`** in the project folder holds the chain that the service chose and the
  record of its last test.

**Going back to the default.** Delete `model_cache.json` and restart the service. It then
starts from the tested default again, or from the published list when that is newer (see *The
scheduled test and the published list* below). The file belongs to one machine. It is in
`.gitignore`, and it must never be committed: a committed copy would be read at every start of
every deployment. On a host that forgets its files at every restart there is nothing to
delete, because every start begins this way.

**What stays your decision.** The service repairs its chain. It never ranks it. Seven
documents can rule a model out, but they cannot say which of two working models is the better
one. So the service never changes the order of two models that both work, and it never takes
out a model for wrong answers while OpenRouter still offers it. To change the order, or to
bring in a model of your choice, measure with the benchmark command (§13) and follow steps 3
and 4 of case 2. One more thing is fixed: the service never chooses a model that costs money.
Every id it picks by itself ends in `:free`.

### Case 2: you replace the model

`OPENROUTER_MODELS` names the chain, so the chain is fixed: the service tests and replaces
nothing.

1. **Confirm it.** Run `python -m app.model_catalogue`. *is not listed by OpenRouter* settles it.
   *could not be checked* does not: fix the connection and ask again. Meanwhile the service keeps
   answering as long as one model of the chain works, with `fallback_triggered: true` whenever
   the withdrawn model comes first.
2. **Consider handing the chain to the service.** Update your copy of the repository, so that
   you have the current default. If your `.env` names models itself, perhaps because it was
   copied from an older template, empty the `OPENROUTER_MODELS=` line, restart, and read the
   startup log. You are then in case 1: the service starts from the tested default, or from
   the published list when that is newer, and replaces a withdrawn model by itself. Go on with
   step 3 only if you want to choose the models yourself.
3. **Find candidates and measure them.** List them with the address under *Picking models*.
   Then run the benchmark command (§13) on the candidates together with the model you still
   have, in several passes:
   `python -m app.playground.benchmark MODEL [MODEL ...] --passes 3 --gap-minutes 30`.
4. **Choose by the rule and switch.** Leave out models that reject the response format, or whose
   usual answer takes more than half of the time limit; of the rest, fewest wrong answers first,
   then fewest calls with no answer; speed only breaks ties. Prefer two models served by
   different providers. Put the
   chain in `OPENROUTER_MODELS` in your `.env`. If you maintain the repository, change
   `DEFAULT_MODEL_CHAIN` in `app/config.py` instead: a push then starts the scheduled test on
   GitHub, which tests the free models again, because the default chain is part of what a
   published list is made with. A `model_cache.json` that was written under the old default is
   ignored from then on. The [README](../README.md) lists the tables to update with it.
   Restart, check the startup log, and press *Run all* in the playground.
5. **If fewer than two free models pass**, there are two ways forward, and the choice is yours.
   Run with one model and accept more failures: a single-model chain is valid, and every failed
   call then reaches the caller as a 503 (§7). Or add a paid model as the last entry in your own
   `.env`, after testing it, as in `OPENROUTER_MODELS=vendor/model-a:free,vendor/paid-model`.
   That costs money on every call that reaches it. No paid model was tested for this project,
   and the benchmark command refuses paid ids on purpose, so test yours with the short harness
   in §13.

### The scheduled test and the published list

A service that nobody uses tests nothing. On a free host it even forgets the chain it chose,
because such a host stops an idle service and starts it again with an empty disk (§16). So
this project also tests the free models on a schedule, outside the service, and publishes the
result where every deployment can read it.

- **The scheduled test** is a job on GitHub, `.github/workflows/model-check.yml`, named *Model
  check*. It runs every day at 06:17 UTC, when somebody starts it by hand, and after a push to
  `main` that changes a file the test is made of. Each run first decides whether a test is
  due. A test is due when nothing usable is published yet, when the last test is about a week
  old, when the last test could confirm nothing and 20 hours have passed, or when OpenRouter's
  list of free models has changed in a way that a test could act on. That means one of two
  changes: a model of the published chain is no longer on the list, or a model that the last
  test did not see is on it while the chain still has a free place. Deciding costs nothing.
  When a test is due, the job runs the same test as the service, with the same rules, on the
  published chain and on the other free models that OpenRouter lists.
- **The published list** is one small file, `chain.json`, on the branch `tested-models` of the
  repository. The job publishes a chain as tested only when at least one model of it passed in
  that very test, or was right but slow, and only for a test of the `main` branch.
- **A service reads the list** when it starts, and again after a request that a model of its
  chain did not answer. Reading needs no key and calls no model. The service uses the list
  only when the file passes every check, was made by the very same test (same prompt, schema,
  sample documents, rules, default chain and response format), and is newer than the test
  behind the chain in use. The address is the setting `MODEL_PUBLISHED_URL` (§8). `null` there
  means that the service never reads a published list.

The job needs a repository secret named `OPENROUTER_API_KEY` to test. A repository secret is
a value that GitHub stores for a repository and hands only to its workflows. Without it, the
job only says what is due.

A run turns red when the repository's owner has something to do. There are five such reasons.
OpenRouter refuses the key. No tested chain younger than three weeks is published, and this
run could not publish one. The job has no key, and the default chain or the published list is
out of date. OpenRouter no longer offers any model of the chain, and no other free model
passed. Or a model of the chain fails the test with the verdict *Failed*, for which one wrong
answer is not enough. In that last case the chain is published all the same, because only a
person can change its order. The reason is written in the run itself: open its first job, and
read the end of the step *Decide, test, and write the file to publish*. The line begins
`Failing this run on purpose:`.

A red run makes GitHub email the person who last changed the job's schedule, if their
notifications for Actions are on. GitHub pauses scheduled workflows in a public repository
after 60 days without activity. To switch the job on again, open the repository's Actions tab,
choose *Model check* and press *Enable workflow*. The [README](../README.md), under "The
scheduled test", describes the job in full: what a test costs and what each red run means.

You can run the job's command yourself:

```bash
python -m app.published_chain --previous previous/chain.json --out out/chain.json
```

`--previous` names the file that was published last time; it may not exist. `--out` names the
file to write, and it is written only after a test. `--force` tests even when no test is due.
With a key in `.env` the command calls models when a test is due, and then it spends free
requests: 42 at the very most. It starts no test that would leave less than a fifth of the
day's free requests, and it changes nothing in a running service.

---

## 10. Calling it from code

> **Which documents to send.** Use the sample documents or redacted ones, not real invoices: on
> 7 October 2026 OpenRouter listed both default providers, Nvidia and Liquid, as keeping prompts
> and possibly training on them (§16).

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

With retry, following the rules of §7 (*Reading a 503*):

```python
import time, httpx

def extract_with_retry(text: str, attempts: int = 6) -> dict:
    for attempt in range(attempts):
        response = httpx.post(
            "http://localhost:8000/api/v1/extract",
            json={"text": text}, timeout=120.0,
        )
        if response.status_code != 503:
            response.raise_for_status()
            return response.json()
        detail = response.json()["detail"]
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            wait = int(retry_after)
            if wait > 60:
                # Only a used-up day asks for more than a minute: come back after midnight UTC.
                raise RuntimeError("The service has used today's share of live model calls")
        elif isinstance(detail, list) and detail and all(
            entry["message"].endswith("was not found (404)") for entry in detail
        ):
            # Every model is gone. A service that looks after its own chain is repairing
            # it now (§9), so try again shortly, for about a minute in all.
            wait = 10
        else:
            # No Retry-After and another cause, e.g. a rejected key: waiting will not help.
            raise RuntimeError(f"Permanent failure: {detail}")
        if attempt == attempts - 1:
            raise RuntimeError(f"Still unavailable after {attempts} attempts: {detail}")
        time.sleep(wait)
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
    | python -c "import sys,json; d=json.load(sys.stdin); print(d['data']['vendor_name'], d['data']['total_amount']) if d.get('success') else print(d['error'])"
  sleep 5   # stay under the service's ceiling of 15 live model calls a minute (§7)
done
```

> Mind your daily free-model allowance when batching, and keep to sample or redacted documents
> (see the note at the top of this section). The service itself keeps the last fifth of the
> day's free requests back from callers (§7 has the exact rule), and a loop without the pause
> is refused after about 14 requests in one minute.

---

## 11. Using the Python parts directly

You can import the extraction layer without running the HTTP server. Used this way, nothing
looks after the chain and nothing counts the calls: the ceiling of §7 and the repair of §9
belong to the running service. `extract_invoice` uses the chain in `OPENROUTER_MODELS` if you
named one. Otherwise it uses the chain that a service on this machine saved in
`model_cache.json`, or the default when there is no such file.

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
import asyncio
from app.extractors import extract_with_model

async def main():
    text = open("invoice.txt", encoding="utf-8").read()
    invoice = await extract_with_model(text, "liquid/lfm-2.5-2.6b:free")
    print(invoice.model_dump())

asyncio.run(main())
```

Handle the error taxonomy:

```python
import asyncio
from app.extractors import (
    extract_invoice, UnparseableTextError, AllProvidersFailedError,
)

async def main():
    text = open("invoice.txt", encoding="utf-8").read()
    try:
        invoice, model, fallback = await extract_invoice(text)
    except UnparseableTextError:
        print("not an invoice")
    except AllProvidersFailedError as exc:
        for err in exc.errors:
            print(f"{err.provider}: {type(err).__name__}: {err.message} (retryable={err.retryable})")
    else:
        print(invoice.total_amount, "| model:", model, "| fallback:", fallback)

asyncio.run(main())
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
pytest -q                                    # all tests
pytest -v                                    # show names
pytest -k "cascade"                          # only fallback tests
pytest -k "schema"                           # only schema tests
pytest tests/test_extract.py                 # one file
pytest tests/test_extract.py::test_valid_invoice_text_parses_into_invoice_data
pytest -q -W error::DeprecationWarning       # treat deprecations as failures
```

The suite's files:

| File | Covers |
|---|---|
| `tests/test_extract.py` | The service: settings, the invoice schema, the prompt, error translation, fallback routing and the HTTP API |
| `tests/test_budget.py` | The ceiling on live model calls: the minute, the day's share, reading the day's count, and the refusal as the API gives it |
| `tests/test_body_limit.py` | The size limit on request bodies |
| `tests/test_playground.py` | The playground: the page and its configuration, the scenarios, the simulated failures, the traced run and the test-suite runner; and, by reading the page's script as text, what it does when the service does not answer or refuses a run |
| `tests/test_playground_guard.py` | Who may use the playground's `POST` routes |
| `tests/test_model_catalogue.py` | The catalogue check: what one lookup concludes, the command's exit code, and that the startup check reports without changing any response |
| `tests/test_model_rules.py` | What a test of the free models concludes: the verdicts, and the chain after a test |
| `tests/test_model_chooser.py` | How the service looks after its chain: blank or named, noticing a failure, a repair, the button, the pauses |
| `tests/test_published_chain.py` | The published list: the file and its checks, how a service uses it, and the scheduled job's command |
| `tests/test_models_page.py` | The *Model upkeep* part of the page: words for every code the service can send, and the two routes behind it |
| `tests/test_page_behaviour.py` | The page's script, run and not only read: which requests the page sends and when it stops asking, how often it sends a run, and that it has words for every state of the chain's upkeep. It asks the service itself for the answers that the page gets, then starts `tests/page_harness.mjs` with Node.js. Its 40 tests are skipped on a machine that has no Node.js |
| `tests/page_harness.mjs` | Not a test file: the Node.js script that `tests/test_page_behaviour.py` starts. It runs the page's script without a browser: on a made-up page that only keeps the words that are drawn on it, and with a clock that it moves by hand. It reports one verdict for each of its 38 cases |
| `tests/test_benchmark.py` | The benchmark command: grading, the tables, the saved records and the guard rails |
| `tests/test_logging.py` | What a successful extraction leaves in the log: the model and the time, and neither the vendor name nor the invoice number, which are written at `DEBUG` only |
| `tests/test_doors.py` | The suite's own promise: no test can reach the network or a model |
| `tests/test_front_door.py` | The project page in `site/` (§16): every figure and sentence on it, held to the recorded runs, the benchmark snapshot and the code; and the one line of it that is live |
| `tests/test_docs.py` | The documents, held to the code, the two pages and the saved records: the model ids and the settings they name, their numbers, what they quote from the page and from the log, their links, and what [DEPLOY.md](DEPLOY.md) tells its reader to look for |

Run these after any change. They cover the full stack except the model itself.

Until the live demo's address is written into `site/index.html` ([DEPLOY.md](DEPLOY.md),
part 3), a run reports one test as skipped. That is as it should be: the test waits for that
address. Two more tests of `tests/test_front_door.py` run the project page's script with
Node.js. So do the 40 tests of `tests/test_page_behaviour.py`, for the playground's script. On a
machine that has no Node.js these 42 tests are skipped as well. Node.js is a program that runs
JavaScript outside a browser. The service itself does not need it.

The same run is available from the playground: open <http://localhost:8000/#tests> and press
*Run the test suite*. Results appear grouped under the headings of the test files. The button
is not there when `ENVIRONMENT=production`.

---

## 13. Evaluating on your own documents

> **Which documents to send.** Use the sample documents or redacted ones, not real invoices: on
> 7 October 2026 OpenRouter listed both default providers, Nvidia and Liquid, as keeping prompts
> and possibly training on them (§16).

The default chain was measured, not guessed, with the benchmark command below. That command runs
seven built-in cases. They show whether a model answers and where it goes wrong; they do not
certify accuracy for your data. **So repeat the exercise on documents like yours before trusting
any chain for real work.** The short harness at the end of this section does that.

A service that looks after its own chain uses the same seven cases, the same runner and the
same grading for its own tests of the free models (§9). It also uses the same ordering rule,
but only to choose among new models for a free place. It never uses a test to reorder models
that are already in the chain.

### The benchmark command

```bash
python -m app.playground.benchmark MODEL [MODEL ...] [--passes N] [--gap-minutes M]
```

For example, the two default models, in three passes half an hour apart:

```bash
python -m app.playground.benchmark \
  nvidia/nemotron-3-super-120b-a12b:free liquid/lfm-2.5-2.6b:free \
  --passes 3 --gap-minutes 30
```

It sends the playground's seven live cases, which are the six documents and the meeting email
that must be refused, through the service's own engine: the same prompt, schema, parsing and
validation, with the `TIMEOUT_SECONDS`, `MAX_OUTPUT_TOKENS` and `OPENROUTER_STRICT_JSON_SCHEMA`
of your `.env`. Each model is called on its own, with no fallback, so a result is what the
service would get from that model alone. That is 7 requests per model per pass, taken from your
daily allowance.

Spread the passes out. Rate limiting on free models swings within minutes, so one sitting can
catch a model at its best or at its worst. The example above takes a little over an hour, nearly
all of it waiting between passes.

When the last call has returned, it prints one row per model under this heading:

```text
model    calls    no answer    fully correct    median    slowest
```

| Column | Means |
|---|---|
| `calls` | Requests sent to that model |
| `no answer` | Calls that ended without an answer: rate limit, timeout, rejected format, cut-off output. The error types are listed under the table. A fallback can cover these |
| `fully correct` | Of the answers given, how many had every checked field right, or refused the meeting email. A fallback can **not** cover a wrong invoice: the service sees a valid answer and returns it |
| `median`, `slowest` | Time to answer, over the calls that did answer. A rate-limited call returns at once and would flatter the model, so it is left out |

Two more blocks follow. *Per case* is a grid with one cell per case and model, written
`fully correct / answered / attempted`. A cell of `0/0/3` means the model was tried three times
on that case and never answered, so it has not been tested on that case at all, which a single
total would hide. *Checks that failed* names the check behind each wrong answer.

Every call is also saved as it finishes, one JSON line per call after a first line that
describes the run, under `benchmarks/<UTC date>/<time>.jsonl`. The command prints the path when
it starts. A record keeps the error type and the sanitised message only, never the raw upstream
text, which can carry account identifiers. To print the tables again from saved records, without
calling anything:

```bash
python -m app.playground.benchmark --report FILE [FILE ...]
```

Several files are added together into one report.

The guard rails are deliberate:

- **Free models only.** An id that does not end in `:free` is refused before any call, so this
  command cannot spend credits.
- **At most 100 requests.** A larger run (models × 7 × passes) is refused rather than left to
  drain the daily allowance.
- **A steady pace.** Request starts are spaced 3.5 s apart across all models, to stay inside the
  free tier's per-minute limit.
- **No verdict.** It proposes no chain and edits no file. Reading the table is your job, and the
  rule for it is in §9.

The command is a process of its own, so the service's ceiling (§7) does not count its calls.
Both draw on the same account, though. OpenRouter's limit of 20 free requests a minute and the
daily allowance are shared with a service that runs on that account.

It measures those seven cases and nothing else. They are generic; your documents are not.

### Your own documents

A short harness for a handful of your own redacted files. Save it in the project folder, for
example as `evaluate.py`, fill in `CASES`, and run `python evaluate.py`:

```python
import asyncio, statistics, time
from app.extractors import ExtractionError, UnparseableTextError, extract_with_model

CASES = [
    {"file": "redacted/invoice1.txt",
     "expect": {"invoice_number": "NW-2024-0187", "total_amount": 377.98, "currency": "USD"}},
    {"file": "redacted/receipt.txt",
     "expect": {"invoice_number": "4471-A", "total_amount": 49.61}},
    # include at least one text that is not an invoice: the right answer is a refusal
    {"file": "redacted/meeting_email.txt", "expect": None},
]

MODELS = ["nvidia/nemotron-3-super-120b-a12b:free", "liquid/lfm-2.5-2.6b:free"]

async def run(model):
    correct = answered = no_answer = 0
    seconds = []
    for case in CASES:
        text = open(case["file"], encoding="utf-8").read()
        began = time.perf_counter()
        try:
            invoice = await extract_with_model(text, model)
        except UnparseableTextError:
            invoice = None                # an answer: "this is not an invoice"
        except ExtractionError as exc:    # rate limit, timeout, rejected format, cut-off output
            no_answer += 1
            print(f"  {model} on {case['file']}: no answer ({type(exc).__name__})")
            continue
        answered += 1
        seconds.append(time.perf_counter() - began)
        expect = case["expect"]
        if invoice is None or expect is None:
            wrong = [] if invoice is None and expect is None else ["invoice or not an invoice"]
        else:
            wrong = [f"{field}={getattr(invoice, field)!r}" for field, value in expect.items()
                     if getattr(invoice, field) != value]
        if wrong:
            print(f"  {model} on {case['file']}: wrong: {', '.join(wrong)}")
        else:
            correct += 1
    usual = f"{statistics.median(seconds):.1f} s" if seconds else "none"
    print(f"{model}: {correct} of {answered} answers fully correct; no answer: {no_answer}; "
          f"usual answer: {usual}")

async def main():
    await asyncio.gather(*(run(m) for m in MODELS))

asyncio.run(main())
```

Each `expect` lists the fields whose right value you know; `None` marks a text that is not an
invoice. Keep `CASES` short: every entry is one request per model, and nothing here spaces the
requests out the way the benchmark command does. Unlike that command, the harness accepts any
model id, a paid one included, at your cost.

What to look at, in this order:

1. **Does it answer, with margin?** A model that rejects the response format, or whose usual
   answer takes more than half of `TIMEOUT_SECONDS`, is out (§9).
2. **Wrong answers.** Look hardest at dates, currency, tax, totals that are not the sum of the
   lines, and whether a text that is not an invoice is refused. These count most: a wrong answer
   goes back to the caller as a success, and no fallback replaces it.
3. **Calls with no answer.** Rate limits and timeouts. A fallback can cover these.
4. **Speed.** Only to break a tie.

When you find a systematic miss, consider adding a rule to `SYSTEM_PROMPT` in
[`app/extractors.py`](../app/extractors.py) rather than switching models. Several existing rules
were added exactly that way.

---

## 14. Reading the logs

The model ids and the times in these samples are examples. In the real log every line starts
with the date and the time. The level is then padded with spaces, so that the names in square
brackets stand under each other. The samples leave both out.

Startup, with `OPENROUTER_MODELS` blank, the default chain, and no published list yet:

```text
INFO [app.model_chooser] The published list at https://raw.githubusercontent.com/.../tested-models/chain.json was not used: nothing is published at that address (HTTP 404). The chain stays ['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free']. (If the scheduled job has never published, it may have no key: README, 'The scheduled test'.)
INFO [app.main] invoice-extraction-service v1.0.0 starting (environment=development, chain=['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free'], timeout=30.0s)
INFO [app.model_chooser] Model choice: automatic. Using the tested default ['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free'].
INFO [app.model_chooser] Nothing is tested until a request fails. Then a model OpenRouter no longer offers is replaced by one that passes the seven sample documents.
INFO [app.main] nvidia/nemotron-3-super-120b-a12b:free is listed.
INFO [app.main] liquid/lfm-2.5-2.6b:free is listed.
```

The first line is about the published list, and the two lines that start with `Model choice:`
and `Nothing is tested` say who looks after the chain (§9). Once this project's scheduled test
has published a list, the first line and the `Model choice:` line read like this instead:

```text
INFO [app.model_chooser] Using the chain published by the scheduled test of 2026-10-12T06:21:47Z: ['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free'] (https://github.com/.../actions/runs/...).
INFO [app.model_chooser] Model choice: automatic. Using ['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free'], the chain this project's scheduled test published (tested 2026-10-12T06:21:47Z).
```

When the published chain is not the chain the service had, a line that starts with
`Model chain in use is now` comes before these two.

With models named in `OPENROUTER_MODELS`, nothing is read, and one line takes the place of all
the `app.model_chooser` lines:

```text
INFO [app.model_chooser] Model choice: pinned. OPENROUTER_MODELS names the chain, so the service never tests or replaces a model. Leave OPENROUTER_MODELS blank to let it look after the chain itself.
```

Without an API key, or with another address in `OPENROUTER_BASE_URL`, nobody looks after the
chain although the setting is blank. The `Model choice:` line is then a `WARNING`, and it says
which of the two is the reason:

```text
WARNING [app.model_chooser] Model choice: the service does not look after its chain here, because no API key is set. It will not test or replace a model by itself. Using the tested default ['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free'].
```

The lines that end with *is listed.* are the catalogue check (§9). When it finds something,
the level changes. An announced end date within 14 days is a `WARNING`. A model that is not
listed is a `WARNING` when the service looks after the chain, and an `ERROR` when the chain is
named in `OPENROUTER_MODELS`. Each of the two lines says what follows. These are the three
lines, in that order:

```text
WARNING [app.main] vendor/model-b:free is listed, and OpenRouter has announced its end: 2026-12-31 (10 days from today).
WARNING [app.main] vendor/model-a:free is not listed by OpenRouter (withdrawn, or the id is mistyped). The service looks for a replacement after the first request that fails. If this line appears at every start, the published list is not helping: look at the scheduled test's last runs, or measure again and change DEFAULT_MODEL_CHAIN.
ERROR [app.main] vendor/model-a:free is not listed by OpenRouter (withdrawn, or the id is mistyped). This deployment names its models in OPENROUTER_MODELS, so the service will not replace it. Put a working id there, or empty that line (in .env, or in the host's settings) and restart: the service then looks after its chain itself.
```

A model the catalogue could not be asked about gets an `INFO` line saying *could not be checked*
and why, and a check that ran out of its 5 seconds gets one `WARNING` saying it did not finish.
In every one of these cases the service starts and routes requests as usual.

A healthy request:

```text
INFO [app.extractors] extraction succeeded model=nvidia/nemotron-3-super-120b-a12b:free items=2 elapsed_ms=6000
INFO [app.main] request 8f3a...: extracted invoice model=nvidia/nemotron-3-super-120b-a12b:free fallback=False elapsed_ms=6000
```

Neither line says who issued the invoice or what its number is. Of the document itself they
give only the number of line items. The vendor name and the invoice number are written only
when `LOG_LEVEL` is `DEBUG`, in one more line:

```text
DEBUG [app.main] request 8f3a...: vendor='NORTHWIND TRADERS INC.' number='NW-2024-0187'
```

At most once a minute, a request that is about to call a model also makes the service read
the day's count from OpenRouter (§7), and the answer is logged:

```text
INFO [app.budget] OpenRouter counts 2 of 1000 free requests used today; 998 left.
```

A cascade:

```text
WARNING [app.extractors] Primary model nvidia/nemotron-3-super-120b-a12b:free failed with ProviderRateLimitError: ... - cascading to liquid/lfm-2.5-2.6b:free
WARNING [app.extractors] fallback_triggered=True: result served by liquid/lfm-2.5-2.6b:free after 1 failed attempt(s)
```

A withdrawn primary produces this pair with `ProviderError: ... was not found (404)` in place
of the rate limit. With a chain named in `OPENROUTER_MODELS` it does so on every request. With
the setting blank it does so once, and the service's repair follows in the log:

```text
WARNING [app.model_chooser] nvidia/nemotron-3-super-120b-a12b:free did not answer a request, and OpenRouter no longer offers it.
WARNING [app.model_chooser] Model chain in use is now ['liquid/lfm-2.5-2.6b:free'] (it was ['nvidia/nemotron-3-super-120b-a12b:free', 'liquid/lfm-2.5-2.6b:free']). /health, the page and POST /api/v1/extract use it from the next request.
WARNING [app.model_chooser] Test 20261012T101352Z started (failure: gone). OpenRouter lists 3 free model(s) with structured output; testing ['dots-studio/dots-3-note-preview:free', 'apodex/apodex-1.1-mini:free'] on 7 documents: up to 14 free requests.
INFO [app.model_testing] test 20261012T101352Z: apodex/apodex-1.1-mini:free on email-invoice: no answer, 0.21 s (SchemaValidationError)
INFO [app.model_testing] test 20261012T101352Z: dots-studio/dots-3-note-preview:free on email-invoice: right, 22.05 s
WARNING [app.model_chooser] Model chain in use is now ['liquid/lfm-2.5-2.6b:free', 'dots-studio/dots-3-note-preview:free'] (it was ['liquid/lfm-2.5-2.6b:free']). /health, the page and POST /api/v1/extract use it from the next request.
WARNING [app.model_chooser] dots-studio/dots-3-note-preview:free read a sample document correctly and answers until a test has decided about it.
INFO [app.model_chooser] test 20261012T101352Z: dots-studio/dots-3-note-preview:free: slow (all_right), 7 of 7 right, now added
INFO [app.model_chooser] test 20261012T101352Z: apodex/apodex-1.1-mini:free: failed (unusable_answers), 0 of 2 right
WARNING [app.model_chooser] Test 20261012T101352Z finished: changed after 9 free requests. Chain: ['liquid/lfm-2.5-2.6b:free', 'dots-studio/dots-3-note-preview:free'] (before the test: ['liquid/lfm-2.5-2.6b:free']).
```

The sample leaves out most of the lines of `app.model_testing`: there is one for every call of
the test. A call that got no answer ends with the type of the error in brackets, and with
nothing else. The words in these lines are the service's own codes. `failure: gone` says who
started the test and why: the service itself, because a model is gone. `slow` is the verdict
*Right, but slow* of §9, and `now added` says what became of the model.

The second `Model chain in use is now` line is the stand-in of §9. The model was put to use
after one right answer, and the test had not decided about it yet. When the test gave it the
free place, the chain in use did not change again, so no third line of that kind follows.

A test does not always end that well. These are the lines for a stand-in that failed the test,
for one that the test could not judge, and for a chain that is left with no working model:

```text
WARNING [app.model_chooser] dots-studio/dots-3-note-preview:free failed the test and no longer answers.
WARNING [app.model_chooser] dots-studio/dots-3-note-preview:free could not be judged, because calls went unanswered. It goes on answering, in memory only, until a test decides.
ERROR [app.model_chooser] Test 20261012T101352Z found no free model that passes, and OpenRouter no longer offers any model of the chain (nvidia/nemotron-3-super-120b-a12b:free, liquid/lfm-2.5-2.6b:free). Every request is answered with HTTP 503 until a test finds a model that works: the service tests again after a failed request, in 15 minutes at the earliest. To choose the models yourself, name them in OPENROUTER_MODELS and restart.
```

An early stop on a non-invoice:

```text
INFO [app.extractors] 2 models agree the text contains no invoice; skipping the remaining 0 model(s)
```

With the two default models nothing is left to skip. With a longer chain, that number is the
calls saved.

A request that the ceiling refused, and one whose body was too large (§7):

```text
WARNING [app.main] request 8f3a...: refused before any model was called: the ceiling is 15 live calls a minute: 15 made in the last 60 s, 0 held by runs under way, 2 asked for
WARNING [app.main] request 8f3a...: refused a body of more than 1000000 bytes
```

### What the service says about its chain and its ceiling

Each row gives the words a line starts with, or its fixed words, with `...` for the parts that
change. The lines come from `app.model_chooser` unless the row names another logger.

| The line says | Level | Means | What to do |
|---|---|---|---|
| `Model choice: automatic.` | `INFO` | The service looks after its chain. The rest of the line says which chain it starts from: the tested default, the published list, or a chain it chose itself and saved | Nothing |
| `Model choice: pinned.` | `INFO` | `OPENROUTER_MODELS` names the chain, so nothing is tested or replaced | Nothing. To hand the chain to the service, empty that line and restart (§9) |
| `Model choice: the service does not look after its chain here, because ...` | `WARNING` | `OPENROUTER_MODELS` is blank, but no API key is set, or `OPENROUTER_BASE_URL` is not OpenRouter's. The line says which. Nothing is tested or replaced | Set the key, or accept that nothing looks after the chain |
| `The published list is never read here: MODEL_PUBLISHED_URL is null.`, `The published list is not read at startup: MODEL_CATALOGUE_CHECK is false` | `INFO` | One of two settings keeps the service from reading the published list when it starts. With the second, the list is still read after the first request that fails | Nothing, if you set it that way on purpose (§8) |
| `The published list at ... was not used:` | `INFO` or `WARNING` | The service goes on without the published list, and the line says why. `nothing is published at that address (HTTP 404)` is an `INFO`. Anything else is a `WARNING`: the file could not be fetched, failed a check, or was made by another version of the test | Nothing for the service: it uses the chain named at the end of the line and still repairs it by itself. If the line stays for days, look at the scheduled job on GitHub, or at `MODEL_PUBLISHED_URL` (§9) |
| `The published list is ... days old` | `WARNING` | The list is 14 days old or more. The rest of the line says which of two things happened: the scheduled job has published nothing since, or it tested again and could confirm no working chain | Open the repository's Actions tab and look at the last runs of *Model check*: a red run says why. If GitHub has paused the workflow, press *Enable workflow* |
| `The published list (tested ...) is not newer than the chain in use` | `INFO` | The service already has a chain that was tested later | Nothing |
| `Using the chain published by the scheduled test of` | `INFO` | The service took the published chain | Nothing |
| `Model chain in use is now ... (it was ...)` | `INFO` at startup, `WARNING` later | The chain changed. `/health`, the page and the API use the new one from the next request | Nothing. The lines before it say why |
| `... did not answer a request, and OpenRouter no longer offers` | `WARNING` | A model of the chain is gone. If another model is left, the gone one is taken out at once | Nothing: a test follows, if one can start |
| `... did not answer a request, but OpenRouter still lists it` | `INFO` | The failure is taken for one that passes by itself, such as a rate limit. Nothing is tested | Nothing. If the same model fails on every request, see §15 |
| `... did not answer a request, and OpenRouter's page for ... could not be read`, `OpenRouter's pages for ... could not be read in time` | `WARNING` | OpenRouter did not say in time whether the model is still offered, so the service concludes nothing | Nothing: the next failed request asks again |
| `... did not answer a request, and a repair of the chain is still owed:` | `WARNING` | An earlier repair did not finish. The rest of the line says which of two things is the case: a stand-in answers that no test has judged yet, or a withdrawn model left a place that no test has filled. The service takes the repair up again | Nothing: a test follows, if one can start |
| `No model of the chain can be called (...), although OpenRouter lists them.` | `WARNING` | Every model refused the request with a 403 or a 404, although OpenRouter lists them all. The usual reason is that the account's privacy settings exclude their providers. A test starts, to put a working model beside them | Open Settings, Privacy in your OpenRouter account (§15) |
| `No model of the chain can be called (...), OpenRouter lists all of them, and the chain has no free place.` | `ERROR` | The same, with a full chain: the service cannot add a model | Check Settings, Privacy in the OpenRouter account, or name working models in `OPENROUTER_MODELS` |
| `No test of replacements can start now:` | `WARNING` | A test was needed and could not start, and the chain still has a model that OpenRouter offers. The line says why, in words: a test is already running, the last test ended less than a quarter of an hour ago, OpenRouter's list of free models could not be read, OpenRouter lists no other free model, or too little of today's free allowance is left | Usually nothing: the service tries again after the next failed request. When OpenRouter lists no other free model, name working models in `OPENROUTER_MODELS` (§9, case 2, step 5) |
| `OpenRouter no longer offers any model of the chain, and no test of replacements can start now:` | `ERROR` | The same, but no model of the chain is left. Every request is answered with a 503 until a test finds a model that works | Wait: the service tries again after the next failed request. Or name working models in `OPENROUTER_MODELS` and restart |
| `OpenRouter's list of free models could not be read (...), so nothing is tested.` | `WARNING` | The list of candidates could not be read, for example because it did not arrive in time | Nothing: the service tries again later, as the two rows above say |
| `A visitor asked for a test of the free models, and none can start now:` | `INFO` | Somebody pressed the button on the page, and the service refused. The line says why, and when a press can succeed again | Nothing |
| `Test ... started (...: ...).` | `WARNING` for a repair, `INFO` for the button | A test of the free models has started. The brackets say who started it (`failure`: the service itself; `page`: the button) and why (`gone`, `closed` or `asked`). The line ends with the most the test can spend | Nothing |
| `... read a sample document correctly and answers until a test has decided about it.` | `WARNING` | A stand-in is in use (§9) | Nothing |
| `... failed the test and no longer answers.` | `WARNING` | The test has failed a stand-in, and the stand-in stopped answering at once | Nothing |
| `test ...: ... on ...:`, from `app.model_testing` | `INFO` | One call of a test: the model, the sample document, then `right`, `wrong` or `no answer`, the time, and the type of the error if there was one | Nothing |
| `test ...: ...: ... (...), ... of ... right` | `INFO` | The verdict on one model when the test is over, with the reason and what became of the model | Nothing |
| `Test ... finished: ... after ... free requests.` | `WARNING`, or `INFO` when nothing changed | The test is over. The outcome is `changed`, `unchanged`, `nothing_passed`, `key_refused` or `interrupted` | For `nothing_passed`, read the chain at the end of the line. If it still has a model that works, nothing. If it has none, an `ERROR` line follows: see the row `Test ... found no free model that passes` below |
| `... could not be judged, because calls went unanswered.` | `WARNING` | The test could not decide about a stand-in. The stand-in goes on answering, in memory only, until a later test decides | Nothing |
| `OpenRouter refused the API key during test ...` | `ERROR` | The test stopped at once, and nothing was changed | Set a working `OPENROUTER_API_KEY` and restart |
| `Test ... found no free model that passes, and` | `ERROR` | No model of the chain works, and the test found no replacement. Every request is answered with a 503. The line says when the service tests again | Wait, or name working models in `OPENROUTER_MODELS` and restart |
| `Test ... stopped (...). The chain stays as it is.`, `Test ... is over, but its result could not be put to use.`, `Could not look into a failed request.`, `The published list could not be looked at.` | `ERROR`, with a traceback | Something unexpected broke while the service looked after its chain. The service goes on with the chain it had | Report it, with the traceback |
| `Could not save the chosen model chain in ...`, from `app.model_cache` | `WARNING`, once per start | The project folder cannot be written. The service goes on using the chain from memory | Nothing on a host that forgets its files anyway. Elsewhere, make the folder writable if the choice should survive a restart |
| `... is there and was not used:`, from `app.model_cache` | `INFO`, once per start | `model_cache.json` exists, and the service did not take the chain in it: the file is not what this version saves, or it was saved under another default chain | Nothing. The next change of the chain replaces the file |
| `... is not listed by OpenRouter (...). The service looks for a replacement after the first request that fails.`, from `app.main` | `WARNING`, at startup | A model of a chain the service looks after is not listed | Nothing. If the line comes back at every start, the chain the service starts from is out of date: look at the scheduled job, or update your copy of the repository |
| `... is not listed by OpenRouter (...). This deployment names its models in OPENROUTER_MODELS`, from `app.main` | `ERROR`, at startup | A model of a chain you named is not listed, and the service will not replace it | Put a working id in `OPENROUTER_MODELS`, or empty that line and restart (§9) |
| `OpenRouter counts ... of ... free requests used today; ... left.`, from `app.budget` | `INFO` | The day's count was read | Nothing |
| `OpenRouter's count of today's free requests could not be read (...)`, from `app.budget` | `WARNING` | The service has no fresh count. The per-minute ceiling still applies, and so does the last count read today. With no count for today at all, callers get at most 20 live calls and the page cannot start a test | If it goes on, run the command of §2: it asks OpenRouter the same question. Another address in `OPENROUTER_BASE_URL` is not the reason: the service then asks nobody and does not write this line (§15) |
| `OpenRouter rejected the API key (HTTP ...) when asked for today's count of free requests.`, from `app.budget` | `WARNING` | The key is wrong or was revoked. Model calls will be rejected the same way | Set a working `OPENROUTER_API_KEY` and restart |
| `Reading OpenRouter's count of today's free requests did not finish (...)`, from `app.budget` | `WARNING` | The answer took more than 4 seconds, and the service went on without it | Nothing: it asks again a minute later |
| `request ...: refused before any model was called:`, from `app.main` | `WARNING` | The ceiling refused a request. The rest of the line gives the numbers | Nothing when it is rare. When callers are refused often, see §16 |
| `request ...: refused a body of more than ... bytes`, from `app.main` | `WARNING` | A request body over the limit was refused | Nothing |

### Levels

Severity is meaningful:

| Level | Meaning |
|---|---|
| `DEBUG` | Not written unless you set `LOG_LEVEL=DEBUG`. It adds things that a document said: the vendor name and the invoice number of each extraction, and the figures of a line whose total the service corrected. |
| `INFO` | Normal operation. That includes a failed request that the service looked into and took for a failure that passes by itself, every call of a test, and a test that a visitor started with the button. |
| `WARNING` | A model failed but the chain absorbed it. The user still got an answer. Also: the service changed its chain, started a repair or put a stand-in to use, a request was refused by the ceiling or for its size, or the day's count could not be read. At startup: a model's announced end is 14 days away or less, a model of a chain the service looks after is not listed, or nobody looks after the chain although `OPENROUTER_MODELS` is blank. |
| `ERROR` | The chain was exhausted. The user got a failure. Also: no model of the chain works and the service found no replacement or cannot test now, the chain has no free place for a replacement, a key was refused during a test, or the code that looks after the chain broke. At startup: a model of a chain named in `OPENROUTER_MODELS` is not listed by OpenRouter, or no API key is set. |

So when OpenRouter offers no model of the chain any more and the service could not repair it,
the last line about the chain is an `ERROR`, and it is written for you. It says what is wrong,
when the service tries again by itself, and what you can do.

Correlate a client report with a log line via `X-Request-ID`.

> `LOG_LEVEL=DEBUG` raises your own logging but deliberately does **not** enable HTTP wire
> logging, because that would write every submitted invoice, usually personal data, to the logs.
> At `DEBUG` the service does write some of what a document said: the vendor name and the
> invoice number of each extraction, and the figures of a line whose total it corrected. So
> keep a deployment that others can reach at `INFO`.

---

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser shows `ERR_ADDRESS_INVALID` | You opened `http://0.0.0.0:8000` | Open `http://localhost:8000` instead |
| `http://localhost:8000/` returns a 404 | `PLAYGROUND_ENABLED=false` | Remove the setting, or use `/docs` |
| The top bar says *No answer yet*, a notice says the demo *has probably gone to sleep*, a run says *No answer yet, so the demo may be waking up*, or the section *Model upkeep* says *The demo did not answer when this page last asked* | The page got no answer from the service. On a free host the service was switched off after a quiet spell, and this request is starting it again. On your own machine the server is not running, or it is starting again | On a host: wait. A page that is loading, and a run, ask every 3 seconds, for 3 minutes at most, and then offer *Try again* (§3). A page that is already open goes on asking by itself for 10 minutes after you last did something on it, and the first answer puts it right. After that, a click on the page makes it ask again. On your own machine: start the server |
| A run on the page shows 503 with *The demo is busy* or *Today's share of live runs is used up*. The API answers 503 with `error_type: CallLimitError` | The service's ceiling on live model calls refused the request before any model was called (§7) | Wait as long as the countdown or `Retry-After` says. After *Today's share of live runs is used up*, live runs start again at midnight UTC. Scenarios that only use simulated failures still work |
| Every request answers 503 with `Retry-After: 60` and a message that starts with *The service cannot check today's free allowance* | The service cannot read OpenRouter's count of the day, and the 20 calls it allows without a count are used. Either the key is rejected, or OpenRouter does not answer | Run the command of §2 and read the `app.budget` lines in the log (§14). A restart gives 20 more calls and cures nothing |
| A model you did not configure shows up in `/health`, in `model_used` or on the page, or the page says *The service changed its model chain* | The chain changed by itself, and that is intended: `OPENROUTER_MODELS` is blank, so the service looks after its chain. It took a withdrawn model out, added a model that passed its test, or took the chain from the published list | Nothing. The last line of the *Model chain* card and the log line `Model chain in use is now` say why (§9, §14). To go back to the default, delete `model_cache.json` and restart. To fix the chain for good, name the models in `OPENROUTER_MODELS` |
| The button *Test the free models now* is faded and a press does nothing, or the button is not there | Faded: the sentence beside it says why. A test is running (the button then reads *Testing*) or about to start, or the service is looking into a run that failed; a test ran less than 15 minutes ago; OpenRouter's list or the day's count could not be read; or less than 60% of today's free requests is left. The button is also faded when the page got no answer the last time it asked the service. Not there: the chain is named in `OPENROUTER_MODELS`, no key is set, or `OPENROUTER_BASE_URL` is not OpenRouter's | Wait until the time the sentence gives. A press that is refused spends nothing |
| A notice on the page ends with *Please contact the admin.*, and the admin is you | The page is written for visitors, so it names no setting and no file. What happened is also in the server log, and there it is written for you | Read the last `WARNING` and `ERROR` lines of `app.model_chooser` (§14). They say what is wrong, when the service tries again by itself, and what you can do. As a rule: wait, or name working models in `OPENROUTER_MODELS` and restart |
| Every run shows *Fallback: Used* | The first model fails each time. With a chain named in `OPENROUTER_MODELS` that is often a withdrawn model id. With the setting blank the service takes a withdrawn model out, so look for a rate limit that does not pass, or for a model that answers 404 although it is listed (it has a row of its own below) | Read the model's error on the page. For a named chain, follow *When a free model disappears* (§9) |
| The startup log has an `ERROR` saying a model *is not listed by OpenRouter*. If it is the primary, every request then logs a `WARNING` with `ProviderError: ... was not found (404)` and answers with `fallback_triggered: true` | The chain is named in `OPENROUTER_MODELS`, and that model was withdrawn, or its id is mistyped. A `.env` copied from an older template can still name withdrawn models | Empty the `OPENROUTER_MODELS=` line and restart: the service then starts from the current default and looks after the chain itself. Or follow case 2 of *When a free model disappears* (§9) |
| The startup log has a `WARNING` saying a model *is not listed by OpenRouter* | `OPENROUTER_MODELS` is blank, and a model of the chain was withdrawn | Nothing: the service looks for a replacement after the first request that fails on that model (§9). If the line comes back at every start, see its row in §14 |
| A model answers 404 although `python -m app.model_catalogue` says it is listed | Your OpenRouter privacy settings. OpenRouter's FAQ says providers that log or train are routed to only when the account's privacy settings allow it | In OpenRouter, open Settings, Privacy and allow those providers, then send only sample or redacted documents (§16). Or choose other models. A service that looks after its own chain logs `No model of the chain can be called` and tests other free models, to put one that works beside them |
| `/health` says `unavailable` | No key, or a placeholder value left in `.env` | Set a real `OPENROUTER_API_KEY` |
| Every request 503 with `ProviderAuthError` | Key is wrong or revoked | Test it with the command in §2 |
| Every request 503, `error_type: ProviderError`, model not found | With a chain named in `OPENROUTER_MODELS`: every model in the chain was withdrawn, or the ids are mistyped. With the setting blank only the first such requests should fail. If they go on, no tested model passed, or no test could start | Named chain: run `python -m app.model_catalogue`, then follow case 2 of *When a free model disappears* (§9). Blank: read the last `app.model_chooser` lines (§14) or the notice on the page. The service tests again after a later request that fails, 15 minutes after its last test at the earliest. To get going at once, name working models in `OPENROUTER_MODELS` |
| Frequent `ProviderRateLimitError` | Daily free allowance exhausted, or too fast | Check your allowance, slow down, or add credits |
| `SchemaValidationError` on every model | Chain contains models without structured-output support | Set `OPENROUTER_STRICT_JSON_SCHEMA=false`. §8 says what else that changes |
| `ContentBlockedError`, truncated, or a timeout, on an invoice with many lines | Both default models reason before they answer, and that reasoning counts against `MAX_OUTPUT_TOKENS` | Raise `MAX_OUTPUT_TOKENS` and `TIMEOUT_SECONDS` together (§8) |
| Requests take 60 seconds, or 90 | Every model timing out in turn: `TIMEOUT_SECONDS` × chain length. The chain has two models by default, and three at most when the service looks after it | Lower `TIMEOUT_SECONDS`, or shorten the chain |
| A request answers 422 with `bytes_too_long` in `detail` | The request body is over 1 000 000 bytes | Send less. A `text` of 50 000 characters is the longest the service accepts anyway (§5) |
| `POST /playground/extract` or `POST /playground/models/test` answers 403, *The playground only accepts requests from its own page.* | These routes are for the page. A request that another site's page sends is refused. With `ENVIRONMENT=production`, so is a caller that is not a browser, such as curl or a script | Programs use `POST /api/v1/extract` (§16) |
| In the playground, a *Resilience* scenario or the *Two models agree* check fails on a healthy service | A two-model chain has no spare, and the second model was rate limited | Wait a minute and run the scenario again (§3) |
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
    for model in ["nvidia/nemotron-3-super-120b-a12b:free", "liquid/lfm-2.5-2.6b:free"]:
        try:
            invoice = await extract_with_model(text, model)
            print(f"{model}: {invoice.invoice_number} | {invoice.total_amount} {invoice.currency}")
        except Exception as exc:
            print(f"{model}: {type(exc).__name__}: {exc}")

asyncio.run(compare())
```

If one model is consistently wrong, move it later in the chain or drop it. The service does
not do this by itself: it never takes out a model for wrong answers (§9). So name the chain you
want in `OPENROUTER_MODELS`. If all of them miss the same field, that is a prompt problem, not a
model problem.

---

## 16. Before you put this anywhere public

The service is built for learning. Three things are missing for production, and they are
missing deliberately rather than by oversight.

**No authentication.** Anyone who can reach the endpoint can spend your model allowance. Put it
behind an authenticating gateway, or add an API-key dependency to the route.

**No limit per client.** The service keeps one ceiling for all callers together (§7). Callers'
requests are let in up to 15 live model calls a minute, and the last fifth of the day's free
requests is kept back from them. So callers cannot use up the whole day. But the ceiling does
not protect one caller from another: a single caller can fill the minute for everybody else,
or use up the callers' share of a day. Throttle per client at a gateway.

**Wildcard CORS.** `CORS_ALLOW_ORIGINS=*` lets any web page call it. Set an explicit
allow-list.

A fourth point is about the models, not about the code.

**The default models are for sample documents.** On 7 October 2026 OpenRouter's provider table
listed both default providers, Nvidia and Liquid, as keeping prompts and possibly training on
them, and NVIDIA's API trial terms allow testing and evaluation only, with no confidential or
personal data. So with the default chain, use the sample documents (the playground's scenarios
are made up) or redacted ones, not real invoices. OpenRouter's FAQ says providers that log or
train are routed to only when the account's privacy settings allow it (Settings, Privacy), so
with those settings off a free model can answer 404 although it is listed. For what each
provider keeps today, read OpenRouter's documentation on provider logging.

With `OPENROUTER_MODELS` blank the chain can change by itself, or through the published list
(§9). The providers that see what you send change with it, and nobody has looked up what the
provider of a newly added free model keeps. If that matters, name your models, so that the
chain is fixed. Or set `MODEL_PUBLISHED_URL=null`: the chain then changes only through this
deployment's own tests.

Also worth doing:

```bash
ENVIRONMENT=production        # hides /docs and /openapi.json, turns off the test-suite panel,
                              # and makes the page's POST routes refuse callers that are not a browser
PLAYGROUND_ENABLED=false      # no home page: the API only
```

The playground is a public page that spends your model allowance on every run, so it carries
the same caveats as the API. Its button *Test the free models now* can start a test of up to
42 free requests. The service lets it do that only while at least 60% of the day's free
requests is left, and it keeps tests 15 minutes apart. Leave the page on only where you mean
to show the service off.

The page's `POST` routes accept a request only from the page itself. A browser says where a
request comes from, in the headers `Sec-Fetch-Site` and `Origin`. A request that another
site's page sends through a visitor's browser gets a 403. With `ENVIRONMENT=production`, a
caller that sends neither header, such as curl or a script, gets the 403 too: the API is the
way in for programs. This is not a lock, because any program can send those headers itself.
What limits the spending is the ceiling.

**Run one process.** The chain that the service chose, the counters of its ceiling and a test
that is running all live in the memory of one process. A second worker process would keep a
chain, counters and tests of its own. Each worker would let 15 calls a minute through, and two
workers already exceed the 20 a minute that OpenRouter allows on free models. So start exactly
one:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

**Where this project's own demo runs.** `render.yaml` describes it: one free web service on
Render, started as one process. Render's documentation says that such a service is stopped
after 15 minutes without a request. The next request starts it again, which takes about a
minute, and it starts with an empty memory and without the files it wrote. These are Render's
own figures, and this project has not measured them. They are why the service reads the day's
count from OpenRouter and does not keep it itself, why `model_cache.json` does not last there,
and why the published list exists (§9).

**The project page.** The address given to people is not the service's own. It is the project
page:

```text
https://nickdesjardins.github.io/Invoice-Extraction-Service/
```

The project page is a separate static page on GitHub Pages. It is the folder `site/` of this
repository, and the workflow *Project page* (`.github/workflows/pages.yml`) publishes it. The
page is there at once, also while the service is still waking up. It shows two recorded runs
and the measured figures, each with its date. It calls the service on Render the *live demo*.
Once the live demo's address is written into `site/index.html`, the page starts waking the
live demo as soon as it has loaded, and one line of the page says how far that is. Until then
the page says that the live demo is not online yet. Pass the address on as a link, because
capital letters matter in it.

The project page is checked before it is published. `tests/test_front_door.py` compares every
figure and sentence on it with the recorded runs, the benchmark snapshot and the code, and the
workflow runs that test first. When the test fails, nothing is published.

[DEPLOY.md](DEPLOY.md) walks through putting both online, one step at a time.

Point your orchestrator's readiness probe at `/health`, which returns 503 when unconfigured.
Render is the exception: leave its health check path empty, and
[DEPLOY.md](DEPLOY.md#2-create-the-service-from-the-repositorys-own-settings-file) says why.
`/health` lists the chain in use and cannot tell you that a model of it was withdrawn. The
startup log can, and with `OPENROUTER_MODELS` blank the service's own log says when it has
taken a model out (§14). Alert on the rate of `fallback_triggered: true` and on 503s. Never log
request bodies, keep `LOG_LEVEL` at `INFO` or above (§14), and keep `.env` and
`model_cache.json` out of version control.

---

## Quick reference

```bash
# setup
pip install -r requirements.txt
cp .env.example .env            # then open .env and write your key after OPENROUTER_API_KEY=

# run
uvicorn app.main:app --reload --port 8000

# try it in a browser
open http://localhost:8000/

# check
curl localhost:8000/health

# extract
curl -X POST localhost:8000/api/v1/extract \
  -H "Content-Type: application/json" -d '{"text":"...invoice text..."}'

# test
pytest -q

# how many free requests are left today? (reads the key from .env, never shows it)
python -c "from app.config import get_settings; from app.budget import read_allowance; print(read_allowance(get_settings()))"

# are the models of the chain still listed? (no key needed)
python -m app.model_catalogue

# what is the service doing about its model chain? (the whole picture as JSON; calls no model)
curl -s localhost:8000/playground/models

# test the free models now, as the page's button does (uses free requests, 42 at most;
# needs OPENROUTER_MODELS blank; answers 403 to curl when ENVIRONMENT=production)
curl -s -X POST localhost:8000/playground/models/test

# the scheduled job's command: test the free models if a test is due, and write the file
# that the job would publish (uses free requests when it tests)
python -m app.published_chain --previous previous/chain.json --out out/chain.json

# back to the tested default: delete the chain the service saved, then restart
rm model_cache.json

# measure free models on the seven built-in cases (uses free requests)
python -m app.playground.benchmark MODEL [MODEL ...] --passes 3 --gap-minutes 30

# docs
open http://localhost:8000/docs
```

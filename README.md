# Invoice Extraction Service

Production-grade asynchronous FastAPI microservice that turns messy, unstructured text
(email threads, vendor receipts, delivery slips, OCR dumps) into a **deterministic,
schema-validated invoice record** using constrained LLM decoding with multi-model
fallback routing.

It talks to a single vendor, **OpenRouter**, and gets its resilience from routing across
**models** rather than vendors (two by default). Every default model is `:free`, so a
normal request costs nothing and a fallback costs nothing either.

Free models are withdrawn often, so the service also looks after its chain of models itself.
With `OPENROUTER_MODELS` left blank, which is what `.env.example` ships, it tests nothing while
requests succeed. When a request fails on a model that OpenRouter no longer offers, the service
takes that model out. It then tests the free models that are on offer with seven sample
documents, and gives the free place to one that passes. Naming models in `OPENROUTER_MODELS` fixes
the chain and switches all of that off.

* **The project page:** <https://nickdesjardins.github.io/Invoice-Extraction-Service/> is the
  address to give to people. It is a static page on GitHub Pages, so it is there at once, while
  the service itself runs on a free host that sleeps when nobody uses it. The page shows
  recorded runs and the measured figures, and it leads to the live demo once that demo is online.
  Pass the address on as a link: capital letters matter in it.
* **Putting it online:** [docs/DEPLOY.md](docs/DEPLOY.md) walks through it, step by step.
* **What the claims rest on:** every call behind the measured figures is saved under
  [benchmarks/](benchmarks/README.md). The test suite covers the service, and it holds both
  pages and these documents to the code. It needs no key and no network: see
  [Testing strategy](#testing-strategy).

> **New here?** Start with **[docs/PYTHON_LEARNING.md](docs/PYTHON_LEARNING.md)** if you are
> learning Python: it explains the actual code with runnable examples and exercises.
> **[docs/LEARNING.md](docs/LEARNING.md)** explains the Applied AI concepts and why the code is
> shaped this way. **[docs/USAGE.md](docs/USAGE.md)** covers installing, calling,
> configuring and troubleshooting the service.

| Position | Default model                            | Provider | Why this position                                                    |
|----------|------------------------------------------|----------|----------------------------------------------------------------------|
| Primary  | `nvidia/nemotron-3-super-120b-a12b:free` | Nvidia   | First under the ordering rule: fewest wrong answers, then fewest calls with no answer |
| Fallback | `liquid/lfm-2.5-2.6b:free`               | Liquid   | Called only when the primary's call fails. A different provider, so one outage does not stop both |

Only one model is called on a healthy request. The fallback runs only when the primary's call
fails. It replaces a failed call, never a wrong answer, which is why the first position goes to
the model that is wrong least often and not to the quickest one.

This default is a dated snapshot (7 October 2026), not a ranking. Free models are withdrawn
often, and mostly without notice. The measurements, the ordering rule and what happens when a
model disappears are under [Model selection](#model-selection-a-dated-snapshot). Free endpoints
may also keep what you send them: read [Data handling](#data-handling) before you send a real
invoice.

## Architecture

```text
                                 ┌──────────────────────────────────────────────────────────────┐
                                 │                     extraction-service                       │
                                 │                                                              │
  POST /api/v1/extract           │  ┌────────────┐    ┌────────────────┐    ┌────────────────┐  │
  {"text": "..."}  ─────────────►│  │  FastAPI   │───►│ ExtractRequest │───►│  Extraction    │  │
                                 │  │  main.py   │    │  (Pydantic v2) │    │  Dispatcher    │  │
  ◄──────────────────────────────│  │            │◄───│ ExtractResponse│◄───│  extractors.py │  │
  {"success": true,              │  └────────────┘    └────────────────┘    └───────┬────────┘  │
   "data": {...InvoiceData},     │        │                                         │           │
   "model_used": "...",          │        │ 422 / 503 / 500                         │           │
   "fallback_triggered": bool}   │        ▼                                         ▼           │
                                 │  ┌────────────┐        ┌─────────────────────────────────┐   │
  GET /health                    │  │  Uniform   │    1st │ nemotron-3-super-120b-a12b:free │──┐│
  ──────────────────────────────►│  │  Error     │  ┌────►│ strict json_schema              │  ││
                                 │  │  Envelope  │  │     └─────────────────────────────────┘  ││
                                 │  └────────────┘  │  429 / 404 / in-band 503 / timeout /     ││
                                 │                  │  truncated / invalid JSON  ──► WARN      ││
                                 │                  │                                          ││
                                 │                  │ 2nd ┌─────────────────────────────────┐  ││
                                 │                  └────►│ lfm-2.5-2.6b:free               │──┤│
                                 │                        └─────────────────────────────────┘  ││
                                 │                        (one shared HTTP client, OpenRouter) ││
                                 │                                                             ││
                                 │        parse_invoice_json ──► InvoiceData.model_validate ◄──┘│
                                 │        (strip fences, JSON decode, normalise, recalc totals) │
                                 └──────────────────────────────────────────────────────────────┘
```

**Request flow**

1. `main.py` validates the body against `ExtractRequest` (10–50 000 chars). Failures → `422 validation_error`.
   A body of more than 1 000 000 bytes gets the same answer, without being read to the end.
2. Before any model is called, the request has to get past the service's one ceiling on live model
   calls (`app/budget.py`). A live call is one that really goes to a model. The request needs room
   for every model it may call. When the ceiling is reached, the request ends here with
   `503 providers_unavailable` and a `Retry-After` header, and no model is called.
3. `ExtractionDispatcher` walks the chain in order: the ids listed in `OPENROUTER_MODELS`, or,
   with that setting blank, the chain the service looks after. That is the two default models,
   until a repair changes it or the service takes a newer chain from the *published list*: the
   small file in which this project's scheduled test publishes the chain it tested. The first
   model that returns a validated, non-empty `InvoiceData` wins.
4. Each attempt runs under `asyncio.wait_for(TIMEOUT_SECONDS)`, translates SDK exceptions into
   the service taxonomy, and pipes raw JSON through `parse_invoice_json` → Pydantic.
5. `InvoiceData` validators normalise the record identically regardless of which model
   answered: line totals are recomputed (`total = quantity × unit_price`, to the cent), currency
   symbols are mapped to ISO‑4217, `"N/A"`-style placeholders collapse to `""`, money is rounded.
6. The response reports `model_used`, `execution_time_ms` (all attempts included) and
   `fallback_triggered`.
7. After a request that no model answered, or that a fallback answered, the route tells
   `app/model_chooser.py` which models did not give the answer, and returns. What follows happens
   in the background, so the caller never waits for it.

**Beside the request.** With `OPENROUTER_MODELS` blank, four more things happen. Only the last two
call a model, and they call free models only.

| When                                                  | What happens                                                             | Calls a model?                 |
|-------------------------------------------------------|--------------------------------------------------------------------------|--------------------------------|
| The service starts                                    | It reads the published list, then asks OpenRouter whether each model of the chain is still listed | No                             |
| A model of the chain did not answer a request         | The service looks into it, in the background: it reads the published list, then OpenRouter's own page for that model | No                             |
| A place in the chain has to be filled, or a visitor presses *Test the free models now* on the page | A test of the free models, with the seven sample documents | Yes: free requests, 42 at most |
| About once a week, on GitHub                          | The scheduled test, which publishes the chain it tested                  | Yes: free requests, 42 at most |

The first three are described under [When a model disappears](#when-a-model-disappears) and the
fourth under [The scheduled test](#the-scheduled-test).

### Repository layout

```text
Invoice-Extraction-Service/
├── app/
│   ├── __init__.py       # package version
│   ├── config.py         # pydantic-settings Settings, the ordered model chain
│   ├── schemas.py        # InvoiceItem, InvoiceData, ExtractRequest/Response, ErrorResponse, HealthResponse
│   ├── extractors.py     # BaseExtractor, OpenRouterExtractor, ExtractionDispatcher, exceptions
│   ├── main.py           # FastAPI app: CORS, request-id middleware, body size limit, error handlers, /health, /api/v1/extract
│   ├── budget.py         # the one ceiling on live model calls, for all callers together
│   ├── model_catalogue.py # asks OpenRouter whether a model is still listed, and which free models are on offer; it only reports
│   ├── model_chooser.py  # looks after the chain when OPENROUTER_MODELS is blank; the only module that changes it
│   ├── model_rules.py    # what a test of the free models concludes: a verdict per model, and the chain after it
│   ├── model_testing.py  # one test of the free models: sends the sample documents and records every call
│   ├── model_cache.py    # the chain the service chose: in memory, and in model_cache.json (written at run time, never committed)
│   ├── published_chain.py # the published list: its format and checks, and the command the scheduled job runs
│   └── playground/       # browser playground served at / (the page is optional, see "Playground")
│       ├── __init__.py   # exports install_playground (main.py calls it)
│       ├── routes.py     # the page, its config, the traced extraction stream, the model upkeep routes, the test-suite runner
│       ├── simulation.py # fault injection, per-attempt tracing and the counting of each real call, around the real dispatcher
│       ├── catalogue.py  # scenarios (document + failure plan + checks) and the benchmark snapshot
│       ├── benchmark.py  # command: measures free models on the live scenarios and saves the records
│       ├── suite.py      # runs pytest in a child process and groups the results
│       └── static/       # index.html, assets/ (app.css, app.js, theme.js, favicon.svg), no build step
├── benchmarks/           # saved benchmark records (<UTC date>/<time>.jsonl) and the notes behind other figures
├── docs/                 # USAGE.md, LEARNING.md, PYTHON_LEARNING.md, DEPLOY.md
├── site/                 # the project page, published on GitHub Pages apart from the service
│   ├── index.html        # the page; the address of the live demo is written in it once, and nowhere else
│   ├── assets/           # door.css, door.js, theme.js, favicon.svg: no build step
│   ├── recorded/         # the two runs the page shows, saved as JSON
│   └── og.png            # the picture shown beside the address when it is pasted somewhere
├── tools/                # og-card.html: the card that is photographed to make site/og.png (not published)
├── tests/
│   ├── __init__.py
│   ├── conftest.py       # environment isolation, applied before app import; every way out to the network closed
│   ├── test_extract.py   # the service: unit + integration tests (no network, client faked)
│   ├── test_playground.py # the playground, through the same fake
│   ├── test_model_catalogue.py # the catalogue check: what it concludes, and that it changes nothing
│   ├── test_benchmark.py # the benchmark command: grading, tables and guard rails (no model called)
│   ├── test_budget.py    # the ceiling: the minute, the day's share, who may spend, the refusal inside the API's contract
│   ├── test_body_limit.py # the size limit on request bodies
│   ├── test_playground_guard.py # who may use the page's POST routes
│   ├── test_doors.py     # the suite's own promise that no test reaches the network
│   ├── test_model_rules.py # verdicts and chains, as pure functions and on the saved records
│   ├── test_model_chooser.py # the service looking after its chain, with scripted models
│   ├── test_published_chain.py # the published list: the file, a service that reads it, the scheduled job's command
│   ├── test_models_page.py # the model upkeep part of the page, and the two routes behind it
│   ├── test_page_behaviour.py # the playground's script, run with Node.js on the service's own answers (skipped without Node.js)
│   ├── page_harness.mjs  # the Node.js script that test_page_behaviour.py starts: a made-up page, and a clock moved by hand
│   ├── test_logging.py   # what an extraction leaves in the log: the model and the time, not the vendor name or the invoice number
│   ├── test_front_door.py # the project page: every figure and sentence on it against the recordings, the snapshot and the code
│   └── test_docs.py      # the documents against the code, the two pages and the saved records
├── .github/
│   └── workflows/        # GitHub shows each one under the name in quotes
│       ├── model-check.yml # "Model check", daily: tests the free models when a test is due, and publishes the tested chain
│       ├── tests.yml     # "Tests": runs the test suite on every push to main and on every pull request
│       └── pages.yml     # "Project page": checks the project page against its sources, then publishes site/ on GitHub Pages
├── .env.example
├── .python-version       # 3.12: the Python the host and the GitHub jobs run
├── pytest.ini            # two settings of the test run; one makes a test that stands still say where
├── render.yaml           # Render Blueprint: one free web service, started as one process
├── requirements.txt      # exact versions, because the host installs this file again at every deploy
└── README.md
```

Two modules of the `playground` package are also used by the service itself, whether the page is
switched on or not: `catalogue.py` holds the seven sample documents that a test of the models
sends, and `benchmark.py` sends them and grades the answers.

### Where it runs

`render.yaml` describes the deployment this project is built for: one free web service on Render
that serves the API and the page, started as one process (`--workers 1`). Render's documentation
says what a free web service has to expect. It is stopped after 15 minutes without a request, and
the next request starts it again. That start takes about a minute. And the service comes back
with an empty memory and without the files it wrote. These are Render's own figures: nothing was
measured on Render for this README. Three parts of the design follow from them:

* **What has to outlast a sleep is kept elsewhere.** The count of today's free requests is
  OpenRouter's own, and the chain that was last tested is a small file on GitHub
  ([The scheduled test](#the-scheduled-test)).
* **Starting only reads.** Any request starts a sleeping service, a crawler's included, so nothing
  at startup calls a model.
* **The address to give to people is not the service's.** A visitor who opens a sleeping service
  and stays for half a minute would only see the host's loading page. So the address to pass on
  is the project page, <https://nickdesjardins.github.io/Invoice-Extraction-Service/>. It is
  one static page in the folder `site/`, served by GitHub Pages, so it is there at once. The
  playground itself also copes with a service that does not answer: it says so, waits, and then
  carries on by itself ([Playground](#playground)).

**The project page** shows two recorded runs and the figures of the snapshot, each with its date.
The service that runs on the host is called the live demo there. Once the live demo has an
address, the page starts waking it as soon as the page has loaded, and its one live line says how
far that is. Until then the page says "The live demo is not online yet." A page that is written by
hand goes stale, as this README once did. So `tests/test_front_door.py` compares every figure and
every sentence of the page with the recordings in `site/recorded/`, with the benchmark snapshot
and with the code. The workflow that publishes the page is `.github/workflows/pages.yml`, which
GitHub shows as "Project page". It runs that test first, and it publishes nothing when the test
fails.

[docs/DEPLOY.md](docs/DEPLOY.md) walks through putting both online: the project page on GitHub
Pages, and the service on Render.

## Setup

Requires **Python 3.11+**. The host and the GitHub jobs run 3.12, the version named in
`.python-version`.

```bash
git clone https://github.com/nickdesjardins/Invoice-Extraction-Service.git
cd Invoice-Extraction-Service
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# then open .env in an editor and write your key after OPENROUTER_API_KEY=
# (a key comes from https://openrouter.ai/keys)
# leave OPENROUTER_MODELS blank: the service then starts from the tested default chain
# and looks after it by itself

uvicorn app.main:app --reload --port 8000
# or: python -m app.main
```

`OPENROUTER_MODELS` is blank in `.env.example` on purpose. A blank value means "start from the
tested default in `app/config.py`, and look after the chain". Free models are withdrawn often, and
a chain named in `.env` stays frozen on the models of the day you copied it. With the setting
blank, the service does two things for itself:

* When it starts, it reads the chain that this project's scheduled test published. That is one
  small public file, read without a key. The service uses it when it is newer than what it has.
* When a request fails on a model that OpenRouter no longer offers, it takes that model out and
  tests the free models on offer to fill its place. That needs no update and no restart.

Naming models in `OPENROUTER_MODELS` fixes the chain: the service then runs exactly those models,
and it reads, tests and replaces nothing. The startup log says which of the two is in force, in a
line that begins `Model choice: automatic` or `Model choice: pinned`. *Pinned* is the log's word
for a chain that is fixed. One more case exists: the setting is blank, but no key is set, or
`OPENROUTER_BASE_URL` is not OpenRouter's own address. The line is then a `WARNING`. It says that
the service does not look after its chain, and which of the two is the reason. With a key set,
the log also says, for each model in the chain, whether OpenRouter still lists it. The rules are
under [When a model disappears](#when-a-model-disappears).

Then open <http://localhost:8000/>. The home page is a [playground](#playground) for running
extractions by hand and watching the fallback chain work. (`python -m app.main` listens on
`127.0.0.1` only; to serve other machines use `uvicorn app.main:app --host 0.0.0.0`, and open
it by host name, since `0.0.0.0` is a listen address and not something a browser can visit.)

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
| `OPENROUTER_API_KEY`            | *(unset)*                      | The only credential. Missing → `/health` reports `unavailable`, and the service tests and replaces no model |
| `OPENROUTER_MODELS`             | see the table at the top       | Ordered, comma-separated chain. First is primary, rest are fallbacks. Blank means: start from that default, and let the service look after the chain. Any value fixes the chain, even the default ids written out: nothing is then read, tested or replaced |
| `MODEL_PUBLISHED_URL`           | this project's `chain.json` on the branch `tested-models` | Where the service reads the chain that the scheduled test published: an `https` address. The request sends no key. Only a service that looks after its chain makes it (`OPENROUTER_MODELS` blank, a key set, OpenRouter's own base address), at startup and after a failed request. `null` means never read it, which keeps a deployment on its own tests (see [The scheduled test](#the-scheduled-test)) |
| `OPENROUTER_BASE_URL`           | `https://openrouter.ai/api/v1` | OpenAI-compatible endpoint. With any other address the service has no list of free models and no count of the day's free requests. It then replaces no model, and live calls are limited by the per-minute ceiling alone (see *The ceiling on live model calls* under [Security & operations](#security--operations)) |
| `OPENROUTER_STRICT_JSON_SCHEMA` | `true`                         | `false` falls back to plain JSON mode for models without structured outputs. Such a deployment does not use this project's published list, which was made with the strict format: it relies on its own tests |
| `MAX_OUTPUT_TOKENS`             | `2048`                         | Output cap. Providers reserve this against per-minute token budgets. A model's reasoning counts against it, so long invoices need more (see [Latency vs cost trade-offs](#latency-vs-cost-trade-offs)) |
| `TIMEOUT_SECONDS`               | `30`                           | Per-model budget. Worst case ≈ this × chain length: 60 s with the two default models, 90 s when the chain has three. Half of it is also the line between "Passed" and "Right, but slow" in a test of the models |
| `SERVICE_NAME`                  | `invoice-extraction-service`   | Name reported by `/health`, shown at the foot of the page and written in the startup log line |
| `ENVIRONMENT`                   | `development`                  | Reported by `/health`; enables reload via `python -m app.main`; `production` hides `/docs`, switches the page's test-suite route off and makes the page's `POST` routes refuse callers that are not a browser |
| `LOG_LEVEL`                     | `INFO`                         | Python logging level. `DEBUG` also writes the vendor name and the invoice number of each extraction to the log, and the figures of a line whose total was corrected, so keep `INFO` wherever other people's documents arrive |
| `CORS_ALLOW_ORIGINS`            | `*`                            | Comma-separated allow-list                                          |
| `PLAYGROUND_ENABLED`            | `true`                         | Serves the browser playground at `/` and its `/playground/*` routes. `false` leaves a pure API. The service still looks after its chain then, but nobody can watch it or start a test from the page |
| `MODEL_CATALOGUE_CHECK`         | `true`                         | At startup, when a key is set, asks OpenRouter's public catalogue whether each configured model is still listed, and logs the answer. The lookup sends no key and changes nothing by itself. `false` starts without any request to the network: neither that lookup nor the reading of the published list, for example when working offline |
| `OPENROUTER_SITE_URL`           | *(unset)*                      | `HTTP-Referer` header for OpenRouter attribution                    |
| `OPENROUTER_APP_NAME`           | `invoice-extraction-service`   | `X-Title` header for OpenRouter attribution                         |

The other numbers of the service are not settings. Each is a named constant in the code with its
reason beside it: the three places of a chain, the 15 minutes between two tests, the ceiling of 15
live calls a minute. To change one, change the constant.

**Changing the chain** needs no code change. The ids below are placeholders:

```bash
# a single model: no fallback, and the lowest worst-case latency
OPENROUTER_MODELS=vendor/model-a:free
# two models: the second is called only when the first one's call fails
OPENROUTER_MODELS=vendor/model-a:free,vendor/model-b:free
# a paid model in last position: an id without ":free" can cost money
OPENROUTER_MODELS=vendor/model-a:free,vendor/model-b:free,vendor/model-c
```

A chain that you name is fixed: the service calls exactly those models, in that order, and no
longer looks after the chain. Duplicates are removed while order is preserved. A blank value is
not an error: it means the tested default, looked after by the service. A value with no id in it,
such as a lone comma, is rejected at startup. The third line shows the syntax only, because no paid
model was tested with this service. A chain that the service looks after never holds a paid model:
every id it chooses for itself has to end in `:free`.

Put the model that is wrong least often first, not the quickest one: the ordering rule and its
reason are under [Model selection](#model-selection-a-dated-snapshot). And measure a model
before you rely on it. On 7 October 2026 one free model that was listed as supporting structured
output rejected this service's strict schema on every call.

## Model selection: a dated snapshot

The default chain is a snapshot taken on **7 October 2026**. It is not a ranking, and it will not
stay true, because free models come and go. This project's first default chain was built around
the free variants of `nex-agi/nex-n2.5-mini` and `nex-agi/nex-n2.5-pro`. OpenRouter listed those
two as free for 17 days, from 8 to 25 September 2026, and then withdrew them; this project's
first commit was on 21 September, four days before the end. So this section records what was
measured and on which day, the rule that puts the models in their order, and what happens when
one of today's models goes the same way.

The default chain is still a choice that a person made by hand, from measurements, on a stated
day. The service can repair that chain. It never ranks it again.

### Snapshot

<!-- snapshot:start -->
Measured on **7 October 2026**, 17:54 to 19:02 UTC: 3 passes of 7 documents per model, with a time limit of 30 s per call.

| Model                                    | Calls | No answer | Fully correct | Median answer | Slowest answer | Note                                                       |
|------------------------------------------|-------|-----------|---------------|---------------|----------------|------------------------------------------------------------|
| `nvidia/nemotron-3-super-120b-a12b:free` | 21    | 0         | 21 of 21      | 7.1 s         | 14.4 s         |                                                            |
| `liquid/lfm-2.5-2.6b:free`               | 21    | 2         | 16 of 19      | 2.8 s         | 9.8 s          | Wrong tax on the service invoice in every pass             |
| `dots-studio/dots-3-note-preview:free`   | 21    | 1         | 20 of 20      | 21.6 s        | 27.5 s         | Usual answer takes most of the time limit; ends 2026-12-31 |
| `apodex/apodex-1.1-mini:free`            | 21    | 21        | none          | none          | none           | Rejects the strict response format                         |

Saved records: [`benchmarks/2026-10-07/175417Z.jsonl`](benchmarks/2026-10-07/175417Z.jsonl).
<!-- snapshot:end -->

### The default chain

| Position | Model                                    | Provider | Why this position                                              |
|----------|------------------------------------------|----------|----------------------------------------------------------------|
| Primary  | `nvidia/nemotron-3-super-120b-a12b:free` | Nvidia   | First under the ordering rule below: in the snapshot it gave no wrong answer, and every call answered |
| Fallback | `liquid/lfm-2.5-2.6b:free`               | Liquid   | The other model left after step 1 of the rule. Served by a different provider, so an outage or a rate limit at one provider does not stop both |

The figures come from one command, `python -m app.playground.benchmark`. It sends the
playground's seven live cases (six documents, plus one meeting email that must be refused) to
each model on its own, with no fallback, through the service's own prompt, schema, parsing and
validation. A result is therefore what the service would get from that model alone. For each
model the command keeps two things apart:

* **No answer.** The call ended without one: a rate limit, a timeout, a rejected response format
  or cut-off output. A fallback can cover these.
* **Fully correct.** Of the answers that were given, how many had every checked field right, or
  refused the meeting email. Any other answer is a **wrong answer**. A fallback cannot cover a
  wrong invoice: the service sees a valid response and returns it. The one wrong answer the
  chain does re-check is the refusal of a real invoice, because a single "not an invoice"
  verdict sends the document to the next model when there is one.

Every call behind the figures is saved under `benchmarks/`, and the command's `--report` option
prints the tables again from those files, so the snapshot can be checked without calling a model.

**The ordering rule**

1. Leave out a model that rejects the response format, or whose usual answer takes more than
   half of the time limit. Such a model has no margin: a longer document or a slow moment turns
   its answer into a timeout.
2. Of the rest, the first position goes to the model with the fewest wrong answers.
3. Between models with equally few wrong answers, it goes to the one with the fewest calls with
   no answer.
4. Speed only breaks ties.

The reason is in how the chain works: it moves on only when a call *fails*. A fallback therefore
replaces a failed call, never a wrong answer. A wrong answer from the primary is a valid response
as far as the service can tell, so it goes back to the caller with `fallback_triggered: false`,
and the fallback never sees the document. The first position is for being right. The second
position is for the calls that fail.

The rule is also code. `rank()` in `app/playground/benchmark.py` is the four steps as one
function. The test suite applies it to the snapshot's figures, and the result has to be the
default chain.
The service uses the same function when it fills a free place in its chain (see
[When a model disappears](#when-a-model-disappears)). It never uses it to change the order of
models that are already in the chain.

Step 1 on 7 October 2026: four free models advertised structured output, and two were left out.
`apodex/apodex-1.1-mini:free` rejected this service's strict schema on every call.
`dots-studio/dots-3-note-preview:free` gave no wrong answer, but its usual answer took about 22
of the 30 seconds allowed, and OpenRouter has announced its end for 2026-12-31. The other two are
the default chain. OpenRouter's "any free model" router, `openrouter/free`, was tried and left
out as well: it picks a free model at random, and it sent this service's requests to models that
could not answer them.

Steps 2 to 4 on the two that were left: `nvidia/nemotron-3-super-120b-a12b:free` gave no wrong
answer, and `liquid/lfm-2.5-2.6b:free` gave the wrong tax for the service invoice in all three
passes. That email says no tax applies, so the right value is `0`; in a trial run earlier the
same day (`run2.jsonl`) the model returned no tax at all. Step 2 therefore settled the order.
Know what it means for the fallback: when the primary's call fails, the answer comes from a model
with a known miss. The trial runs, and what else rests on them, are described in
[benchmarks/README.md](benchmarks/README.md).

### What this table cannot tell you

* **The snapshot shows whether a model can do the job. It does not measure accuracy.** Seven
  cases are enough to catch a model that rejects the schema, times out or misreads a common
  layout. They are far too few to say how often a model is right, and they cannot rank two
  models that both pass.
* **The cases are not independent of the prompt.** The prompt was written against documents of
  these kinds, and two of its rules quote them: rule 3 names the `RCPT#` label of the till
  receipt, and rule 4 uses the German invoice's date, `07.05.2024`, as its example.
* **It is one day of measurement.** Free-model rate limiting swings within minutes, and a model
  can be changed or withdrawn the day after.

So before you rely on the chain for your own kind of document, try documents of that kind. The
playground's *Your own document* entry sends any pasted text through the chain. Use redacted
copies for that, not real invoices: see [Data handling](#data-handling).

### Free models come and go

These figures were measured on 7 October 2026 from archived copies of OpenRouter's public model
list in the Internet Archive. The two tables behind them are kept with the benchmark records
(see [benchmarks/README.md](benchmarks/README.md)). Of the ids ending in `:free` that were listed
30 days earlier, 83% were still listed; of those listed 61 days earlier, 64%. For a chain of two
models that is roughly a 31% chance of losing at least one within 30 days and 59% within 61 days,
if both behave like the average free id and independently of each other. Most withdrawals showed
no end date first: since January 2026, 38 of the 63 free ids that were withdrawn never showed one
in an archived copy. For the 25 that did, the notice was days, not weeks: at the median the date
first appears in a copy 3.3 days before the stated end, and the last copy without it is 12.2 days
before. The real figure lies between the two, because the copies are up to 19 days apart. The
pool is also shrinking: 71 ids ended in `:free` in May 2025, and 16 did on 7 October 2026.

### When a model disappears

What happens depends on one setting. With `OPENROUTER_MODELS` blank, the service repairs its chain
itself. With models named there, the chain is fixed, and replacing a model is a job for a person.

**With `OPENROUTER_MODELS` blank, the service repairs the chain.** It needs a key for that, and
OpenRouter's own address as its base URL. The code is in `app/model_chooser.py`. What a test may
conclude is written as pure functions in `app/model_rules.py`. These are the rules:

* **Nothing is tested while requests succeed.** A chain that works costs no free request of its
  own, and starting the service calls no model.
* **A call that failed is looked into, at no cost.** This *look* happens after a request that no
  model answered, and after a request that a fallback answered. It runs in the background, so the
  caller does not wait for it. It calls no model, and it happens once a minute at most. First the
  service asks for the published list again. It leaves that out when it asked less than five
  minutes ago and the address has answered since the service started. When a newer tested chain
  is waiting there, the service takes it. Then the service asks OpenRouter's own page about each
  model that did not answer. It leaves out a model that the chain no longer holds, which can
  happen when a published chain has just been taken.
* **A model leaves only when two sources agree.** The model gave no answer here, and OpenRouter's
  own page for that model says "Model not found". The model is then taken out of the chain at
  once, as long as another model is left. A rate limit, a timeout or an upstream error on a model
  that OpenRouter still lists changes nothing, and nothing is tested because of it. A page that
  cannot be read within five seconds decides nothing either: the next failed request asks again.
* **A free place is filled by a test.** The service asks OpenRouter for the free models that say
  they support structured output. It tests those that are not in the chain, six at most. Models
  that had no room in one test go first in the next. Each one gets the benchmark's seven sample
  documents, one after the other, through the service's own prompt, schema, parsing and
  validation. A model's test ends after two calls that were not fully right, because the model
  can no longer pass then. Every call is one free request, so a test costs 42 at the very most.
  With the list of 7 October 2026 it costs 9: two models were listed outside the default chain,
  and the test of one of them ends after two calls, because that model rejects the response
  format.
* **Repair, never rank.** The page says it in its own words: "Repaired, never reshuffled". A chain
  has three places: two, like the tested default, and one spare. A test fills free places
  behind the models that are already in the chain. The models that passed come first, in the
  order that the ordering rule gives. Then at most one model that is right but slow is added
  behind them, if a place is still free. A model that is already in the chain keeps its place
  and its order, whatever one test says about it, and a chain is never emptied. One exception
  puts a newcomer in front, and it ranks nothing. A model of the tested default that was
  withdrawn, comes back and passes a test goes back to the place that a person measured it for:
  behind a default model that stands before it, and in front of every other model.
* **A stand-in answers meanwhile.** A stand-in is a model that is put to use before a test has
  decided about it. In a repair, the first document goes to every model under test at once,
  because somebody may be waiting for an answer. A model whose first answer is right is put to
  use straight away, whichever document that answer was for, as long as a place is free. It
  answers behind the models of the chain that OpenRouter still offers. It is kept in memory only
  and never saved, so a restart does not find it. The test of all seven documents has the last
  word. A stand-in that fails the test stops answering at once. A stand-in that the test gives a
  place stays for good. And a stand-in that the test could not judge, because calls went
  unanswered, goes on answering until a later test decides.
* **Tests are bounded.** One test runs at a time. The next one may start 15 minutes after the
  last one ended, whoever starts it. A test starts only when OpenRouter's list could be read. And
  a repair does not start when the day's count shows fewer free requests left than its test may
  send.
* **A repair that could not finish stays owed.** Perhaps no test could start, or no tested model
  passed, or a stand-in is answering that no test has judged. The service then takes the repair
  up again after the next request that a model of the chain does not answer. No timer does that:
  a service that gets no requests repairs nothing.
* **Only free models, always.** Every id the service chooses for itself has to have the form
  `vendor/model:free`. That is checked in six places, wherever an id comes in or goes out: in
  OpenRouter's list, where both prices also have to be zero, in the saved file, in the published
  list, in the job that publishes it, before a test sends a model its first document, and once
  more at the moment a chain is put to use.
* **Listed is not the same as callable.** An account's privacy settings can close a listed model
  to that account (see [Data handling](#data-handling)). The service goes by this sign: every
  model of the chain refused a request with a 403 or a 404, and OpenRouter still lists all of
  them. Nothing is taken out then. If the chain has a free place, a test looks for a model to
  put beside them. If it has none, the log says so in an `ERROR` line.
* **The choice is kept in memory.** `model_cache.json`, in the project folder, is only how the
  choice survives a restart on a machine that keeps its disk. It is never committed, and it is
  ignored when it was written under another default chain. Delete it and restart to go back to
  the tested default. On a host that forgets its files, the service starts again from the
  published list, or from the tested default.

A test gives each model one of four verdicts. The words are the ones the page shows:

| Verdict         | When                                                                                   | What follows                                    |
|-----------------|----------------------------------------------------------------------------------------|-------------------------------------------------|
| Passed          | All seven documents were sent, and at most one was missed (a wrong answer, or no answer). The prompt-injection attempt was answered right. The usual (median) answer took at most half of `TIMEOUT_SECONDS` | The model may take a free place |
| Right, but slow | The same, but the usual answer took longer                                              | It may be added behind the others. One such model at most |
| Failed          | Two answers were wrong or unusable, or the answer to the prompt-injection attempt was wrong | No place. A repair does not test the model again while this process runs |
| Not judged      | Calls went unanswered: a rate limit, a timeout, an upstream error                       | Nothing. That says something about the moment, not about the model |

One document has to be answered right: the prompt-injection attempt, the sample that carries hidden
instructions. A wrong tax amount is a mistake, and doing what a document says is a hole. A wrong
answer to any other document is a miss like any other, and one miss is allowed. That holds for the
text that is not an invoice as well.

Why "repair" and not "rank": seven documents can rule a model out, but they cannot rank two models
that both work, and free models are rate limited in bursts. In the third saved pass of 7 October
2026 the tested fallback was right on only 4 of 7 documents, and two of its three misses were
calls that were rate limited. The test suite holds the rules to those records: judged alone, each
of the three saved passes leaves the two default models in their places.

**What the service does not do.**

* It never changes the order of two models that both work.
* It never takes a model out for answering wrongly. A model of the chain that fails a test keeps
  its place, and the test shows it as "Failed".
* It does not wait for a full test before it uses a stand-in: a stand-in answers real requests
  after it has read one sample document correctly.
* It does not measure accuracy. Seven documents can rule a model out. They cannot show how often
  a model is right.

Changing the order, or replacing a model that answers wrongly, stays a decision for a person,
made by measuring: steps 2 to 5 below.

**Where to see it.** Each change of the chain while the service runs is a `WARNING` line in the
log: `Model chain in use is now [...] (it was [...])`. `GET /health` lists the chain in use. The
page shows the rest in its section *Model upkeep*: where the chain comes from, a test while it
runs, and what the last test found (see [Playground](#playground)). The page is written for
visitors, so it names no setting. What the admin needs is in the log. When no model of the chain
works any more and no replacement was found, an `ERROR` line of `app.model_chooser` says what is
wrong, when the service tries again by itself, and what a person can do.

**With models named in `OPENROUTER_MODELS`, none of that happens.** The chain is fixed. A
withdrawn model is still tried whenever its turn comes: a withdrawn primary is tried on every
request, fails with a 404 in a fraction of a second, and the answer still says
`fallback_triggered: true`, until the id is taken out of the chain. Emptying the setting and
restarting hands the chain back to the service. Replacing the model by hand takes the five steps
below. Steps 2 to 5 are also the way to change the tested default, or the order of two models
that both work.

1. **The check tells you**, at the next start, and sometimes a few days before.
   `app/model_catalogue.py` asks OpenRouter's public catalogue, with no API key, whether each
   configured model is still listed. At startup the service logs one `ERROR` line for each model
   of a fixed chain that is not listed. The line says what to do: put a working id in
   `OPENROUTER_MODELS`, or empty that line and restart. An announced end date within 14 days is
   a `WARNING`. `python -m app.model_catalogue` asks the same question on demand and prints one
   line per model:

   ```text
   vendor/model-a:free is not listed by OpenRouter (withdrawn, or the id is mistyped). Replace it in OPENROUTER_MODELS, or in DEFAULT_MODEL_CHAIN in app/config.py.
   vendor/model-b:free is listed.
   ```

   Its exit code is 0 only when every model is listed and none has an end date within 14 days,
   and 1 otherwise, including when the catalogue could not be read. It checks the chain the
   service would use, and its first line says which chain that is when it is not the built-in
   default: the one named in `OPENROUTER_MODELS`, or one that a service on this machine saved in
   `model_cache.json`. The playground shows a chip on the model. The check only reports: it
   takes no model out.
2. **List the candidates**: the free models that advertise structured output. OpenRouter's
   public model list shows them, and [docs/USAGE.md](docs/USAGE.md), section 9, has the command.
   "Listed" does not mean "works": on 7 October 2026 `apodex/apodex-1.1-mini:free` was listed as
   supporting structured output and rejected this service's strict schema on every call.
3. **Measure them** with the benchmark command, in several passes, because free-model rate
   limiting swings within minutes:

   ```bash
   python -m app.playground.benchmark MODEL [MODEL ...] [--passes N] [--gap-minutes M]
   python -m app.playground.benchmark --report FILE [FILE ...]
   ```

   Spread the passes out, for example `--passes 3 --gap-minutes 30`. A pass sends 7 requests per
   model, and they count against the daily allowance. The command calls free models only (an id
   that does not end in `:free` is refused), refuses a run of more than 100 requests and starts
   requests 3.5 s apart. It saves each call as one JSON line under
   `benchmarks/<UTC date>/<time>.jsonl`, with the sanitised error message only, and `--report`
   prints the tables again from saved files without calling anything. For each model it reports
   the calls, the calls with no answer and their error types, how many of the answers were fully
   correct, the median and the slowest answer time, a grid per case (fully correct / answered /
   attempted) and the checks that failed. It proposes no chain and edits no file: applying the
   ordering rule is left to you.
4. **Change `DEFAULT_MODEL_CHAIN`** in `app/config.py`, in the order the rule gives. A chain that
   a service saved under the old default (`model_cache.json`) is then ignored, and so is a list
   that was published under it. When the change is pushed to `main`, the scheduled job starts,
   finds that the published list was made under the old default, and tests again (see
   [The scheduled test](#the-scheduled-test)). To change one deployment only, set
   `OPENROUTER_MODELS` instead. That fixes the chain of that deployment, and the scheduled test
   neither sees nor serves that value.
5. **Update the tables together**: the snapshot and the chain table in this section, the table
   at the top of this README, and the page's copy of the snapshot in
   `app/playground/catalogue.py`. `tests/test_docs.py` compares them: a figure that was forgotten,
   a chain table that names the wrong model in a position, or a withdrawn model still written
   with its `:free` suffix fails the suite. When the snapshot in this README is out of date, the
   failure prints the block to paste. Two more things follow the snapshot. Its day and its time
   window also say when the new default counts as tested (see
   [The scheduled test](#the-scheduled-test)). And the project page in `site/index.html` shows
   the same figures, which `tests/test_front_door.py` compares with the snapshot.

**History (September 2026).** The first default chain had three models: the two withdrawn ones
named at the top of this section, with `liquid/lfm-2.5-2.6b:free` between them. It came out of a
benchmark of six documents, scored one field check at a time. That benchmark cannot be repeated,
because its scripts and documents were not kept, and its scores are not given here: they cannot
be compared with the snapshot above, which uses different documents and counts whole answers.
What it left in the code is still asserted by the tests: three prompt rules (strip labels such
as `RCPT#`, read day-first dates correctly, use `null` rather than `0` when tax is simply
absent) and the recovery of a JSON object from a reasoning preamble.

## The scheduled test

A service that nobody uses tests nothing. On a free host it also forgets what it knew, at every
sleep. After a month of silence its models may all be gone, and the first visitor would be the one
to find out. The scheduled test is there to spare that visitor. About once a week, a job on GitHub
runs the service's own test of the free models and publishes the chain that results. A service
reads that one small file when it starts, so that a service which slept for weeks wakes up on
models that work.

It serves the services that leave `OPENROUTER_MODELS` blank. A chain that you name yourself is
neither tested nor replaced by it.

**What the job does.** The workflow is `.github/workflows/model-check.yml`, which GitHub shows as
"Model check". It has three small jobs, and the first of them runs one command,
`python -m app.published_chain`:

| Job, and its name in a run                          | What it does                                                        | What it holds                         |
|-----------------------------------------------------|---------------------------------------------------------------------|---------------------------------------|
| `test`: "Test the free models, if a test is due"    | Decides whether a test is due, which costs nothing and needs no key. When one is due, it runs the test with the service's own code and rules, on the same seven sample documents | The OpenRouter key, in one step. It can only read the repository |
| `publish`: "Publish the tested chain"               | Looks at the file once more (small, dated, and a chain of one to three different free ids), then commits it as `chain.json` on the branch `tested-models`. It runs for the `main` branch only | The right to write to the repository. It runs only git and a few lines of standard-library Python |
| `verdict`: "Is a tested chain published?"           | Turns the run red when the owner has something to do                | Nothing                               |

Testing and publishing are two jobs on purpose. The job that holds the key and runs downloaded
packages cannot write to the repository, and the job that can write runs no downloaded code.

**When it tests.** The workflow starts every day at 06:17 UTC, when someone starts it by hand, and
on a push to `main` that changes a file the test is made of (the workflow file lists them).
Starting is not testing. A test spends free requests, so one is due only for a reason:

* nothing usable is published: there is no file yet, or the file fails one of the checks below;
* the last test is six and a half days old, so that the daily run of the seventh day is the one
  that tests;
* the last test could confirm nothing, and 20 hours have passed;
* OpenRouter's list of free models has changed in a way that a test could act on;
* someone started the workflow by hand and ticked "Test the free models now, even if no test is
  due".

A test can do two things to a chain: take out a model that is gone, and fill a free place. So a
change of OpenRouter's list counts in two cases only. A model of the published chain is no longer
on the list. Or a model that the last test did not see is on the list while the chain still has
a free place. A model that comes and goes outside a full chain changes nothing that a test could
change, so it costs no test.

**What it tests, and what it publishes.** The test starts from the published chain, or from the
built-in default when nothing usable is published. It tests that chain and the other free models
that OpenRouter lists, six models at most, by the rules above: repair, never rank. So a chain
with a free place can gain a model, and the published chain can be longer than the default chain.
Judged by these rules, two of the three saved passes of 7 October 2026 give a chain of three
models: the two default ones, and behind them `dots-studio/dots-3-note-preview:free`, which was
right but slow. Models that had no room in one test go first in the next. It does not test when
its requests would leave less than a fifth of the day's free requests. It publishes a chain only
when at least one model of that chain passed in that very test, or was right but slow. Otherwise
the published chain and its date stay as they were, and the file only notes that a test ran, so
that the same test is not repeated every day. Only what the `main` branch tested is published. A
run that somebody starts by hand on another branch tests that branch's code, which is not the
code that a deployment of `main` runs, so its result is not published.

**Where it publishes, and how a service reads it.** The file is `chain.json` on the branch
`tested-models` of this repository, a branch with no history in common with the code. It is not
`main` on purpose: a host that deploys on every push to `main` would restart the service at every
publication. A service reads the file at this address:

```text
https://raw.githubusercontent.com/nickdesjardins/Invoice-Extraction-Service/tested-models/chain.json
```

That is `DEFAULT_PUBLISHED_URL` in `app/config.py`. The setting `MODEL_PUBLISHED_URL` points a
deployment to another address, or to none (`null`). The request sends no key and calls no model. A
service makes it once when it starts, and waits 8 seconds at most. It makes it again after a
failed request, and then waits 3 seconds at most.

The file is a claim like any other, so nothing in it is used before it is checked, and it is used
whole or not at all:

* It is at most 64 000 bytes, it is a JSON object of version 1, and its date is not in the future.
* Its chain is one to three different ids that all end in `:free`. So no file can make a service
  call a model that costs money.
* It was made by the very same test: the same prompt, schema, sample documents with their checks,
  default chain and response format, and the same three constants of the rules (how many places
  a chain has, how many documents a model may miss, and which document has to be answered
  right). The file carries a short fingerprint of all that, and a service compares it with its
  own. A fork that rewrites the prompt therefore ignores this project's file by itself.
* Its test is newer than the test behind the chain in use. For the built-in default, that test is
  the measuring that a person did, and it counts as over in its last minute: 19:02 UTC on
  7 October 2026. So a list that the job tests later on that same day is newer, and is taken.

A list is never taken while a test is running. After a failed request, a list that names the very
chain that has just failed is not taken either. A chain that was taken from the list is a chain
like any other afterwards: a model in it leaves only when the two sources agree. And no file,
missing or bad, can stop a service from starting. The service logs one line and goes on with the
chain it has.

**What a red run means.** A run is red when the owner has something to do. The job that turns red
is the third one, "Is a tested chain published?". The reason is in the first one: open the job
"Test the free models, if a test is due" and read the end of its step "Decide, test, and write
the file to publish". A line that begins `Failing this run on purpose:` says which of these five
reasons it is:

* OpenRouter refused the key.
* A test was due and could not confirm a chain, while no chain younger than three weeks is
  published. Either the test ran and no model of the chain passed, or it could not run:
  OpenRouter's list could not be read, or too few free requests were left for the day. Services
  are then on their own, and each repairs its chain when it has to.
* The job has no key, a test is due, and either a model of the built-in default chain is no
  longer listed or the published list is three weeks old.
* OpenRouter no longer offers any model of the chain, and no other free model passed the test.
  Services that read the list then have no model that works. The job tests again the next day.
* A chain was published, and a model of it failed the test, which means that its verdict was
  "Failed". One wrong answer is not enough for that. The chain is published all the same,
  because a test never changes the order of a chain. Only a person can: by measuring again and
  changing `DEFAULT_MODEL_CHAIN`.

A red run without such a line means that the job itself broke: an earlier step failed, or the
command ended with a Python traceback. GitHub sends an email about a failed scheduled run to the
person who last changed the schedule in the workflow file, if that person's notifications for
Actions are switched on ([docs/DEPLOY.md](docs/DEPLOY.md#3-switch-githubs-emails-for-failed-runs-on)
says where). A red "Model check" is how this project tells its owner that it needs a person.

**The 60-day pause.** GitHub pauses scheduled workflows in a public repository after 60 days
without activity in the repository. It does not say whether the job's own commits on
`tested-models` count, so assume that they do not. While the workflow is paused, nothing new is
published. A service notices by the age of the list: from 14 days on, its log says how old the
list is and how to enable the workflow, and the page says the age beside the chain. Services go on
repairing their chains themselves meanwhile. To enable the workflow again, open the repository on
GitHub, choose the tab *Actions*, choose "Model check" in the list on the left, and press "Enable
workflow". The command `gh workflow enable model-check.yml` does the same. A push does not start
a workflow that is paused.

**What it costs.** No money. GitHub Actions is free for a public repository on GitHub's standard
runners, which is what this workflow uses. The test spends free requests of the OpenRouter account
whose key is the secret: none on a day when no test is due, about 23 for a test of the four models
that were listed on 7 October 2026, and 42 at the very most. One test a week is about 100 free
requests a month. They come out of the same daily allowance as everything else that account does
with free models.

**Adding the secret.** The job needs one repository secret, `OPENROUTER_API_KEY`. On GitHub, open
the repository, then *Settings*, *Secrets and variables*, *Actions*, and press "New repository
secret". The name is `OPENROUTER_API_KEY`, and the value is an OpenRouter key. A key made only for
this job can be withdrawn without touching the key of the service. Only one step of the `test` job
sees the secret.

Without the secret, the job still says what is due, in a yellow warning on the run. It tests
nothing and publishes nothing, and the run stays green unless one of the reasons above applies.
Services then find nothing at the address: each logs one line when it starts and uses the tested
default.

In a fork, the job warns when the repository it publishes to is not the one that services read.
Change `DEFAULT_PUBLISHED_URL` in `app/config.py` then, or set `MODEL_PUBLISHED_URL`.

The same command can be run by hand. When a test is due it spends free requests of the key in your
environment:

```bash
python -m app.published_chain --previous FILE --out FILE [--force]
```

`--previous` is the file that was published last time, and it may be missing. `--out` is where the
new file is written, and nothing is written unless a test ran. `--force` tests even when no test
is due.

## Data handling

The default models cost no money. Their providers may keep what you send them. On 7 October
2026:

* OpenRouter's provider table listed both default providers, Nvidia and Liquid, as keeping
  prompts and as possibly training on them.
* NVIDIA's API trial terms allowed testing and evaluation only, with no confidential or personal
  data.

So send this chain the sample documents or redacted ones, not real invoices. Every scenario on
the playground is a sample document. A real invoice carries names, addresses, prices and often
bank details.

**The chain can change, and the providers with it.** With `OPENROUTER_MODELS` blank, a model can
join the chain in two ways: the service adds it after a test of its own, or the published list
names it. The published list reaches every deployment that leaves `MODEL_PUBLISHED_URL` at its
default. Either way, the newcomer is a free model that OpenRouter lists, and nobody looked up what
its provider does with prompts. So the advice above holds for whatever free model is in the chain,
not only for the two default ones. Two settings keep the choice in your own hands:

* `MODEL_PUBLISHED_URL=null` keeps a deployment on its own tests: no published list is read.
* Naming models in `OPENROUTER_MODELS` fixes the chain: no model joins it or leaves it by itself.

A test of the models sends the seven sample documents and nothing else, so no document of a caller
goes to a model that is only being tested. A stand-in is different: once it is put to use, it
answers real requests.

OpenRouter's FAQ says that providers which log prompts or train on them are routed to only when
the account's privacy settings allow it (Settings, Privacy). With those settings off, a free
model can therefore answer 404 although it is listed. If `python -m app.model_catalogue` says a
model is listed and every call to it still fails with "was not found (404)", look at those
settings before you replace the model. A service that looks after its own chain reads that case
the same way: it takes no model out, and its log names the privacy settings.

The service itself stores no document. The file it may write, `model_cache.json`, and the
published `chain.json` hold model ids, dates, timings and verdicts, and no text of any document.

Its log keeps what a document said out as well, at the usual level. For a successful extraction
the log says which model answered and how long that took. It does not say the vendor name or the
invoice number: those two are written at `DEBUG` only, for a developer who sets `LOG_LEVEL=DEBUG`
on a machine of their own. At `DEBUG` the log also holds the figures of a line whose total the
service corrected. One kind of line can still quote a document at the usual level: when a call
fails, the log holds the upstream error text, and that text can repeat part of what was sent.

These are notes of what was published on one day, not legal advice, and they can change. For
the current state read OpenRouter's documentation on provider logging, and each provider's own
terms.

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
    {"name": "openrouter", "model": "nvidia/nemotron-3-super-120b-a12b:free", "role": "primary",  "configured": true},
    {"name": "openrouter", "model": "liquid/lfm-2.5-2.6b:free",               "role": "fallback", "configured": true}
  ],
  "active_providers": [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "liquid/lfm-2.5-2.6b:free"
  ]
}
```

`status` is `ok` when a key is configured and `unavailable` otherwise (HTTP 503, so orchestrators
can pull the pod). It is a **readiness** signal based on configuration, not a live upstream probe,
so it stays cheap and cannot itself be rate limited.

The other side of that choice: `/health` asks OpenRouter nothing, and says `ok` whenever a key is
set, whether or not the models in the chain still exist. The catalogue check does not change it
either. What it lists is the chain in use. With `OPENROUTER_MODELS` blank, that is the chain the
service looks after: the list changes, without a restart, when the service takes a withdrawn model
out or adds a tested one, and it can have up to three entries.

A withdrawn model does not show up as such in `/health`. It shows up in the log, as a chip on the
playground and, when it is the primary, as `fallback_triggered: true` in the responses. With a
fixed chain that goes on for every request, until someone takes the id out. With a chain the
service looks after, it lasts until the service has taken the model out. It does that after the
first request that fails on the model, once OpenRouter's own page confirms that the model is
gone. The log line at startup differs in the same way. For a fixed chain it is an `ERROR` with
the remedy. For a chain the service looks after it is a `WARNING`, which says that the service
looks for a replacement after the first request that fails.

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
  "model_used": "nvidia/nemotron-3-super-120b-a12b:free",
  "execution_time_ms": 6000.0,
  "fallback_triggered": false
}
```

This shows the shape of a response. The `execution_time_ms` above is an example value, not a
measurement (measured answer times are in the snapshot under
[Model selection](#model-selection-a-dated-snapshot)), and the wording of free-text fields such
as `vendor_name` can differ from one model to another.

Two things happen around a request that the response does not show. Before the first model is
called, the service asks its ceiling on live model calls for room. At most once a minute that
includes one request to OpenRouter for the day's count of free requests, which can hold a run up
for 4 seconds at most. And after a request in which a model of the chain did not answer, the
service looks into it in the background (see
[When a model disappears](#when-a-model-disappears)). The caller never waits for that look, and
the answer is what it would have been without it.

Every response carries `X-Request-ID` (echoed from the request when it matches
`[A-Za-z0-9._-]{1,128}`, otherwise generated) and `X-Process-Time-Ms`. The one exception is the
answer to a CORS preflight, the `OPTIONS` request that a browser sends by itself before a `POST`
from another site's page. The CORS layer gives that answer before the request reaches the layer
that adds the two headers.

#### Error contract

Every non-2xx body uses the `ErrorResponse` envelope `{success: false, error, message, detail}`.

| HTTP | `error`                 | When                                                                                  | Retry? |
|------|-------------------------|---------------------------------------------------------------------------------------|--------|
| 422  | `validation_error`      | Body fails `ExtractRequest` (e.g. `text` shorter than 10 chars), or is larger than 1 000 000 bytes, which is refused without being read to the end. `detail` lists issues | No     |
| 422  | `unparseable_text`      | Models ran but reported the text contains **no invoice** (garbage input)              | No     |
| 503  | `providers_unavailable` | Every model failed. `detail` lists each attempt with a `retryable` flag; `Retry-After: 5` only when at least one failure was transient | Sometimes |
| 503  | `providers_unavailable` | The ceiling on live model calls is reached, and no model was called. `detail` is one object whose `error_type` is `CallLimitError`. `Retry-After` is the real wait in seconds | Yes, after `Retry-After` |
| 4xx  | `http_error`            | Routing errors such as 404 / 405. Also a 400 for a body that cannot be read as JSON at all: bytes that are not UTF-8, or nesting that is too deep | No     |
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

**The two 503s.** The status and the `error` code are the same on purpose: the ceiling adds no
new status to the contract, and it answers in the usual envelope. `detail` tells the two apart.
When every model failed it is a list with one entry per attempt. When the ceiling refused the run
it is one object, and the `message` is a sentence written for whoever is waiting:

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

`Retry-After` says how long to wait. It is 1 to 60 seconds when the last minute is full. It is 60
seconds when the service cannot read the day's count from OpenRouter and has used the few calls
it allows itself without one. And when the part of the day's free requests that is open to
callers is used up, it is the number of seconds until midnight UTC. So a client should not simply
sleep for whatever the header says. The ceiling itself is described under
[Security & operations](#security--operations).

**When a model has been withdrawn**, a caller of a service that looks after its chain sees one of
two things:

* The first model is gone and a later one answers: `200` with `fallback_triggered: true`. The
  service then takes the model out, usually within seconds. After that the model is no longer
  called, and the answers say `fallback_triggered: false` again.
* Every model of the chain is gone: `503`, with one `detail` entry per attempt and no
  `Retry-After`, because a retry cannot get past a 404. Nothing in that answer says that a repair
  has started: the answer is sent before the service has looked into the failure. The same
  request succeeds once a stand-in is in place. How soon that is depends on the free models on
  offer: the only model that could do the job on 7 October 2026, apart from the two default
  ones, needed about 20 seconds for its first document.

## Playground

`GET /` serves a single page for trying the service by hand. It is plain HTML, CSS, one
JavaScript module and a short theme script in `app/playground/static/`, with no build step and
nothing loaded from another origin. The page is written for visitors, and it uses two names.
What a visitor is using is *the demo*. The software, and what it does, is *the service*.

* **Scenarios.** Fourteen prepared runs with the default chain, each a document, an optional
  failure plan and the checks a correct run must pass. Six are documents, two are inputs the
  service has to refuse, and six exercise the fallback routing. The list follows the chain in
  use: it has sixteen runs when the chain has three models, because two of them need a third, and
  ten when the chain has one. The six documents and the meeting email among the refusals are also
  the seven sample documents that the benchmark command measures and that a test of the models
  sends. *Run all* executes the scenarios in turn and scores those that get an answer, for about
  12 model calls with the default chain, so the page doubles as a small live eval.
* **Two models leave no spare.** With the default chain, *Not an invoice* needs both models to
  answer before its "Two models agree" check can pass, and the four scenarios in which the
  primary fails use the second model as the live fallback, so they go red when that model is
  rate limited. A red run of that kind is a free model refusing a call, not a fault in the
  routing.
* **Your own document.** Any pasted text goes through the same chain. Paste a sample or a
  redacted document, not a real invoice: see [Data handling](#data-handling).
* **Live timeline.** A run shows each model in the chain as it is tried: outcome, error type, a
  short message and time spent, then the total response time and the model that answered. Three
  of the engine's messages tell an admin which setting to change. The page puts those three into
  words for a visitor, and its block *Raw request and response* keeps the API's own text. On a
  narrow screen the result stands below the request, and a run that the visitor starts brings it
  into view.
* **Simulated failures.** Any position in the chain can be told to fail in one of eight ways: 429,
  upstream 503, timeout, prose instead of JSON, JSON in the wrong shape, truncation, a "not an
  invoice" verdict, or a rejected key. Only the model call is replaced. The timeout, parsing,
  validation and routing around it are the real ones, so a fallback can be shown on demand
  instead of waiting for a model to fail.
* **Model status.** When OpenRouter does not list a model of the chain, or lists it with an
  announced end date, the page shows a chip on that model. For a fixed chain the chip reads
  "Unlisted from OpenRouter, please contact the admin.", because only the admin can replace the
  model. For a chain that the service looks after it reads "No longer offered by OpenRouter",
  because the service replaces the model itself. An announced end shows "Ends" and the date. The
  page is read by visitors, so it never tells them which setting to change: what an admin has to
  do is in the server log.
* **Model upkeep.** Under the heading "What happens when a free model disappears", the page shows
  how the service looks after its chain:
  * One line under the model chain says where the chain comes from: the tested default, this
    project's scheduled test, a change the service made itself, or the admin of the demo.
  * A test is drawn while it runs: one row for each model, one mark for each sample document, and
    the verdict.
  * When nothing is happening, the page shows the last test: the service's own, or the scheduled
    one when that is newer. Before any test has run, it shows a recorded one: the first pass of
    the benchmark of 7 October 2026, which is 28 real calls, judged by the rules that a test
    uses. *Replay this test* plays a finished test again and calls nothing.
  * The button *Test the free models now* starts a test. It is there only when the service looks
    after its chain, in production too. Such a test also covers the models of the chain, and it
    follows the same rule as a repair: repair, never rank. It puts no new stand-in to use, but
    it does judge a stand-in that is already answering. The service refuses a press, without
    spending a free request, in these cases: a test is under way, or the service is looking
    into a failed request; a test ended less than 15 minutes ago; OpenRouter's list of free
    models cannot be read; less than 60% of the day's free requests are left, or that count
    cannot be read.
  * When a run fails because a model is gone, a notice says what the service is doing about it.
    The notice stands in the result panel, under the outcome of the run: that is where a visitor
    who has just started a run is looking. It stays for an hour after the service last did
    something about the failure. While a test runs, the notice has one line for each tested
    model, with a clock that runs for a model that is being asked.
  * The page sends a failed run again by itself, once, when the chain has gained a model that
    it did not have when the run failed. It does that only for a single run of the visitor's
    own, and only for a visitor who stayed with it: its result is still on view, the document is
    unchanged and no failure is simulated. The new result says that the run was sent again.
  * When a stand-in has answered a run and then fails its test, the result of that run says so:
    the model no longer answers, and its answer may be wrong.
* **The ceiling.** A run that the service's ceiling on live model calls refuses is shown as not
  run, not as failed, with the service's own sentence. When the wait that the service asked for
  is over, the result says so and offers the same run again. After a refusal *Run all* waits
  until the service has room again, which is a minute at most. It does that twice at most for
  one scenario, and it stops when the part of the day that is open to visitors is used up. A run
  that only simulates failures calls no model, so it is never refused.
* **A service that sleeps.** A free host stops the service when nobody uses it, and the next
  request wakes it. When the page gets no answer, it says so and asks `GET /health` every 3
  seconds, for 3 minutes at most. When the service answers, the page finishes loading by itself.
  A run that got no answer waits in the same way and is then sent again, once. That is not done
  for a run that the service had already started on, because such a run may have called a model.
* **No request waits for ever.** A host may hold a request and never answer it. So the page
  stops waiting for a run when nothing has come from the service for one model's time limit and
  90 seconds more, which is 120 seconds with the default `TIMEOUT_SECONDS`. Every step of the
  run that arrives starts that wait again. Every other request of the page has a limit of 3
  minutes.
* **A tab that is left open.** Every request keeps a sleeping host awake, so the page asks for
  news about the chain only while something is happening. While the service is looking into a
  failed run or testing models, the page asks every 2 seconds. For 10 minutes after the visitor
  last did something, it asks every 20 seconds. Otherwise it asks for no news, and a tab that is
  hidden asks for none either. When such a question gets no answer, the page no longer shows a
  test as running, and it says that what it shows may be out of date. The first answer puts the
  page right again, without a reload.
* **Keyboard and screen reader.** Each row of a test is one stop for the keyboard, and its tip
  says in words what the marks show. The button that starts a test stays where the keyboard can
  reach it while it cannot be used, and the sentence beside it says why. A notice, the outcome
  of a run and the end of a wait are also written to a part of the page that a screen reader
  reads out.
* **Test suite.** A panel runs `pytest` in a child process and shows every test, grouped under
  the banner comments of the test files. It is off when `ENVIRONMENT=production`.

| Route                          | Purpose                                                          |
|--------------------------------|------------------------------------------------------------------|
| `GET /`                        | The page                                                         |
| `GET /playground/config`       | Model chain (with what the catalogue check found at startup), scenarios, fault catalogue, benchmark snapshot, the ceiling's figure for a minute |
| `POST /playground/extract`     | One extraction, streamed as NDJSON events                        |
| `GET /playground/models`       | How the service looks after its chain. The answer is the whole picture every time, and it is never cached. Asking starts nothing and calls no model |
| `POST /playground/models/test` | Start a test of the free models: `202` when it started, `409` with the reason when it may not start |
| `POST /playground/tests`       | Run the test suite (never in production)                         |

None of these is in the OpenAPI schema, and the dispatcher is not modified. The three `POST`
routes accept a request only from the page itself (see
[Security & operations](#security--operations)).

The page's endpoint wraps each engine in a `TracedExtractor` that reports attempts as they
happen. It puts a `FaultyExtractor` where a failure is simulated, and a `CountedExtractor` around
each engine that is called for real. It hands the list to a `BudgetedDispatcher`: the same
`ExtractionDispatcher`, with one question asked first, namely whether there is room under the
ceiling. `POST /api/v1/extract` asks that question in the same way. The stream is `start`, then
`attempt_started` / `attempt_finished` per model, then one `result` carrying the status,
`Retry-After` header and body the public endpoint returns for that outcome. Those come from the
application's own exception handlers rather than being restated, and a test compares the two
endpoints for every outcome, a refusal by the ceiling included.

Set `PLAYGROUND_ENABLED=false` for an API-only deployment. The service still looks after its
chain then. It does not need the page for that: only the place to watch it is gone.

## Fallback mechanics

`ExtractionDispatcher.extract()` iterates the chain in use. Before it starts, the run has asked
the ceiling on live model calls for room (see [Security & operations](#security--operations)):

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
| **In-band upstream error** (HTTP 200 + `error`)| `ProviderError` / rate limit | OpenRouter's own error member                   |
| No answer within `TIMEOUT_SECONDS`            | `ProviderTimeoutError`       | `asyncio.wait_for`, `APITimeoutError`, 408/504   |
| HTTP 401 / 403                                | `ProviderAuthError` / `ProviderError` | Bad key, or a model the key cannot reach. Not retryable |
| HTTP 404 / 402                                | `ProviderError`              | Model not found (a withdrawn or mistyped id), or out of credits. Not retryable |
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
  verdicts settle it. With the default chain the two models are the whole chain, so both are
  asked. In a longer chain this quorum is what stops the walk: without it, a text that is not an
  invoice costs one more model call for every model left, and a full `TIMEOUT_SECONDS` whenever
  one of them is queued.
* The chain is exhausted and **any** model reported "no invoice" → `422 unparseable_text`. A
  semantic verdict outranks transient failures from the models that could not answer.
* All models fail for other reasons → `503 providers_unavailable`, one `detail` entry per attempt.
* The ceiling has no room → `503 providers_unavailable` before the walk starts, with
  `Retry-After`. No model is called, so `detail` is one object and not a list of attempts.

**Design choices**

* No in-SDK retries (`max_retries=0`): the next model *is* the retry, which keeps worst-case
  latency bounded and predictable at `TIMEOUT_SECONDS × chain length`.
* One chain everywhere. `/health`, the page, the API and the Python function `extract_invoice()`
  all go through the same dispatcher, so they cannot disagree about the chain in use. When the
  service changes its chain, a request that is already running finishes on the chain it started
  with, and the next request uses the new one.
* The order of the chain is about being right, not about being quick. The chain moves on only
  when a call fails, so a fallback replaces a failed call, never a wrong answer: a wrong answer
  from the primary is returned as it is. The first position therefore goes to the model with the
  fewest wrong answers, then the fewest calls with no answer, and speed only breaks ties.
* A withdrawn model id is never skipped on the catalogue's word alone. With a fixed chain it is
  still tried whenever its turn comes, which for the primary is every request. It fails fast
  with a 404 (`ProviderError`), and the next model answers with `fallback_triggered: true`, until
  the id is removed from the chain. With a chain that the service looks after, the model is taken
  out once two sources agree: it gave no answer to a real request, and OpenRouter's own page for
  it says "Model not found". Either way, a wrong answer from the catalogue alone can never switch
  off a model that works.
* One shared HTTP client for every model, since they differ only by id. It is built at startup,
  because construction reads CA bundles from disk and would otherwise block the event loop.
* `temperature=0` throughout → highly repeatable output.
* Primary failures log at `WARNING`, an exhausted chain at `ERROR`, and the aggregated cause is
  surfaced in the 503 body so operators can see which model broke and why.
* All models share one prompt and one Pydantic schema, so a fallback answer has exactly the same
  shape and normalisation guarantees as a primary answer.

## Latency vs cost trade-offs

With the default chain, **cost is zero**: every model is `:free`. The trade-off is therefore
latency, rate limits and what a provider may do with your text
([Data handling](#data-handling)), not money.

| Scenario                           | Latency                                         |
|------------------------------------|-------------------------------------------------|
| Primary succeeds (the common case) | One model call: the primary's answer time       |
| Text is not an invoice             | Two model calls, one after the other: both models must agree before it is settled |
| One fallback                       | The time the primary took to fail, then the fallback's answer time. A rate limit or a withdrawn id fails in a fraction of a second; a timeout costs the full `TIMEOUT_SECONDS` first |
| Worst case, whole chain            | up to `TIMEOUT_SECONDS` × chain length: 60 s with the two default models, 90 s when the chain has three |
| The ceiling has no room            | No model call. The answer is a 503, and its `Retry-After` says how long to wait |

This table has no measured seconds, because answer times belong to one set of models on one
day. They are in the snapshot under [Model selection](#model-selection-a-dated-snapshot).

* Free models are rate limited per account and can queue under load. This key's allowance is
  **1000 free-model requests per day**. To check yours, use the command under
  [Check your key and allowance](docs/USAGE.md#check-your-key-and-allowance) in the usage guide.
  It reads the key from `.env`, so the key is never typed into a command line. The service
  reads the same figure itself, at most once a minute, to keep its ceiling.
* Looking after the chain costs free requests, never money. It costs none while requests
  succeed. A repair costs 7 requests at most for each model it tests: 9 in all with the four
  models that OpenRouter listed on 7 October 2026, and 42 at the very most. A test started from
  the page, or the scheduled test, also covers the models of the chain: about 23 requests with
  those four models. All of them count against the same daily allowance.
* A third model in the chain raises the worst case, and it changes one more thing. A text that is
  not an invoice is settled by two models that agree. When the second model fails on such a text,
  the third one is asked for the second opinion.
* `TIMEOUT_SECONDS=30` is deliberately generous, because free models can be slow. Look at the
  slowest answers in the snapshot before you lower it: a budget shorter than a model's slow
  answers turns good answers into timeouts, and every timeout costs a cascade.
* A shorter chain means lower worst-case latency. A single-model chain is valid configuration.
* Long invoices need a higher output cap and a longer timeout, together. Both default models
  reason before they answer, and that reasoning counts against `MAX_OUTPUT_TOKENS` (default
  2048). In a test on 7 October 2026 an invoice with 20 line items was cut off on both models at
  the default cap. With the cap at 6000 the primary answered a 40-line invoice in 28 s, close to
  the 30 s timeout, and the fallback timed out. A cut-off answer is a failed call
  (`ContentBlockedError`), so when both models are cut off the request ends as a 503. For long
  invoices raise `MAX_OUTPUT_TOKENS` and `TIMEOUT_SECONDS` together: the higher cap lets a model
  finish its answer, and it then needs the time to do so.
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
It also pins a chain of three positions of its own, and it switches off the two things a service
reads when it starts: the catalogue check and the published list. So the suite depends neither on
the default chain nor on which models OpenRouter lists today. A chain that is named is a fixed
chain, so a test that wants the service to look after its chain blanks the setting itself.

The suite cannot reach the network, and that is so by construction. The service has three ways
out, and `tests/conftest.py` closes each of them for every test: the object behind every read
that sends no key (OpenRouter's catalogue, its list of free models, the published list), the
object behind the reading of the day's count, and the part of the model client that opens a
connection. A test that reaches for one of them fails, at once and again when it ends, because
the code under test is built to carry on after a failed call and would otherwise hide it. Every
test also starts with a full day's count, an empty minute, no chosen chain, and a saved-chain
file in a temporary folder. `tests/test_doors.py` reaches for each of the three on purpose and
checks that it is closed.

* **Test 1** – valid invoice text parses into `InvoiceData`; asserts the call used the primary
  model, the strict `json_schema` response format and `temperature=0`, and that no later model was touched.
* **Test 2** – a failing model cascades to the next, parametrised over 429, 503, 404, timeout, an
  unexpected exception, invalid JSON, schema mismatch, an empty response, an in-band upstream
  error and truncation; plus a test that two failures fall through to the third model.
* **Test 3** – `POST /api/v1/extract` through `httpx.AsyncClient` + `ASGITransport`, plus the
  422/500/503 paths, CORS preflight, request-id hardening and `/health` states.
* Schema invariants, strict-schema generation, reasoning-preamble recovery, prompt hardening and
  the full SDK error-translation matrix are covered by focused unit tests.
* `tests/test_playground.py` covers the playground through the same fake: every simulated fault
  raises what a real model would, the stream reports each attempt, the `result` event equals the
  public endpoint's response for every outcome, and the page stays within its own security policy
  (no inline script or style, no HTML built from response text). It also checks that the page's
  benchmark snapshot carries its date, names its saved records and adds up. And it reads the
  page's script as text, and holds it to the service: how long the page waits for a service that
  does not answer, that every request has a time limit, that it asks for news only while
  something is happening, and that it reads a refusal by the ceiling where the service puts it.
* `tests/test_model_catalogue.py` pins the two rules the catalogue check leans on. Only
  OpenRouter's own "Model not found" answer counts as "not listed"; a network error, any other
  status or an unreadable answer is "could not be checked". And the check reports without
  changing anything: with the primary of a fixed chain marked as not listed, the model is still
  called, the response still says `fallback_triggered: true` and `/health` answers exactly what it
  answered before. The file also covers the command's exit code, and that a failing or slow
  catalogue neither stops nor holds up startup. The lookup takes its HTTP call as an argument, so
  these tests never reach OpenRouter.
* `tests/test_budget.py` covers the ceiling on live model calls, on a clock that the test moves
  by hand. The minute: the sixteenth call in a minute is refused, a run needs room for every call
  it may make, and twenty runs that arrive together cannot all be let in. The day: visitors stop
  when a fifth is left, a run needs every call it may make above that fifth, and yesterday's
  count is not today's. The reading of the day's count: the key goes to OpenRouter's own
  encrypted address and nowhere else, only three numbers of the answer are kept or logged, and a
  visitor who goes away does not take the reading along. And through the service: one test sends
  each kind of request to the public endpoint, from an invoice to a body that is too large, a
  full minute, a used-up day and a crash. It checks that every answer is a 200, 422, 500 or 503,
  which are the statuses the OpenAPI schema declares, and that every error uses the one envelope.
  So no limit of the service adds a status to the endpoint.
* `tests/test_body_limit.py` covers the size limit on request bodies. Each oversized body is a
  generator that counts how many pieces were taken from it, which shows that the refusal comes
  before the body is read. The file also holds the one status that is older than the limits and
  that the schema does not declare: the 400 for a body that cannot be read as JSON at all.
  `tests/test_playground_guard.py` goes through what a browser can say about where a request
  comes from, for the page's three `POST` routes, in development and in production.
* `tests/test_logging.py` checks what a successful extraction leaves in the log, through the API
  and through the page: one line with the model and the time, and no line at `INFO` or above
  with the vendor name or the invoice number. Both are still written at `DEBUG`.
* `tests/test_model_rules.py` covers what a test of the models concludes, as pure functions: each
  verdict, each case of "repair, never rank", the measured place of a default model that comes
  back, and the turn of the models that had no room. It also judges the three saved passes of 7
  October 2026 by those rules: each of them leaves the hand-picked chain in place.
* `tests/test_model_chooser.py` covers the service looking after its chain. The models answer
  from a script, OpenRouter's list and pages are plain Python values, and the saved file lives in
  a temporary folder. Among its cases: a blank setting means the service looks after the chain,
  and naming the default models still fixes it; startup calls no model; a withdrawn first model
  is replaced after the first failed request; a model that is only rate limited changes nothing;
  a simulated failure is not evidence; a stand-in that the test could not judge goes on
  answering, and is never saved; a repair that could not fill the place is taken up again; a test
  never calls an id that is not free; a paid model can never be chosen.
* `tests/test_published_chain.py` covers the published list from both ends. What the job writes
  is what a service reads. A file that fails a check is not used, and no file can stop a service
  from starting. A test is due only for a reason. The job fails on purpose when no model of the
  chain is left, or when a model of the chain failed the test. The file also checks that the
  workflow starts on every file that the test is made of, and that it publishes from `main` only.
* `tests/test_models_page.py` covers the *Model upkeep* part of the page and the two routes
  behind it. The page has to have words for every code that the service can send, it may read
  only what the service sends, and no sentence of it may name a setting, a file or a key.
* `tests/test_page_behaviour.py` runs the playground's script, where `tests/test_playground.py`
  and `tests/test_models_page.py` read it. It first asks the service itself, with scripted
  models, for the answers a page can get: a run, a refusal, and what the service says about its
  chain in every state that the page has sentences for. Then it starts `tests/page_harness.mjs`
  once, with Node.js. The harness loads the script on a made-up page that only keeps the words
  that are drawn on it, with a clock that it moves by hand, and plays those answers back in 38
  cases. The cases hold what the script does: which requests it sends and when it stops asking,
  how often it sends a run, and that it has words for every state it is shown. Where Node.js is
  not installed, the 40 tests of this file are skipped.
* `tests/test_front_door.py` covers the project page in `site/`. Every piece of text on that page
  is one of three things: a statement that the test works out again from the recordings, the
  snapshot or the code; a quotation that has to stand word for word in the file it names; or a
  label that carries no figure. Anything else fails. The file also holds the page as it is read:
  the order of its parts, where each link leads, that nothing is hidden, and that the address of
  the live demo is written in exactly one place. One of its tests is skipped until that address
  is filled in.
* `tests/test_benchmark.py` covers the benchmark command without calling a model: which
  scenarios count as live cases, the grading (one wrong field makes the whole answer wrong, and
  a call with no answer is not graded), tables that keep "no answer" apart from "wrong answer",
  records that hold the sanitised message only, and the guard rails (an id that does not end in
  `:free`, a run of more than 100 requests and a missing key are each refused before any call).
* `tests/test_docs.py` holds the documents to the code, to the two pages and to the saved
  records. An id ending in `:free` that is written in this README, in `docs/*.md` or in
  `.env.example` has to be in the default chain or in the page's benchmark snapshot
  (placeholders such as `vendor/model-a:free` are exempt). The default chain has to be what the
  snapshot measured, the chain tables in the documents have to show that chain in its order, and
  the snapshot's figures have to agree between the saved records, the page and this README.
  Every setting has to be named in `.env.example`, in the configuration table of this README and
  in `docs/USAGE.md`. The numbers in the documents are built from the code's named constants.
  What the documents quote from the page has to be on the page: the two chips for a model that
  OpenRouter does not list, the four verdicts, the titles of the notices. What they say the page
  does is held to the page's script: how long it waits, how often it asks for news, and when it
  sends a run again. So is what they say each part of the page means: a notice, a tile or a chip
  of a result, the test on show, the button that starts a test. A log line that `docs/USAGE.md`
  shows has to be one that the code can write, and code that a guide shows has to be in the file
  the guide names. A link has to lead to a file or to a heading that is there.
  This README has to keep a section headed "The scheduled test", because a log line and the
  guides point to it. And `docs/DEPLOY.md` is held to what it tells its reader to look for: the
  names of the three workflows and of their jobs, the line of `site/index.html` that holds the
  live demo's address, and the start command and the plan in `render.yaml`.

No test runs either page in a browser. Both scripts are run with Node.js instead, on a made-up
page, with a clock that is moved by hand. `tests/test_page_behaviour.py` does that for the
playground's script, and two tests of `tests/test_front_door.py` do it for the project page's
script, which is short. So the tests hold what each script does, and not what a browser draws
from it. Where Node.js is not installed, those two tests are skipped, and so are the 40 tests of
`tests/test_page_behaviour.py`.

The playground's *Test suite* panel runs all of this from the browser.

## Security & operations

* **Prompt injection.** The document is wrapped in `<document>` markers, any literal marker inside
  the submitted text is defanged first, and the system prompt states that document content is
  untrusted data rather than instructions. The benchmark's live cases include an injection
  attempt (a `</document>` break, then an order to rewrite the vendor and the total), so every
  model you measure is tested against it.
* **Secrets.** The API key is a `SecretStr`, so it cannot leak through a settings `repr` or dump,
  and `/health` never echoes it. Placeholder values from `.env.example` count as unset, so an
  unedited template reports `unavailable` rather than pretending to be ready.
* **Logging.** Every request gets an `X-Request-ID`; fallbacks log at `WARNING`, exhausted chains
  at `ERROR`. The HTTP client loggers are held at `INFO` even when `LOG_LEVEL=DEBUG`, because at
  DEBUG they would write the caller's full invoice text to the logs. The service's own line for
  a successful extraction names the model and the time, and nothing that the document said. The
  vendor name and the invoice number are written at `DEBUG` only, so keep a public deployment at
  `INFO` (see [Data handling](#data-handling)). When the service changes its chain while it
  runs, it says so at `WARNING`, with the chain before and after. A run that the ceiling refuses
  is one `WARNING` line with the numbers behind the refusal.
* **CORS.** `CORS_ALLOW_ORIGINS=*` disables credentials (browsers require an explicit origin for
  credentialed requests); set an explicit list to enable them.
* **Not included.** The extract endpoint is unauthenticated: anyone who can reach it can run it.
  It is throttled, but only by one ceiling for all callers together (the next item), not by a
  limit for each caller. Put the service behind an authenticating gateway before exposing it, and
  limit each client there.
* **The ceiling on live model calls.** `app/budget.py` keeps one ceiling for everybody who can
  make the service call a model: callers of the API, visitors of the page, and the tests of the
  models. A live model call is a call that really goes to a model, as opposed to a simulated one.
  * *The minute.* A run is admitted only while it leaves the last 60 seconds at 15 live calls or
    fewer. It needs room for every call it may make, which is one for each model of the chain.
    OpenRouter allows 20 a minute on free models, and the difference is left for calls that this
    process cannot count. The count is kept in memory.
  * *The day.* The count of the account's free requests for the UTC day is OpenRouter's own. The
    service reads it with `GET /key`, at most once a minute. The last fifth of the day's limit is
    kept back from callers and visitors, for the service's own repair and for the scheduled
    test. A run is admitted only while every call it may make would start with more than that
    fifth left. With a limit of 1000 and the two default models, runs therefore stop when 201
    requests or fewer are left. A host that sleeps cannot reset this count, because the service
    does not keep it. For as long as the count cannot be read at all, the service lets 20 live
    calls through for each start, and then refuses. That rule is for a count that exists and
    cannot be read. With another address in `OPENROUTER_BASE_URL` nobody counts the day, and
    only the per-minute ceiling applies.
  * *A refusal* comes before the chain starts, so no model is called. It is a `503` in the usual
    envelope, with `Retry-After` set to the real wait (see the two 503s under [API](#api)).
  * *Who else may spend.* The button on the page needs 60% of the day to be left. The scheduled
    job leaves 20% of the day. A repair only needs its own requests to fit. A running test paces
    itself at ten calls a minute, and its calls count in the same minute.
  * *What it does not do.* It does not tell callers apart. One caller can fill the minute for
    everybody else. One determined caller can also use up the share of a day that is open to
    visitors: 800 of 1000 requests at 15 a minute take about 53 minutes, and 40 of 50 take about
    3 minutes. The last fifth stays for the repair and for the scheduled test. And the ceiling
    counts for one process only (see *Scaling*).
* **Body size.** A request body of more than 1 000 000 bytes is refused with the
  `422 validation_error` envelope. A length that the request declares is believed before a byte
  is read. A body with no declared length is counted as it arrives, and so is a body that is
  sent in pieces (chunked), whatever length it declares. The largest valid document, 50 000
  characters, is at most 600 000 bytes of JSON.
* **Playground.** The page is served under a Content-Security-Policy that allows only same-origin
  scripts and styles, and it never builds HTML from response text, because model output is
  untrusted. Its `POST` routes accept a request only from the page itself. `Sec-Fetch-Site` has to
  be `same-origin` or `none`. A browser too old to send that header has to send an `Origin` that
  is the service's own host. A caller that sends neither header, such as curl or a script, passes
  in development and gets a `403` in production. Any program can forge these headers. The guard
  stops another site's page from spending the allowance through a visitor's browser, and what
  limits the spending itself is the ceiling. The test-suite route is off in production. The page
  spends model allowance as the API does, and its button can start a test of up to 42 free
  requests: set `PLAYGROUND_ENABLED=false` wherever the API should stand alone.
* **Model catalogue check.** `app/model_catalogue.py` (standard library only) asks OpenRouter's
  public catalogue, with no API key, whether each configured model is still listed. The request
  is `GET https://openrouter.ai/api/v1/model/{id}`. By itself it only reports: it never changes
  routing, `fallback_triggered`, `/health` or any response of `POST /api/v1/extract`. "Not
  listed" is concluded only from OpenRouter's own 404 "Model not found". A network error, another
  status or an unreadable answer is "could not be checked" and changes nothing. Other base URLs,
  ids that start with `@` and ids without a slash are never looked up. At startup it runs when
  `MODEL_CATALOGUE_CHECK` is true (the default) and a key is set. The whole check then has a
  5-second budget, and it can neither stop startup nor delay it beyond that. "Listed" does not
  mean "works": only a real request through the service shows that. With `OPENROUTER_MODELS`
  blank, `app/model_chooser.py` asks the same page again after a real request has failed on a
  model, and takes the model out when the answer is "Model not found".
* **Free models only.** A chain that the service looks after can never hold a model that costs
  money: every id has to have the form `vendor/model:free`, and that is checked in each of the
  six places where an id comes in or goes out (see
  [When a model disappears](#when-a-model-disappears)).
* **Scheduled test.** `.github/workflows/model-check.yml` starts daily at 06:17 UTC, tests the
  free models when a test is due, about once a week, and publishes the chain it tested. It needs
  one repository secret, the OpenRouter key, which only one step of one job sees, and the job
  that can write to the repository runs no downloaded code. GitHub pauses scheduled workflows in
  a public repository after 60 days without activity. All of it is under
  [The scheduled test](#the-scheduled-test). OpenRouter also offers an email of its own, "Model
  deprecation alert" (Settings, Notifications). It is a partial signal: it covers models in
  recent use whose retirement is scheduled.
* **The published list is trusted within limits.** It can change the chain of every deployment
  that reads it, and so the free providers that receive documents. It can name free models only,
  it is ignored unless it was made by the very same test, and `MODEL_PUBLISHED_URL=null` switches
  it off. Its fingerprint is not a secret: it protects against a list made by other code, not
  against a hostile file. If the repository or the account is ever renamed, change
  `DEFAULT_PUBLISHED_URL` in `app/config.py` at once, because the old address can go on answering
  for whoever takes the old name.
* **Requests the service sends by itself.** There are three kinds, and none of them carries a
  document. The service asks OpenRouter's catalogue about models, without a key. With
  `OPENROUTER_MODELS` blank it also reads the published list from GitHub, without a key. And to
  keep its ceiling it asks OpenRouter for the day's count. That one request carries the key: it
  goes to OpenRouter's own `https` address only, and it follows no redirect. A test of the models
  is a different thing: it calls models, and it sends them the sample documents only.
* **Data handling.** On 7 October 2026 OpenRouter listed both default providers as keeping
  prompts and as possibly training on them, and a chain that the service looks after can come to
  hold other free models. Send any of them sample or redacted documents, not real invoices: see
  [Data handling](#data-handling).
* **Key rotation.** Settings and the client are cached per process, and python-dotenv will not
  override an already-exported variable, so rotating the key requires a process restart.
* **Scaling.** The service is fully async and I/O bound. Run it as one process, as `render.yaml`
  does. The ceiling on live calls, the chain that the service chose and a running test of the
  models all live in the memory of one process. With `--workers N`, each worker would allow 15
  calls a minute of its own, which is above OpenRouter's 20 from two workers on, and each would
  keep a chain and run tests of its own. Rely on `/health` returning 503 when no key is set.
  Render is the one place where `/health` should not be the host's health check:
  [docs/DEPLOY.md](docs/DEPLOY.md#2-create-the-service-from-the-repositorys-own-settings-file)
  says why.

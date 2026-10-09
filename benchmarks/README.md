# Benchmark records

Each file here is a record of real calls to free models, one JSON object per line. They are kept
so that the figures in the main README and on the playground page can be checked, and so that the
next measurement has something to be compared with.

Every call sent one of the playground's sample documents. No file holds a key, an account
identifier or raw upstream text: only the service's own error type and its sanitised message.

## Dated runs: `<UTC date>/<UTC time>.jsonl`

Written by `python -m app.playground.benchmark`. The first line describes the run: the models,
the number of passes, the time limit and the output cap. Every other line is one call.

| Field                   | Meaning                                                                    |
|-------------------------|----------------------------------------------------------------------------|
| `run`, `pass`           | The run, and which pass of it the call belongs to                          |
| `started`               | When the request was sent, in UTC                                          |
| `model`, `case`         | The model that was called and the playground scenario it was sent          |
| `outcome`               | `answered`, `refused` ("this is not an invoice") or `no_answer`            |
| `correct`               | `true` when every checked field was right, or the meeting email was refused. `null` when there was no answer to grade |
| `checks`                | Each check's label, and whether it passed                                  |
| `seconds`               | Time until the call returned                                               |
| `error_type`, `message` | For `no_answer`: the service's error type and its sanitised message        |

To print the tables for a file again, without calling any model:

```bash
python -m app.playground.benchmark --report benchmarks/2026-10-07/175417Z.jsonl
```

`2026-10-07/175417Z.jsonl` is the run behind the snapshot in the main README ("Model selection")
and on the playground page. `tests/test_docs.py` compares both with this file, so a figure that
is changed in one place only fails the suite. The running service also reads pass 1 of this file,
which the page shows and replays as "A recorded test" while it has no newer test to show, so the
file must stay in the repository and in the deployment.

The project page (`site/index.html`) shows the same figures, and links to this file. There
`tests/test_front_door.py` does the comparing. The two runs that the project page shows are not
benchmark records and are not kept here: they are in `site/recorded/`.

## `exploratory/`

Everything in `exploratory/2026-10-07/` was produced on 7 October 2026 by one-off scripts that
were not kept in the repository. That is why it is labelled exploratory: none of it can be
produced again as it is, and `--report` cannot read it. It is here because statements in the
documents rest on it and on nothing else.

### Trial runs: `run*.jsonl`

Four files written between about 15:57 and 16:22 UTC, before the benchmark command existed.
Their fields differ from those of the dated runs.

| File                  | Started (UTC) | What was sent                                                     |
|-----------------------|---------------|-------------------------------------------------------------------|
| `run1.jsonl`          | 15:57         | Two passes. Strict schema: the two default models, the `dots-studio` model and OpenRouter's "any free model" router. JSON mode: the `apodex` model and two Google Gemma models |
| `run2.jsonl`          | 16:05         | One pass. The router and the `apodex` model with `strictreq`; the Liquid model with the strict schema |
| `run3.jsonl`          | 16:19         | One pass. The two default models, strict schema                    |
| `run3-apodex4k.jsonl` | after `run3`  | One pass. The `apodex` model in JSON mode with `MAX_OUTPUT_TOKENS=4096` |

| Field             | Meaning                                                                          |
|-------------------|----------------------------------------------------------------------------------|
| `scenario`        | The playground scenario that was sent                                            |
| `model`           | The id that was asked for                                                        |
| `mode`            | `strict`: the service's default, a strict JSON schema. `json`: plain JSON mode (`OPENROUTER_STRICT_JSON_SCHEMA=false`). `strictreq`: the strict schema, with OpenRouter asked to use only providers that support every parameter sent |
| `served_by`       | The model OpenRouter reported as having answered. It differs from `model` for the router |
| `outcome`         | `answered`, `refused` or `error`                                                 |
| `passed`, `total` | Field checks passed and checked. They include a "Returns HTTP 200" check that every answer passes, which the benchmark command no longer counts |
| `missed`          | The checks that failed, each with the value the model returned                   |
| `error`           | The service's error type and its sanitised message                               |
| `seconds`, `pass` | Time until the call returned, and the pass the call belongs to                   |

What they show:

* **The "any free model" router sends a request to whichever free model it picks.** Its 14 calls
  in `run1.jsonl` were served by eight different models, and 5 of them ended without an answer.
  With `strictreq`, in `run2.jsonl`, 3 of its 7 calls ended without an answer.
* **Rate limiting on the Liquid model swings within minutes.** It was rate limited on 9 of 14
  calls in `run1.jsonl`, 3 of 7 in `run2.jsonl` and 1 of 7 in `run3.jsonl`. In the dated run,
  two to three hours later, it was rate limited on 2 of 21.
* **What the Liquid model's wrong tax looks like.** In `run2.jsonl`, `missed` shows that it
  returned no tax at all for the service invoice, where the email says no tax applies and the
  right value is `0`. That is the one time these files hold its answer to that document: in the
  other trial runs the call was rate limited, and the dated run records only that the check
  failed.
* **Two Google Gemma models never answered.** All 28 of their calls were rate limited.

### Model-list history: `model-list-history.txt`, `model-list-withdrawals.txt`

The turnover figures in the main README ("Free models come and go") come from these two tables.
They were built from 106 copies of OpenRouter's public model list
(`https://openrouter.ai/api/v1/models`): copies kept by the Internet Archive, the oldest from 17
January 2025, and the live list of 7 October 2026.

| Figure in the documents                                              | Where it is                                                    |
|----------------------------------------------------------------------|----------------------------------------------------------------|
| 83% and 64% of free ids still listed after 30 and 61 days            | `model-list-history.txt`, section "survival": the lines for 20260906 and 20260806 |
| 71 ids ending in `:free` in May 2025, 16 on 7 October 2026           | The same file, first section: the lines for 20250503 and LIVE-20261007 |
| The two withdrawn default models were free from 8 to 25 September    | The same file, section "traces": first listed in the copy of 8 September, end date 2026-09-25, absent from the copy of 26 September |
| 38 of 63 withdrawn free ids never showed an end date                 | `model-list-withdrawals.txt`, first section                    |
| Notice between about 3 and 12 days at the median                     | The same section: "notice-at-least" (3.3) and "notice-at-most" (12.2) |

Two limits. The copies are not evenly spaced: the gaps run up to 19 days, so an end date that
was announced and acted on between two copies counts as "never showed a date", and every
duration is a bound, not a measurement. And the "31% and 59%" for a two-model chain are
arithmetic on the survival figures (1 - 0.83 x 0.83 and 1 - 0.64 x 0.64), on the assumption that
both models behave like the average free id, independently of each other.

### Long-invoice trial: `long-invoice-trial.txt`

The advice to raise `MAX_OUTPUT_TOKENS` and `TIMEOUT_SECONDS` together for long invoices rests on
this file: 16 calls made at about 17:20 UTC. With the default cap of 2048, both default models
were cut off on an invoice of 20 line items. With the cap at 6000, the primary answered 20 lines
in 14.4 s and 40 lines in 28.1 s, and the fallback timed out at 30 s on both. It is one call per
case, so read it as an observation, not as a measurement.

### What has no record

Some dated statements in the documents were read off web pages, and no file here backs them:
what OpenRouter's provider table and its FAQ said about data handling, NVIDIA's API trial terms,
OpenRouter's "Model deprecation alert" email, and how GitHub treats scheduled workflows. The
documents word them as what was said on 7 October 2026. Two things were read a day later, on 8
October 2026: more about GitHub (who gets the email about a failed scheduled run, and what
GitHub Actions costs a public repository), and what Render's documentation says about a free
web service (when it is stopped, how long a start takes, and what it forgets). The steps of
[docs/DEPLOY.md](../docs/DEPLOY.md) that name a screen of GitHub or of Render were written from
the same reading, and held against the same documentation once more on 9 October 2026: the
menus and the buttons, what the free plan allows, and how Render checks that a service is up.
Nobody had carried those steps out when they were written. Check all of them at the source
before you rely on them.

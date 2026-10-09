"""What a test of the free models concludes, as pure functions.

Calls go in; a verdict per model and a chain come out. No file, no clock, no network,
so every rule here can be read and tested on its own. Three callers share them: the
service's own test (:mod:`app.model_chooser`), the scheduled job
(:mod:`app.published_chain`) and the recorded run the page can replay.

The rules, in three sentences:

1. **Repair, never rank.** A model that is in the chain keeps its place and its
   order, whatever one test says about it. Seven documents can rule a model out; they
   cannot rank two models, and free models are rate limited in bursts.
2. **A model leaves only when two sources agree**: OpenRouter's own page for it says
   "Model not found", and it gave no answer here.
3. **A free place goes to a model that passed**, behind the models already there. One
   model that is right but slow may take the last place, because a slow right answer
   is better than none.

One exception to "behind", and it ranks nothing: a model of the built-in default
chain that passes goes back to where the person who measured that chain put it. A
first model that was withdrawn for a few days leads again when it returns.

Changing the order of two models that both work stays a person's job: measure with
``python -m app.playground.benchmark`` over several passes and change
``DEFAULT_MODEL_CHAIN`` (README, "Model selection").
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from app.playground import benchmark

#: Models in a chain, at most: two places like the tested default, and one spare. The
#: spare is where a slow or newly found model can do no harm, because the last model
#: is called only when every model before it has failed.
PLACES = 3
#: Documents a model may miss (a wrong answer, or no answer) and still pass. The
#: fallback of the tested default gets one document wrong in every recorded pass.
MAX_MISSES = 1
#: A model's test ends once this many of its calls were not fully right: it can no
#: longer pass, and every call counts against the daily allowance.
GIVE_UP_AFTER = MAX_MISSES + 1
#: The one document a model must get right to pass: a wrong tax amount is a mistake,
#: doing what a document says is a hole.
MUST_BE_RIGHT = "prompt-injection"
#: Models tested at once, at most: 6 x 7 documents = 42 requests.
CANDIDATE_LIMIT = 6
#: Seconds between request starts during a test: 10 a minute, half of the 20 a minute
#: OpenRouter allows an account on free models. Visitors' runs are admitted under the
#: ceiling of :mod:`app.budget`, which counts a test's calls with their own.
#: One minute is left open on purpose: the one in which a repair starts. The calls
#: visitors made just before it and the test's own then add up, to 20 to 25 in those 60
#: seconds. A call that OpenRouter turns away for it costs a visitor one fallback, or
#: leaves a candidate "not judged" and the repair owed. Holding every call of a test
#: back until the minute has room would let a busy page delay a repair for as long as
#: it stays busy.
SECONDS_BETWEEN_STARTS = 6.0

#: No usable answer to a document that is known to be answerable: the model's own doing.
UNUSABLE = ("SchemaValidationError", "ContentBlockedError")
#: The key was refused or is missing. No model can fix that, so a test stops.
KEY_PROBLEMS = ("ProviderAuthError", "ProviderNotConfiguredError")

#: Every verdict :func:`judge` can give, and every reason. The page has words for each
#: (a test compares these tuples with the page's tables).
VERDICTS = ("passed", "slow", "failed", "not_judged")
REASONS = (
    "all_right",
    "one_missed",
    "hidden_instructions",
    "wrong_answers",
    "unusable_answers",
    "no_answers",
    "unanswered",
)
#: What became of a model once the chain was decided.
PLACINGS = ("kept", "added", "dropped", "no_place")


def judge(
    tested: Sequence[str],
    calls: Sequence[dict[str, Any]],
    cases: Sequence[str],
    timeout_seconds: float,
    finished: bool = False,
) -> list[dict[str, Any]]:
    """One row per tested model: the benchmark's summary, a verdict and its reason.

    ``cases`` are the ids of the sample documents, in the order they are sent.
    ``verdict`` stays ``None`` while a model's calls are still coming in.

    * ``passed``: every document was sent, at most one was missed, the document with
      hidden instructions was answered right, and the usual (median) answer took at
      most half of the time limit (step 1 of the README's rule).
    * ``slow``: the same, but the usual answer took longer. Good for the last place only.
    * ``failed``: two answers were wrong or unusable, or the model did what a document
      told it to do. This says something about the model.
    * ``not_judged``: calls went unanswered (rate limits, timeouts, upstream errors).
      This says something about the moment, so nothing is held against the model.
    """
    measured = {row["model"]: row for row in benchmark.summarise(calls)}
    rows = []
    for model in tested:
        row = measured.get(model) or {
            "model": model,
            "calls": 0,
            "no_answer": 0,
            "errors": {},
            "answered": 0,
            "correct": 0,
            "median_seconds": None,
            "slowest_seconds": None,
        }
        wrong = row["answered"] - row["correct"]
        unusable = sum(row["errors"].get(kind, 0) for kind in UNUSABLE)
        misses = row["calls"] - row["correct"]
        guarded = next(
            (call["correct"] for call in calls if call["model"] == model and call["case"] == MUST_BE_RIGHT),
            None,
        )
        done = finished or row["calls"] >= len(cases) or misses >= GIVE_UP_AFTER
        verdict: Optional[str]
        why: Optional[str]
        if not done:
            verdict, why = None, None
        elif guarded is False:
            verdict, why = "failed", "hidden_instructions"
        elif wrong + unusable >= GIVE_UP_AFTER:
            verdict, why = "failed", "wrong_answers" if wrong else "unusable_answers"
        elif row["calls"] >= len(cases) and misses <= MAX_MISSES and guarded is True:
            quick = bool(benchmark.rank([row], timeout_seconds))
            verdict, why = ("passed" if quick else "slow"), ("all_right" if not misses else "one_missed")
        else:
            verdict, why = "not_judged", "unanswered" if row["answered"] else "no_answers"
        rows.append({**row, "verdict": verdict, "why": why, "placing": None})
    return rows


def next_chain(
    current: Sequence[str],
    rows: Sequence[dict[str, Any]],
    gone: Sequence[str],
    timeout_seconds: float,
    measured: Sequence[str] = (),
) -> list[str]:
    """The chain to use after a test. It can only be repaired, never reshuffled.

    ``current`` is the chain in use, ``rows`` what :func:`judge` said, ``gone`` the
    models OpenRouter's own page answered "Model not found" for, and ``measured`` the
    built-in default chain, in the order a person gave it by measuring.

    A model of the chain leaves only when it is gone *and* gave no answer in this
    test (a model that was not tested gave none). Every other model of the chain
    keeps its place and its order, whatever its verdict. Free places are then filled
    behind the models that are already there: first the models that passed, in the
    README's order, then at most one model that is right but slow. A test in which
    nothing can be kept or added changes nothing: a chain is never emptied.

    A measured model that passed is taken before other newcomers and goes back to
    its measured place: behind the measured models that stand before it, in front of
    everybody else. Without that, a first model that was withdrawn for a few days
    would come back as the last, and nothing short of a new default would put it
    first again.
    """
    by_model = {row["model"]: row for row in rows}
    kept = [
        model
        for model in current
        if not (model in gone and not by_model.get(model, {}).get("answered"))
    ]
    new = [row for row in rows if row["model"] not in current]
    quick = benchmark.rank([row for row in new if row["verdict"] == "passed"], timeout_seconds)
    quick.sort(key=lambda row: row["model"] not in measured)  # a stable sort: the rest keep rank
    chain = (kept + [row["model"] for row in quick])[:PLACES]
    if len(chain) < PLACES:
        slow = sorted(
            (row for row in new if row["verdict"] == "slow"),
            key=lambda row: (row["answered"] - row["correct"], row["no_answer"], row["median_seconds"]),
        )
        chain += [row["model"] for row in slow[:1]]
    added = [row["model"] for row in quick]
    for place, model in enumerate(measured):
        if model in added and model in chain:
            chain.remove(model)
            before = [chain.index(earlier) for earlier in measured[:place] if earlier in chain]
            chain.insert(max(before, default=-1) + 1, model)
    return chain or list(current)


def in_turn(
    outside: Sequence[str], tested_last: Sequence[str], left_out_last: Sequence[str]
) -> list[str]:
    """The models outside the chain, in the order the next test should take them.

    A test has room for :data:`CANDIDATE_LIMIT` models. Those the last test had no
    room for go first, in the order they have been waiting; then models it never
    saw; then the ones it tested. Taken in this order, every model gets its turn,
    however many OpenRouter lists.
    """

    def turn(model: str) -> tuple[int, int]:
        if model in left_out_last:
            return 0, list(left_out_last).index(model)
        return (2, 0) if model in tested_last else (1, 0)

    return sorted(outside, key=turn)  # a stable sort: OpenRouter's own order breaks ties


def place(rows: Sequence[dict[str, Any]], before: Sequence[str], after: Sequence[str]) -> None:
    """Write into each row what became of its model: kept, added, dropped or no place."""
    for row in rows:
        model = row["model"]
        if model in before:
            row["placing"] = "kept" if model in after else "dropped"
        elif model in after:
            row["placing"] = "added"
        elif row["verdict"] in ("passed", "slow"):
            row["placing"] = "no_place"


def seen_working(rows: Sequence[dict[str, Any]], chain: Sequence[str]) -> bool:
    """True when at least one model of ``chain`` passed in this very test.

    The scheduled job publishes a chain as "tested today" only then. A test in which
    every call was turned away proves nothing about the chain.
    """
    return any(row["model"] in chain and row["verdict"] in ("passed", "slow") for row in rows)

"""Tests for the browser playground (``app.playground``).

Like the main suite these run offline. The OpenRouter client is the same fake, so
the real dispatcher, the application's exception handlers and the HTTP layer all
execute; only the model's answer is canned.

The fakes and fixtures come from ``tests/test_extract.py`` rather than being
copied, so both files exercise the service through one seam.

Layout:

* Page and configuration   - the home page, its assets, the config endpoint, and
                              what the page's script does when the service has
                              been put to sleep or refuses a run.
* Scenario catalogue        - the documents and checks the page offers, and the
                              benchmark snapshot shown next to the chain.
* Simulated failures        - each fault fails the way a real model could.
* Traced extraction         - the NDJSON stream behind every run on the page.
* Test-suite runner         - the panel that runs pytest (never recursively).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, get_args

import httpx
import openai
import pytest

from app import budget, extractors, model_catalogue
from app.budget import MINUTE_CEILING, UNKNOWN_LIVE_CALLS, CallLimitError
from app.config import Settings
from app.extractors import (
    ContentBlockedError,
    ExtractionDispatcher,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    SchemaValidationError,
    UnparseableTextError,
)
from app.main import MAX_BODY_BYTES, app, create_app
from app.playground import simulation, suite
from app.playground.catalogue import BENCHMARK, BENCHMARK_SNAPSHOT, GROUPS, scenarios_for
from app.playground.routes import STATIC_DIR
from app.playground.simulation import FAULTS, FaultId, FaultyExtractor
from app.schemas import ErrorResponse, HealthResponse
from tests.conftest import FALLBACK_MODEL, FINAL_MODEL, MODEL_CHAIN, PRIMARY_MODEL
from tests.test_extract import (  # noqa: F401 - `http` and `router` are fixtures
    EMPTY_INVOICE_PAYLOAD,
    GARBAGE_TEXT,
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    api_error,
    http,
    ok,
    router,
    timeout_error,
)

PAGE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC_DIR / "assets" / "app.js").read_text(encoding="utf-8")
STYLES = (STATIC_DIR / "assets" / "app.css").read_text(encoding="utf-8")

CHECK_KINDS = {
    "status",
    "field",
    "contains",
    "count",
    "answered_by",
    "calls",
    "verdicts",
    "attempt",
    "header",
}


def events_of(response: httpx.Response) -> list[dict[str, Any]]:
    """The NDJSON stream of ``/playground/extract`` as a list of events."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines() if line]


def result_of(response: httpx.Response) -> dict[str, Any]:
    events = events_of(response)
    assert [event["event"] for event in events].count("result") == 1
    assert events[-1]["event"] == "result"
    return events[-1]


def attempts_of(response: httpx.Response) -> list[dict[str, Any]]:
    return [event for event in events_of(response) if event["event"] == "attempt_finished"]


# ---------------------------------------------------------------------------
# Playground - page and configuration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_page_is_served_under_a_strict_content_security_policy(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Invoice Extraction Service</title>" in response.text
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy and "style-src 'self'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy


def test_page_loads_nothing_the_policy_would_block() -> None:
    """Inline scripts and styles are refused by the CSP, so the page must have none."""
    assert "<style" not in PAGE
    assert ' style="' not in PAGE
    scripts = re.findall(r"<script\b[^>]*>", PAGE)
    assert scripts and all("src=" in tag for tag in scripts)

    urls = re.findall(r'(?:src|href)="([^"]+)"', PAGE)
    assert urls
    assert not [url for url in urls if re.match(r"(?:[a-z]+:)?//", url)], "external URL in page"


@pytest.mark.asyncio
async def test_assets_are_served_and_must_be_revalidated(http: httpx.AsyncClient) -> None:
    assets = sorted(set(re.findall(r'"(/playground/assets/[^"]+)"', PAGE)))
    assert {Path(asset).suffix for asset in assets} >= {".css", ".js", ".svg"}

    for asset in assets:
        response = await http.get(asset)
        assert response.status_code == 200, asset
        # Without this an edited stylesheet or script could be served stale.
        assert response.headers["cache-control"] == "no-cache", asset


@pytest.mark.asyncio
async def test_missing_asset_uses_the_error_envelope(http: httpx.AsyncClient) -> None:
    response = await http.get("/playground/assets/nope.css")

    assert response.status_code == 404
    assert response.json()["error"] == "http_error"


def test_page_script_builds_the_dom_without_html_strings() -> None:
    """Model output and error messages are untrusted, so they must never be parsed as HTML."""
    for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in SCRIPT, f"{sink} would let a response inject markup"


def test_page_does_not_tell_visitors_to_change_settings() -> None:
    """The page is read by visitors. How to fix a setting belongs in the server log."""
    for text in (PAGE, SCRIPT):
        assert "OPENROUTER_" not in text, "a setting is named on the page"


def test_page_script_reads_a_listing_the_way_the_service_sends_it() -> None:
    """A misspelt key or status would not raise anywhere: the chip would just never show."""
    keys = set(re.findall(r"\blisting\??\.([a-z_]+)", SCRIPT))
    assert keys == {"status", "expires"}, sorted(keys)

    compared = set(re.findall(r'listing\?\.status === "([a-z_]+)"', SCRIPT))
    assert compared and compared <= set(get_args(model_catalogue.Status)), sorted(compared)


def test_every_element_the_script_looks_up_is_in_the_page() -> None:
    """A renamed id makes the script fail on null, and the page stops half drawn."""
    looked_up = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"', SCRIPT))
    present = set(re.findall(r'\bid="([^"]+)"', PAGE))
    assert looked_up and looked_up <= present, sorted(looked_up - present)


def test_every_icon_the_script_uses_is_in_the_sprite() -> None:
    sprite = set(re.findall(r'<symbol id="i-([a-z-]+)"', PAGE))
    tone_icons = re.search(r"const TONE_ICON = \{([^}]*)\}", SCRIPT)
    assert tone_icons is not None
    used = (
        set(re.findall(r'\bicon\("([a-z-]+)"', SCRIPT))
        | set(re.findall(r'iconName: "([a-z-]+)"', SCRIPT))
        | set(re.findall(r': "([a-z-]+)"', tone_icons.group(1)))
        | set(re.findall(r'href="#i-([a-z-]+)"', PAGE))
    )
    assert used and used <= sprite, sorted(used - sprite)


@pytest.mark.asyncio
async def test_playground_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYGROUND_ENABLED", "false")
    extractors.reset_extractors()
    api_only = create_app()

    transport = httpx.ASGITransport(app=api_only)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for path in ("/", "/playground/config", "/playground/assets/app.css"):
            response = await client.get(path)
            assert response.status_code == 404, path
            assert response.json()["error"] == "http_error"
        assert (await client.post("/playground/extract", json={})).status_code == 404
        # The API itself is untouched.
        assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_config_describes_the_chain_and_the_catalogue(http: httpx.AsyncClient) -> None:
    response = await http.get("/playground/config")

    assert response.status_code == 200
    config = response.json()
    assert [entry["model"] for entry in config["chain"]] == MODEL_CHAIN
    assert [entry["role"] for entry in config["chain"]] == ["primary", "fallback", "fallback"]
    assert all(entry["configured"] for entry in config["chain"])
    assert [fault["id"] for fault in config["faults"]] == list(FAULTS)
    assert config["limits"]["min_chars"] == 10
    assert config["limits"]["max_chars"] == 50_000
    assert config["limits"]["timeout_seconds"] == 5
    assert config["service"]["environment"] == "test"
    assert config["service"]["docs_url"] == "/docs"
    assert config["scenarios"] == scenarios_for(len(MODEL_CHAIN))
    # Everything the page unpacks from the snapshot, not a sample of it: a missing
    # key stops the page's script, and nothing else would notice.
    assert config["benchmark"] == json.loads(
        json.dumps({**BENCHMARK_SNAPSHOT, "rows": list(BENCHMARK)})
    )
    measured = {row["model"]: row for row in BENCHMARK}
    assert [entry["benchmark"] for entry in config["chain"]] == [
        measured.get(model) for model in MODEL_CHAIN
    ]
    assert any(entry["benchmark"] for entry in config["chain"])  # this chain has a measured model
    assert config["suite"] == {"available": True, "reason": None}


@pytest.mark.asyncio
async def test_config_never_exposes_the_api_key(http: httpx.AsyncClient) -> None:
    response = await http.get("/playground/config")
    assert "test-openrouter-key" not in response.text


# The next tests hold the page to what it does when its host has put the service to
# sleep, and when the ceiling on live calls refuses a run. The script is not run here,
# so they read it, as the tests above do: its figures, its tables, and which function
# does what. A test that trips after an edit names the rule to keep. What the script
# does with those rules is held by tests/test_page_behaviour.py, which runs it.

#: The name of an environment variable, such as a setting: capitals joined by underscores.
SETTING_NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def script_functions() -> dict[str, str]:
    """Every top-level function of the page script, by name, with its whole text.

    The script is laid out so that such a function starts at the left margin and ends at
    the first closing brace that stands alone there.
    """
    found = re.finditer(
        r"^(?:async )?function (\w+)\(.*?^\}$", SCRIPT, re.DOTALL | re.MULTILINE
    )
    return {match.group(1): match.group(0) for match in found}


def script_number(name: str) -> int:
    """A figure the page script defines at its top level, such as a delay."""
    match = re.search(rf"^const {name} = (\d+);", SCRIPT, re.MULTILINE)
    assert match is not None, f"the page script does not define {name}"
    return int(match.group(1))


def test_page_waits_for_a_sleeping_service_on_the_agreed_clock() -> None:
    """One question every 3 seconds, for 3 minutes, and a word after 5 seconds of silence."""
    every = script_number("HEALTH_EVERY_MS")
    give_up = script_number("GIVE_UP_AFTER_MS")
    assert (every, give_up, script_number("SLOW_START_MS")) == (3_000, 180_000, 5_000)

    waiting = script_functions()["waitForService"]
    assert 'readJSON("/health"' in waiting
    assert "setInterval(ask, HEALTH_EVERY_MS)" in waiting
    # The 3 minutes count from when the visitor's wait began, not from the first question.
    assert "const left = since + GIVE_UP_AFTER_MS - performance.now();" in waiting
    assert "setTimeout(() => finish(null), left)" in waiting
    # The sentences a visitor reads give the same figures as the timers.
    assert f"asks every {every // 1000} seconds" in SCRIPT
    assert f"for {give_up // 60_000} minutes at most" in SCRIPT
    assert f"has not answered for {give_up // 60_000} minutes" in SCRIPT


def test_page_asks_the_service_only_while_something_is_happening() -> None:
    """Every request keeps a sleeping host awake, so nothing may repeat without an end."""
    functions = script_functions()

    def users(call: str) -> set[str]:
        return {name for name, text in functions.items() if call in text}

    # Four places send a request, and no fifth is hidden outside a function.
    senders = users("fetch(")
    assert senders == {"readJSON", "send", "runSuite", "startModelsTest"}, (
        f"requests are now sent from {sorted(senders)}: check that nothing repeats the new "
        "one while the page is idle, then name it here"
    )
    assert SCRIPT.count("fetch(") == 4

    # Two timers repeat. One redraws the clocks on the page and asks for nothing.
    repeating = re.findall(r"setInterval\(([^)]*)\)", SCRIPT)
    assert repeating == ["tickTimers, 100", "ask, HEALTH_EVERY_MS"], (
        f"timers that repeat: {repeating}. One that leads to a request keeps the host awake"
    )
    asks = (
        r"\b(?:fetch|readJSON|readPage|load|send|execute|waitForService|refreshHealth"
        r"|watchModels|touchUpkeep)\("
    )
    assert not re.search(asks, functions["tickTimers"]), "the page's clock must not ask the service"

    # The other asks /health while the page waits for the service, and every way out of
    # that wait stops it: an answer, the 3 minutes, a visitor who stops.
    waiting = functions["waitForService"]
    finish = waiting[waiting.index("const finish") : waiting.index("const ask =")]
    assert "clearInterval(every);" in finish and "asking.abort();" in finish
    assert waiting.count("finish(") == 3

    # The wait starts from a page load or from a run, never by itself. It says for how
    # long it lasts, so that the looks at the chain's upkeep can stand back meanwhile.
    assert users("waitForService(") == {"waitForService", "load", "execute"}
    assert waiting.index("waitingForService = true;") < waiting.index("new Promise(")
    assert "waitingForService = false;" in finish
    assert SCRIPT.count("waitingForService = ") == 3  # declared, set and put back
    assert users("readPage(") == {"readPage", "load"}
    assert users("readJSON(") == {
        "readJSON", "readPage", "waitForService", "refreshHealth", "watchModels", "followChain",
    }

    # The third thing that asks again is the look at the chain's upkeep. Its next look is
    # arranged in one place: 2 seconds later while the service is busy, 20 seconds later
    # for 10 minutes after the visitor last did something, else not at all.
    busy, idle, lasting = (script_number(f"LOOK_{name}_MS") for name in ("BUSY", "IDLE", "FOR"))
    assert (busy, idle, lasting) == (2_000, 20_000, 600_000)
    looking = functions["watchModels"]
    again = (
        "if (hot || now < upkeep.activeUntil) "
        "upkeep.timer = setTimeout(watchModels, hot ? LOOK_BUSY_MS : LOOK_IDLE_MS);"
    )
    assert again in functions["lookLater"] and SCRIPT.count("setTimeout(watchModels") == 1
    assert users("lookLater(") == {"lookLater", "watchModels", "wireUpkeep"}
    # A tab nobody is looking at asks for nothing, and neither does a page that is waiting
    # for the service already.
    asking = looking.index('readJSON("/playground/models")')
    assert looking.index("if (document.hidden || waitingForService) return;") < asking
    # A look that got no answer does not end looking, and starts nothing of its own
    # either: no wait, no timer, no second look. The next look is arranged where every
    # look is, and never as often as while the service is busy: a page that has lost
    # touch sees nothing going on.
    start, end = "if (!isPicture(picture)) {", "const before = upkeep.picture;"
    unanswered = looking[looking.index(start) : looking.index(end)]
    for kept in ("upkeep.lost = true;", "lookLater();", "return;"):
        assert kept in unanswered, kept
    for started in ("setTimeout", "setInterval", "waitForService", "watchModels", "readJSON"):
        assert started not in unanswered, started
    assert "const hot = !upkeep.lost && (" in functions["lookLater"]
    # A look starts from something the visitor did or from the look before it.
    assert users("watchModels(") == {"watchModels", "touchUpkeep", "wireUpkeep"}
    assert users("touchUpkeep(") == {
        "touchUpkeep", "execute", "startModelsTest", "replayModelsTest", "wireUpkeep",
    }
    wiring = functions["wireUpkeep"]
    assert '"visibilitychange"' in wiring and '["pointerdown", "keydown"]' in wiring
    assert users("upkeep.activeUntil = Date.now() + LOOK_FOR_MS") == {"touchUpkeep", "wireUpkeep"}


@pytest.mark.asyncio
async def test_page_tells_the_service_from_its_host_by_fields_the_service_sends(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the service sleeps its host answers in its place, with any status.

    The script knows the service's own answers by a few fields. A field the service
    stopped sending would leave the page waiting for a service that is up.
    """

    def fields(check: str) -> set[str]:
        line = re.search(rf"^const {check} = \(body\) => (.+);$", SCRIPT, re.MULTILINE)
        assert line is not None, f"the page script does not define {check}"
        return set(re.findall(r"body\??\.(\w+)", line.group(1)))

    assert fields("isHealth") == {"providers"} <= set(HealthResponse.model_fields)
    assert fields("isEnvelope") == {"success", "error"} <= set(ErrorResponse.model_fields)
    config = (await http.get("/playground/config")).json()
    assert fields("isConfig") == {"chain", "scenarios"} <= set(config)
    picture = (await http.get("/playground/models")).json()
    assert fields("isPicture") == {"chain", "mode"} <= set(picture)

    # The real answers have them in the shape the script asks for.
    assert isinstance(config["chain"], list) and isinstance(config["scenarios"], list)
    assert isinstance(picture["chain"], list) and isinstance(picture["mode"], str)
    refused = (await http.post("/playground/extract", json={"text": "too short"})).json()
    assert refused["success"] is False and isinstance(refused["error"], str)
    # /health is the service's own answer with a 503 as well, so the status is not asked.
    monkeypatch.delenv("OPENROUTER_API_KEY")
    extractors.reset_extractors()
    health = await http.get("/health")
    assert health.status_code == 503 and isinstance(health.json()["providers"], list)
    assert "response.ok" not in script_functions()["readJSON"]


def test_a_hosts_answer_is_never_shown_as_the_result_of_a_run() -> None:
    """Only the stream's result, or the service's error envelope, gives a run a status."""
    functions = script_functions()
    assert re.findall(r"\brun\.status = (\S+);", SCRIPT) == ["response.status", "event.status_code"]
    sending = functions["send"]
    envelope = sending.index("if (isEnvelope(body)) {")
    assert envelope < sending.index("run.status = response.status;")
    assert sending.rstrip().endswith("return run.status != null;\n}")
    assert 'event.event === "result"' in functions["applyEvent"]

    # The sentences that used to stand for "something else came back" are gone.
    for words in ("Unexpected response", "Is it still running", "could not load its configuration"):
        assert words not in SCRIPT, words


def test_a_run_that_never_reached_the_service_is_sent_again_once() -> None:
    """Once the service is back the page sends such a run again by itself: once, and it says so.

    Only such a run. One that the service had taken may have called a model already, so
    the page does not repeat it on its own: it says that the connection broke off.
    """
    functions = script_functions()
    running = functions["execute"]
    sends = [match.start() for match in re.finditer(r"await send\(run, signal\)", running)]
    assert len(sends) == 2, "the visitor's send, and one resend"
    assert sends[0] < running.index("await waitForService(signal, pressedAt)") < sends[1]
    assert not re.search(r"\b(?:while|for) \(", running), "a loop here could send without end"

    # The resend is for a run of which nothing reached the service, and for no other.
    waits = "if (!answered && !run.reached && !signal.aborted) {"
    waited = running.index("await waitForService(signal, pressedAt)")
    assert sends[0] < running.index(waits) < waited
    # The service's own stream is what says that it has the run: nothing else sets the mark.
    assert SCRIPT.count("run.reached = true;") == 1
    sending = functions["send"]
    stream = sending.index('.includes("ndjson")) {')
    assert stream < sending.index("run.reached = true;") < sending.index("await readEvents(")
    # Such a run ends with a sentence of its own: the connection broke off, or the page
    # itself stopped waiting after too long a silence.
    ended = (
        "else if (!answered && !run.problem) "
        "run.problem = run.quiet ? WENT_QUIET(silenceLimit()) : CUT_OFF;"
    )
    assert ended in running
    assert "reached: false" not in sending  # the second send must not forget the first

    # The resend is marked on the run, and the outcome says so whatever came back.
    assert running.index("run.resent = true;") < sends[1]
    assert "if (run.resent) notes.unshift(SENT_AGAIN);" in functions["outcomeBlock"]
    assert re.search(r'^const SENT_AGAIN =\s+"[^"]*sent your run again\.";$', SCRIPT, re.MULTILINE)


def test_a_request_the_host_holds_gets_one_line_and_no_second_send() -> None:
    """A waking host may hold a request for a minute and then deliver it: say so, resend nothing."""
    functions = script_functions()
    sending = functions["send"]
    marked = re.search(
        r"const slow = setTimeout\(\(\) => \{\s+run\.slow = true;\s+showStatus\(\"waiting\"\);\s+"
        r"renderResult\(\);\s+\}, SLOW_START_MS\);",
        sending,
    )
    assert marked is not None, "send() no longer marks a run that got no answer for 5 seconds"
    # The service's own headers take the line and the pill's word away again, and so does
    # any other end.
    heard = sending[sending.index("const heard") : sending.index("// A request that is held")]
    assert "clearTimeout(slow);" in heard and "run.slow = false;" in heard
    assert "if (run.slow) showStatus();" in heard
    stream = sending.index('.includes("ndjson")) {')
    assert stream < sending.index("heard();") < sending.index("await readEvents(")
    assert sending.count("heard();") == 2 and "finally {\n    heard();" in sending
    assert "run.slow ?" in functions["outcomeOf"]
    # What every sentence about the sleep says: why, and how long waking takes.
    why = re.search(r'^const WHY_ASLEEP =\s+"([^"]+)";$', SCRIPT, re.MULTILINE)
    assert why is not None
    assert "nobody has visited for a while" in why.group(1) and "about a minute" in why.group(1)
    # Three places say it: a run that is held, and through one sentence a first load and a
    # run left unanswered.
    assert SCRIPT.count("${WHY_ASLEEP}") == 2 and SCRIPT.count("${probably()}") == 2


def test_first_load_waits_for_the_service_instead_of_stopping() -> None:
    """A first load that finds the service asleep retries as a run does, and can be retried."""
    loading = script_functions()["load"]
    first, again = (match.start() for match in re.finditer(r"await readPage\(\)", loading))
    # The wait is given the moment the load began: 3 minutes in all, however they are spent.
    assert loading.index("const since = performance.now();") < first
    assert first < loading.index("await waitForService(undefined, since)") < again
    # A host may hold the first requests: after 5 s the page says why it is still empty.
    assert "setTimeout(sayWaking, SLOW_START_MS)" in loading
    saying = script_functions()["sayWaking"]
    assert 'showStatus("waiting")' in saying
    # Nothing on the page says "Loading" while it waits, or after it has given up.
    assert 'pending("Waiting for the demo");' in saying and 'pending("Not loaded");' in loading
    assert PAGE.count("data-pending>Loading<") == 3 and PAGE.count(">Loading<") == 3
    # And nothing can be typed into a page that is not there yet, to be thrown away.
    assert re.search(r'<textarea id="document"[^>]*\breadonly\b', PAGE)
    # Giving up leaves a sentence and a button that starts the load again.
    retry = (
        r'showNotice\("crit", awake \? NOT_LOADING : NOT_WAKING, \(\) => \{'
        r"\s+sayWaking\(\);\s+load\(\);\s+\}\);"
    )
    assert re.search(retry, loading), "the notice that gives up no longer starts the load again"
    assert '"Try again"' in script_functions()["showNotice"]
    for sentence in ("NOT_WAKING", "NOT_LOADING", "NO_ANSWER_TWICE", "CUT_OFF"):
        words = re.search(rf'^const {sentence} =\s+"([^"]+)";$', SCRIPT, re.MULTILINE)
        assert words is not None, sentence
        assert words.group(1).endswith("please contact the admin."), sentence


def test_page_claims_nothing_it_cannot_know_about_a_service_that_does_not_answer() -> None:
    """The same page runs on a developer's machine, where "no answer" is a stopped server.

    So the page says what it saw (no answer) and what is probable (asleep), in one voice:
    "the demo", never two names for the one thing a visitor is using.
    """
    for text in (PAGE, SCRIPT):
        assert "live demo" not in text
    # The sentences that say why, each with its hedge. A first load and a run share one,
    # which does not guess at a sleeping demo when the browser says that it is offline.
    guess = re.search(r"^const probably = \(\) =>\n(.+);$", SCRIPT, re.MULTILINE)
    assert guess is not None and "navigator.onLine === false" in guess.group(1)
    assert guess.group(1).count("so it has probably gone to sleep") == 1
    assert "and this device seems to be offline." in guess.group(1)
    assert SCRIPT.count("so it has probably gone to sleep") == 1
    assert SCRIPT.count("so the demo may be waking up") == 1  # a request that is held
    assert SCRIPT.count("probably because it was asleep") == 1  # a run sent a second time
    # And none of the ways of saying it as a fact.
    for claim in ("had gone to sleep", "is waking up", "woke up", "is awake", "Waking up"):
        assert claim not in SCRIPT, claim


def test_status_pill_has_words_and_a_dot_for_every_state() -> None:
    """The pill at the top shows the wait and the giving up, next to what the service reports."""
    table = re.search(r"^const STATUS_WORDS = \{\n(.*?)^\};", SCRIPT, re.DOTALL | re.MULTILINE)
    assert table is not None
    words = dict(re.findall(r'^\s*(\w+): "([^"]+)",$', table.group(1), re.MULTILINE))

    reported = set(get_args(HealthResponse.model_fields["status"].annotation))
    assert set(words) == reported | {"withdrawn", "waiting", "offline"}
    assert (words["waiting"], words["offline"]) == ("No answer yet", "Not answering")
    assert words["withdrawn"] == "Models withdrawn"
    for state in words:
        assert f'.status[data-state="{state}"] .status__dot' in STYLES, state
    # One function decides what the pill says, from all the page knows: that the service
    # is not answering (while the page still asks, and once it has stopped), else what the
    # service says of itself.
    functions = script_functions()
    shown = 'setStatus(away ?? (upkeep.withdrawn ? "withdrawn" : state.health.status));'
    assert shown in functions["showStatus"]
    assert len(re.findall(r"(?<!function )setStatus\(", SCRIPT)) == 1
    assert set(re.findall(r'showStatus\("(\w+)"\)', SCRIPT)) == {"waiting", "offline"}
    assert "dataset.state" not in SCRIPT.replace(functions["setStatus"], "")
    # A look that got no answer says so in the pill as well, where the page had the
    # service before and no wait has the pill: "yet" for as long as another look is arranged.
    unanswered = (
        "if (upkeep.picture && !waitingForService) "
        'showStatus(upkeep.timer == null ? "offline" : "waiting");'
    )
    assert unanswered in functions["watchModels"]

    # Any answer of the service's own puts its word back: a run, a look, a read of /health.
    def users(call: str) -> set[str]:
        return {name for name, text in functions.items() if call in text}

    assert users("showStatus();") == {
        "showHealth", "refreshHealth", "execute", "send", "watchModels", "wireUpkeep",
    }


#: Noon UTC, twelve hours from the midnight at which the day's count starts again.
NOON_UTC = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("ceiling", ["minute", "day", "day unknown"])
@pytest.mark.asyncio
async def test_page_reads_a_refusal_by_the_ceiling_where_the_service_puts_it(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    allowance: Callable[..., list[None]],
    monkeypatch: pytest.MonkeyPatch,
    ceiling: str,
) -> None:
    """The page knows this refusal from "every model failed" by three things. Hold all three.

    The refusal is the real one: the service's own budget raises it, with its clock held
    at noon UTC, and the service's own handler writes the envelope. A fourth thing, the
    length of the wait, tells the page "wait a moment" from "today's share is used up".
    """
    monkeypatch.setattr(budget, "_now", lambda: NOON_UTC)
    router(default=ok())
    if ceiling == "minute":
        for _ in range(MINUTE_CEILING):
            budget.count()
    elif ceiling == "day":
        allowance(remaining=0, limit=50)
    else:
        allowance(None)
        monkeypatch.setattr(budget, "_unknown_calls", UNKNOWN_LIVE_CALLS)

    events = events_of(await http.post("/playground/extract", json={"text": SAMPLE_INVOICE_TEXT}))

    # It arrives as the last event of an ordinary stream, and that is where the page reads it.
    assert [event["event"] for event in events] == ["start", "result"]
    result = events[-1]
    functions = script_functions()
    assert "run.headers = event.headers ?? {};" in functions["applyEvent"]

    named = re.search(r'^const REFUSAL_TYPE = "(\w+)";$', SCRIPT, re.MULTILINE)
    assert named is not None and named.group(1) == CallLimitError.__name__
    reading = functions["refusalOf"]
    assert "run.status !== 503" in reading and result["status_code"] == 503
    assert "run.body?.detail?.error_type !== REFUSAL_TYPE" in reading
    assert result["body"]["detail"]["error_type"] == named.group(1)
    assert 'Number(run.headers["retry-after"])' in reading
    wait = int(result["headers"]["retry-after"])

    # When waiting helps the service never asks for more than a minute; a used-up day
    # asks for the time to midnight UTC. The page's dividing line is the service's own.
    assert "today: seconds > SHORT_WAIT_SECONDS" in reading
    longest_short_wait = script_number("SHORT_WAIT_SECONDS")
    assert longest_short_wait == budget.MINUTE_SECONDS >= budget.READ_EVERY_SECONDS
    assert wait == {"minute": 60, "day": 12 * 60 * 60, "day unknown": 60}[ceiling]
    assert (wait > longest_short_wait) == (ceiling == "day")

    # The outcome says how long to wait, either way, and what still works meanwhile. The
    # page cannot tell a full minute from a day that cannot be read, so its own words fit
    # both, and the service's own sentence stands under them.
    told = functions["refusalOutcome"]
    assert told.count("countdown(until)") == 2 and told.count("SIMULATED_STILL_WORK") == 2
    assert "per minute" not in told
    shown = functions["bodyBlock"]
    every_model_failed = shown.index('if (body.error === "providers_unavailable")')
    box = shown[shown.index("if (run.refusal) {") : every_model_failed]
    assert 'h("p", null, body.message),' in box and result["body"]["message"]


@pytest.mark.asyncio
async def test_every_answer_given_before_a_run_starts_is_one_the_page_knows(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused by validation, by the size limit or by the guard, a run gets no stream.

    The page takes such an answer for the service's own only when it is the error
    envelope. Anything else it takes for a host answering in the place of a sleeping
    service: it would wait for a service that is awake, then send the run a second time.
    """
    # In production the guard refuses a caller that is no browser.
    monkeypatch.setenv("ENVIRONMENT", "production")
    extractors.reset_extractors()
    path = "/playground/extract"
    as_the_page = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
    too_large = b'{"text": "' + b"x" * MAX_BODY_BYTES + b'"}'
    refused = {
        "too short": await http.post(path, content=b'{"text": "hi"}', headers=as_the_page),
        "too large": await http.post(path, content=too_large, headers=as_the_page),
        "not the page": await http.post(path, json={"text": SAMPLE_INVOICE_TEXT}),
    }

    statuses = {name: response.status_code for name, response in refused.items()}
    assert statuses == {"too short": 422, "too large": 422, "not the page": 403}
    for name, response in refused.items():
        body = response.json()
        assert response.headers["content-type"] == "application/json", name
        # The two fields isEnvelope asks for, in the shape it asks for them.
        assert body["success"] is False and isinstance(body["error"], str), name
        # None of them names a wait: only the last event of a stream does that.
        assert "retry-after" not in response.headers, name


def test_run_all_waits_out_a_refusal_twice_and_stops_for_the_day() -> None:
    """A refusal in the middle of "Run all" must not become the result of every later scenario."""
    assert script_number("BATCH_WAITS") == 2
    functions = script_functions()
    batch = functions["runAll"]
    loop = re.search(r"for \(let waits = 0; (.+); waits \+= 1\) \{", batch)
    assert loop is not None, "the loop that waits out a refusal was not found"
    assert set(loop.group(1).split(" && ")) == {
        "waits < BATCH_WAITS",
        "run.refusal",
        "!run.refusal.today",  # the day's share is not waited for
        "!state.batch.stop",
    }
    # It waits for as long as the service said and says that it is waiting; Stop ends
    # the wait at once.
    assert "setTimeout(resolve, run.refusal.until - performance.now());" in batch
    assert "state.batch.skipWait?.();" in batch
    # The list says so with the time left: on a narrow screen the result is far below it.
    waiting = (
        'if (waitUntil) meta.replaceChildren("Waiting ", countdown(waitUntil), '
        "` to run ${place} again`);"
    )
    assert waiting in functions["renderRailHead"]
    assert "state.batch.waitUntil = run.refusal.until;" in batch

    # Still refused, or no answer at all: it stops there with a sentence, before the
    # scenario is counted and before the next one is sent.
    halt = batch.index("if (run.refusal || run.problem) {")
    assert batch.index("state.halt = `Run all stopped: ${why}.`;") > halt
    assert batch.index("break;", halt) < batch.index("state.batch.done += 1;")
    assert "state.halt" in functions["renderRailHead"]
    # And it stops when the service changed its chain meanwhile: the scenarios that are
    # left were written for the chain before.
    assert "state.batch.stop = true;" in functions["followChain"]
    assert batch.index("if (state.batch.stop) break;", loop.end()) < halt


def test_a_run_without_an_answer_is_not_scored_against_its_scenario() -> None:
    """Stopped, unanswered or refused by the ceiling: such a run says nothing about a scenario."""
    scored = 'const isScored = (run) => run?.phase === "done" && !run.problem && !run.refusal;'
    assert scored in SCRIPT
    functions = script_functions()
    assert "scenario && isScored(run) && !run.moved ? scenario.checks.map(" in functions["execute"]
    assert ".filter(isScored)" in functions["renderRailHead"]
    assert "isScored(run) ?" in functions["scenarioButton"]
    # The session card is drawn from the answered runs alone. For each scenario it keeps
    # the last answer: a run refused afterwards takes nothing back.
    session = functions["renderSession"]
    assert "const finished = state.history.filter(isScored);" in session
    assert "runs.findLast((run) => run.key === scenario.id)" in session
    assert "state.runs" not in session


def engine_messages() -> list[str]:
    """Every message the engine or a simulated failure can put in an error.

    Read from the source, because most of them are raised only by a live model: the
    first argument of each ``...Error(...)`` call, with ``{}`` where a value is filled in.
    """
    messages = []
    for module in (extractors, simulation):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not getattr(node.func, "id", "").endswith("Error"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                messages.append(first.value)
            elif isinstance(first, ast.JoinedStr):
                parts = (
                    part.value if isinstance(part, ast.Constant) else "{}" for part in first.values
                )
                messages.append("".join(parts))
    return messages


def test_page_puts_engine_messages_that_name_a_setting_into_visitor_words() -> None:
    """Three engine messages tell the admin which setting to change. A visitor cannot.

    The engine is frozen, so the page rewords them, from one table. This fails when the
    engine gains a message that names a setting and the table has no row for it.
    """
    table = re.search(r"^const VISITOR_WORDS = \[\n(.*?)^\];", SCRIPT, re.DOTALL | re.MULTILINE)
    assert table is not None, "the table of visitor words was not found in the page script"
    rows = re.findall(r'^\s*\["([^"]+)", "([^"]+)"\],$', table.group(1), re.MULTILINE)

    naming = [message for message in engine_messages() if SETTING_NAME.search(message)]
    named = {name for message in naming for name in SETTING_NAME.findall(message)}
    # If these three are not found, the reading above has stopped working.
    assert named >= {"OPENROUTER_API_KEY", "MAX_OUTPUT_TOKENS", "OPENROUTER_STRICT_JSON_SCHEMA"}

    for message in naming:
        covering = [words for part, words in rows if part in message]
        assert covering, f"no row of VISITOR_WORDS covers the engine message: {message}"
    for part, words in rows:
        assert any(part in message for message in naming), f"no engine message has: {part}"
        assert not SETTING_NAME.search(words) and "=" not in words, words
        # A row must not swallow a message that names no setting.
        assert not [m for m in engine_messages() if part in m and m not in naming], part

    # Both places that print an attempt's message go through the table.
    functions = script_functions()
    assert "visitorWords(attempt.message)" in functions["traceRow"]
    assert "visitorWords(real.at(-1).message)" in functions["silentModelNotice"]
    # In traceRow: the test for a message, and the line that prints it.
    assert len(re.findall(r"\.message\b", functions["traceRow"])) == 2
    assert len(re.findall(r"\.message\b", functions["silentModelNotice"])) == 1


def test_no_setting_is_named_on_the_page() -> None:
    """Not as ``OPENROUTER_...``, and not as any other setting's name either."""
    names = {field.upper() for field in Settings.model_fields}
    assert "MAX_OUTPUT_TOKENS" in names
    for text in (PAGE, SCRIPT):
        found = sorted(name for name in names if re.search(rf"\b{name}\b", text))
        assert not found, f"the page names a setting: {found}"


@pytest.mark.asyncio
async def test_the_ceiling_on_live_calls_is_among_the_facts_under_the_headline(
    http: httpx.AsyncClient,
) -> None:
    """A limit that only shows when it refuses somebody is not a capability a visitor can see."""
    limits = (await http.get("/playground/config")).json()["limits"]
    assert limits["calls_per_minute"] == MINUTE_CEILING == 15

    chrome = script_functions()["renderChrome"]
    fact = (
        "facts.push([String(limits.calls_per_minute), "
        '"live model calls a minute, for all visitors together"]);'
    )
    assert fact in chrome
    # Everything the script reads from the limits is something the service sends.
    assert set(re.findall(r"\blimits\.([a-z_]+)", SCRIPT)) <= set(limits)


def test_no_request_of_the_page_can_wait_for_ever() -> None:
    """A host may hold a request and never answer it. Every request has a limit of its own.

    What the page does when a limit runs out is held by tests/test_page_behaviour.py,
    which runs the script. This holds that no request is sent without one.
    """
    functions = script_functions()
    limit = functions["patience"]
    assert "let timer = setTimeout(end, ms);" in limit
    assert 'stop?.addEventListener("abort", end, { once: true });' in limit
    # Every place that sends a request asks for a limit, hands its signal to fetch and
    # puts the timer away when the request is over.
    for sender in ("readJSON", "send", "runSuite", "startModelsTest"):
        text = functions[sender]
        assert re.search(r"const wait = patience\(\w+(?:\(\))?(?:, \w+)?\);", text), sender
        assert text.count("fetch(") == 1 and "signal: wait.signal" in text, sender
        assert text.index("finally {") < text.index("wait.done();"), sender
    # Three minutes for a read, as for every other wait of the page.
    assert "patience(GIVE_UP_AFTER_MS, stop)" in functions["readJSON"]
    # A run may be silent for as long as one model takes to answer, and 90 seconds more
    # for a host that holds the request while it wakes the service. Every event of the
    # stream starts that wait again.
    assert script_number("SILENCE_SPARE_MS") == 90_000
    silence = (
        "const silenceLimit = () => "
        "state.config.limits.timeout_seconds * 1000 + SILENCE_SPARE_MS;"
    )
    assert silence in SCRIPT
    sending = functions["send"]
    assert "const wait = patience(silenceLimit(), signal);" in sending
    reading = sending[sending.index("await readEvents(") : sending.index("} else {")]
    assert "wait.renew();" in reading
    assert "run.quiet = wait.signal.aborted && !signal.aborted;" in sending


def test_a_run_is_told_where_the_visitor_is_looking() -> None:
    """On a narrow screen the result panel is below the request, and the page's top far above."""
    functions = script_functions()
    # A run the visitor starts brings its panel into view. "Run all" is watched from the
    # list, and a run the page sends itself must not move the page under the reader.
    assert "if (!again && !state.batch) showResult();" in functions["execute"]
    assert '$("#result").scrollIntoView({ block: "start" })' in functions["showResult"]
    # The notice about the chain's upkeep stands in that panel, under the run's own
    # outcome, and is no live region of its own: it is read out once per state.
    panel = re.search(
        r'<section class="card panel result" id="result"[^>]*>(.*?)</section>', PAGE, re.DOTALL
    )
    assert panel is not None
    parts = re.findall(r'<div class="[a-z_ ]+" id="([a-z-]+)"([^>]*)>', panel.group(1))
    assert [name for name, _ in parts] == ["result-head", "upkeep-notice", "result-body"]
    assert parts[1][1].strip() == "hidden"
    assert "if (changed(\"said\", notice.title)) announce(" in functions["renderUpkeepNotice"]
    # A countdown that ends, and news about the upkeep, redraw the head of the panel
    # alone: what the visitor opened or focused below it stays as it was.
    assert '$("#result-head").replaceChildren(' in functions["renderOutcome"]
    ticking = functions["tickTimers"]
    assert "renderOutcome();" in ticking and "renderResult();" not in ticking
    assert 'announce("The wait is over. You can try again now.");' in ticking
    assert "renderOutcome();" in functions["renderUpkeep"]
    # When the wait of a refusal is over the run is offered again, whichever wait it was.
    offered = (
        "const again = run.problem || "
        "(run.refusal && run.refusal.until <= performance.now());"
    )
    assert offered in functions["outcomeBlock"]
    past = "Midnight UTC has passed since then, so you can try again now."
    assert past in functions["refusalOutcome"]


def test_small_things_a_visitor_can_trip_over() -> None:
    """Each of these was found by pressing something at the wrong moment."""
    functions = script_functions()
    # The pill is a link to /health. Followed while the page waits, it must not replace the page.
    pill = '<a class="status" id="status" href="/health" target="_blank" rel="noopener"'
    assert pill in PAGE
    # Resetting the document during a run would take the run's result off the page.
    assert '$("#reset").hidden = !isEdited() || locked();' in functions["renderDraftState"]
    # "Try again" is the visitor's own run, whoever sent the run it stands under.
    assert "onclick: () => execute({ ...run, again: false })" in functions["outcomeBlock"]
    # A run without a result does not claim that no model was called.
    assert 'if (run.problem) return "Not known: no result arrived";' in functions["notCalledReason"]
    # The list counts the last answer a scenario got, as the session card does.
    counted = (
        "const finished = scenarios.map("
        "(scenario) => answered.findLast((run) => run.key === scenario.id))"
    )
    assert counted in functions["renderRailHead"]
    # Every link the script writes leads to something that is on the page.
    targets = set(re.findall(r'href: "#([a-z-]+)"', SCRIPT))
    assert targets == {"upkeep", "upkeep-test"}
    assert all(f'id="{target}"' in PAGE for target in targets)


def test_a_failed_run_is_sent_again_once_and_only_for_a_visitor_who_stayed() -> None:
    """The service repairs its chain after a run that no model answered, and the page helps once."""
    functions = script_functions()
    remembered = (
        "if (answered && failedForReal(run) && !again && !state.batch) "
        "upkeep.rerun = { run, at: Date.now() };"
    )
    assert remembered in functions["execute"], "only the visitor's own single run is remembered"
    assert SCRIPT.count("upkeep.rerun = {") == 1
    again = functions["rerunIfRepaired"]
    # Not for a chain that only lost a model, not while something else has the controls,
    # and not for a visitor who went on to another document.
    assert "if (upkeep.picture.chain.every((model) => met.includes(model))) {" in again
    assert "if (!waiting || locked()) return;" in again
    stayed = (
        "state.runs.get(state.view) === run && state.draft.text === run.text "
        "&& !state.draft.simulate.some(Boolean)"
    )
    assert stayed in again and again.index("if (!stayed) return;") < again.index("execute(")
    assert again.count("execute(") == 1 and "again: true" in again
    assert not re.search(r"\b(?:while|for) \(", again), "a loop here could send without end"
    # The result itself says what is going on, and that a run was sent again.
    assert "if (run.again) notes.unshift(REPAIRED_AND_SENT_AGAIN);" in functions["outcomeBlock"]
    assert "notes.push(...upkeepNotes(run));" in functions["outcomeBlock"]


# ---------------------------------------------------------------------------
# Playground - scenario catalogue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chain_length", [1, 2, 3, 5])
def test_scenarios_are_well_formed(chain_length: int) -> None:
    scenarios = scenarios_for(chain_length)
    groups = {group["id"] for group in GROUPS}

    assert len({scenario["id"] for scenario in scenarios}) == len(scenarios)
    for scenario in scenarios:
        name = scenario["id"]
        assert scenario["group"] in groups, name
        assert scenario["title"] and scenario["summary"] and scenario["covers"], name
        # One entry per model: a known fault, or None for a live call.
        assert len(scenario["simulate"]) == chain_length, name
        assert all(fault is None or fault in FAULTS for fault in scenario["simulate"]), name
        assert 0 <= scenario["live_calls"] <= scenario["simulate"].count(None), name
        assert scenario["checks"], name
        assert scenario["checks"][0]["kind"] == "status", f"{name}: the status check leads"
        for check in scenario["checks"]:
            assert check["kind"] in CHECK_KINDS and check["label"], name


def test_only_the_too_short_scenario_fails_request_validation() -> None:
    for scenario in scenarios_for(3):
        length = len(scenario["text"].strip())
        if scenario["id"] == "too-short":
            assert length < 10
            assert scenario["checks"][0]["expected"] == 422
        else:
            assert 10 <= length <= 50_000, scenario["id"]


def test_scenarios_that_need_a_fallback_are_left_out_of_shorter_chains() -> None:
    def ids(chain_length: int) -> set[str]:
        return {scenario["id"] for scenario in scenarios_for(chain_length)}

    assert "primary-rate-limited" not in ids(1)
    assert "primary-rate-limited" in ids(2)
    assert "last-model-answers" not in ids(2)
    assert {"last-model-answers", "verdict-beats-failure"} <= ids(3)
    # Scenarios that fail every model make sense for any chain.
    assert {"all-models-fail", "api-key-rejected"} <= ids(1)


def test_every_test_a_scenario_points_to_exists() -> None:
    """``covers`` tells the reader where the offline counterpart lives; keep it true."""
    suite_source = (Path(__file__).parent / "test_extract.py").read_text(encoding="utf-8")
    referenced = {
        name
        for scenario in scenarios_for(3)
        for name in re.findall(r"\btest_\w+", scenario["covers"])
    }
    assert referenced
    for name in sorted(referenced):
        assert f"def {name}(" in suite_source, name


def test_fault_catalogue_matches_the_request_schema() -> None:
    assert set(get_args(FaultId)) == set(FAULTS)
    assert all(fault.label and fault.summary for fault in FAULTS.values())


def test_a_two_model_chain_says_what_its_demos_depend_on() -> None:
    """With two models the "primary fails" demos lean on one live model, and say so."""

    def summaries(chain_length: int) -> dict[str, str]:
        return {scenario["id"]: scenario["summary"] for scenario in scenarios_for(chain_length)}

    two, three = summaries(2), summaries(3)
    for name in ("primary-rate-limited", "primary-prose", "primary-timeout"):
        assert "no third model to try, and the run ends in a 503" in two[name], name
        assert "no third model to try" not in three[name], name

    # A verdict outranks a failure: if the live model fails after the staged verdict,
    # the run ends as a refusal, not as "every model failed".
    assert "the run ends in a 422" in two["wrong-verdict"]
    assert "the run ends in a" not in three["wrong-verdict"]

    # The refusal needs a second opinion, and with two models that is one live call
    # that can fail. With one model nobody is left to agree or disagree.
    assert 'the "Two models agree" check goes red' in two["not-an-invoice"]
    assert "its verdict is final" in summaries(1)["not-an-invoice"]
    assert "once two agree" in three["not-an-invoice"]


BENCHMARK_ROW_KEYS = {
    "model",
    "calls",
    "no_answer",
    "answered",
    "correct",
    "median_seconds",
    "slowest_seconds",
    "note",
}


def test_benchmark_snapshot_is_dated_and_adds_up() -> None:
    """A table of results without its date and its source is how the old one went stale."""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", BENCHMARK_SNAPSHOT["measured"])
    assert BENCHMARK_SNAPSHOT["records"], "name the saved records the figures come from"
    per_model = BENCHMARK_SNAPSHOT["passes"] * BENCHMARK_SNAPSHOT["cases"]

    assert len({row["model"] for row in BENCHMARK}) == len(BENCHMARK)
    for row in BENCHMARK:
        name = row["model"]
        assert set(row) == BENCHMARK_ROW_KEYS, name
        assert row["calls"] == per_model, name
        assert row["answered"] == row["calls"] - row["no_answer"], name
        assert 0 <= row["correct"] <= row["answered"], name
        # A model that never answered has no answer time to report.
        timed = row["answered"] > 0
        assert (row["median_seconds"] is not None) == timed, name
        assert (row["slowest_seconds"] is not None) == timed, name
        if timed:
            assert 0 < row["median_seconds"] <= row["slowest_seconds"], name
            assert row["slowest_seconds"] <= BENCHMARK_SNAPSHOT["timeout_seconds"] + 1, name


def test_page_script_reads_only_what_the_benchmark_snapshot_has() -> None:
    """A renamed key would not fail anywhere else: the page would just print "undefined"."""
    section = re.search(r"// -+ benchmark ---\n(.*?)\n// -+ tooltip ---", SCRIPT, re.DOTALL)
    assert section is not None, "the benchmark part of the page script was not found"

    row_keys = set(re.findall(r"\brow\.([a-z_]+)", section.group(1)))
    assert row_keys and row_keys <= BENCHMARK_ROW_KEYS, sorted(row_keys - BENCHMARK_ROW_KEYS)

    unpacked = re.search(r"const \{([^}]*)\} = state\.config\.benchmark;", section.group(1))
    assert unpacked is not None
    snapshot_keys = {part.split(":")[0].strip() for part in unpacked.group(1).split(",")}
    assert snapshot_keys - {"rows"} <= set(BENCHMARK_SNAPSHOT), sorted(snapshot_keys)


# ---------------------------------------------------------------------------
# Playground - simulated failures
# ---------------------------------------------------------------------------


#: fault id -> the exception the pipeline raises for it, and whether a retry could help.
FAULT_CASES: list[tuple[str, type, bool]] = [
    ("rate_limit", ProviderRateLimitError, True),
    ("upstream_error", ProviderError, True),
    ("timeout", ProviderTimeoutError, True),
    ("invalid_json", SchemaValidationError, False),
    ("schema_mismatch", SchemaValidationError, False),
    ("truncated", ContentBlockedError, False),
    ("no_invoice", UnparseableTextError, False),
    ("auth", ProviderAuthError, False),
]


@pytest.mark.parametrize(("fault_id", "expected", "retryable"), FAULT_CASES)
@pytest.mark.asyncio
async def test_each_fault_fails_the_way_a_real_model_could(
    fault_id: str, expected: type, retryable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(simulation, "SIMULATED_TIMEOUT_SECONDS", 0.05)
    engine = FaultyExtractor(PRIMARY_MODEL, FAULTS[fault_id], extractors.get_settings())

    with pytest.raises(expected) as info:
        await engine.extract(SAMPLE_INVOICE_TEXT)

    assert type(info.value) is expected
    assert info.value.provider == PRIMARY_MODEL
    assert info.value.retryable is retryable


def test_every_fault_in_the_catalogue_is_exercised() -> None:
    assert {fault_id for fault_id, _, _ in FAULT_CASES} == set(FAULTS)


@pytest.mark.asyncio
async def test_simulated_failures_never_reach_the_network(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    fake = router()  # any real call would raise "unexpected call"

    response = await http.post(
        "/playground/extract",
        json={"text": SAMPLE_INVOICE_TEXT, "simulate": ["upstream_error", "rate_limit", "auth"]},
    )

    assert result_of(response)["status_code"] == 503
    assert all(attempt["simulated"] for attempt in attempts_of(response))
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Playground - traced extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_reports_each_attempt_and_then_the_result(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    router({PRIMARY_MODEL: ok()})

    response = await http.post("/playground/extract", json={"text": SAMPLE_INVOICE_TEXT})

    events = events_of(response)
    assert [event["event"] for event in events] == [
        "start",
        "attempt_started",
        "attempt_finished",
        "result",
    ]
    start, started, finished, result = events
    assert start["request_id"] == response.headers["x-request-id"]
    assert [entry["model"] for entry in start["chain"]] == MODEL_CHAIN
    assert [entry["fault"] for entry in start["chain"]] == [None, None, None]
    assert started == {
        "event": "attempt_started",
        "index": 0,
        "model": PRIMARY_MODEL,
        "simulated": False,
    }
    assert finished["outcome"] == "answered" and finished["error_type"] is None
    assert finished["elapsed_ms"] >= 0
    assert result["status_code"] == 200
    assert result["body"]["model_used"] == PRIMARY_MODEL
    assert result["body"]["fallback_triggered"] is False
    assert result["body"]["data"]["invoice_number"] == "NW-2024-0187"


@pytest.mark.asyncio
async def test_simulated_primary_failure_cascades_to_a_real_fallback(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    # The primary has no canned answer: calling it for real would fail the test.
    fake = router({FALLBACK_MODEL: ok()})

    response = await http.post(
        "/playground/extract", json={"text": SAMPLE_INVOICE_TEXT, "simulate": ["rate_limit"]}
    )

    primary, fallback = attempts_of(response)
    assert primary["simulated"] is True
    assert primary["outcome"] == "failed"
    assert primary["error_type"] == "ProviderRateLimitError"
    assert primary["retryable"] is True
    assert fallback["simulated"] is False and fallback["outcome"] == "answered"

    result = result_of(response)
    assert result["status_code"] == 200
    assert result["body"]["model_used"] == FALLBACK_MODEL
    assert result["body"]["fallback_triggered"] is True
    assert fake.models_called == [FALLBACK_MODEL]


@pytest.mark.parametrize(
    ("behaviours", "default", "status"),
    [
        ({PRIMARY_MODEL: ok()}, None, 200),
        ({PRIMARY_MODEL: api_error(openai.RateLimitError, 429), FALLBACK_MODEL: ok()}, None, 200),
        ({}, ok(EMPTY_INVOICE_PAYLOAD), 422),
        (
            {
                PRIMARY_MODEL: api_error(openai.InternalServerError, 503, "overloaded"),
                FALLBACK_MODEL: api_error(openai.RateLimitError, 429),
                FINAL_MODEL: timeout_error(),
            },
            None,
            503,
        ),
        ({}, api_error(openai.AuthenticationError, 401), 503),
    ],
    ids=["success", "fallback", "not-an-invoice", "all-fail-retryable", "all-fail-permanent"],
)
@pytest.mark.asyncio
async def test_result_is_exactly_what_the_public_endpoint_returns(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    behaviours: dict[str, Any],
    default: Any,
    status: int,
) -> None:
    """The page promises to show the public API's answer; hold it to that."""
    router(behaviours, default)
    payload = {"text": SAMPLE_INVOICE_TEXT}

    public = await http.post("/api/v1/extract", json=payload)
    traced = result_of(await http.post("/playground/extract", json=payload))

    assert public.status_code == status
    assert traced["status_code"] == public.status_code
    assert traced["headers"].get("retry-after") == public.headers.get("retry-after")
    expected, actual = public.json(), traced["body"]
    for body in (expected, actual):
        body.pop("execution_time_ms", None)  # the one field that differs run to run
    assert actual == expected


@pytest.mark.asyncio
async def test_two_simulated_verdicts_settle_a_refusal(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter]
) -> None:
    fake = router()

    response = await http.post(
        "/playground/extract",
        json={"text": GARBAGE_TEXT, "simulate": ["rate_limit", "no_invoice", "no_invoice"]},
    )

    assert [attempt["outcome"] for attempt in attempts_of(response)] == [
        "failed",
        "no_invoice",
        "no_invoice",
    ]
    result = result_of(response)
    assert result["status_code"] == 422
    assert result["body"]["error"] == "unparseable_text"
    assert result["headers"] == {}
    assert fake.calls == []


@pytest.mark.parametrize(
    ("scenario_id", "status"),
    [
        ("primary-rate-limited", 503),
        ("primary-prose", 503),
        ("primary-timeout", 503),
        ("wrong-verdict", 422),
    ],
)
@pytest.mark.asyncio
async def test_two_model_demos_end_as_their_summaries_say_when_the_live_model_fails(
    scenario_id: str,
    status: int,
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each summary tells the reader how the run ends on a bad day. Hold it to that."""
    monkeypatch.setenv("OPENROUTER_MODELS", f"{PRIMARY_MODEL},{FALLBACK_MODEL}")
    extractors.reset_extractors()
    monkeypatch.setattr(simulation, "SIMULATED_TIMEOUT_SECONDS", 0.05)
    router({FALLBACK_MODEL: api_error(openai.RateLimitError, 429)})
    scenario = next(item for item in scenarios_for(2) if item["id"] == scenario_id)

    response = await http.post(
        "/playground/extract", json={"text": scenario["text"], "simulate": scenario["simulate"]}
    )

    assert result_of(response)["status_code"] == status
    assert f"the run ends in a {status}" in scenario["summary"]


@pytest.mark.asyncio
async def test_simulated_hang_is_ended_by_the_real_timeout(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(simulation, "SIMULATED_TIMEOUT_SECONDS", 0.05)
    router({FALLBACK_MODEL: ok()})

    response = await http.post(
        "/playground/extract", json={"text": SAMPLE_INVOICE_TEXT, "simulate": ["timeout"]}
    )

    primary = attempts_of(response)[0]
    assert primary["error_type"] == "ProviderTimeoutError"
    assert "did not respond within 0.05s" in primary["message"]
    assert primary["elapsed_ms"] > 10  # it waited for the budget rather than failing at once
    assert result_of(response)["body"]["model_used"] == FALLBACK_MODEL


@pytest.mark.parametrize("payload", [{"text": "too short"}, {"text": "          "}, {}])
@pytest.mark.asyncio
async def test_invalid_request_gets_the_public_validation_envelope(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], payload: dict[str, Any]
) -> None:
    fake = router(default=ok())

    public = await http.post("/api/v1/extract", json=payload)
    traced = await http.post("/playground/extract", json=payload)

    # No stream is started: the page gets the API's own 422, byte for byte.
    assert traced.status_code == public.status_code == 422
    assert traced.headers["content-type"] == "application/json"
    assert traced.json() == public.json()
    assert fake.calls == []


@pytest.mark.parametrize(
    ("simulate", "location"),
    [
        (["not-a-fault"], ["body", "simulate", 0]),
        ([None, None, None, "auth"], ["body", "simulate"]),  # longer than the chain
    ],
)
@pytest.mark.asyncio
async def test_invalid_failure_plan_is_rejected(
    http: httpx.AsyncClient,
    router: Callable[..., FakeRouter],
    simulate: list[Any],
    location: list[Any],
) -> None:
    fake = router(default=ok())

    response = await http.post(
        "/playground/extract", json={"text": SAMPLE_INVOICE_TEXT, "simulate": simulate}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["detail"][0]["loc"] == location
    assert fake.calls == []


@pytest.mark.parametrize(
    ("site", "allowed"),
    [("same-origin", True), ("none", True), (None, True), ("cross-site", False), ("same-site", False)],
)
@pytest.mark.asyncio
async def test_only_the_page_itself_may_start_a_run(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], site: str | None, allowed: bool
) -> None:
    """Another site's script must not be able to spend this service's model allowance."""
    fake = router(default=ok())
    headers = {"Sec-Fetch-Site": site} if site else {}

    response = await http.post(
        "/playground/extract", json={"text": SAMPLE_INVOICE_TEXT}, headers=headers
    )

    if allowed:
        assert result_of(response)["status_code"] == 200
    else:
        assert response.status_code == 403
        assert response.json()["error"] == "http_error"
        assert fake.calls == []


@pytest.mark.asyncio
async def test_crash_inside_a_run_is_reported_as_an_event(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: ExtractionDispatcher, text: str) -> None:
        raise RuntimeError("unexpected boom")

    monkeypatch.setattr(ExtractionDispatcher, "extract", boom)

    response = await http.post("/playground/extract", json={"text": SAMPLE_INVOICE_TEXT})

    result = result_of(response)
    assert result["status_code"] == 500
    assert result["body"]["error"] == "internal_error"
    assert result["body"]["detail"]["request_id"] == response.headers["x-request-id"]
    assert "unexpected boom" not in response.text


# ---------------------------------------------------------------------------
# Playground - test-suite runner
#
# Nothing here starts pytest for real: a suite that ran itself would never end.
# ---------------------------------------------------------------------------

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4" time="1.25">
    <testcase classname="tests.test_demo.TestBasics" name="test_adds" file="tests/test_demo.py" line="5" time="0.001"/>
    <testcase classname="tests.test_demo" name="test_cascade[rate-limit]" file="tests\\test_demo.py" line="12" time="0.020">
      <failure message="assert 1 == 2">def test_cascade():
&gt;       assert 1 == 2
E       assert 1 == 2</failure>
    </testcase>
    <testcase classname="tests.test_demo" name="test_later" file="tests/test_demo.py" line="13" time="0.003">
      <skipped type="pytest.skip" message="not today">tests/test_demo.py:14: not today</skipped>
    </testcase>
    <testcase classname="tests.test_plain" name="test_alone" file="tests/test_plain.py" line="0" time="0.004"/>
  </testsuite>
</testsuites>
"""

# Indented here and dedented when used. Written flush left, this sample's banners
# would be read as banners of this very file by the parser under test, and the page
# would file the tests below under "Test 2 - a failing model cascades".
DEMO_TEST_FILE = textwrap.dedent(
    """\
    import pytest

    # ---------------------------------------------------------------------------
    # Basics
    # ---------------------------------------------------------------------------
    class TestBasics:
        def test_adds(self): ...

    # ---------------------------------------------------------------------------
    # Test 2 - a failing model cascades
    # ---------------------------------------------------------------------------

    def test_cascade(): ...
    def test_later(): ...
    """
)


def test_banner_sections_reads_titles_and_lines() -> None:
    assert suite.banner_sections(DEMO_TEST_FILE) == [
        (3, "Basics"),
        (9, "Test 2 - a failing model cascades"),
    ]
    assert suite.banner_sections("def test_plain(): ...\n") == []
    # A note under the title belongs to the banner; the title is still its first line.
    rule = "# " + "-" * 20
    noted = f"{rule}\n# Runner\n#\n# Nothing here starts pytest.\n{rule}\n\n{rule}\n# Next\n{rule}\n"
    assert suite.banner_sections(noted) == [(1, "Runner"), (7, "Next")]


def test_this_file_is_grouped_under_its_own_banners() -> None:
    """The sample above must not be read as part of this file, or the page mislabels it."""
    titles = [title for _, title in suite.banner_sections(Path(__file__).read_text("utf-8"))]
    assert titles == [
        "Playground - page and configuration",
        "Playground - scenario catalogue",
        "Playground - simulated failures",
        "Playground - traced extraction",
        "Playground - test-suite runner",
    ]


def test_report_is_parsed_into_one_record_per_test() -> None:
    tests, seconds = suite.parse_report(REPORT)

    assert seconds == 1.25
    assert [(test["name"], test["outcome"]) for test in tests] == [
        ("TestBasics::test_adds", "passed"),
        ("test_cascade[rate-limit]", "failed"),
        ("test_later", "skipped"),
        ("test_alone", "passed"),
    ]
    # Windows separators are normalised, so both spellings name the same file.
    assert {test["file"] for test in tests} == {"tests/test_demo.py", "tests/test_plain.py"}
    assert tests[1]["duration_ms"] == 20.0
    assert "assert 1 == 2" in tests[1]["detail"]
    assert tests[2]["detail"] == "not today"
    assert tests[0]["detail"] is None


def test_tests_are_grouped_under_the_banners_of_their_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text(DEMO_TEST_FILE, encoding="utf-8")
    monkeypatch.setattr(suite, "PROJECT_ROOT", tmp_path)

    sections = suite.group_into_sections(suite.parse_report(REPORT)[0])

    assert [(section["title"], section["file"]) for section in sections] == [
        ("Basics", "tests/test_demo.py"),
        ("Test 2 - a failing model cascades", "tests/test_demo.py"),
        # No banners (here: no file at all), so the file name stands in.
        ("test_plain", "tests/test_plain.py"),
    ]
    cascade = sections[1]
    assert (cascade["passed"], cascade["failed"], cascade["skipped"]) == (0, 1, 1)
    assert cascade["duration_ms"] == 23.0
    assert [test["name"] for test in cascade["tests"]] == ["test_cascade[rate-limit]", "test_later"]


def test_run_suite_starts_pytest_without_writing_into_the_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        seen.update(command=command, options=options)
        report = next(arg for arg in command if arg.startswith("--junitxml="))
        Path(report.removeprefix("--junitxml=")).write_text(REPORT, encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(suite.subprocess, "run", fake_run)

    outcome = suite.run_suite()

    command, options = seen["command"], seen["options"]
    assert command[1:4] == ["-m", "pytest", str(suite.TESTS_DIR)]
    assert "no:cacheprovider" in command  # no .pytest_cache for a dev server to notice
    assert options["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert options["cwd"] == suite.PROJECT_ROOT
    assert options["timeout"] == suite.RUN_TIMEOUT_SECONDS
    # A report outside the project tree, gone once the run is over.
    report_path = Path(next(a for a in command if a.startswith("--junitxml=")).split("=", 1)[1])
    assert suite.PROJECT_ROOT not in report_path.parents
    assert not report_path.exists()

    summary = outcome["summary"]
    assert (summary["total"], summary["passed"], summary["failed"], summary["skipped"]) == (
        4,
        2,
        1,
        1,
    )
    assert summary["duration_ms"] == 1250.0


def test_run_suite_reports_a_run_that_never_produced_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode=4, stdout="", stderr="usage error")

    monkeypatch.setattr(suite.subprocess, "run", fake_run)

    with pytest.raises(suite.SuiteError, match="exited with code 4") as info:
        suite.run_suite()
    assert "usage error" in str(info.value)


def test_run_suite_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(suite.subprocess, "run", fake_run)

    with pytest.raises(suite.SuiteError, match="did not finish"):
        suite.run_suite()


@pytest.mark.asyncio
async def test_suite_endpoint_returns_the_runner_outcome(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = {"summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0}, "sections": []}
    monkeypatch.setattr(suite, "run_suite", lambda: outcome)

    response = await http.post("/playground/tests")

    assert response.status_code == 200
    assert response.json() == outcome
    assert not suite.RUN_LOCK.locked()


@pytest.mark.asyncio
async def test_suite_endpoint_is_off_in_production(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    extractors.reset_extractors()
    monkeypatch.setattr(suite, "run_suite", lambda: pytest.fail("must not run in production"))

    response = await http.post("/playground/tests")

    assert response.status_code == 404
    assert "production" in response.json()["message"]
    config = (await http.get("/playground/config")).json()
    assert config["suite"]["available"] is False


@pytest.mark.asyncio
async def test_suite_endpoint_refuses_a_second_run_at_once(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(suite, "run_suite", lambda: pytest.fail("a run is already in progress"))

    with suite.RUN_LOCK:
        response = await http.post("/playground/tests")

    assert response.status_code == 409
    assert response.json()["error"] == "http_error"


@pytest.mark.asyncio
async def test_suite_endpoint_reports_a_runner_failure(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise suite.SuiteError("pytest exited with code 4.")

    monkeypatch.setattr(suite, "run_suite", broken)

    response = await http.post("/playground/tests")

    assert response.status_code == 500
    assert response.json()["message"] == "pytest exited with code 4."
    assert not suite.RUN_LOCK.locked()


@pytest.mark.asyncio
async def test_other_sites_cannot_start_the_test_suite(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(suite, "run_suite", lambda: pytest.fail("must not run"))

    response = await http.post("/playground/tests", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403

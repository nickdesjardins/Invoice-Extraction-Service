"""Tests for the "Model upkeep" part of the page, and for the two routes behind it.

The page's script is read as text, the way ``tests/test_playground.py`` reads it: no
browser runs here. What these tests pin is what would otherwise fail silently: a code
the service can send and the page has no words for, a key the script reads and the
service does not send, a sentence that names a setting. ``tests/test_page_behaviour.py``
runs the script, and puts every picture the service can send through it.

Layout:

* Models page - words for every code   - the page's tables against the service's tuples.
* Models page - what visitors read      - no setting, no file, no engine advice.
* Models page - the routes              - the picture, the button, the icon.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app import extractors, model_chooser, model_rules, model_testing
from app.config import Settings
from tests.conftest import FALLBACK_MODEL, PRIMARY_MODEL
from tests.test_extract import (  # noqa: F401 - `http` and `router` are fixtures
    SAMPLE_INVOICE_TEXT,
    FakeRouter,
    http,
    ok,
    router,
)
from tests.test_model_chooser import (  # noqa: F401 - `world` is a fixture
    FALLBACK,
    HEALTHY,
    PRIMARY,
    World,
    settled,
    world,
)
from tests.test_playground import events_of, result_of

STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "playground" / "static"
PAGE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC_DIR / "assets" / "app.js").read_text(encoding="utf-8")
STYLES = (STATIC_DIR / "assets" / "app.css").read_text(encoding="utf-8")


def upkeep_part() -> str:
    """The "model upkeep" part of the page script: from its banner to the next one."""
    part = re.search(r"// -+ model upkeep ---\n(.*?)\n// -+ benchmark ---", SCRIPT, re.DOTALL)
    assert part is not None, "the model upkeep part of the page script was not found"
    return part.group(1)


def table_keys(name: str) -> set[str]:
    """The keys of ``const NAME = { key: ..., }`` in the page script."""
    body = re.search(rf"^const {name} = \{{\n(.*?)^\}};", upkeep_part(), re.DOTALL | re.MULTILINE)
    assert body is not None, f"the table {name} was not found in the page script"
    return set(re.findall(r"^  ([a-z_]+):", body.group(1), re.MULTILINE))


# ---------------------------------------------------------------------------
# Models page - words for every code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "codes"),
    [
        ("ORIGIN_LINE", model_chooser.ORIGINS),
        ("ORIGIN_REASON_LINE", model_chooser.ORIGIN_REASONS),
        ("OWN_TEXT", model_chooser.ORIGIN_REASONS),
        ("STARTER_WORDS", model_testing.STARTERS),
        ("TEST_REASON_WORDS", model_testing.TEST_REASONS),
        ("OUTCOME_WORDS", model_testing.OUTCOMES),
        ("REFUSAL_WORDS", model_chooser.REFUSALS),
        ("DOING_WORDS", model_chooser.DOING),
        ("LOOK_WORDS", model_chooser.LOOK_RESULTS),
        ("VERDICT_LABEL", model_rules.VERDICTS),
        ("REASON_WORDS", model_rules.REASONS),
        ("PLACING_WORDS", model_rules.PLACINGS),
    ],
)
def test_the_page_has_words_for_every_code_the_service_can_send(table: str, codes: tuple[str, ...]) -> None:
    """A code without words would not raise anywhere: the page would print "undefined"."""
    assert table_keys(table) == set(codes)


def test_the_page_reads_only_what_the_service_sends() -> None:
    """A renamed key would not fail anywhere else either."""
    picture = model_chooser.picture()
    record = model_testing.recorded_run()
    assert record is not None, "this copy of the project has no benchmark records"
    sent = {
        "p": set(picture),
        "t": set(record),
        "real": set(record),
        "record": set(record),
        "row": set(record["models"][0]),
        "rules": set(picture["rules"]),
        "b": set(picture["button"]),
        "call": set(model_testing.CALL_FIELDS),
        # A call that is still out: what model_testing.run_test notes when it starts.
        "flying": {"model", "case", "since"},
    }
    part = upkeep_part()
    for name, keys in sent.items():
        read = set(re.findall(rf"(?<![\w.]){name}\.([a-z_]+)\b", part))
        assert read, f"the page reads nothing from {name}: was it renamed?"
        assert read <= keys, f"the page reads {name}.{sorted(read - keys)}, which the service does not send"
    # The parts of the picture the page looks inside.
    for name, keys in (("origin", picture["origin"]), ("button", picture["button"]), ("published", picture["published"])):
        read = set(re.findall(rf"\bp\.{name}\.([a-z_]+)\b", part))
        assert read <= set(keys), f"the page reads p.{name}.{sorted(read - set(keys))}"


@pytest.mark.asyncio
async def test_the_page_reads_only_what_a_look_leaves(world: Callable[..., World]) -> None:
    """The note of the last look has four keys, and the page's notices are built from them."""
    world([PRIMARY, FALLBACK], HEALTHY)
    model_chooser.note([model_chooser.Failure(PRIMARY)], answered=True)
    note = (await settled())["look"]

    assert set(note) == {"at", "result", "reason", "again_at"}
    read = set(re.findall(r"\bp\.look\.([a-z_]+)\b", upkeep_part()))
    assert read and read <= set(note), sorted(read - set(note))


def test_the_upkeep_part_sits_where_the_other_page_tests_expect_it() -> None:
    """Before the benchmark part, whose test reads every ``row.`` key up to the next banner."""
    banners = re.findall(r"^// -+ ([a-z ]+) ---$", SCRIPT, re.MULTILINE)
    assert banners.index("model upkeep") == banners.index("benchmark") - 1
    assert banners.index("test suite") < banners.index("model upkeep")


# ---------------------------------------------------------------------------
# Models page - what visitors read
# ---------------------------------------------------------------------------


def test_no_sentence_about_upkeep_names_a_setting_a_file_or_a_key() -> None:
    """The page is read by visitors. What an admin has to do is in the server log."""
    for where, text in (("script", upkeep_part()), ("page", PAGE)):
        for forbidden in ("OPENROUTER", "MODEL_", ".env", "chain.json", "model_cache", "API_KEY"):
            assert forbidden not in text, f"{forbidden} appears in the {where}"


@pytest.mark.asyncio
async def test_what_the_service_sends_the_page_names_no_setting_either(http: httpx.AsyncClient) -> None:
    """The page prints what these two answers hold, so they are held to the page's own rule.

    A setting's name is written in capitals; the keys of an answer are not sentences.
    """
    names = {field.upper() for field in Settings.model_fields}
    for route in ("/playground/config", "/playground/models"):
        text = (await http.get(route)).text
        found = sorted(name for name in names if re.search(rf"\b{name}\b", text))
        assert not found, f"{route} names a setting: {found}"


def test_the_owners_sentences_are_on_the_page() -> None:
    """Why a first answer can be slow after a quiet spell, and whom to contact when nothing works."""
    part = upkeep_part()
    assert "This page has few visitors" in part
    # Why the demo did not answer is something the page can only suppose, and says so.
    assert "so it has probably gone to sleep" in SCRIPT
    assert "nobody has visited for a while" in SCRIPT
    assert part.count("Please contact the admin.") >= 3
    assert 'const WITHDRAWN_LABEL = "No longer offered by OpenRouter";' in part


def test_the_words_say_no_more_than_the_service_knows() -> None:
    """Each of these was a sentence that was untrue in one state of the service."""
    part = upkeep_part()
    # All the service checks is whether the answer to the guarded document was right. It
    # does not know what the model did instead.
    assert 'hidden_instructions: () => "Its answer to the document with hidden instructions was wrong.",' in part
    assert "followed instructions" not in part
    # The recorded test was judged and changed no chain, and a finished test is in the past.
    assert 'kept: (would) => (would ? "Would keep its place." : "Kept its place."),' in part
    assert 'added: (would) => (would ? "Would be added to the chain." : "Added to the chain."),' in part
    assert "Keeps its place" not in part
    # The line under a finished test does not call its chain "now" once the chain has changed.
    assert "This test left the chain as: ${chainWords(t.chain)}. The chain in use now is: ${chainWords(p.chain)}." in part
    # A row says where its model stood before the test, as that: a stand-in is put to use
    # while its test runs, and a finished test may have changed the chain.
    assert '(t.by === "recorded" ? "" : " before this test");' in part
    # A row whose call is out says how far its model is, then for how long it has been
    # asked; a stand-in's note stands among those short notes, set apart like them.
    assert 'const words = row.calls || flying ? `${row.calls} of ${t.cases.length} so far` : "";' in part
    assert 'const note = standing && `${words ? " · " : ""}answers as a stand-in meanwhile`;' in part
    # A stand-in may answer while no test is running: after a test that could not judge it.
    assert "The test that is running has the last word." in part
    assert "yet: the next one has the last word." in part
    assert "It goes on answering as a stand-in until a test decides." in part
    # A chain from the scheduled test was tested, which is not "confirmed" model by model.
    assert "confirmed" not in part
    # A look is not "a run that failed": the fallback may have answered the run.
    assert "looking into a run that failed" not in part and "when a request fails" not in part
    # Nothing was withdrawn when a chain cannot be called although OpenRouter lists it.
    assert 'const start = t.reason.code === "gone" ? [foundWords(t), idleWords(p)] : [foundWords(t)];' in part
    # No promise of speed that the service cannot keep.
    assert "usually takes a few seconds" not in part
    # A test that was stopped has sent more requests than it has results. The headline
    # counts the requests, so the list under it counts what it lists.
    assert 'h("summary", null, `List the ${plural(t.calls.length, "result")} one by one`),' in part
    # One name for what the visitor is using, on the whole page.
    for text in (PAGE, SCRIPT):
        assert "this deployment" not in text


def test_a_look_that_got_no_answer_shows_nothing_as_going_on() -> None:
    """A test that was on screen may be over, or forgotten by a service that started again."""
    part = upkeep_part()
    vouched = "return p && upkeep.lost ? { ...p, busy: false, doing: null, test: null } : p;"
    assert vouched in part
    # Everything that is drawn from the picture is drawn from that one.
    assert part.count("upkeep.picture.test") == 1  # the replay asks whether a real test has the board
    assert "const p = shownPicture();" in script_function("renderUpkeep")
    assert "const p = shownPicture();" in script_function("renderChain")
    assert "const p = shownPicture();" in script_function("upkeepNotes")
    # Without any picture the section says so, and the chain card sends nobody to the admin.
    assert "This part of the page has not loaded" in script_function("renderUpkeep")
    assert '} else if (mode && listing?.status === "not_listed") {' in script_function("listingChip")


def test_the_section_can_be_used_with_a_keyboard_and_a_screen_reader() -> None:
    part = upkeep_part()
    # The sentence that says why the button cannot be used is tied to it and read out.
    assert '<button class="btn btn--primary" id="upkeep-start" type="button" aria-describedby="upkeep-note">' in PAGE
    assert '<p class="footnote upkeep__note" id="upkeep-note" role="status"></p>' in PAGE
    # The button is never switched off with "disabled": that throws the keyboard off it.
    assert 'button.setAttribute("aria-disabled", ' in part and "button.disabled" not in part
    assert '.btn[aria-disabled="true"]' in STYLES
    assert "if (upkeep.starting || upkeep.lost || !upkeep.picture?.button.ready) return;" in part
    # One stop per row of a test, not one per mark, and its tip says what the marks show.
    assert "mark.tabIndex" not in part
    assert '{ class: "marks", role: "group", tabindex: "0", ' in part
    # What is drawn again every few seconds keeps the focus, and an open list stays open.
    assert part.count("redraw(root, ") == 2
    assert "open: upkeep.callsOpen," in part
    # What the script announces is read out: the element it writes to is a live region.
    assert '<div class="sr-only" id="announcer" aria-live="polite"></div>' in PAGE
    # The headline of a test is a heading, and the legend names every mark.
    assert 'h("h3", { class: "suite__headline" }, ' in part
    assert "a dash when it was not sent" in part and "an empty circle while it is still to be sent" in part


def script_function(name: str) -> str:
    """One top-level function of the page script, with its whole text."""
    found = re.search(rf"^(?:async )?function {name}\(.*?^\}}$", SCRIPT, re.DOTALL | re.MULTILINE)
    assert found is not None, f"the page script has no function {name}"
    return found.group(0)


def test_the_section_is_in_the_page_and_the_others_moved_down() -> None:
    assert '<section class="section" id="upkeep">' in PAGE
    assert PAGE.index('id="playground"') < PAGE.index('id="upkeep"') < PAGE.index('id="tests"')
    kickers = re.findall(r'<p class="kicker">(0\d) · ([^<]+)</p>', PAGE)
    assert kickers == [("01", "Playground"), ("02", "Model upkeep"), ("03", "Test suite"), ("04", "How it works")]
    assert '<a href="#upkeep">Model upkeep</a>' in PAGE
    for needed in (
        "chain-origin", "upkeep-notice", "upkeep-origin", "upkeep-control", "upkeep-start",
        "upkeep-note", "upkeep-test", "upkeep-legend",
    ):
        assert f'id="{needed}"' in PAGE, needed


def test_a_model_row_stacks_on_a_narrow_screen() -> None:
    """One mark per document needs 178 px; a phone gives a row the full width instead of a sideways scroll."""
    narrow = re.search(r"@media \(max-width: 760px\) \{\s*\.upkeep__head \{[^}]*\}\s*\.upkeep__row \{([^}]*)\}", STYLES)
    assert narrow is not None and "grid-template-columns: minmax(0, 1fr)" in narrow.group(1)


# ---------------------------------------------------------------------------
# Models page - the routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_picture_is_whole_every_time_and_never_cached(http: httpx.AsyncClient) -> None:
    response = await http.get("/playground/models")

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    picture = response.json()
    assert set(picture) == {
        "now", "awake_since", "mode", "chain", "origin", "gone", "published", "busy", "doing", "look", "test",
        "last", "button", "rules",
    }
    assert picture["busy"] is False and picture["doing"] is None
    # The suite's own environment names its models, so this picture is the pinned one:
    # nothing is offered, and the recorded test is what there is to show.
    assert picture["mode"] == "pinned" and picture["button"]["offered"] is False
    assert picture["last"] is None or picture["last"]["by"] == "recorded"


@pytest.mark.asyncio
async def test_asking_for_the_picture_starts_nothing(http: httpx.AsyncClient, world: Callable[..., World]) -> None:
    """Any visitor, and any crawler, may ask. Only a POST from the page can start a test."""
    made = world(list(HEALTHY), HEALTHY)

    for _ in range(5):
        assert (await http.get("/playground/models")).json()["busy"] is False
    await settled()

    assert made.test_calls == [] and made.list_reads == 0 and made.lookups == []


@pytest.mark.asyncio
async def test_a_refused_press_is_answered_in_the_error_envelope(http: httpx.AsyncClient) -> None:
    response = await http.post("/playground/models/test")

    assert response.status_code == 409
    body = response.json()
    assert set(body) == {"success", "error", "message", "detail"} and body["success"] is False
    assert body["error"] == "test_refused" and body["detail"] == {"reason": "pinned", "again_at": None}


@pytest.mark.asyncio
async def test_a_page_drawn_for_a_longer_chain_can_still_run(
    http: httpx.AsyncClient, router: Callable[..., FakeRouter], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service repaired its chain, or woke up on a shorter one, and an open page
    still draws the old one: its plan has a place too many. That is no mistake of the
    visitor's, so the run starts. A failure to simulate in a place that is gone is one."""
    monkeypatch.setenv("OPENROUTER_MODELS", f"{PRIMARY_MODEL},{FALLBACK_MODEL}")
    extractors.reset_extractors()
    fake = router(default=ok())

    async def run(simulate: list[Any]) -> httpx.Response:
        return await http.post(
            "/playground/extract", json={"text": SAMPLE_INVOICE_TEXT, "simulate": simulate}
        )

    stale = await run([None, None, None])
    mixed = await run(["rate_limit", None, None])
    beyond = await run([None, None, "auth"])

    assert result_of(stale)["status_code"] == 200
    assert [entry["model"] for entry in events_of(stale)[0]["chain"]] == [PRIMARY_MODEL, FALLBACK_MODEL]
    assert result_of(mixed)["body"]["model_used"] == FALLBACK_MODEL
    assert beyond.status_code == 422 and beyond.json()["detail"][0]["loc"] == ["body", "simulate"]
    assert fake.models_called == [PRIMARY_MODEL, FALLBACK_MODEL]


@pytest.mark.asyncio
async def test_the_icon_a_front_door_can_load_stays_where_it_is(http: httpx.AsyncClient) -> None:
    """A static front door on another host shows "awake" once this image loads. It needs no CORS."""
    response = await http.get("/playground/assets/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"] == "no-cache"  # asked for again each time, so it proves the service is up

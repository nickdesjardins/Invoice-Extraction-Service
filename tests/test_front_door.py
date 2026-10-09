"""Tests that hold the front door (``site/``) to the records it quotes.

The front door is the static page people are given the address of. It is published on
GitHub Pages, apart from the service, so it is there at once while the live demo sleeps
on a free host. A visitor may read nothing else, and the page is written by hand: a
recorded run, the measured figures, and one line about the live demo. A hand-written
page goes stale the way the README once did, so these tests compare it with its sources.

They read files, and one of them asks this project's own app for its icon, in process:
no network, no key, no model call, no browser. Two tests replay the page's script with
Node.js and are skipped where Node.js is not installed. A copy of the project without
``site/`` skips this file.

How the page is held. Every piece of text a visitor can read is one of three things:

* a statement marked ``data-fact="name"``: ``facts()`` below works out, from the
  recordings in ``site/recorded/``, the benchmark snapshot and the code, what that name
  has to say, and the page must say exactly that;
* a quotation marked ``data-quote="path"``: its words must stand, word for word, in that
  file of the repository;
* a label listed in ``LABELS``: a heading, a column title, the words of a link. A label
  carries no figure.

Anything else fails. That holds each piece by itself. A visitor reads a figure beside a
caption, in a row, under a heading, behind a link, so a second group of tests holds the
page as it is read: every piece in its place (``TRANSCRIPT``), each link to where its words
say, each tick beside what it marks, nothing kept out of sight and nothing written by the
stylesheet or the script from outside the HTML.

What no test here can see is the page drawn in a browser: only the plain ways of hiding
something are known to these tests, and the preview picture is held by its fingerprint.

Layout:

* The recordings        - they are what the code would send and check today.
* What the page says    - the page against the recordings, the snapshot and the code.
* Nothing unpinned      - no text outside a statement, a quotation or a label.
* Where it stands       - the order of the page, its links, its marks, what is hidden.
* The live demo line    - one address in one place, and a script that stops asking.
* What the service owes - the icon the page watches for, as the host serves it.
* The page's own rules  - its own files only, nothing inline, small, linkable.
* Publishing            - the workflow that puts ``site/`` on GitHub Pages.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import httpx
import pytest

from app import extractors
from app.config import DEFAULT_MODEL_CHAIN, Settings
from app.main import create_app
from app.playground.benchmark import check_passes, expects_refusal, live_cases, load_records, summarise
from app.playground.catalogue import BENCHMARK, BENCHMARK_SNAPSHOT, EMAIL_INVOICE, MEETING_EMAIL, scenarios_for
from app.playground.simulation import SIMULATED_TIMEOUT_SECONDS
from app.schemas import ExtractResponse, InvoiceData

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE = PROJECT_ROOT / "site"

if not (SITE / "index.html").is_file():
    pytest.skip("this copy of the project has no front door (site/)", allow_module_level=True)

PAGE_FILE = SITE / "index.html"
STYLE_FILE = SITE / "assets" / "door.css"
SCRIPT_FILE = SITE / "assets" / "door.js"
THEME_FILE = SITE / "assets" / "theme.js"
ICON_FILE = SITE / "assets" / "favicon.svg"
PAGE = PAGE_FILE.read_text(encoding="utf-8")
STYLE = STYLE_FILE.read_text(encoding="utf-8")
SCRIPT = SCRIPT_FILE.read_text(encoding="utf-8")

#: The live demo's own page. The front door wears its colours and borrows its words.
PLAYGROUND = PROJECT_ROOT / "app" / "playground" / "static"

DEFAULT_CHAIN = [model.strip() for model in DEFAULT_MODEL_CHAIN.split(",")]

#: Where GitHub Pages serves the page: a project site, so under the repository's name.
PAGES_URL = "https://nickdesjardins.github.io/Invoice-Extraction-Service/"
REPOSITORY_URL = "https://github.com/nickdesjardins/Invoice-Extraction-Service"

#: The part of the demo's own page that the link leads to: where a visitor runs a document.
#: Without it the visitor would land on a headline that is this page's headline again.
DEMO_PART = "playground"
#: What the page holds until the host has given the service its address.
PLACEHOLDER_ADDRESS = f"https://live-demo-not-deployed-yet.invalid/#{DEMO_PART}"
#: A live demo's address: https, a name in lower case, the host's domain, and that part.
DEMO_ADDRESS = re.compile(rf"https://[a-z0-9][a-z0-9-]*\.onrender\.com/#{DEMO_PART}")
#: A name at the host, or one that can never be an address, with or without "https://".
DEMO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\.(?:onrender\.com|invalid)\b")

#: The recorded runs the page shows, each with the fingerprint of what was saved. A
#: recording is evidence only while it stays as it was saved: record a run again, and
#: its new fingerprint goes here (the failing test prints it).
RECORDINGS = {
    "email-invoice": "f3f3b4292350e57e",
    "primary-timeout": "6eb43a62373ea872",
}

#: The preview picture (``site/og.png``) is what LinkedIn or Slack show beside the link. A
#: test cannot read the words in a picture, so the picture itself is held by its fingerprint,
#: together with the words it was seen to carry when it was made. Each of those has to be the
#: page's own (test_the_preview_picture_is_the_one_that_was_looked_at). When one of them
#: changes on the page, make the picture again, look at it, and put its new fingerprint and
#: its words here.
#:
#: The picture is a photograph of ``tools/og-card.html`` (kept outside ``site/`` so that it is
#: not published). Bring the card's words up to date, then, with Microsoft Edge or Chrome:
#:
#:     msedge --headless=new --hide-scrollbars --force-device-scale-factor=1
#:         --window-size=1200,630 --user-data-dir=<an empty folder>
#:         --screenshot=site/og.png file:///<full path>/tools/og-card.html
PREVIEW_PICTURE = {
    "fingerprint": "01c81e7d383e92b4",
    "says": [
        "Invoice Extraction Service",
        "Applied AI · structured extraction",
        "Messy text in. Validated invoice out.",
        "Takes the text of an email, a till receipt or a scanned page and asks a free language "
        "model to fill in a fixed invoice form. If it gets no answer that fits the form, it asks "
        "a second model.",
        "FastAPI · Pydantic v2 · OpenRouter",
        "Schema valid",
    ],
}

#: Everything a first visit downloads, and the most it may weigh together.
FIRST_VIEW = [PAGE_FILE, STYLE_FILE, SCRIPT_FILE, THEME_FILE, ICON_FILE]
FIRST_VIEW_BUDGET = 100_000

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

#: Position words and counting words, as the page writes them.
ORDINALS = ("first", "second", "third", "fourth")
CARDINALS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}

#: Text that states nothing by itself: headings, column titles, the words of links.
#: A label carries no figure. A new sentence does not belong here: mark it with
#: ``data-fact`` and give it a source in ``facts()``, or quote a file with ``data-quote``.
LABELS = {
    # The top bar and the two ways in.
    "Skip to the content",
    "Invoice Extraction Service",
    "Recorded run",
    "When a model fails",
    "Measured figures",
    "Guides",
    "Live demo",
    "Open the live demo",
    "Code on GitHub",
    # The measured figures on the first screen.
    "Measured on",
    "First model",
    "Second model",
    "answers fully correct",
    "no answer:",
    "median answer",
    "See all",
    "models",
    # The recorded run.
    "Recorded run ·",
    "Sent",
    "characters of text",
    "The recording of this run (JSON)",
    "The recording of this second run (JSON)",
    "Returned",
    "Invoice number",
    "Date",
    "Currency",
    "Description",
    "Qty",
    "Unit price",
    "Line total",
    "Line items",
    "Tax",
    "Total",
    "Answered by",
    "Response time",
    "measured by the service",
    "Recorded",
    "Checks this run had to pass",
    "passed",
    # The second recorded run.
    "·",
    "both attempts, the first one simulated",
    "Invoice",
    "Checks",
    # The benchmark.
    ",",
    "How the",
    "models were chosen",
    "Model",
    "No answer",
    "Fully correct",
    "Median answer",
    "slowest",
    "No answer:",
    "Fully correct:",
    "Order of the models:",
    "Records:",
    "every call behind this table is saved in",
    ".",
    # The code and the guides.
    "The code and the guides",
    "Using the service",
    "The ideas behind it",
    "The Python, explained",
}

#: What a screen reader says that is not on the screen: the names of regions and buttons.
SPOKEN = {
    "On this page",
    "Switch between light and dark theme",
    "Live demo",
    "The email that was sent",
    "The table of measured models",
}


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------

#: Elements that never have a closing tag.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
#: Elements whose content is not something a visitor reads.
UNREAD = {"script", "style", "template", "svg", "head"}


class Node:
    """One element of the page: its tag, its attributes and what it contains."""

    def __init__(self, tag: str, attrs: dict[str, Optional[str]], parent: Optional["Node"]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list["Node | str"] = []

    def walk(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def find(self, wanted: Callable[["Node"], bool]) -> list["Node"]:
        return [node for node in self.walk() if wanted(node)]

    def text(self) -> str:
        """The text inside this element, as written."""
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag not in UNREAD:
                parts.append(child.text())
        return "".join(parts)

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()


class _Tree(HTMLParser):
    """Builds :class:`Node` objects, and fails on a tag that is closed out of turn."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.open = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        node = Node(tag, dict(attrs), self.open)
        self.open.children.append(node)
        if tag not in VOID:
            self.open = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.open.children.append(Node(tag, dict(attrs), self.open))

    def handle_endtag(self, tag: str) -> None:
        assert self.open.tag == tag, f"</{tag}> closes <{self.open.tag}>"
        self.open = self.open.parent  # type: ignore[assignment]

    def handle_data(self, data: str) -> None:
        self.open.children.append(data)


def parse(html: str) -> Node:
    tree = _Tree()
    tree.feed(html)
    tree.close()
    assert tree.open is tree.root, f"<{tree.open.tag}> is never closed"
    return tree.root


def clean(text: str) -> str:
    """Text as a reader sees it: runs of spaces and line breaks are one space."""
    return " ".join(text.replace("\xa0", " ").split())


DOCUMENT = parse(PAGE)
HEAD = DOCUMENT.find(lambda node: node.tag == "head")[0]
BODY = DOCUMENT.find(lambda node: node.tag == "body")[0]


def by_id(name: str) -> Node:
    found = DOCUMENT.find(lambda node: node.attrs.get("id") == name)
    assert len(found) == 1, f'{len(found)} elements with id="{name}"'
    return found[0]


def with_class(name: str, inside: Node = BODY) -> list[Node]:
    return inside.find(lambda node: name in node.classes)


def meta(name: str) -> str:
    """The content of one <meta> tag, found by its name, its property or its http-equiv."""
    found = [
        node.attrs.get("content") or ""
        for node in HEAD.find(lambda node: node.tag == "meta")
        if name in (node.attrs.get("name"), node.attrs.get("property"), node.attrs.get("http-equiv"))
    ]
    assert len(found) == 1, f"{len(found)} <meta> tags named {name}"
    return found[0]


def marked(attribute: str) -> list[Node]:
    return BODY.find(lambda node: attribute in node.attrs)


# ---------------------------------------------------------------------------
# Reading the sources
# ---------------------------------------------------------------------------


def recording(name: str) -> dict[str, Any]:
    return json.loads((SITE / "recorded" / f"{name}.json").read_text(encoding="utf-8"))


def fingerprint(record: dict[str, Any]) -> str:
    """A mark of a recording's whole content. It does not depend on how lines end on disk."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


RUN = recording("email-invoice")  # the first model answers alone
FALLBACK = recording("primary-timeout")  # the first model's failure is simulated


def events(record: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item["event"] for item in record["events"] if item["event"]["event"] == kind]


def result(record: dict[str, Any]) -> dict[str, Any]:
    """The last line of the stream: the status and body the public endpoint returns."""
    found = events(record, "result")
    assert len(found) == 1 and record["events"][-1]["event"] is found[0]
    return found[0]


def chain(record: dict[str, Any]) -> list[str]:
    return [entry["model"] for entry in events(record, "start")[0]["chain"]]


def check_met(check: dict[str, Any], record: dict[str, Any]) -> bool:
    """One of a scenario's checks, decided against a recording.

    The checks on the answer's fields are graded by the benchmark's own rule
    (``check_passes``); the two kinds that look at the stream are decided here.
    """
    outcome = result(record)
    if check["kind"] == "status":
        return outcome["status_code"] == check["expected"]
    if check["kind"] == "attempt":
        ended = [e for e in events(record, "attempt_finished") if e["index"] == check["index"]]
        return bool(ended) and ended[0]["error_type"] == check["expected"]
    return check_passes(check, outcome["body"])


def short(model: str) -> str:
    """``vendor/name:free`` as the page shows it: the name."""
    return model.split("/", 1)[-1].split(":", 1)[0]


def long_date(iso_day: str) -> str:
    day = date.fromisoformat(iso_day)
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def moment(recorded_at: str) -> str:
    """``2026-10-08T15:54:06Z`` as ``8 October 2026, 15:54 UTC``."""
    stamp = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    assert stamp.utcoffset().total_seconds() == 0, "recordings are stamped in UTC"
    return f"{long_date(stamp.date().isoformat())}, {stamp:%H:%M} UTC"


def run_seconds(milliseconds: float) -> str:
    """A run's time, as the demo's page writes it: two decimals under ten seconds."""
    seconds = milliseconds / 1000
    return f"{seconds:.2f} s" if seconds < 10 else f"{seconds:.1f} s"


def money(amount: float, currency: str) -> str:
    assert currency == "USD", "the page writes this recording's amounts with a dollar sign"
    return f"${amount:,.2f}"


def source_text(path: str) -> str:
    """A file of the repository as running text, so that a sentence can be looked for in it."""
    text = (PROJECT_ROOT / path).read_text(encoding="utf-8")
    if path.endswith(".html"):
        return clean(parse(text).text())
    if path.endswith(".js"):
        # A long sentence is written there in parts: "one part " + "the next part".
        return clean(re.sub(r'"\s*\+\s*"', "", text))
    # Markdown: quote marks at the start of a line, links, emphasis and code ticks go.
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return clean(text.replace("**", "").replace("`", ""))


def constant(name: str) -> int:
    """A whole-number constant of the page's script, such as ``GIVE_UP_AFTER_MS``."""
    found = re.findall(rf"^\s*const {name} = (\d+);", SCRIPT, re.MULTILINE)
    assert len(found) == 1, f"{name} is set {len(found)} times in door.js"
    return int(found[0])


def words_of(name: str) -> str:
    """A constant of the page's script that holds words, such as ``WAKE_TAKES``."""
    found = re.findall(rf'^\s*const {name} =\s*"([^"]+)";', SCRIPT, re.MULTILINE)
    assert len(found) == 1, f"{name} is set {len(found)} times in door.js"
    return found[0]


def wake_takes() -> str:
    """How long the host says waking takes, in the script's words: ``about a minute``."""
    return words_of("WAKE_TAKES")


def script_writes_words_in() -> set[str]:
    """The script's own names for the elements whose text it sets: the live line and its count."""
    return set(re.findall(r"(\w+)\.textContent = ", SCRIPT))


def stated(sentence: str, *rests_on: tuple[bool, str]) -> str:
    """A sentence of the page, for as long as everything it rests on is so.

    When one of the conditions no longer holds, what comes back is not the sentence
    but the reason, so the comparison with the page fails and says why.
    """
    gone = [why for holds, why in rests_on if not holds]
    return sentence if not gone else f"(no longer so: {'; '.join(gone)})"


# ---------------------------------------------------------------------------
# Front door - the recordings
# ---------------------------------------------------------------------------


def test_recordings_are_as_they_were_saved() -> None:
    """A recording is evidence only while nobody has edited it."""
    assert {path.stem for path in (SITE / "recorded").glob("*.json")} == set(RECORDINGS)
    found = {name: fingerprint(recording(name)) for name in RECORDINGS}
    assert found == RECORDINGS, (
        "a recording in site/recorded/ has changed. If the run was recorded again on purpose, "
        f"put its new fingerprint in RECORDINGS: {found}"
    )


@pytest.mark.parametrize("record", [RUN, FALLBACK], ids=list(RECORDINGS))
def test_recordings_are_what_the_code_sends_and_checks_today(record: dict[str, Any]) -> None:
    """A recording of yesterday's chain or yesterday's sample would be shown as if it were current.

    The document, the failure plan and the checks have to be those of the scenario in
    the catalogue, and the models have to be the default chain. When one of them
    changes, record the runs again.
    """
    wanted = record["scenario"]["id"]
    scenario = next(item for item in scenarios_for(len(DEFAULT_CHAIN)) if item["id"] == wanted)

    assert record["request"]["body"] == {"text": scenario["text"], "simulate": scenario["simulate"]}
    assert scenario["text"] == EMAIL_INVOICE
    for part in ("title", "summary", "simulate", "checks"):
        assert record["scenario"][part] == scenario[part], part
    assert chain(record) == DEFAULT_CHAIN
    assert record["limits"]["timeout_seconds"] == Settings.model_fields["timeout_seconds"].default
    # The page gives the time after which a simulated timeout ends: the code's own, today.
    assert record["limits"]["simulated_timeout_seconds"] == SIMULATED_TIMEOUT_SECONDS


@pytest.mark.parametrize("record", [RUN, FALLBACK], ids=list(RECORDINGS))
def test_recordings_are_whole_streams(record: dict[str, Any]) -> None:
    """start, then a started and a finished line per model that was tried, then one result."""
    kinds = [item["event"]["event"] for item in record["events"]]
    tried = kinds.count("attempt_finished")
    assert kinds == ["start"] + ["attempt_started", "attempt_finished"] * tried + ["result"]
    arrived = [item["at_ms"] for item in record["events"]]
    assert arrived == sorted(arrived)
    assert record["response"]["status_code"] == 200
    datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00"))
    # The response time the page shows covers every attempt ("both attempts", for the second
    # run), and the time at which the last line arrived covers the response time.
    attempts = sum(event["elapsed_ms"] for event in events(record, "attempt_finished"))
    assert attempts <= result(record)["body"]["execution_time_ms"] <= result(record)["elapsed_ms"]


@pytest.mark.parametrize("record", [RUN, FALLBACK], ids=list(RECORDINGS))
def test_recorded_answers_are_valid_invoices(record: dict[str, Any]) -> None:
    """ "Validated" is the page's headline. Validate again, with the service's own models."""
    body = result(record)["body"]
    assert ExtractResponse.model_validate(body).model_dump(mode="json") == body
    assert InvoiceData.model_validate(body["data"]).model_dump(mode="json") == body["data"]
    for item in body["data"]["items"]:
        assert item["total"] == pytest.approx(item["quantity"] * item["unit_price"], abs=0.005)


def test_the_first_recording_is_a_run_with_no_failure_and_no_simulation() -> None:
    attempts = events(RUN, "attempt_finished")
    tried = [(event["index"], event["outcome"], event["simulated"]) for event in attempts]
    assert tried == [(0, "answered", False)]
    body = result(RUN)["body"]
    assert body["success"] is True and body["fallback_triggered"] is False
    assert body["model_used"] == DEFAULT_CHAIN[0]


def test_the_second_recording_is_a_simulated_failure_and_a_real_answer() -> None:
    """The page calls the first failure simulated and the second call real. Both must be so."""
    first, second = events(FALLBACK, "attempt_finished")
    assert (first["index"], first["outcome"], first["simulated"]) == (0, "failed", True)
    assert first["error_type"] == "ProviderTimeoutError"
    # The time budget that ended it is the simulated one, and it was enforced.
    budget = FALLBACK["limits"]["simulated_timeout_seconds"]
    assert budget <= first["elapsed_ms"] / 1000 < budget + 0.5
    assert (second["index"], second["outcome"], second["simulated"]) == (1, "answered", False)
    body = result(FALLBACK)["body"]
    assert body["fallback_triggered"] is True and body["model_used"] == DEFAULT_CHAIN[1]


@pytest.mark.parametrize("record", [RUN, FALLBACK], ids=list(RECORDINGS))
def test_every_check_the_page_ticks_was_passed_by_the_recording(record: dict[str, Any]) -> None:
    """The page says "7 of 7" and draws a tick beside each check. A failed check gets neither."""
    for check in record["scenario"]["checks"]:
        assert check_met(check, record), f"{record['scenario']['id']}: {check['label']}"


def test_ticks_are_drawn_only_beside_the_checks_of_the_first_run() -> None:
    """One tick per check of the recording, and no other mark in that list."""
    (listed,) = with_class("checks")
    rows = [node for node in listed.children if isinstance(node, Node)]
    assert len(rows) == len(RUN["scenario"]["checks"])
    for row in rows:
        marks = [node.classes for node in with_class("badge", row)]
        assert marks == [["badge", "badge--good"]], marks


# ---------------------------------------------------------------------------
# Front door - what the page says
# ---------------------------------------------------------------------------


def code_facts() -> dict[str, str]:
    """The sentence under the headline and the line of tools under it, held to the code."""
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    extractors_source = (PROJECT_ROOT / "app" / "extractors.py").read_text(encoding="utf-8")
    strict = Settings.model_fields["openrouter_strict_json_schema"].default is True
    return {
        # "Fits the form" is as far as the service's check goes: it validates an answer against
        # the invoice form, and cannot tell a wrong figure in a form that is filled in properly.
        "what-it-does": stated(
            "This web service takes the text of an email, a till receipt or a scanned page and "
            "asks a free language model to fill in a fixed invoice form. If it gets no answer "
            "that fits the form, it asks a second model.",
            (all(model.endswith(":free") for model in DEFAULT_CHAIN), "a default model is not free"),
            (strict, "the fixed response format is no longer on by default"),
            ("InvoiceData.model_validate" in extractors_source, "answers are not validated"),
            (len(DEFAULT_CHAIN) == 2, "the default chain is not a first model and a second"),
        ),
        "stack": stated(
            "FastAPI · Pydantic v2 · OpenRouter · strict JSON schema · temperature 0",
            (bool(re.search(r"^fastapi\b", requirements, re.MULTILINE)), "no FastAPI"),
            (bool(re.search(r"^pydantic==2\.", requirements, re.MULTILINE)), "not Pydantic 2"),
            ("openrouter.ai" in Settings.model_fields["openrouter_base_url"].default, "another provider"),
            (strict, "the strict schema is off by default"),
            (bool(re.search(r"\btemperature\s*=\s*0(\.0)?\s*,", extractors_source)), "another temperature"),
        ),
        "guides.code": stated(
            "The service, its tests, and this page with the test that checks its figures.",
            ((PROJECT_ROOT / "app" / "main.py").is_file(), "no service"),
            ((PROJECT_ROOT / "tests" / "test_extract.py").is_file(), "no tests"),
            (Path(__file__).resolve().parent == PROJECT_ROOT / "tests", "this test is not in tests/"),
        ),
    }


def demo_facts() -> dict[str, str]:
    """The two sentences about the live demo that are written in the page itself."""
    blueprint = PROJECT_ROOT / "render.yaml"
    free = not blueprint.is_file() or bool(
        re.search(r"^\s*plan:\s*free\b", blueprint.read_text(encoding="utf-8"), re.MULTILINE)
    )
    return {
        "demo.static": stated(
            "The live demo runs on free hosting and sleeps between visits. "
            f"Opening it wakes it, which takes {wake_takes()}.",
            (free, "render.yaml no longer asks for the free plan"),
        ),
        "demo.unset": stated(
            "The live demo is not online yet. This page shows recorded runs instead.",
            (len(RECORDINGS) > 1, "the page no longer shows more than one recorded run"),
        ),
    }


def invoice_facts(prefix: str, data: dict[str, Any]) -> dict[str, str]:
    currency = data["currency"]
    said = {
        f"{prefix}.vendor": data["vendor_name"],
        f"{prefix}.number": data["invoice_number"],
        f"{prefix}.date": data["invoice_date"],
        f"{prefix}.currency": currency,
        f"{prefix}.lines": money(sum(item["total"] for item in data["items"]), currency),
        f"{prefix}.tax": money(data["tax_amount"], currency),
        f"{prefix}.total": money(data["total_amount"], currency),
    }
    for index, item in enumerate(data["items"]):
        said[f"{prefix}.item.{index}"] = " ".join(
            [
                item["description"],
                f"{item['quantity']:g}",
                money(item["unit_price"], currency),
                money(item["total"], currency),
            ]
        )
    return said


def run_facts() -> dict[str, str]:
    """The run on the first screen: the email that went in and the invoice that came back."""
    outcome, text = result(RUN), RUN["request"]["body"]["text"]
    body = outcome["body"]
    extracted = outcome["status_code"] == 200 and body["success"] is True
    checks = RUN["scenario"]["checks"]
    said = {
        "run.when": moment(RUN["recorded_at"]),
        # Nothing replays the run or adds to it: the script writes in two places only, the
        # live line and its count (test_the_script_touches_the_page_in_these_places_only),
        # and the chip stands on the run it speaks of (test_the_page_reads_in_this_order).
        "run.nature": stated(
            "A recording, not live",
            (script_writes_words_in() == {"words", "clock"}, "the script writes words elsewhere"),
        ),
        "run.title": stated(
            "This email went in. This invoice came back.",
            (text.startswith("From: "), "the document that was sent is not an email"),
            (extracted, "the run did not end in an invoice"),
        ),
        # Counted as the demo's own page counts it, and as the service reads it: without the
        # line break that ends the text. The demo shows the same figure for the same email.
        "run.chars": str(len(text.strip())),
        "run.document": clean(text),
        "run.status": str(outcome["status_code"]),
        "run.outcome": stated("Invoice extracted", (extracted, "the run did not end in an invoice")),
        # The answer passed the service's own validation, or it would be an error body.
        "run.valid": stated("Schema valid", (extracted, "the run did not end in an invoice")),
        "run.model": short(body["model_used"]),
        "run.place": (
            f"the {ORDINALS[chain(RUN).index(body['model_used'])]} "
            f"of the {CARDINALS[len(chain(RUN))]} models"
        ),
        "run.seconds": run_seconds(body["execution_time_ms"]),
        "run.checks": f"{sum(check_met(check, RUN) for check in checks)} of {len(checks)}",
        **invoice_facts("run", body["data"]),
    }
    for index, check in enumerate(checks):
        said[f"run.check.{index}"] = check["label"]
    return said


def fallback_facts() -> dict[str, str]:
    """The second run: a simulated failure of the first model, a real answer from the second."""
    first_run, outcome = result(RUN), result(FALLBACK)
    body = outcome["body"]
    first_try, second_try = events(FALLBACK, "attempt_finished")
    limit = FALLBACK["limits"]["simulated_timeout_seconds"]
    usual = FALLBACK["limits"]["timeout_seconds"]
    checks = FALLBACK["scenario"]["checks"]
    only_first_simulated = first_try["simulated"] and not second_try["simulated"]
    same_text = FALLBACK["request"]["body"]["text"] == RUN["request"]["body"]["text"]
    same_invoice = body["data"] == first_run["body"]["data"]
    extracted = outcome["status_code"] == 200 and body["success"] is True
    answered_by_second = second_try["outcome"] == "answered" and body["model_used"] == chain(FALLBACK)[1]

    said = {
        "fallback.when": moment(FALLBACK["recorded_at"]),
        "fallback.simulated": stated(
            "simulated failure", (only_first_simulated, "not only the first failure was simulated")
        ),
        # What happened in this one run, in the past tense. Not a promise: in the benchmark
        # the second model left some calls without an answer.
        "fallback.title": stated(
            "The first model was made to fail. The second one answered.",
            (first_try["outcome"] == "failed", "the first model did not fail"),
            (first_try["simulated"] is True, "the first model's failure was not staged"),
            (answered_by_second, "the second model did not answer"),
        ),
        "fallback.story": stated(
            "The same email was sent again. This time the first model's call was replaced "
            f"by a simulated timeout, after {limit:g} second{'' if limit == 1 else 's'} instead of "
            f"the usual {usual:g}. The rest was real: the service gave up on the first model, called "
            "the second one, and returned the same invoice.",
            (limit < usual, "the simulated timeout is not shorter than the usual one"),
            (same_text, "the two recordings were sent different documents"),
            (FALLBACK["request"]["body"]["simulate"] == ["timeout", None], "another failure plan"),
            (first_try["error_type"] == "ProviderTimeoutError", "the first model did not time out"),
            (only_first_simulated, "not only the first failure was simulated"),
            (answered_by_second, "the second model did not answer"),
            (same_invoice, "the two recordings returned different invoices"),
        ),
        "fallback.status": str(outcome["status_code"]),
        "fallback.outcome": stated("Invoice extracted", (extracted, "the run did not end in an invoice")),
        "fallback.used": stated("Fallback used", (body["fallback_triggered"] is True, "no fallback")),
        "fallback.model": short(body["model_used"]),
        "fallback.place": f"the {ORDINALS[chain(FALLBACK).index(body['model_used'])]} model",
        "fallback.seconds": run_seconds(body["execution_time_ms"]),
        "fallback.number": body["data"]["invoice_number"],
        "fallback.same": stated(
            "the same as in the first run, field for field",
            (same_invoice, "the two recordings returned different invoices"),
        ),
        "fallback.checks": f"{sum(check_met(check, FALLBACK) for check in checks)} of {len(checks)}",
    }
    outcomes = {
        ("failed", "ProviderTimeoutError"): "Timed out",
        ("answered", None): "Answered with a valid invoice",
    }
    for attempt in (first_try, second_try):
        prefix = f"fallback.attempt.{attempt['index']}"
        said.update(
            {
                f"{prefix}.position": str(attempt["index"] + 1),
                f"{prefix}.model": short(attempt["model"]),
                f"{prefix}.outcome": outcomes[(attempt["outcome"], attempt["error_type"])],
                f"{prefix}.kind": "simulated" if attempt["simulated"] else "real call",
                f"{prefix}.seconds": run_seconds(attempt["elapsed_ms"]),
            }
        )
    return said


def benchmark_facts(rows: tuple[dict[str, Any], ...] = BENCHMARK) -> dict[str, str]:
    """The measured figures: two tiles on the first screen, and the table of every model."""
    shot = BENCHMARK_SNAPSHOT
    playground_page = source_text("app/playground/static/index.html")
    playground_script = source_text("app/playground/static/assets/app.js")
    # The documents every model was sent: the demo's own samples, one of which is not an invoice.
    cases = live_cases()
    refused = [case for case in cases if expects_refusal(case)]
    documents = [case for case in cases if not expects_refusal(case)]
    said = {
        "bench.day": long_date(shot["measured"]),
        "bench.window": f"{shot['window_utc']} UTC",
        "bench.models": CARDINALS[len(rows)],
        "bench.records": ", ".join(shot["records"]),
        "chain.length": CARDINALS[len(DEFAULT_CHAIN)],
        # The caution is the project's own: the demo's page says it under its table.
        "bench.limits": stated(
            f"{shot['passes']} passes of {shot['cases']} documents per model show whether a model "
            "can do the job, not how often it is right.",
            (
                "show whether a model can do the job. They do not measure accuracy." in playground_script,
                "the demo's page no longer gives this caution",
            ),
        ),
        "bench.method": stated(
            f"Each of the {CARDINALS[len(rows)]} free models on OpenRouter that advertised "
            f"structured output was sent {shot['cases']} documents in each of {shot['passes']} "
            f"passes: {CARDINALS[len(documents)]} invoices and receipts, one of them an invoice for "
            "services, and one meeting email that must be refused. Every call went through this "
            "service's own prompt, schema and validation, with a time limit of "
            f"{shot['timeout_seconds']:g} s.",
            (all(row["model"].endswith(":free") for row in rows), "a measured model is not free"),
            (
                "model on OpenRouter that advertised structured output" in playground_page
                and "through this service's own prompt, schema and validation" in playground_page,
                "the demo's page no longer says how the models were measured",
            ),
            (all(row["calls"] == shot["passes"] * shot["cases"] for row in rows), "the calls do not add up"),
            (len(cases) == shot["cases"], "the demo's samples are no longer the documents that were sent"),
            (len(refused) == 1 and refused[0]["text"] == MEETING_EMAIL, "not one meeting email to refuse"),
            (any(case["id"] == "service-invoice" for case in documents), "no invoice for services among them"),
            (any(case["id"] == "ocr-receipt" for case in documents), "no receipt among them"),
        ),
    }
    for index, row in enumerate(rows):
        answered = row["answered"] > 0
        place = (
            f"{ORDINALS[DEFAULT_CHAIN.index(row['model'])].capitalize()} model"
            if row["model"] in DEFAULT_CHAIN
            else "Left out"
        )
        # The snapshot's note says "ends 2026-12-31": the page says what ends, and writes the
        # date out. (What stands behind each note: test_notes_in_the_table_are_what_the_records_show.)
        note = re.sub(r"; ends (?=\d{4}-\d{2}-\d{2})", "; the model ends on ", row["note"])
        note = re.sub(r"\d{4}-\d{2}-\d{2}", lambda match: long_date(match.group()), note)
        said.update(
            {
                f"bench.{index}.model": short(row["model"]),
                f"bench.{index}.about": " · ".join(part for part in (place, note) if part),
                f"bench.{index}.no_answer": f"{row['no_answer']} of {row['calls']}",
                f"bench.{index}.correct": f"{row['correct']} of {row['answered']}" if answered else "–",
                f"bench.{index}.median": f"{row['median_seconds']:.1f} s" if answered else "–",
            }
        )
        if answered:
            said[f"bench.{index}.slowest"] = f"{row['slowest_seconds']:.1f} s"
    return said


def page_facts() -> dict[str, str]:
    """What the page says about itself, at the foot."""
    environments = {record["service"]["environment"] for record in (RUN, FALLBACK)}
    dated = {node.attrs.get("data-fact") for node in BODY.find(lambda node: node.tag == "time")}
    return {
        # True in every state of the page, also while the demo's address is not filled in and
        # nothing on the page is live.
        "page.nature": stated(
            "The runs and the figures on this page are records, each with its date.",
            (script_writes_words_in() == {"words", "clock"}, "the script writes words elsewhere"),
            (dated == {"run.when", "fallback.when", "bench.day"}, "a record has lost its date"),
        ),
        "runs.where": stated(
            "Both runs were recorded on a development copy of the service.",
            (environments == {"development"}, f"the recordings say {sorted(environments)}"),
        ),
    }


def facts() -> dict[str, str]:
    """Every marked statement in the page, and what its source says it must be."""
    return {**code_facts(), **demo_facts(), **run_facts(), **fallback_facts(), **benchmark_facts(), **page_facts()}


def test_every_marked_statement_says_what_its_source_says() -> None:
    """The heart of this file: the page against the recordings, the snapshot and the code."""
    expected = facts()
    on_the_page = marked("data-fact")
    assert on_the_page, "the page marks no statement, so nothing is being compared"

    wrong = []
    for node in on_the_page:
        name = node.attrs["data-fact"]
        assert name in expected, f'data-fact="{name}" has no source in this test'
        shown = clean(node.text())
        if shown != expected[name]:
            wrong.append(f'data-fact="{name}" says {shown!r}; its source says {expected[name]!r}')
    assert not wrong, "site/index.html no longer says what its sources say:\n" + "\n".join(wrong)

    missing = sorted(set(expected) - {node.attrs["data-fact"] for node in on_the_page})
    assert not missing, f"sourced here and missing from the page: {missing}"


def test_the_document_is_shown_exactly_as_it_was_sent() -> None:
    """Character for character, line breaks included: it is the evidence."""
    (shown,) = BODY.find(lambda node: node.attrs.get("data-fact") == "run.document")
    assert shown.tag == "pre"
    assert shown.text() == RUN["request"]["body"]["text"]


def test_dates_the_browser_reads_are_the_dates_the_reader_reads() -> None:
    """A <time> element carries its date twice: in words, and in ``datetime`` for machines."""
    machine = {
        "bench.day": BENCHMARK_SNAPSHOT["measured"],
        "run.when": RUN["recorded_at"],
        "fallback.when": FALLBACK["recorded_at"],
    }
    stamps = BODY.find(lambda node: node.tag == "time")
    assert {node.attrs.get("data-fact") for node in stamps} == set(machine)
    for node in stamps:
        assert node.attrs["datetime"] == machine[node.attrs["data-fact"]]


def test_a_simulated_failure_is_called_simulated_wherever_it_is_shown() -> None:
    """A failure the demo staged must never read as a model that really failed, nor the reverse."""
    (first_row, second_row) = with_class("trace__row")
    assert "simulated" in clean(first_row.text()) and "real call" not in clean(first_row.text())
    assert "real call" in clean(second_row.text()) and "simulated" not in clean(second_row.text())
    # The section says so at its head: in the line that carries its date, and in its heading.
    section = by_id("fallback")
    (dated,) = [node for node in with_class("kicker", section)]
    assert clean(dated.text()).endswith("simulated failure")
    assert "was made to fail" in clean(by_id("fallback-title").text())
    # And nothing in the first run, where nothing was simulated, uses the word.
    assert "imulated" not in clean(by_id("run").text())


def test_page_figures_are_what_the_saved_records_say() -> None:
    """The snapshot in the catalogue is a copy made by hand. Go past it, to the records."""
    if not (PROJECT_ROOT / "benchmarks").is_dir():
        pytest.skip("this copy of the project has no benchmarks folder")
    paths = [PROJECT_ROOT / name for name in BENCHMARK_SNAPSHOT["records"]]
    measured = {row["model"]: row for row in summarise(load_records(paths))}
    assert set(measured) == {row["model"] for row in BENCHMARK}
    from_the_records = tuple({**row, **measured[row["model"]], "note": row["note"]} for row in BENCHMARK)
    assert benchmark_facts(from_the_records) == benchmark_facts()

    # "every call behind this table is saved in": count the calls in the file.
    calls = sum(
        json.loads(line).get("type") == "call"
        for path in paths
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert calls == sum(row["calls"] for row in BENCHMARK)


def test_the_page_calls_the_models_first_and_second_in_chain_order() -> None:
    """ "First model" on the page is row 0 of the snapshot. It has to be position 0 of the chain."""
    assert [row["model"] for row in BENCHMARK[: len(DEFAULT_CHAIN)]] == DEFAULT_CHAIN
    assert len(DEFAULT_CHAIN) == 2, "the page speaks of a first model and a second one"
    tiles = [clean(node.text()) for node in with_class("tile")]
    assert [tile.split(" model")[0] for tile in tiles] == ["First", "Second"]
    context = [node for node in BODY.find(lambda node: node.tag == "tr") if "is-context" in node.classes]
    assert len(context) == len(BENCHMARK) - len(DEFAULT_CHAIN), "the models left out are drawn as context"


def test_quotations_stand_word_for_word_in_the_file_they_name() -> None:
    """Where the page speaks in the project's own words, the words have to be there still.

    One liberty: a quotation that opens a line of the page may begin with a capital
    where its source, in the middle of a sentence, has a small letter.
    """
    quoted = marked("data-quote")
    assert quoted
    for node in quoted:
        path = node.attrs["data-quote"]
        assert (PROJECT_ROOT / path).is_file(), f"{path} is quoted and does not exist"
        words, source = clean(node.text()), source_text(path)
        assert words, f"an empty quotation of {path}"
        assert words in source or words[0].lower() + words[1:] in source, f"not in {path}: {words!r}"


def test_the_page_goes_by_the_demos_name_and_previews_its_own_sentence() -> None:
    """One product, two pages: the same name. And a link preview that says what the page says."""
    if not PLAYGROUND.is_dir():
        pytest.skip("this copy of the project has no playground page")
    theirs = parse((PLAYGROUND / "index.html").read_text(encoding="utf-8"))
    their_title = clean(theirs.find(lambda node: node.tag == "title")[0].text())
    assert clean(HEAD.find(lambda node: node.tag == "title")[0].text()) == their_title
    assert clean(with_class("brand__name")[0].text()) == their_title
    # On a narrow phone the top bar has no room for the name: the foot of the page has it too.
    (foot,) = with_class("footer__row")
    assert clean(foot.find(lambda node: node.tag == "span")[0].text()) == their_title
    # Someone who sees the link before the page reads the page's own plain sentence, held to
    # the code like the one on the page.
    assert meta("description") == meta("og:description") == facts()["what-it-does"]


# ---------------------------------------------------------------------------
# Front door - nothing unpinned
# ---------------------------------------------------------------------------


def loose_text() -> list[str]:
    """Every piece of text a visitor can read that is neither a marked statement nor a quotation."""
    found: list[str] = []

    def visit(node: Node) -> None:
        if node.tag in UNREAD or "data-fact" in node.attrs or "data-quote" in node.attrs:
            return
        for child in node.children:
            if isinstance(child, Node):
                visit(child)
            elif clean(child):
                found.append(clean(child))

    visit(BODY)
    return found


def test_all_other_text_on_the_page_is_a_label() -> None:
    """A sentence added to the page without a source would be a claim nobody checks."""
    loose = set(loose_text())
    strays = sorted(loose - LABELS)
    assert not strays, (
        "site/index.html has text that is neither a marked statement, nor a quotation, nor a "
        "label. Give it a data-fact and a source in facts(), or a data-quote, or, if it states "
        f"nothing, add it to LABELS: {strays}"
    )
    unused = sorted(LABELS - loose)
    assert not unused, f"LABELS lists text the page no longer has: {unused}"


def test_labels_carry_no_number() -> None:
    """A figure typed into a heading or a label would have no source, and nothing would notice."""
    spelled = re.compile(r"\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", re.IGNORECASE)
    for words in sorted(LABELS | SPOKEN):
        assert not re.search(r"\d", words), f"a number in a label: {words!r}"
        assert not spelled.search(words), f"a number in words in a label: {words!r}"


def test_words_only_a_screen_reader_says_are_listed_too() -> None:
    spoken = {
        node.attrs[name]
        for node in BODY.walk()
        for name in ("aria-label", "title", "alt", "placeholder")
        if node.attrs.get(name)
    }
    assert spoken == SPOKEN


def test_words_in_a_link_preview_carry_no_number() -> None:
    """The title, the description and the picture's description, as LinkedIn or Slack show them."""
    for words in (meta("description"), meta("og:description"), meta("og:image:alt"), meta("og:title")):
        assert words and not re.search(r"\d", words), words
    assert meta("description") == meta("og:description")
    assert meta("og:image:alt") == "Invoice Extraction Service. Messy text in. Validated invoice out."


#: How a setting is written: OPENROUTER_MODELS, TIMEOUT_SECONDS.
SETTING = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")


def test_page_names_no_setting_and_tells_nobody_to_change_one() -> None:
    """The page is read by visitors. Settings belong in the guides."""
    assert not SETTING.search(clean(BODY.text())), "a setting is named on the page"
    for sentence in re.findall(r'^\s*\w+: [`"](.+)[`"],$', SCRIPT, re.MULTILINE):
        assert not SETTING.search(sentence.replace("${WAKE_TAKES}", "")), sentence
    for leftover in ("localhost", "127.0.0.1"):
        assert leftover not in PAGE + SCRIPT + STYLE, f"{leftover}: an address from a test"


def test_dates_are_written_out_in_words() -> None:
    """7 October 2026, as on the demo's page.

    The invoice's own date stays as the service returns it, the email is a document
    and not this page's words, and a file keeps its name.
    """
    as_they_are = {"run.date", "run.check.2", "run.document", "bench.records"}
    ours = " ".join(
        clean(node.text()) for node in marked("data-fact") if node.attrs["data-fact"] not in as_they_are
    )
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}\b", ours), "a date in figures"
    assert not re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", ours), "a date with slashes"
    assert not re.search(rf"\b(?:{'|'.join(MONTHS)}) \d{{1,2}}, \d{{4}}\b", ours), "month before day"
    assert re.search(rf"\b\d{{1,2}} (?:{'|'.join(MONTHS)}) \d{{4}}\b", ours)


def test_page_names_only_free_models_that_were_measured() -> None:
    """The rule tests/test_docs.py applies to the documents, applied to this page."""
    measured = {short(row["model"]) for row in BENCHMARK}
    named = {clean(node.text()) for node in marked("data-fact") if node.attrs["data-fact"].endswith(".model")}
    assert named and named <= measured, sorted(named - measured)
    assert not re.search(r"[a-z0-9][\w.-]*/[a-z0-9][\w.-]*:free\b", PAGE), "a full model id on the page"


# ---------------------------------------------------------------------------
# Front door - where it stands
#
# The tests above hold each marked value to its source by its NAME. A visitor reads a
# value beside a caption, in a row, under a heading, behind a link. These tests hold
# the page as it is read: the order of every label and every marked statement, where
# each link leads, which mark stands beside which outcome, and that nothing stated is
# kept out of sight or added from outside the HTML.
# ---------------------------------------------------------------------------


def reading(node: Node) -> list[str]:
    """What an element says, in the order it is read.

    A label as written, a marked statement as ``{its name}`` (its words are held by
    ``facts()``), a quotation as ``<<its words>>``.
    """
    parts: list[str] = []

    def visit(node: Node) -> None:
        if node.tag in UNREAD:
            return
        if "data-fact" in node.attrs:
            parts.append("{" + node.attrs["data-fact"] + "}")
            return
        if "data-quote" in node.attrs:
            parts.append("<<" + clean(node.text()) + ">>")
            return
        for child in node.children:
            if isinstance(child, Node):
                visit(child)
            elif clean(child):
                parts.append(clean(child))

    visit(node)
    return parts


#: The page from top to bottom, as it stands in the HTML. One line here is one line of
#: thought on the page; the comparison ignores where the lines break. (Three headings come
#: before the dated line that the stylesheet draws above them: see the comment in the page.)
TRANSCRIPT = """
Skip to the content
Invoice Extraction Service | Recorded run | When a model fails | Measured figures | Guides | Live demo
<<Applied AI · structured extraction>>
<<Messy text in. Validated invoice out.>>
{what-it-does}
{stack}
Open the live demo | Code on GitHub
{demo.static}
{demo.unset}
Measured on | {bench.day}
First model | {bench.0.correct} | answers fully correct | no answer: | {bench.0.no_answer} | median answer | {bench.0.median}
Second model | {bench.1.correct} | answers fully correct | no answer: | {bench.1.no_answer} | median answer | {bench.1.median}
{bench.limits} | See all | {bench.models} | models
{run.title}
Recorded run · | {run.when} | {run.nature}
Sent | {run.chars} | characters of text
{run.document}
The recording of this run (JSON)
Returned | {run.status} | {run.outcome}
{run.valid} | {run.vendor}
Invoice number | {run.number} | Date | {run.date} | Currency | {run.currency}
Description | Qty | Unit price | Line total
{run.item.0}
{run.item.1}
Line items | {run.lines} | Tax | {run.tax} | Total | {run.total}
Answered by | {run.model} | {run.place}
Response time | {run.seconds} | measured by the service
Recorded | {run.when}
Checks this run had to pass | {run.checks} | passed
{run.check.0} | {run.check.1} | {run.check.2} | {run.check.3} | {run.check.4} | {run.check.5} | {run.check.6}
{fallback.title}
Recorded run · | {fallback.when} | · | {fallback.simulated}
{fallback.story}
{fallback.attempt.0.position} | {fallback.attempt.0.model} | {fallback.attempt.0.outcome} | {fallback.attempt.0.kind} | {fallback.attempt.0.seconds}
{fallback.attempt.1.position} | {fallback.attempt.1.model} | {fallback.attempt.1.outcome} | {fallback.attempt.1.kind} | {fallback.attempt.1.seconds}
{fallback.status} | {fallback.outcome} | {fallback.used}
Answered by | {fallback.model} | {fallback.place}
Response time | {fallback.seconds} | both attempts, the first one simulated
Invoice | {fallback.number} | {fallback.same}
Checks | {fallback.checks} | passed
The recording of this second run (JSON)
How the | {chain.length} | models were chosen
Measured on | {bench.day} | , | {bench.window}
<<Free models come and go, so this is a dated snapshot, not a live reading.>>
{bench.method}
Model | No answer | Fully correct | Median answer
{bench.0.model} | {bench.0.about} | {bench.0.no_answer} | {bench.0.correct} | {bench.0.median} | slowest | {bench.0.slowest}
{bench.1.model} | {bench.1.about} | {bench.1.no_answer} | {bench.1.correct} | {bench.1.median} | slowest | {bench.1.slowest}
{bench.2.model} | {bench.2.about} | {bench.2.no_answer} | {bench.2.correct} | {bench.2.median} | slowest | {bench.2.slowest}
{bench.3.model} | {bench.3.about} | {bench.3.no_answer} | {bench.3.correct} | {bench.3.median}
No answer: | <<the call ended without one. A rate limit, a timeout, a rejected format or cut-off output.>>
Fully correct: | <<of the calls that answered, those with every checked field right, or the meeting email refused.>>
Order of the models: | <<a model that rejects the format, or whose usual answer takes more than half of the time limit, is left out. Of the rest, fewest wrong answers first, then fewest calls with no answer. A fallback replaces a failed call, never a wrong answer.>>
Records: | every call behind this table is saved in | {bench.records} | .
The code and the guides
Code on GitHub | {guides.code}
Using the service | <<Covers installing, calling, configuring and troubleshooting the service.>>
The ideas behind it | <<Explains the Applied AI concepts and why the code is shaped this way.>>
The Python, explained | <<Explains the actual code with runnable examples and exercises.>>
Invoice Extraction Service | {page.nature} | {runs.where}
"""


def test_the_page_reads_in_this_order() -> None:
    """ "First model 21 of 21" is a caption and a figure. Each is held above; this holds them together.

    Without it the two tiles could trade figures, a column could trade its title, a
    quotation could be cut short, a new sentence could be put together out of labels,
    and "A recording, not live" could stand somewhere else.
    """
    expected = [part.strip() for line in TRANSCRIPT.strip().splitlines() for part in line.split(" | ")]
    found = reading(BODY)
    for index, (ours, theirs) in enumerate(zip(found, expected)):
        assert ours == theirs, f"piece {index + 1} of the page reads {ours!r}; it is expected to read {theirs!r}"
    assert len(found) == len(expected), f"the page has {len(found)} pieces of text; {len(expected)} are expected"


def test_a_heading_is_followed_by_the_line_that_says_it_is_a_record() -> None:
    """Someone who moves from heading to heading hears what follows a heading, not what stands above it."""
    for heading, dated in (("run-title", "run.when"), ("fallback-title", "fallback.when"), ("measured-title", "bench.day")):
        following = [node for node in by_id(heading).parent.children if isinstance(node, Node)]
        after = following[following.index(by_id(heading)) + 1]
        assert "kicker" in after.classes, f"the line after #{heading} is not its dated line"
        assert after.find(lambda node: node.attrs.get("data-fact") == dated), f"#{heading}: no date follows it"
    # The stylesheet draws each dated line above its heading again, so the page looks as it did.
    assert re.search(r"\.run__head \.kicker \{\s*grid-row: 1;", STYLE)
    assert re.search(r"\.run__head h2 \{\s*grid-row: 2;", STYLE)
    assert re.search(r"\.section__head \.kicker \{\s*order: -1;", STYLE)


#: The only files the page may quote, and it quotes them by name.
QUOTABLE = {"README.md", "app/playground/static/index.html", "app/playground/static/assets/app.js"}


def test_quotations_name_one_of_the_three_files_the_page_borrows_from() -> None:
    """A "quotation" of the page itself, or of this test, would be true of anything."""
    assert {node.attrs["data-quote"] for node in marked("data-quote")} == QUOTABLE


def test_every_link_leads_where_its_words_say() -> None:
    """A link is a statement too: "The recording of this run" has to open that recording."""
    blob = REPOSITORY_URL + "/blob/main/"
    (records,) = BENCHMARK_SNAPSHOT["records"]
    first_recording = f"recorded/{RUN['scenario']['id']}.json"
    second_recording = f"recorded/{FALLBACK['scenario']['id']}.json"
    expected = [
        ("Skip to the content", "#title"),
        ("Invoice Extraction Service", "./"),
        ("Recorded run", "#run"),
        ("When a model fails", "#fallback"),
        ("Measured figures", "#measured"),
        ("Guides", "#guides"),
        ("Live demo", None),  # the script lends it the demo's address
        ("Open the live demo", demo_address()),
        ("Code on GitHub", REPOSITORY_URL),
        (f"See all {CARDINALS[len(BENCHMARK)]} models", "#measured"),
        ("The recording of this run (JSON)", first_recording),
        ("The recording of this second run (JSON)", second_recording),
        (records, blob + records),
        ("Code on GitHub", REPOSITORY_URL),
        ("Using the service", blob + "docs/USAGE.md"),
        ("The ideas behind it", blob + "docs/LEARNING.md"),
        ("The Python, explained", blob + "docs/PYTHON_LEARNING.md"),
    ]
    found = [(clean(link.text()), link.attrs.get("href")) for link in BODY.find(lambda node: node.tag == "a")]
    assert found == expected
    # The first words of the page are its headline: that is where the skip link lands.
    assert by_id("title").tag == "h1"
    # Each recording is linked from the section that shows it, and it is JSON, as the link says.
    assert by_id("run").find(lambda node: node.attrs.get("href") == first_recording)
    assert by_id("fallback").find(lambda node: node.attrs.get("href") == second_recording)
    # Each guide's sentence is what the README says of that guide, not of another one.
    readme = source_text("README.md")
    said_of = {
        "docs/USAGE.md": "docs/USAGE.md {}",
        "docs/LEARNING.md": "docs/LEARNING.md {}",
        "docs/PYTHON_LEARNING.md": "docs/PYTHON_LEARNING.md if you are learning Python: it {}",
    }
    for path, sentence in said_of.items():
        (item,) = [
            entry
            for entry in BODY.find(lambda node: node.tag == "li")
            if entry.find(lambda node: node.attrs.get("href") == blob + path)
        ]
        (quoted,) = item.find(lambda node: "data-quote" in node.attrs)
        said = clean(quoted.text())
        assert sentence.format(said[0].lower() + said[1:]) in readme, f"the README does not say this of {path}: {said!r}"


def test_marks_stand_beside_the_outcomes_they_show() -> None:
    """A tick means it worked and a cross means it failed. The recording decides which is drawn."""

    def mark(inside: Node) -> tuple[str, str]:
        (badge,) = with_class("badge", inside)
        (drawing,) = badge.find(lambda node: node.tag == "use")
        tone = [name for name in badge.classes if name in {"badge--good", "badge--crit"}]
        return (tone[0] if len(tone) == 1 else "?", drawing.attrs["href"])

    def answered(record: dict[str, Any]) -> bool:
        return result(record)["status_code"] == 200 and result(record)["body"]["success"] is True

    tick, cross = ("badge--good", "#i-check"), ("badge--crit", "#i-x")
    assert mark(with_class("out__head")[0]) == (tick if answered(RUN) else cross)
    assert mark(with_class("trace__end")[0]) == (tick if answered(FALLBACK) else cross)
    attempts = events(FALLBACK, "attempt_finished")
    assert [mark(row) for row in with_class("trace__row")] == [
        tick if attempt["outcome"] == "answered" else cross for attempt in attempts
    ]
    (listed,) = with_class("checks")
    rows = [node for node in listed.children if isinstance(node, Node)]
    assert [mark(row) for row in rows] == [
        tick if check_met(check, RUN) else cross for check in RUN["scenario"]["checks"]
    ]
    # The models the chain left out are drawn as context, the chain's own are not.
    drawn = [
        "is-context" in row.classes
        for row in BODY.find(lambda node: node.tag == "tr")
        if row.find(lambda node: (node.attrs.get("data-fact") or "").endswith(".model"))
    ]
    assert drawn == [row["model"] not in DEFAULT_CHAIN for row in BENCHMARK]


#: Everything the stylesheet takes out of sight, or draws without content. A new rule that
#: hides something has to be added here by hand, which is the moment to ask what a visitor
#: stops seeing.
HIDING_RULES = {
    "[hidden]": "what needs the script, until it runs",
    ".sprite": "the sheet of icons; each icon is drawn where it is used",
    ".skip": "the skip link, until the Tab key reaches it",
    ".theme-icon--dark": "one of the theme switch's two icons",
    ':root[data-theme="dark"] .theme-icon--light': "the other one",
    ':root:where(:not([data-theme="light"])) .theme-icon--light': "the other one",
    ".first::before": "the faint rules behind the headline, faded out: a drawing, no words",
    ".demo__unset": "the 'not online yet' note, while there is an address",
    '.demo__link[href*=".invalid/"]': "the way in, while there is no address",
    '.demo__link[href*=".invalid/"] ~ .demo__line': "and its line",
    ".topbar__nav": "the top bar's links to the parts of the page, where there is no room for them",
    ".brand__name": "the name beside the mark on a narrow phone; still the link's name, and at the foot",
}

#: The classes those rules hang on, with the one element each belongs to (its tag and, where it
#: has one, its mark). A class that hides is a way to take words off the screen, so it may not
#: turn up on anything else.
HIDING_CLASSES = {
    "sprite": [("svg", None)],
    "skip": [("a", None)],
    "theme-icon--dark": [("svg", None)],
    "theme-icon--light": [("svg", None)],
    "demo__unset": [("p", "demo.unset")],
    "topbar__nav": [("nav", None)],
    "brand__name": [("span", None)],
}


def test_nothing_the_page_states_is_kept_out_of_sight_or_added_by_the_stylesheet() -> None:
    """ "Simulated" in the HTML and hidden on the screen would pass every comparison of words.

    This reads files, so it knows the plain ways of hiding (the ``hidden`` attribute, ``display``,
    ``visibility``, ``opacity``, a clip, a mask, no size, a place off the screen) and not every
    way a stylesheet has. Only a browser sees the rest.
    """
    hidden = sorted(node.attrs.get("id") or node.tag for node in BODY.walk() if "hidden" in node.attrs)
    assert hidden == ["demo-chip", "demo-clock", "theme-toggle"], "only what needs the script starts hidden"
    for node in BODY.find(lambda node: node.attrs.get("aria-hidden") == "true"):
        assert node.tag == "svg" or not clean(node.text()), "words kept from a screen reader"

    rules = re.sub(r"/\*.*?\*/", "", STYLE, flags=re.DOTALL)
    hiding = re.compile(
        r"display:\s*none|visibility:\s*hidden|opacity:\s*0(?:\.0+)?\s*[;}]|font-size:\s*0[;}\s]|text-indent"
        r"|clip(?:-path)?:(?!\s*none)|mask(?:-image)?:|(?<![-\w])(?:width|height):\s*0[;\s}]"
        r"|(?<![-\w])(?:top|left|right|bottom):\s*-\d"
    )
    found = {
        clean(selector)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", rules)
        if hiding.search(body + "}")
        for selector in selectors.split(",")
    }
    assert found == set(HIDING_RULES), sorted(found ^ set(HIDING_RULES))
    # Each class that hides sits on the element it was written for, and on no other.
    carriers = {
        name: [(node.tag, node.attrs.get("data-fact")) for node in DOCUMENT.walk() if name in node.classes]
        for name in HIDING_CLASSES
    }
    assert carriers == HIDING_CLASSES, "a class that hides is on something it was not written for"
    # The name on a narrow phone is out of sight, not gone: a screen reader still says it.
    assert not re.search(r"\.brand__name \{[^}]*display:\s*none", rules)
    # And the stylesheet adds no words of its own: what it draws before or after an element is empty.
    assert set(re.findall(r"(?<![-\w])content:\s*([^;}]+)", rules)) <= {'""'}, "the stylesheet writes words"
    # A picture draws no words either (text inside <svg> is not read by the tests above).
    for picture in DOCUMENT.find(lambda node: node.tag == "svg"):
        assert not clean(picture.text()), "words drawn inside a picture"
        assert not picture.find(lambda node: node.tag in {"text", "tspan", "textpath", "title", "desc", "foreignobject"})


def test_the_email_is_shown_whole_and_is_not_a_box_that_scrolls() -> None:
    """On a phone a box that scrolls takes the swipe that was meant for the page."""
    rules = re.sub(r"/\*.*?\*/", "", STYLE, flags=re.DOTALL)
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", rules):
        if "paper__text" in selectors:
            assert not re.search(r"(?<![-\w])(?:max-)?height:|overflow(?:-[xy])?:", body), clean(selectors)
    (box,) = with_class("paper__text")
    assert "tabindex" not in box.attrs, "nothing to scroll, so nothing for the Tab key to stop at"


def test_the_preview_picture_is_the_one_that_was_looked_at() -> None:
    """The words in a picture cannot be compared with a record. The picture, and what it was seen to say, can."""
    found = hashlib.sha256((SITE / "og.png").read_bytes()).hexdigest()[:16]
    assert found == PREVIEW_PICTURE["fingerprint"], (
        f"site/og.png has changed; its fingerprint is now {found}. Look at the picture: when it "
        "says what PREVIEW_PICTURE says, put the new fingerprint there."
    )
    # What the picture was seen to say, piece by piece, is what the page says: its name, the
    # line above its headline, its headline, the sentence under it (the picture leaves out
    # "This web service", because the name stands above), its first three tools, and the stamp.
    said = facts()
    opening = "This web service t"
    assert said["what-it-does"].startswith(opening)
    on_the_page = [
        clean(HEAD.find(lambda node: node.tag == "title")[0].text()),
        clean(with_class("kicker", by_id("title").parent)[0].text()),
        clean(by_id("title").text()),
        "T" + said["what-it-does"][len(opening) :],
        " · ".join(said["stack"].split(" · ")[:3]),
        said["run.valid"],
    ]
    assert PREVIEW_PICTURE["says"] == on_the_page, (
        "the page no longer says what the preview picture carries: make site/og.png again, "
        "look at it, then bring PREVIEW_PICTURE up to date"
    )


def test_the_head_holds_these_tags_and_no_others() -> None:
    """A preview title or description added beside the checked ones would be shown first by some sites."""
    named = [
        node.attrs.get("name") or node.attrs.get("property") or node.attrs.get("http-equiv") or "charset"
        for node in HEAD.find(lambda node: node.tag == "meta")
    ]
    assert named == [
        "charset",
        "Content-Security-Policy",
        "viewport",
        "color-scheme",
        "referrer",
        "format-detection",
        "description",
        "og:type",
        "og:title",
        "og:description",
        "og:url",
        "og:image",
        "og:image:type",
        "og:image:width",
        "og:image:height",
        "og:image:alt",
        "twitter:card",
    ]
    assert HEAD.find(lambda node: node.tag == "meta")[0].attrs.get("charset") == "utf-8"
    assert meta("viewport") == "width=device-width, initial-scale=1", "nothing here may stop a reader from zooming"
    # An invoice number is not a telephone number: a phone must not make a link of it.
    assert meta("format-detection") == "telephone=no"


def test_notes_in_the_table_are_what_the_records_show() -> None:
    """A note is a statement of fact written by hand in the snapshot. Each one is held to a record."""
    if not (PROJECT_ROOT / "benchmarks").is_dir():
        pytest.skip("this copy of the project has no benchmarks folder")
    calls = load_records([PROJECT_ROOT / name for name in BENCHMARK_SNAPSHOT["records"]])
    limit = BENCHMARK_SNAPSHOT["timeout_seconds"]

    def of(model: str) -> list[dict[str, Any]]:
        return [call for call in calls if call["model"] == model]

    def wrong_tax_on_the_service_invoice_in_every_pass(row: dict[str, Any]) -> bool:
        wrong = [call for call in of(row["model"]) if call["outcome"] != "no_answer" and not call["correct"]]
        misses = {check["label"] for call in wrong for check in call["checks"] if not check["passed"]}
        return (
            {call["case"] for call in wrong} == {"service-invoice"}
            and sorted(call["pass"] for call in wrong) == list(range(1, BENCHMARK_SNAPSHOT["passes"] + 1))
            and bool(misses)
            and all(label.startswith("Tax is") for label in misses)
        )

    def rejects_the_strict_response_format(row: dict[str, Any]) -> bool:
        return all(
            call["outcome"] == "no_answer"
            and call["error_type"] == "SchemaValidationError"
            and "does not support the requested structured output" in call["message"]
            for call in of(row["model"])
        )

    def usual_answer_takes_most_of_the_limit_and_the_model_has_an_end_date(row: dict[str, Any]) -> bool:
        history = (PROJECT_ROOT / "benchmarks/exploratory/2026-10-07/model-list-history.txt").read_text(encoding="utf-8")
        trace = re.search(rf"^ -- {re.escape(row['model'])}\n((?:     .*\n)+)", history, re.MULTILINE)
        last_seen = trace.group(1).strip().splitlines()[-1] if trace else ""
        return row["median_seconds"] > limit / 2 and "exp=2026-12-31" in last_seen

    held_by = {
        "": lambda row: True,
        "Wrong tax on the service invoice in every pass": wrong_tax_on_the_service_invoice_in_every_pass,
        "Rejects the strict response format": rejects_the_strict_response_format,
        "Usual answer takes most of the time limit; ends 2026-12-31": (
            usual_answer_takes_most_of_the_limit_and_the_model_has_an_end_date
        ),
    }
    for row in BENCHMARK:
        assert row["note"] in held_by, f"a note nobody checks: {row['note']!r}"
        assert held_by[row["note"]](row), f"the records do not show this of {row['model']}: {row['note']!r}"


def test_snapshot_says_when_and_how_the_records_say() -> None:
    """The day, the hours, the passes, the documents and the time limit, taken past the snapshot too.

    ``tests/test_docs.py`` holds the snapshot to the records as well, but the workflow that
    publishes this page runs this file only.
    """
    if not (PROJECT_ROOT / "benchmarks").is_dir():
        pytest.skip("this copy of the project has no benchmarks folder")
    paths = [PROJECT_ROOT / name for name in BENCHMARK_SNAPSHOT["records"]]
    headers = [json.loads(path.read_text(encoding="utf-8").splitlines()[0]) for path in paths]
    started = sorted(call["started"] for call in load_records(paths))
    shot = BENCHMARK_SNAPSHOT
    assert shot["passes"] == sum(header["passes"] for header in headers)
    assert {shot["cases"]} == {len(header["cases"]) for header in headers}
    assert {shot["timeout_seconds"]} == {header["timeout_seconds"] for header in headers}
    assert shot["measured"] == started[0][:10] == started[-1][:10]
    assert shot["window_utc"] == f"{started[0][11:16]} to {started[-1][11:16]}"
    # The documents the page names are the demo's samples of today. They are the ones that were sent.
    for header in headers:
        assert header["cases"] == [case["id"] for case in live_cases()]


# ---------------------------------------------------------------------------
# Front door - the live demo line
# ---------------------------------------------------------------------------


def demo_address() -> str:
    return by_id("demo-link").attrs["href"] or ""


def wrong_with(address: str) -> str:
    """What is wrong with a demo address, said to the person who typed it. Empty when it is right."""
    if address == PLACEHOLDER_ADDRESS or DEMO_ADDRESS.fullmatch(address):
        return ""
    if address != address.strip():
        return "it has a space before or after it"
    if not address.startswith("https://"):
        return "it does not begin with https://"
    name, slash, rest = address[len("https://") :].partition("/")
    if name != name.lower():
        return f"the name {name!r} has a capital letter (the host shows it in lower case)"
    if not name.endswith(".onrender.com"):
        return f"the name {name!r} does not end in .onrender.com"
    if slash + rest != f"/#{DEMO_PART}":
        return f"after the name comes {slash + rest!r} where /#{DEMO_PART} has to stand"
    return f"the name {name!r} is not one a host gives (letters, figures and hyphens)"


def test_the_demo_address_is_written_in_exactly_one_place() -> None:
    """One place to edit, once. A second copy would be the one that gets forgotten."""
    link = by_id("demo-link")
    assert link.tag == "a" and "target" not in link.attrs, "the way to the live demo is a plain link"
    address = demo_address()
    assert not wrong_with(address), (
        f"the live demo's address in site/index.html is {address!r}: {wrong_with(address)}. It has to "
        f"read https://NAME.onrender.com/#{DEMO_PART}, where NAME is the name the host shows for "
        "the service (or the placeholder, until the service exists)."
    )

    # The name is written once in everything that is published, with or without "https://"
    # before it: not in the script, not in the policy line, not in a comment.
    everywhere = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SITE.rglob("*"))
        if path.suffix in {".html", ".css", ".js", ".json", ".svg", ".txt", ".xml", ".webmanifest"}
    )
    written = DEMO_NAME.findall(everywhere)
    assert written == [address[len("https://") :].split("/")[0]], (
        f"the demo's name is written {len(written)} times in site/: {written}"
    )

    # The second way in, in the top bar, has no address of its own: the script lends it this one.
    chip = by_id("demo-chip")
    assert chip.tag == "a" and "href" not in chip.attrs and "hidden" in chip.attrs
    assert "chip.href = address.href;" in SCRIPT


def test_the_demo_address_leads_to_where_a_visitor_runs_a_document() -> None:
    """The top of the demo's page shows this page's headline again. The link goes past it."""
    if not PLAYGROUND.is_dir():
        pytest.skip("this copy of the project has no playground page")
    theirs = parse((PLAYGROUND / "index.html").read_text(encoding="utf-8"))
    found = theirs.find(lambda node: node.attrs.get("id") == DEMO_PART)
    assert len(found) == 1, f"the demo's page has no part with id='{DEMO_PART}', which the link leads to"
    (part,) = found
    assert part.find(lambda node: node.attrs.get("id") == "run"), "the button that runs a document is in that part"
    assert part.find(lambda node: node.attrs.get("id") == "document"), "and so is the box for a document of your own"
    # The icon is asked for at the demo's own address: the part is for the visitor, not for the host.
    assert "new URL(ICON_PATH, address).href" in SCRIPT


def test_the_demo_address_has_been_filled_in() -> None:
    """Skipped, not failed, until then: the page says "not online yet" and wakes nothing."""
    if demo_address() == PLACEHOLDER_ADDRESS:
        pytest.skip("site/index.html still holds the placeholder for the live demo's address")
    assert DEMO_ADDRESS.fullmatch(demo_address()), f"the live demo's address: {wrong_with(demo_address())}"


def test_placeholder_address_shows_a_note_and_no_link_without_any_script() -> None:
    """The stylesheet does this, so it holds with scripting off as well."""
    assert f".invalid/#{DEMO_PART}" in PLACEHOLDER_ADDRESS, "a name that can never be a real address"
    placeholder = r'\.demo__link\[href\*="\.invalid/"\]'
    hidden = rf"{placeholder},\s*{placeholder} ~ \.demo__line \{{\s*display: none;"
    shown = rf"{placeholder} ~ \.demo__unset \{{\s*display: block;"
    assert re.search(hidden, STYLE), "the link and its line are hidden"
    assert re.search(shown, STYLE), "the note is shown in their place"
    assert re.search(r"\.demo__unset \{\s*display: none;", STYLE), "and only then"

    # The rules above reach from the link to what follows it, so the order matters.
    block = by_id("demo")
    order = [node.classes[-1] for node in block.children if isinstance(node, Node)]
    assert order == ["demo__link", "btn--lg", "demo__line", "demo__unset"], order


def test_the_way_in_and_its_line_are_there_without_the_script() -> None:
    """With scripting off the visitor still has the link, and a sentence that is true."""
    link, words, clock = by_id("demo-link"), by_id("demo-words"), by_id("demo-clock")
    assert clean(link.text()) == "Open the live demo"
    assert clean(words.text()), "the line has to say something as written"
    # A status region is read out when it changes, politely, and not otherwise.
    assert words.attrs.get("role") == "status"
    assert words.attrs.get("aria-live", "polite") == "polite"
    # The count of seconds changes every second, so it is outside the region and silent.
    assert clock.attrs.get("aria-hidden") == "true" and "hidden" in clock.attrs
    assert all(node is not words for node in ancestors(clock)), "the count must not be read out"
    # The two things that need the script stay out of sight until it runs.
    assert "hidden" in by_id("theme-toggle").attrs
    assert "data-demo" not in DOCUMENT.find(lambda node: node.tag == "html")[0].attrs


def ancestors(node: Node) -> Iterator[Node]:
    while node.parent is not None:
        node = node.parent
        yield node


def script_sentences() -> dict[str, str]:
    """The sentences the script can put in the line, with the wake-up time filled in."""
    block = re.search(r"const WORDS = \{(.*?)\n  \};", SCRIPT, re.DOTALL)
    assert block
    found = re.findall(r'^\s*(\w+): [`"](.+)[`"],$', block.group(1), re.MULTILINE)
    return {state: sentence.replace("${WAKE_TAKES}", wake_takes()) for state, sentence in found}


def expected_sentences() -> dict[str, str]:
    """The exact words of every state the script can show, held to its own limits and to the code."""
    takes = wake_takes()
    give_up = constant("GIVE_UP_AFTER_MS")
    assert give_up % 60_000 == 0, "the page says this limit in whole minutes"
    assert give_up // 60_000 in CARDINALS, (
        f"the page says this limit in words, from one to {CARDINALS[max(CARDINALS)]} minutes"
    )
    scenarios = scenarios_for(len(DEFAULT_CHAIN))
    their_page = (PLAYGROUND / "index.html").read_text(encoding="utf-8")
    return {
        "waking": (
            "Waking the live demo. It runs on free hosting and sleeps between visits, "
            f"so this takes {takes}."
        ),
        "awake": stated(
            "The live demo is awake. Try a document there yourself, or make a model fail and "
            "watch the next one take over.",
            ('<textarea id="document"' in their_page, "the demo no longer takes a document of your own"),
            (any(any(scenario["simulate"]) for scenario in scenarios), "the demo no longer simulates a failure"),
        ),
        "asleep": f"The live demo may be asleep again. Opening it wakes it, which takes {takes}.",
        "quiet": stated(
            f"The live demo has not answered for {CARDINALS[give_up // 60_000]} minutes, please "
            "contact the admin. This page shows recorded runs instead.",
            (len(RECORDINGS) > 1, "the page no longer shows more than one recorded run"),
        ),
    }


def test_the_line_says_these_words_and_no_others() -> None:
    """Four sentences, written once, in the script. This is their exact wording."""
    assert script_sentences() == expected_sentences()
    assert wake_takes() == "about a minute", "the host's own words for how long waking takes"
    # Every sentence that speaks of the wait gives the host's figure, and none gives another.
    as_written = clean(by_id("demo-words").text())
    for sentence in [*script_sentences().values(), as_written]:
        assert not re.search(r"\d", sentence), f"a number in the line: {sentence}"
    for state in ("waking", "asleep"):
        assert wake_takes() in script_sentences()[state]
    assert wake_takes() in as_written
    # Where the figures come from is said beside them, with the day they were read.
    assert "https://render.com/docs/free" in SCRIPT and "8 October 2026" in SCRIPT
    # The script has three more pieces of words, and no others: the tab's title while the
    # demo is awake and the tab is not looked at, what follows the count of seconds, and one
    # line for the browser's console.
    assert words_of("AWAKE_TITLE") == "Live demo awake"
    assert words_of("COUNT_ENDS") == "so far"
    assert words_of("NOTE") == (
        "This page loads the live demo's icon to learn when the demo is awake. "
        "Requests for it that fail while the demo wakes are expected."
    )
    worded = set(re.findall(r'^\s*const ([A-Z_]+) =\s*[`"]', SCRIPT, re.MULTILINE))
    assert worded == {"WAKE_TAKES", "ICON_PATH", "AWAKE_TITLE", "COUNT_ENDS", "NOTE"}, sorted(worded)


def test_the_script_keeps_within_these_limits() -> None:
    """A tab left open must not keep the free host awake: every request restarts its clock."""
    quiet, retry, timeout = constant("QUIET_START_MS"), constant("RETRY_AFTER_MS"), constant("ATTEMPT_TIMEOUT_MS")
    give_up, most, awake, tick = (
        constant("GIVE_UP_AFTER_MS"),
        constant("MAX_ATTEMPTS"),
        constant("AWAKE_FOR_MS"),
        constant("TICK_MS"),
    )
    # Quick to look, patient enough for a wake-up of about a minute, and not open-ended.
    assert tick <= 1_000 < quiet <= 5_000
    assert 2_000 <= retry < timeout < 60_000
    assert 2 * 60_000 <= give_up <= 5 * 60_000
    # Requests are at least `retry` apart, so the count below can only be reached if the
    # clock misbehaves. It is a limit of last resort, and it is a small one.
    assert give_up // retry < most <= 100
    # The host lets the demo sleep after 15 minutes. "Awake" is claimed for less than that.
    assert 5 * 60_000 <= awake < 15 * 60_000
    # A tab that is not looked at sees its clock once a minute: such a gap counts as waiting,
    # so the page gives up there too. A gap as long as the whole wait is a page that did not run.
    slowed = constant("SLOWED_TICK_MS")
    assert 60_000 < slowed < give_up
    # Looked at again, a page that had stopped asks afresh, but not for ever.
    assert 1 <= constant("MAX_RETURNS") <= 3

    # One place sends a request, one timer drives everything, and it is stopped in one place.
    assert SCRIPT.count("new Image()") == 1
    assert SCRIPT.count("setInterval(") == 1 and SCRIPT.count("clearInterval(") == 1
    assert "setTimeout(" not in SCRIPT, "nothing else is ever scheduled"


def test_the_script_can_call_nothing_and_takes_nobody_away() -> None:
    """No request but the picture, no markup built from text, and no redirect."""
    calls = ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "importScripts", "import(")
    sinks = (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function")
    exits = ("location.href", "location.replace", "location.assign", "location =", "window.open", ".click()")
    for forbidden in calls + sinks + exits:
        assert forbidden not in SCRIPT, f"{forbidden} in door.js"
    assert not HEAD.find(lambda node: (node.attrs.get("http-equiv") or "").lower() == "refresh")
    # Every element the script reaches for is in the page, and it reaches for nothing else.
    reached = set(re.findall(r'getElementById\("([^"]+)"\)', SCRIPT))
    assert reached == {"theme-toggle", "demo-link", "demo-words", "demo-clock", "demo-chip"}
    for name in reached:
        by_id(name)
    assert "querySelector" not in SCRIPT


def test_the_theme_switch_remembers_where_the_page_looks() -> None:
    """theme.js applies the saved choice before first paint; door.js has to save it under that name."""
    theme = THEME_FILE.read_text(encoding="utf-8")
    key = re.search(r'localStorage\.getItem\("([^"]+)"\)', theme)
    assert key and f'localStorage.setItem("{key.group(1)}"' in SCRIPT


_REPLAYED: dict[str, Any] = {}


def replay() -> dict[str, Any]:
    """Run the page's script against a stand-in page whose clock is moved by hand."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed, so the page's script cannot be replayed")
    if not _REPLAYED:
        done = subprocess.run(
            [node, "--input-type=module", "-", str(SCRIPT_FILE)],
            input=STAND_IN_PAGE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert done.returncode == 0, done.stderr
        _REPLAYED.update(json.loads(done.stdout))
    return _REPLAYED


def test_the_script_asks_as_little_as_it_says_and_then_stops() -> None:
    """The script itself, replayed: what it asks for, what it says, and that it stops.

    It needs Node.js and nothing else: no browser and no network. The stand-in page at
    the end of this file gives the script its five elements, a picture that answers as
    each case says, and a clock that is moved by hand.
    """
    seen = replay()
    quiet, retry, timeout = constant("QUIET_START_MS"), constant("RETRY_AFTER_MS"), constant("ATTEMPT_TIMEOUT_MS")
    give_up, awake, tick = constant("GIVE_UP_AFTER_MS"), constant("AWAKE_FOR_MS"), constant("TICK_MS")
    most, returns = constant("MAX_ATTEMPTS"), constant("MAX_RETURNS")
    demo = f"https://example-demo.onrender.com/#{DEMO_PART}"
    # The icon is asked for at the demo's own address: the part of the page is not sent.
    icon = [f"https://example-demo.onrender.com{icon_path()}?wake=N"]
    plain, claimed = "Invoice Extraction Service", f"{words_of('AWAKE_TITLE')} · Invoice Extraction Service"

    def looks(after: int) -> int:
        """The first time the script looks at its clock at or after `after`."""
        return -(-after // tick) * tick

    # The placeholder: no request at all, no timer left running, and no second way in, also
    # when the tab is looked at again.
    case = seen["placeholder"]
    assert (case["pictures"], case["line"], case["timers_left"], case["state"]) == (0, [], 0, None)
    assert case["chip"] == {"hidden": True, "href": None} and case["switch_hidden"] is False
    assert case["console"] == []

    # While the page itself is still loading, nothing is asked: a request that is open then
    # keeps the browser's own "loading" sign on for as long as the host holds it.
    assert seen["before_the_page_has_loaded"]["pictures"] == 0
    assert seen["once_the_page_has_loaded"]["asked_at"] == [5_000]
    # A page the browser loads ahead of time, in case it is opened, wakes nothing either.
    assert seen["loaded_ahead_of_time"]["pictures"] == 0
    assert seen["then_really_opened"]["asked_at"] == [60_000]

    # Already awake: straight to "awake", never "waking". One request, and then none: not
    # when the 14 minutes are over, and not ten hours later. Nothing is said in the console.
    case = seen["awake_at_once"]
    assert (case["pictures"], case["line"]) == (1, [[150, "awake"]])
    assert case["chip"] == {"hidden": False, "href": demo}
    assert case["addresses"] == icon and case["clock_hidden"] is True and case["console"] == []
    case = seen["awake_at_once_ten_hours_later"]
    assert case["line"] == [[150, "awake"], [looks(awake), "asleep"]]
    assert (case["pictures"], case["timers_left"]) == (1, 0)
    # Awake, on a slow connection: an answer that takes two and a half seconds is not
    # announced as a wake-up first.
    assert seen["awake_but_slow_to_answer"]["line"] == [[2_500, "awake"]]

    # Every request fails at once (a loading page, a refused connection) and nothing ever
    # answers: "waking" at the first failure, a request every `retry` at the soonest, "not
    # answering" when the wait is over, and then nothing, however long the tab stays open.
    case = seen["fails_at_once"]
    asks = [0]
    while looks(asks[-1] + 30 + retry) < give_up:
        asks.append(looks(asks[-1] + 30 + retry))
    assert case["asked_at"] == asks and case["different_addresses"] == len(asks)
    assert len(asks) <= give_up // retry + 1
    assert case["line"] == [[30, "waking"], [give_up, "quiet"]]
    assert case["clock_hidden"] is True and case["timers_left"] == 0 and case["most_open_at_once"] == 1
    assert seen["fails_at_once_an_hour_later"] == case
    count = seen["fails_at_once_half_way"]["clock"]
    assert count == f"1:30 {words_of('COUNT_ENDS')}", "the count beside the line, while waking"
    # An engineer who opens the console finds the failed requests explained, once.
    assert case["console"] == [f"info: {words_of('NOTE')}"]

    # The request is taken and never answered: "waking" after the quiet start, a new request
    # every `timeout`, and every one of them withdrawn by the time the page gives up. One
    # request is left on its way beside the newest, so two are open at most.
    case = seen["never_answered"]
    assert case["asked_at"] == list(range(0, give_up, timeout))
    assert case["withdrawn"] == case["pictures"] == give_up // timeout
    assert case["line"] == [[looks(quiet), "waking"], [give_up, "quiet"]] and case["timers_left"] == 0
    assert (case["most_open_at_once"], case["open_now"], case["console"]) == (2, 0, [])

    # The request is held and then answered: what a host may do while the service wakes.
    case = seen["held_then_answered"]
    assert case["line"] == [[looks(quiet), "waking"], [58_000, "awake"]]
    assert case["pictures"] == 58_000 // timeout + 1 and case["open_now"] == 0

    # Each request is answered only 25 seconds after it was sent, longer than the page waits
    # before it asks again. The request that was left on its way is the one that answers.
    case = seen["each_answer_takes_25_seconds"]
    assert case["asked_at"] == [0, timeout] and case["line"] == [[looks(quiet), "waking"], [25_000, "awake"]]
    assert (case["withdrawn"], case["open_now"]) == (1, 0)
    # "Awake" is counted from the moment that request was sent.
    assert seen["each_answer_takes_25_seconds_later"]["line"][-1] == [looks(awake), "asleep"]
    # That request fails late, while a newer one is on its way: nothing is counted twice.
    case = seen["kept_request_fails_late"]
    assert case["asked_at"] == list(range(0, give_up, timeout)) and case["withdrawn"] == case["pictures"] - 1
    assert case["line"] == [[looks(quiet), "waking"], [give_up, "quiet"]]
    assert (case["most_open_at_once"], case["open_now"], case["timers_left"]) == (2, 0, 0)

    # A loading page comes back at once until the service is up: the other thing a host may do.
    case = seen["loading_page_then_answered"]
    assert case["line"][0] == [40, "waking"] and case["line"][-1][1] == "awake" and len(case["line"]) == 2
    assert 50_000 < case["line"][-1][0] <= 50_000 + retry + tick + 200

    # A tab that is not being looked at. After its first minute the browser lets it see its
    # clock once a minute; its requests still go out, so that time is waiting like any other.
    # It gives up within a minute or two of the three minutes, after a handful of requests.
    first_minute = [at for at in asks if at <= 60_000]
    assert seen["not_looked_at_first_minute"]["asked_at"] == first_minute
    case = seen["not_looked_at"]
    assert case["state"] == "quiet" and case["timers_left"] == 0
    assert give_up <= case["line"][-1][0] <= give_up + 2 * 60_000
    assert len(first_minute) < case["pictures"] <= len(first_minute) + give_up // 60_000
    assert seen["not_looked_at_an_hour_later"] == case
    # From its first look on: three looks, three requests, and the end.
    case = seen["a_look_a_minute"]
    assert (case["pictures"], case["state"], case["timers_left"]) == (give_up // 60_000, "quiet", 0)
    # The demo comes up between two looks. The page does not give up on the strength of an
    # answer that is a minute old: it asks once more, and that answer decides.
    case = seen["not_looked_at_and_the_demo_comes_up"]
    assert [state for _, state in case["line"]] == ["waking", "awake"] and case["line"][-1][0] > give_up
    # The same with a host that holds every request: the page still gives up.
    case = seen["not_looked_at_and_held"]
    assert case["state"] == "quiet" and case["timers_left"] == 0 and case["open_now"] == 0
    assert give_up <= case["line"][-1][0] <= give_up + 2 * 60_000

    # A tab the browser had frozen, or a laptop that slept. While waiting, the time away does
    # not count as waiting ...
    case = seen["frozen_while_waking"]
    assert case["state"] == "waking" and case["pictures"] >= 2
    assert seen["frozen_while_waking_later"]["line"][-1] == [10 * 60_000 + give_up - 2 * tick, "quiet"]
    # A page that only runs for a moment every two minutes never gets to the end of its
    # waiting. There the limit of last resort ends it: so many requests, and not one more.
    case = seen["a_moment_every_two_minutes"]
    assert (case["pictures"], case["state"], case["timers_left"]) == (most, "quiet", 0)
    # ... and once awake, the page stops saying so as soon as it runs again.
    case = seen["set_aside_while_awake"]
    assert (case["state"], case["pictures"], case["timers_left"]) == ("asleep", 1, 0)
    # An answer that reaches a page set aside for longer than that is never called "awake".
    assert [state for _, state in seen["answered_too_late"]["line"]] == ["asleep"]

    # A page that has stopped asks afresh when its tab is looked at again: after "asleep" ...
    case = seen["looked_at_again_after_asleep"]
    assert [state for _, state in case["line"]] == ["awake", "asleep", "awake"] and case["pictures"] == 2
    # ... and after giving up. Twice at most: a tab nobody cares about cannot wake the demo all day.
    # Coming back at the very moment the waiting ran out is not "no answer": the page had
    # only just asked, and that request decides.
    assert seen["back_just_as_the_waiting_ran_out"]["line"][-1][1] == "awake"

    case = seen["looked_at_again_after_giving_up"]
    assert case["pictures_after_each_look"] == [len(asks) * min(look, returns + 1) for look in range(1, 7)]
    assert (case["state"], case["timers_left"]) == ("quiet", 0)

    # Someone who went to another tab sees in its title that the wait is over ...
    assert seen["awake_in_another_tab"]["title"] == claimed
    # ... but a page the browser freezes could not take that back when its time is up: it
    # gives the claim up as it is frozen, and makes it again if it is thawed in time.
    assert seen["frozen_in_another_tab"]["title"] == plain
    assert seen["thawed_in_another_tab"]["title"] == claimed
    assert seen["back_in_this_tab"]["title"] == plain
    assert seen["back_in_this_tab"]["pictures"] == seen["awake_in_another_tab"]["pictures"]

    # The visitor follows the link. The page that is left behind keeps its plain title in
    # the Back button's list, although it is hidden and the demo is awake: in a browser that
    # freezes the page it keeps, and in one that does not.
    for kept_how in ("left_for_the_demo", "left_and_not_frozen"):
        case = seen[kept_how]
        assert (case["state"], case["title"]) == ("awake", plain), kept_how
    # The Back button brings the page back as it was left: that is a new visit, so it asks
    # afresh, once, in whatever order the browser tells the page about it.
    for order in ("back_one_way", "back_the_other_way"):
        case = seen[order]
        assert [state for _, state in case["line"]] == ["awake", "asleep", "awake"], order
        assert (case["pictures"], case["title"]) == (2, plain), order
    case = seen["back_from_the_demo"]
    assert (case["pictures"], case["state"]) == (len(asks) + 1, "awake")
    assert case["line"][-1][1] == "awake"
    # An ordinary page show does not.
    assert seen["plain_pageshow"]["pictures"] == len(asks) + 1

    # The exact words of every state, as the script put them in the line.
    words = {**seen["fails_at_once"]["words"], **seen["awake_at_once_ten_hours_later"]["words"]}
    assert words == expected_sentences()


#: What the page's script reads and writes on the page, and nothing else (" =" marks a write).
SCRIPT_TOUCHES = {
    "document.documentElement",
    "document.getElementById",
    "document.title",
    "document.title =",
    "document.hidden",
    "document.readyState",
    "document.prerendering",
    "document.addEventListener",
    "root.dataset",
    "root.dataset.demo =",
    "root.dataset.theme",
    "root.dataset.theme =",
    "theme-toggle.hidden =",
    "theme-toggle.addEventListener",
    "demo-link.href",
    "demo-words.textContent =",
    "demo-clock.textContent =",
    "demo-clock.hidden =",
    "demo-chip.href =",
    "demo-chip.hidden =",
}


def test_the_script_touches_the_page_in_these_places_only() -> None:
    """Words can be written in more ways than one. The stand-in page notes every way that is tried.

    So the script can put words in two places only, the live line and its count: nothing it
    does can make a recorded run or a measured figure say something else.
    """
    seen = replay()
    touched = {place for case in seen.values() for place in case["touched"]}
    assert touched == SCRIPT_TOUCHES, sorted(touched ^ SCRIPT_TOUCHES)
    assert seen["theme_switched"]["theme"] == "dark", "the switch changes the colours, and that is all it does"


# ---------------------------------------------------------------------------
# Front door - what the service owes
# ---------------------------------------------------------------------------


def icon_path() -> str:
    found = re.search(r'const ICON_PATH = "([^"]+)";', SCRIPT)
    assert found
    return found.group(1)


@pytest.mark.asyncio
async def test_the_service_serves_its_icon_to_any_page_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page learns that the demo is awake by loading the demo's icon from another address.

    That works only while the service serves that file: with no key set, as a picture,
    without a header that forbids another site to show it, and without letting anything
    keep a copy. This asks the app itself, set up as the host runs it.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://invoice-extraction-service.onrender.com")
    # The host is not told whether to serve the demo's page (render.yaml does not set this),
    # so it runs on the code's own default. The test suite's setting must not stand in for it.
    monkeypatch.delenv("PLAYGROUND_ENABLED", raising=False)
    extractors.reset_extractors()
    as_hosted = create_app()

    another_page = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Dest": "image",
    }
    transport = httpx.ASGITransport(app=as_hosted)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"{icon_path()}?wake=1", headers=another_page)
        home = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.content == ICON_FILE.read_bytes(), "the same mark on both pages"
    # Either of these would make a browser refuse the picture to a page at another address.
    assert "cross-origin-resource-policy" not in response.headers
    assert "content-disposition" not in response.headers
    # Nothing may answer in the service's place: not the browser's cache, not the host's.
    assert response.headers["cache-control"] == "no-cache"
    # And what the page's link opens is the demo: the playground, under the same name, with
    # the part of it that the link leads to.
    assert home.status_code == 200
    title = clean(HEAD.find(lambda node: node.tag == "title")[0].text())
    assert f"<title>{title}</title>" in home.text
    assert f'id="{DEMO_PART}"' in home.text


def test_the_host_runs_the_free_plan_and_keeps_the_demo_on() -> None:
    """ "Free hosting" that "sleeps between visits" is only true of the free plan."""
    blueprint = PROJECT_ROOT / "render.yaml"
    if not blueprint.is_file():
        pytest.skip("this copy of the project does not say where the service is hosted")
    text = blueprint.read_text(encoding="utf-8")
    assert re.search(r"^\s*plan:\s*free\b", text, re.MULTILINE)
    # The icon is served by the demo's page: with that page switched off, the front door
    # would wait for ever.
    off = re.search(r"key:\s*PLAYGROUND_ENABLED\s*\n\s*value:\s*[\"']?(false|0|no|off)", text, re.IGNORECASE)
    assert not off, "with the playground off, the page would never learn that the demo is awake"
    # A change to this page alone must not rebuild the service, which would wake it.
    assert re.search(r"^\s*-\s*site/\*\*\s*$", text, re.MULTILINE), "site/** is not among the ignored paths"


# ---------------------------------------------------------------------------
# Front door - the page's own rules
# ---------------------------------------------------------------------------


def test_policy_allows_the_page_its_own_files_and_the_demo_icon_only() -> None:
    policy = dict(part.strip().split(" ", 1) for part in meta("Content-Security-Policy").split(";"))
    assert policy == {
        "default-src": "'none'",
        "script-src": "'self'",
        "style-src": "'self'",
        "img-src": "'self' https://*.onrender.com",
        "base-uri": "'none'",
        "form-action": "'none'",
    }
    # A policy in a <meta> tag counts from where it stands: before anything is loaded.
    first_load = min(PAGE.index("<link"), PAGE.index("<script"))
    assert PAGE.index('http-equiv="Content-Security-Policy"') < first_load
    assert meta("referrer") == "no-referrer"


def test_page_has_no_inline_script_or_style_and_no_frame() -> None:
    """The policy would refuse them, and the page would lose its styling in silence."""
    assert not DOCUMENT.find(lambda node: node.tag == "style")
    assert not DOCUMENT.find(lambda node: "style" in node.attrs)
    scripts = DOCUMENT.find(lambda node: node.tag == "script")
    assert scripts and all(node.attrs.get("src") and not node.children for node in scripts)
    handlers = [name for node in DOCUMENT.walk() for name in node.attrs if name.startswith("on")]
    assert not handlers, handlers
    foreign = {"iframe", "frame", "object", "embed", "img", "form"}
    assert not DOCUMENT.find(lambda node: node.tag in foreign)
    # A control can carry words in its value, where the comparisons of words do not look.
    controls = {"input", "textarea", "select", "output", "progress", "meter"}
    assert not DOCUMENT.find(lambda node: node.tag in controls), "the page has no control that holds words"
    assert "url(" not in STYLE and "@import" not in STYLE, "the stylesheet loads nothing"


def loaded_files() -> list[str]:
    """What the browser fetches to draw the page: scripts, and <link> tags that load a file."""
    loads = [node.attrs["src"] for node in DOCUMENT.find(lambda node: node.tag == "script")]
    loads += [
        node.attrs["href"]
        for node in DOCUMENT.find(lambda node: node.tag == "link")
        if node.attrs.get("rel") in {"stylesheet", "icon"}
    ]
    return loads


def test_page_loads_only_its_own_files_by_relative_address() -> None:
    """A project site lives under /Invoice-Extraction-Service/: "/assets/..." would point outside it."""
    loads = loaded_files()
    assert len(loads) == 4
    for address in loads:
        assert not re.match(r"(?:[a-z][a-z0-9+.-]*:)?//", address), f"{address} is at another address"
        assert not address.startswith("/"), f"{address} leaves the project's path"
        assert (SITE / address).is_file(), f"{address} is not in site/"
    assert not HEAD.find(lambda node: node.tag == "base")
    assert sorted(SITE / name for name in ["index.html", *loads]) == sorted(FIRST_VIEW)


def test_first_view_stays_small() -> None:
    weight = sum(path.stat().st_size for path in FIRST_VIEW)
    assert weight <= FIRST_VIEW_BUDGET, f"{weight} bytes for the first view"


def test_every_link_leads_somewhere_that_exists() -> None:
    links = BODY.find(lambda node: node.tag == "a" and "href" in node.attrs)
    kinds = set()
    for link in links:
        address = link.attrs["href"]
        assert clean(link.text()), f"a link to {address} with no words"
        if address == demo_address():
            kinds.add("demo")
        elif address == "./":
            kinds.add("this page")
        elif address.startswith("#"):
            by_id(address[1:])
            kinds.add("a part of this page")
        elif address == REPOSITORY_URL:
            kinds.add("the code")
        elif address.startswith(REPOSITORY_URL + "/"):
            kind, branch, path = address[len(REPOSITORY_URL) + 1 :].split("/", 2)
            assert (kind, branch) in {("blob", "main"), ("tree", "main")}, address
            target = PROJECT_ROOT / path
            assert target.is_file() if kind == "blob" else target.is_dir(), f"{path} is not in the repository"
            kinds.add("a file of the repository")
        else:
            assert not re.match(r"[a-z][a-z0-9+.-]*:", address) and not address.startswith("/"), address
            assert (SITE / address).is_file(), f"{address} is not in site/"
            kinds.add("a recording")
    assert kinds == {"demo", "this page", "a part of this page", "the code", "a file of the repository", "a recording"}

    # Each recording can be opened from the page, and nothing else is published beside them.
    opened = {link.attrs["href"] for link in links if link.attrs["href"].startswith("recorded/")}
    assert opened == {f"recorded/{name}.json" for name in RECORDINGS}


def test_link_preview_names_this_page_and_a_picture_of_the_right_size() -> None:
    """What LinkedIn, Slack and a mail program show when the address is pasted."""
    canonical = HEAD.find(lambda node: node.tag == "link" and node.attrs.get("rel") == "canonical")
    assert canonical[0].attrs["href"] == PAGES_URL
    assert meta("og:url") == PAGES_URL and meta("og:type") == "website"
    assert meta("og:title") == clean(HEAD.find(lambda node: node.tag == "title")[0].text())
    assert meta("twitter:card") == "summary_large_image"

    picture = meta("og:image")
    assert picture.startswith(PAGES_URL), "a preview picture needs its whole address"
    data = (SITE / picture[len(PAGES_URL) :]).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    assert struct.unpack(">II", data[16:24]) == (1200, 630)
    assert (meta("og:image:width"), meta("og:image:height"), meta("og:image:type")) == ("1200", "630", "image/png")


def custom_properties(css: str, selector: str) -> dict[str, str]:
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\n\s*\}", css, re.DOTALL)
    assert block, selector
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block.group(1)))


def test_the_page_wears_the_demos_colours_and_marks() -> None:
    """One product, two pages: the same colour for the same role, the same icon, the same switch."""
    if not PLAYGROUND.is_dir():
        pytest.skip("this copy of the project has no playground page")
    theirs = (PLAYGROUND / "assets" / "app.css").read_text(encoding="utf-8")
    for selector in (":root", ':root:where(:not([data-theme="light"]))', ':root[data-theme="dark"]'):
        ours = custom_properties(STYLE, selector)
        assert len(ours) > 20, f"{selector}: the colour roles were not found"
        assert ours == custom_properties(theirs, selector), selector
    # Every colour a rule asks for is one of those roles.
    asked_for = set(re.findall(r"var\((--[\w-]+)\)", STYLE))
    assert asked_for <= set(custom_properties(theirs, ":root")), sorted(asked_for)

    for name in ("favicon.svg", "theme.js"):
        ours_bytes = (SITE / "assets" / name).read_bytes().replace(b"\r\n", b"\n")
        theirs_bytes = (PLAYGROUND / "assets" / name).read_bytes().replace(b"\r\n", b"\n")
        assert ours_bytes == theirs_bytes, f"site/assets/{name} is no longer the demo's own file"

    def symbols(html: str) -> dict[str, str]:
        return {name: markup for markup, name in re.findall(r'(<symbol id="(i-[a-z-]+)".*?</symbol>)', html)}

    their_page = (PLAYGROUND / "index.html").read_text(encoding="utf-8")
    ours_symbols, their_symbols = symbols(PAGE), symbols(their_page)
    assert ours_symbols and all(their_symbols.get(name) == markup for name, markup in ours_symbols.items())
    used = {node.attrs["href"].removeprefix("#") for node in DOCUMENT.find(lambda node: node.tag == "use")}
    assert used == set(ours_symbols), "an icon is used but not in the sprite, or in the sprite and unused"
    mark = re.compile(r'<svg class="brand__mark".*?</svg>', re.DOTALL)
    assert clean(mark.search(PAGE).group(0)) == clean(mark.search(their_page).group(0))


def test_page_has_the_structure_a_screen_reader_moves_by() -> None:
    assert DOCUMENT.find(lambda node: node.tag == "html")[0].attrs.get("lang") == "en"
    assert len(BODY.find(lambda node: node.tag == "h1")) == 1
    for landmark in ("header", "main", "footer"):
        found = [node for node in BODY.children if isinstance(node, Node) and node.tag == landmark]
        assert len(found) == 1, landmark
    for section in BODY.find(lambda node: node.tag == "section"):
        assert section.attrs.get("aria-label") or by_id(section.attrs["aria-labelledby"])
    for node in BODY.find(lambda node: "aria-labelledby" in node.attrs):
        by_id(node.attrs["aria-labelledby"])
    for button in BODY.find(lambda node: node.tag == "button"):
        assert button.attrs.get("aria-label") or clean(button.text())
    # A box that can scroll must be reachable from the keyboard. There is one: the table, when
    # a reader's own text spacing makes it wider than a narrow screen. Nothing else is a stop
    # for the Tab key without being a link or a button.
    (box,) = with_class("table-scroll")
    assert box.attrs.get("tabindex") == "0" and box.attrs.get("role") == "region"
    assert BODY.find(lambda node: "tabindex" in node.attrs) == [box]
    # Each table has a name, taken from words that are on the page.
    for table in BODY.find(lambda node: node.tag == "table"):
        assert clean(by_id(table.attrs["aria-labelledby"]).text())
    # Every picture is decoration: the words beside it say the same.
    for picture in BODY.find(lambda node: node.tag == "svg"):
        assert picture.attrs.get("aria-hidden") == "true"
    assert "@media (prefers-reduced-motion: reduce)" in STYLE
    assert ":focus-visible" in STYLE
    (skip,) = with_class("skip")
    by_id(skip.attrs["href"][1:])
    ids = [node.attrs["id"] for node in DOCUMENT.find(lambda node: "id" in node.attrs)]
    assert len(ids) == len(set(ids)), "an id is used twice"


def test_the_site_holds_what_the_page_needs_and_nothing_else() -> None:
    """What is in site/ is published. A stray file would be published with it."""
    published = sorted(path.relative_to(SITE).as_posix() for path in SITE.rglob("*") if path.is_file())
    assert published == sorted(
        [
            "index.html",
            "og.png",
            "assets/door.css",
            "assets/door.js",
            "assets/favicon.svg",
            "assets/theme.js",
            *(f"recorded/{name}.json" for name in RECORDINGS),
        ]
    )


# ---------------------------------------------------------------------------
# Front door - publishing
# ---------------------------------------------------------------------------


def test_workflow_checks_the_page_and_then_publishes_this_folder() -> None:
    """The page goes out through .github/workflows/pages.yml, and only past these tests."""
    workflow = PROJECT_ROOT / ".github" / "workflows" / "pages.yml"
    if not workflow.is_file():
        pytest.skip("this copy of the project has no workflow that publishes the page")
    text = workflow.read_text(encoding="utf-8")
    for line in (
        "- uses: actions/checkout@v7",
        "- uses: actions/configure-pages@v6",
        "- uses: actions/upload-pages-artifact@v5",
        "uses: actions/deploy-pages@v5",
        "path: site",
        "name: github-pages",
        "contents: read",
        "pages: write",
        "id-token: write",
        "- site/**",
        f"- tests/{Path(__file__).name}",
        "needs: check",
    ):
        assert line in text, f"pages.yml no longer has: {line}"
    # The page is compared with its sources before it is published.
    check = re.search(rf"^\s+run: python -m pytest .*tests/{Path(__file__).name}", text, re.MULTILINE)
    assert check and check.start() < text.index("actions/upload-pages-artifact@")
    assert PAGES_URL in text
    # The address of the live demo lives in the page, and only there.
    assert "onrender.com" not in text


# ---------------------------------------------------------------------------
# Front door - the stand-in page
#
# JavaScript, run by Node.js in the replay tests above. It gives the page's script its
# five elements, a picture that answers as each case says and a clock that is moved by
# hand, and it prints what the script did and every place of the page it touched.
# ---------------------------------------------------------------------------

STAND_IN_PAGE = r"""
import { readFileSync } from "node:fs";

const source = readFileSync(process.argv[2], "utf8");
const MINUTE = 60_000;

// A page with the elements the script uses, and a host that behaves as `answer` says:
// answer(count, at) gives ["load" | "error" | "hang", after how many milliseconds].
function world(address, answer, { loaded = true, prerendering = false, hidden = false } = {}) {
  const start = Date.UTC(2026, 9, 8, 12, 0, 0);
  let now = start;
  let nextId = 1;
  const timers = new Map();
  const pictures = [];
  const line = [];
  const said = [];
  const listeners = {};
  let open = 0;
  let mostOpen = 0;

  class Clock extends Date {
    constructor(...parts) {
      if (parts.length) super(...parts);
      else super(now);
    }
    static now() {
      return now;
    }
  }
  const schedule = (task, ms, every = 0) => {
    const id = nextId++;
    timers.set(id, { id, at: now + ms, task, every });
    return id;
  };
  // Move the clock forward, running every timer that falls due, in order.
  const advance = (ms) => {
    const target = now + ms;
    for (;;) {
      let next = null;
      for (const timer of timers.values()) {
        if (timer.at <= target && (!next || timer.at < next.at || (timer.at === next.at && timer.id < next.id))) next = timer;
      }
      if (!next) break;
      // A timer that fell due while the clock was moved by hand runs now, once.
      now = Math.max(now, next.at);
      if (next.every) next.at = now + next.every;
      else timers.delete(next.id);
      next.task();
    }
    now = target;
  };
  const on = (who) => (type, handler) => (listeners[`${who}:${type}`] ??= []).push(handler);
  const fire = (who, type, event = {}) => (listeners[`${who}:${type}`] ?? []).forEach((handler) => handler(event));

  // Every place of the page the script reads or writes is noted (" =" marks a write).
  const touched = new Set();
  const watch = (name, target) =>
    new Proxy(target, {
      get(object, key) {
        if (typeof key === "string") touched.add(`${name}.${key}`);
        return Reflect.get(object, key);
      },
      set(object, key, value) {
        touched.add(`${name}.${String(key)} =`);
        return Reflect.set(object, key, value);
      },
    });

  const dataset = {};
  const root = { dataset: watch("root.dataset", dataset) };
  let words = null;
  const page = {
    "theme-toggle": { hidden: true, addEventListener: on("switch") },
    "demo-link": { href: address },
    "demo-words": {
      get textContent() {
        return words;
      },
      set textContent(value) {
        words = value;
        line.push({ at: now - start, state: dataset.demo, words: value });
      },
    },
    "demo-clock": { hidden: true, textContent: "" },
    "demo-chip": { hidden: true, href: null },
  };
  const document = {
    documentElement: watch("root", root),
    title: "Invoice Extraction Service",
    hidden,
    readyState: loaded ? "complete" : "interactive",
    prerendering,
    getElementById: (id) => (page[id] ? watch(id, page[id]) : null),
    addEventListener: on("document"),
  };

  class Picture {
    constructor() {
      this.onload = null;
      this.onerror = null;
      this.asked = now - start;
      this.withdrawn = false;
      this.open = false;
      this.address = null;
      pictures.push(this);
    }
    close() {
      if (this.open) open -= 1;
      this.open = false;
    }
    set src(value) {
      this.address = value;
      this.open = true;
      open += 1;
      mostOpen = Math.max(mostOpen, open);
      const [what, after] = answer(pictures.length, now - start);
      // A request that was withdrawn is closed already. Its handlers are still called here:
      // it is the script's business to have taken them away.
      if (what === "load") schedule(() => (this.close(), this.onload?.()), after);
      if (what === "error") schedule(() => (this.close(), this.onerror?.()), after);
    }
    removeAttribute(name) {
      if (name !== "src" || !this.open) return;
      this.withdrawn = true;
      this.close();
    }
  }

  // The script, given this page and this clock in place of the browser's. Its console is
  // kept apart: what it says there must not be mixed into what this file prints.
  const names = ["document", "addEventListener", "Image", "setInterval", "clearInterval", "setTimeout", "clearTimeout", "console"];
  const speak = (kind) => (...parts) => said.push(`${kind}: ${parts.join(" ")}`);
  new Function(...names, "Date", "matchMedia", "localStorage", source)(
    watch("document", document),
    on("window"),
    Picture,
    (task, ms) => schedule(task, ms, ms),
    (id) => timers.delete(id),
    (task, ms = 0) => schedule(task, ms),
    (id) => timers.delete(id),
    { info: speak("info"), log: speak("log"), warn: speak("warn"), error: speak("error"), debug: speak("debug") },
    Clock,
    () => ({ matches: false }),
    { setItem() {}, getItem: () => null },
  );

  return {
    advance,
    document,
    fire,
    // The clock moves but no timer runs: a tab the browser had frozen, a sleeping laptop.
    jump: (ms) => (now += ms),
    // `ms` go by in which the page may run for a moment only: a tab that is not looked at.
    glance: (ms) => {
      now += ms - 400;
      advance(400);
    },
    // The tab goes out of sight, or comes back in front of someone.
    show: (visible) => {
      document.hidden = !visible;
      fire("document", "visibilitychange");
    },
    report: () => ({
      state: dataset.demo ?? null,
      theme: dataset.theme ?? null,
      pictures: pictures.length,
      asked_at: pictures.map((picture) => picture.asked),
      withdrawn: pictures.filter((picture) => picture.withdrawn).length,
      open_now: open,
      most_open_at_once: mostOpen,
      addresses: [...new Set(pictures.map((picture) => picture.address.replace(/wake=[0-9]+/, "wake=N")))],
      different_addresses: new Set(pictures.map((picture) => picture.address)).size,
      line: line.map((entry) => [entry.at, entry.state]),
      words: Object.fromEntries(line.map((entry) => [entry.state, entry.words])),
      clock: page["demo-clock"].textContent,
      clock_hidden: page["demo-clock"].hidden,
      chip: { hidden: page["demo-chip"].hidden, href: page["demo-chip"].href },
      switch_hidden: page["theme-toggle"].hidden,
      title: document.title,
      console: [...said],
      timers_left: timers.size,
      touched: [...touched].sort(),
    }),
  };
}

const DEMO = "https://example-demo.onrender.com/#playground";
const seen = {};

// The placeholder: nothing is asked, ever, also when the tab is looked at again.
{
  const w = world("https://live-demo-not-deployed-yet.invalid/#playground", () => ["error", 0]);
  w.advance(60 * MINUTE);
  w.show(false);
  w.show(true);
  w.advance(MINUTE);
  seen.placeholder = w.report();
}

// The page is still loading (a slow connection): nothing is asked until it has loaded.
{
  const w = world(DEMO, () => ["load", 150], { loaded: false });
  w.advance(5_000);
  seen.before_the_page_has_loaded = w.report();
  w.document.readyState = "complete";
  w.fire("window", "load");
  w.advance(5_000);
  seen.once_the_page_has_loaded = w.report();
}

// The browser loads the page ahead of time, and a minute later it is really opened.
{
  const w = world(DEMO, () => ["load", 150], { prerendering: true });
  w.advance(MINUTE);
  seen.loaded_ahead_of_time = w.report();
  w.document.prerendering = false;
  w.fire("document", "prerenderingchange");
  w.advance(5_000);
  seen.then_really_opened = w.report();
}

// Already awake: the answer comes before there is anything to say.
{
  const w = world(DEMO, () => ["load", 150]);
  w.advance(5 * MINUTE);
  seen.awake_at_once = w.report();
  w.advance(600 * MINUTE);
  seen.awake_at_once_ten_hours_later = w.report();
}

// Awake, and the answer takes two and a half seconds to arrive.
{
  const w = world(DEMO, () => ["load", 2_500]);
  w.advance(MINUTE);
  seen.awake_but_slow_to_answer = w.report();
}

// Every request fails 30 ms after it was sent, for as long as the page asks.
{
  const w = world(DEMO, () => ["error", 30]);
  w.advance(90_500);
  seen.fails_at_once_half_way = w.report();
  w.advance(10 * MINUTE);
  seen.fails_at_once = w.report();
  w.advance(60 * MINUTE);
  seen.fails_at_once_an_hour_later = w.report();
  // The demo has come up since. The Back button brings this page back from the browser's memory.
  const again = world(DEMO, (count) => (count <= seen.fails_at_once.pictures ? ["error", 30] : ["load", 150]));
  again.advance(70 * MINUTE);
  again.fire("window", "pageshow", { persisted: true });
  again.advance(MINUTE);
  seen.back_from_the_demo = again.report();
  again.fire("window", "pageshow", { persisted: false });
  again.advance(MINUTE);
  seen.plain_pageshow = again.report();
}

// The request is taken and never answered.
{
  const w = world(DEMO, () => ["hang", 0]);
  w.advance(60 * MINUTE);
  seen.never_answered = w.report();
}

// Every request is held until the service is up, 58 seconds after the page opened.
{
  const w = world(DEMO, (count, at) => ["load", Math.max(0, 58_000 - at)]);
  w.advance(5 * MINUTE);
  seen.held_then_answered = w.report();
}

// Every request is answered with the icon, but only 25 seconds after it was sent.
{
  const w = world(DEMO, () => ["load", 25_000]);
  w.advance(5 * MINUTE);
  seen.each_answer_takes_25_seconds = w.report();
  w.advance(20 * MINUTE);
  seen.each_answer_takes_25_seconds_later = w.report();
}

// The first request fails after 50 seconds; every later one is taken and never answered.
{
  const w = world(DEMO, (count) => (count === 1 ? ["error", 50_000] : ["hang", 0]));
  w.advance(60 * MINUTE);
  seen.kept_request_fails_late = w.report();
}

// A loading page comes back 40 ms after each request for 50 seconds; then the service answers.
{
  const w = world(DEMO, (count, at) => (at < 50_000 ? ["error", 40] : ["load", 120]));
  w.advance(5 * MINUTE);
  seen.loading_page_then_answered = w.report();
}

// A tab that is not being looked at, from the start (the link was opened in the background).
// For a minute its clock runs as usual; after that the browser lets it look once a minute.
{
  const w = world(DEMO, () => ["error", 30], { hidden: true });
  w.advance(MINUTE);
  seen.not_looked_at_first_minute = w.report();
  for (let minute = 0; minute < 30; minute += 1) w.glance(MINUTE);
  seen.not_looked_at = w.report();
  for (let minute = 0; minute < 60; minute += 1) w.glance(MINUTE);
  seen.not_looked_at_an_hour_later = w.report();
}

// The same tab, and the demo comes up after three minutes and five seconds, between two looks.
{
  const w = world(DEMO, (count, at) => (at < 185_000 ? ["error", 30] : ["load", 150]), { hidden: true });
  w.advance(MINUTE);
  for (let minute = 0; minute < 10; minute += 1) w.glance(MINUTE);
  seen.not_looked_at_and_the_demo_comes_up = w.report();
}

// The same tab, and a host that takes every request and never answers.
{
  const w = world(DEMO, () => ["hang", 0], { hidden: true });
  w.advance(MINUTE);
  for (let minute = 0; minute < 30; minute += 1) w.glance(MINUTE);
  seen.not_looked_at_and_held = w.report();
}

// A page that sees its clock once a minute from its very first look.
{
  const w = world(DEMO, () => ["error", 30], { hidden: true });
  for (let minute = 0; minute < 180; minute += 1) {
    w.jump(MINUTE);
    w.advance(500);
  }
  seen.a_look_a_minute = w.report();
}

// The browser freezes the tab for ten minutes in the middle of the wait.
{
  const w = world(DEMO, () => ["error", 30]);
  w.advance(20_000);
  w.jump(10 * MINUTE);
  w.advance(5_000);
  seen.frozen_while_waking = w.report();
  w.advance(10 * MINUTE);
  seen.frozen_while_waking_later = w.report();
}

// The page runs for a moment once every two minutes, for hours (a laptop that keeps going
// back to sleep): such gaps do not count as waiting, so it is the count of requests that ends it.
{
  const w = world(DEMO, () => ["error", 30]);
  for (let turn = 0; turn < 200; turn += 1) w.glance(2 * MINUTE);
  seen.a_moment_every_two_minutes = w.report();
}

// The demo has answered, and the laptop then sleeps for twenty minutes. Nobody leaves the tab.
{
  const w = world(DEMO, () => ["load", 150]);
  w.advance(MINUTE);
  w.jump(20 * MINUTE);
  w.advance(1_500);
  seen.set_aside_while_awake = w.report();
  // Now the visitor goes to another tab for a minute and comes back: the page asks afresh.
  w.show(false);
  w.advance(MINUTE);
  w.show(true);
  w.advance(1_500);
  seen.looked_at_again_after_asleep = w.report();
}

// The page has given up. The visitor leaves the tab and comes back, five times.
{
  const w = world(DEMO, () => ["error", 30]);
  w.advance(10 * MINUTE);
  const after = [w.report().pictures];
  for (let look = 0; look < 5; look += 1) {
    w.show(false);
    w.advance(MINUTE);
    w.show(true);
    w.advance(10 * MINUTE);
    after.push(w.report().pictures);
  }
  seen.looked_at_again_after_giving_up = { ...w.report(), pictures_after_each_look: after };
}

// The tab is frozen with the request on its way, and the answer arrives twenty minutes later.
{
  const w = world(DEMO, () => ["load", 150]);
  w.jump(20 * MINUTE);
  w.advance(200);
  seen.answered_too_late = w.report();
}

// The visitor is in another tab when the demo answers, half a minute in ...
{
  const w = world(DEMO, (count, at) => ["load", Math.max(150, 30_000 - at)]);
  w.show(false);
  w.advance(MINUTE);
  seen.awake_in_another_tab = w.report();
  // ... the browser freezes that tab ...
  w.fire("document", "freeze");
  seen.frozen_in_another_tab = w.report();
  // ... thaws it for a moment, while "awake" still holds ...
  w.fire("document", "resume");
  seen.thawed_in_another_tab = w.report();
  // ... and then the visitor comes back.
  w.show(true);
  seen.back_in_this_tab = w.report();
}

// The visitor follows the link while the demo is awake, stays there for twenty minutes and
// comes back with the Back button. One browser freezes the page it keeps in its memory ...
{
  const w = world(DEMO, () => ["load", 150]);
  w.advance(5_000);
  w.fire("window", "pagehide", { persisted: true });
  w.show(false);
  w.fire("document", "freeze");
  seen.left_for_the_demo = w.report();
  w.jump(20 * MINUTE);
  w.fire("document", "resume");
  w.fire("window", "pageshow", { persisted: true });
  w.show(true);
  w.advance(5_000);
  seen.back_one_way = w.report();
}
// ... another does not, and tells the page that it is seen before it tells it that it is back.
{
  const w = world(DEMO, () => ["load", 150]);
  w.advance(5_000);
  w.show(false);
  w.fire("window", "pagehide", { persisted: true });
  seen.left_and_not_frozen = w.report();
  w.jump(20 * MINUTE);
  w.show(true);
  w.fire("window", "pageshow", { persisted: true });
  w.advance(5_000);
  seen.back_the_other_way = w.report();
}

// A tab nobody looked at, and a demo that came up late. The visitor comes back when the
// page's waiting is just used up: the request sent at that moment still gets its answer.
{
  const w = world(DEMO, (count, at) => (at < 150_000 ? ["error", 30] : ["load", 1_500]), { hidden: true });
  w.advance(MINUTE);
  w.glance(MINUTE);
  w.jump(61_500);
  w.show(true);
  w.advance(10_000);
  seen.back_just_as_the_waiting_ran_out = w.report();
}

// The theme switch is pressed: it changes the colours and writes no words.
{
  const w = world(DEMO, () => ["load", 150]);
  w.advance(1_000);
  w.fire("switch", "click");
  seen.theme_switched = w.report();
}

console.log(JSON.stringify(seen));
"""

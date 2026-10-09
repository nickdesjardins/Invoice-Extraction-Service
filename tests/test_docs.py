"""Tests that hold the documents to the code, to the page and to the saved benchmark records.

This project's documents went stale once already: the README went on naming two
models, and their scores, after OpenRouter had withdrawn both. Nothing failed,
because nothing compared the words with anything. These tests do that comparing.

They need no network, no key and no model call. Most of them only read files. Some
import the service's own modules, to ask a constant, a route or a function what it is,
and one runs the examples of the Python guide. A copy of the project without its
documents, or without the ``benchmarks`` folder, skips the tests that need them. A
records file that the snapshot names and that is missing from an existing ``benchmarks``
folder is a failure: the figures would have no source.

Layout:

* Model ids            - a free model named in a document is one the project measured,
                         and the chain tables show the default chain.
* What the page says   - the notice for an unlisted model, where the documents quote it.
* Benchmark snapshot   - the table on the page is what the saved records say, and
                         the table in the README is the table on the page.
* Settings             - every setting is named in the template, in the README and in
                         the usage guide, and a copy of the template changes nothing.
* No longer true       - what the documents said before the service kept a ceiling,
                         saved a file and ran as one process; no document puts the key
                         on a command line; and no note for whoever checks a document
                         is left in one.
* Links and pointers   - a link, and a section named by its title, lead somewhere.
* The page's own words - what the documents quote from the page is on the page.
* Shared facts         - sentences that more than one document states: the rules of a
                         test, a red run of the scheduled job, what the log keeps.
* README               - its numbers, its layout, its routes, the scheduled test.
* Usage guide          - its numbers, its samples, its commands and its log lines.
* Learning guide       - the files, tests, names, numbers and code it shows.
* Python guide         - its source blocks, its examples and its file table.
* Deployment guide     - the workflows and their jobs, the line of the project page that
                         holds the live demo's address, what render.yaml sets, and what
                         the guide quotes from the two pages and from the log.
* Notes on the records - the file the page replays.
* What nothing held    - sentences that stood in a document while no test read them,
                         more of what the documents quote from the page, and the
                         figures the documents take from the notes under benchmarks/.
* All the page says    - every notice about the chain, and every title of a run that is
                         refused, has its place in the usage guide; and which tests
                         run the script of a page.
* What the page does   - how long the page waits and how often it asks, when it sends a
                         run again, and where a visitor finds what it tells: each as
                         the page's script has it.
* What the page shows  - what each notice, tile, chip and row means, which test the
                         section on model upkeep shows and what a replay calls, when
                         the button is there, and what production hides.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import importlib.util
import inspect
import io
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from dotenv import dotenv_values

# tests/conftest.py replaces budget.read_allowance for every test. This is the real
# one, taken when this file is imported and so before any fixture runs. One test asks
# it a question that it answers without the network.
from app.budget import read_allowance as real_read_allowance
from app.config import DEFAULT_MODEL_CHAIN
from app.playground.benchmark import load_records, summarise
from app.playground.catalogue import BENCHMARK, BENCHMARK_SNAPSHOT

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CHAIN = [model.strip() for model in DEFAULT_MODEL_CHAIN.split(",")]

#: Everything a reader is told to trust, and the template they copy their settings from.
DOCUMENTS = [
    PROJECT_ROOT / "README.md",
    *sorted((PROJECT_ROOT / "docs").glob("*.md")),
    PROJECT_ROOT / ".env.example",
]

#: The other places a model id can reach a reader: the notes on the records, the
#: page itself, and the scheduled job. (The record files are data and are left out.)
ALSO_READ = [
    PROJECT_ROOT / "benchmarks" / "README.md",
    PROJECT_ROOT / "app" / "playground" / "static" / "index.html",
    PROJECT_ROOT / "app" / "playground" / "static" / "assets" / "app.js",
    PROJECT_ROOT / ".github" / "workflows" / "model-check.yml",
]

#: ``vendor/model:free``, as OpenRouter writes the id of a free model. A slash may
#: come before it, so an id at the end of a link is seen as well.
FREE_MODEL_ID = re.compile(r"(?<![\w.-])([a-z0-9][\w.-]*)/([a-z0-9][\w.-]*):free\b")

#: A row of a "Position | Model" table: | Primary | `vendor/model:free` | ...
CHAIN_TABLE_ROW = re.compile(r"^\|\s*(Primary|Fallback)\s*\|\s*`([^`]+)`\s*\|", re.MULTILINE)

README_START, README_END = "<!-- snapshot:start -->", "<!-- snapshot:end -->"

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


# ---------------------------------------------------------------------------
# Documents - model ids
# ---------------------------------------------------------------------------


def test_documents_name_only_free_models_that_were_measured() -> None:
    """A withdrawn model must not linger in a document as if it were still on offer.

    Every id written with its ``:free`` suffix has to be in the default chain or in
    the benchmark snapshot, which carries its date. ``vendor/...:free`` is the
    placeholder the documents use to show syntax. A withdrawn model can still be
    mentioned as history: without the suffix, so nobody pastes it into a setting.
    """
    present = [path for path in DOCUMENTS + ALSO_READ if path.is_file()]
    if not present:
        pytest.skip("this copy of the project has no documents")

    measured = set(DEFAULT_CHAIN) | {row["model"] for row in BENCHMARK}
    for path in present:
        named = {
            f"{vendor}/{model}:free"
            for vendor, model in FREE_MODEL_ID.findall(path.read_text(encoding="utf-8"))
            if vendor != "vendor"
        }
        unknown = sorted(named - measured)
        assert not unknown, (
            f"{path.relative_to(PROJECT_ROOT)} names free models that are neither in the default "
            f"chain nor in the benchmark snapshot: {unknown}"
        )


def test_the_default_chain_is_what_the_snapshot_measured() -> None:
    """The default cannot move to a model nobody measured, or lose its place in the table."""
    assert [row["model"] for row in BENCHMARK[: len(DEFAULT_CHAIN)]] == DEFAULT_CHAIN
    for row in BENCHMARK[: len(DEFAULT_CHAIN)]:
        assert row["answered"] > 0, f"{row['model']} is in the default chain and never answered"


def test_the_default_chain_is_what_the_ordering_rule_gives() -> None:
    """The README states a rule. Applied to the snapshot's own figures it must give the default.

    Leave out a model that never answered, or whose usual answer takes more than half
    of the time limit; then fewest wrong answers, fewest calls with no answer, and
    speed last.
    """
    limit = BENCHMARK_SNAPSHOT["timeout_seconds"]
    kept = [
        row for row in BENCHMARK if row["answered"] > 0 and row["median_seconds"] <= limit / 2
    ]
    kept.sort(
        key=lambda row: (row["answered"] - row["correct"], row["no_answer"], row["median_seconds"])
    )
    assert [row["model"] for row in kept] == DEFAULT_CHAIN


def chain_tables(text: str) -> list[list[str]]:
    """Every "Position | Model" table in a document, as the ids it lists from Primary down."""
    tables: list[list[str]] = []
    for position, model in CHAIN_TABLE_ROW.findall(text):
        if position == "Primary":
            tables.append([])
        if tables:
            tables[-1].append(model)
    return tables


def test_chain_tables_in_the_documents_show_the_default_chain() -> None:
    """Which model answers first is the one thing a reader takes from these tables."""
    readme = PROJECT_ROOT / "README.md"
    if not readme.is_file():
        pytest.skip("this copy of the project has no README")

    found = {
        path: chain_tables(path.read_text(encoding="utf-8"))
        for path in DOCUMENTS
        if path.is_file()
    }
    # At the top, and again under "Model selection". If neither is found the pattern
    # has stopped matching, and this test would pass while checking nothing.
    assert len(found[readme]) == 2
    for path, tables in found.items():
        for table in tables:
            assert table == DEFAULT_CHAIN, f"{path.relative_to(PROJECT_ROOT)} shows {table}"


# ---------------------------------------------------------------------------
# Documents - what the page says
# ---------------------------------------------------------------------------


def test_documents_quote_the_notice_the_page_shows_for_an_unlisted_model() -> None:
    """The owner chose this sentence for visitors. The documents quote it, so keep them alike."""
    script = (PROJECT_ROOT / "app/playground/static/assets/app.js").read_text(encoding="utf-8")
    label = re.search(r'const UNLISTED_LABEL = "([^"]+)";', script)
    assert label is not None
    assert label.group(1) == "Unlisted from OpenRouter, please contact the admin."

    for name in ("README.md", "docs/USAGE.md"):
        path = PROJECT_ROOT / name
        if path.is_file():
            # Collapse line breaks first: the README wraps the sentence over two lines.
            assert label.group(1) in " ".join(path.read_text(encoding="utf-8").split()), name


# ---------------------------------------------------------------------------
# Documents - benchmark snapshot
# ---------------------------------------------------------------------------


def saved_records() -> list[Path]:
    """The record files the snapshot names. Missing ones fail, unless there is no folder at all."""
    if not (PROJECT_ROOT / "benchmarks").is_dir():
        pytest.skip("this copy of the project has no benchmarks folder")
    paths = [PROJECT_ROOT / name for name in BENCHMARK_SNAPSHOT["records"]]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in paths if not path.is_file()]
    assert not missing, f"the snapshot names records that are not there: {missing}"
    return paths


def run_headers(paths: list[Path]) -> list[dict[str, Any]]:
    """The first line of each records file: what the run was asked to do."""
    headers = []
    for path in paths:
        with path.open(encoding="utf-8") as lines:
            headers.append(json.loads(lines.readline()))
    return headers


def test_snapshot_figures_are_what_the_saved_records_say() -> None:
    """The table on the page is copied by hand, so check the copy against the source."""
    paths = saved_records()
    measured = {row["model"]: row for row in summarise(load_records(paths))}

    assert {row["model"] for row in BENCHMARK} == set(measured)
    for row in BENCHMARK:
        source = measured[row["model"]]
        for figure in ("calls", "no_answer", "answered", "correct", "median_seconds", "slowest_seconds"):
            assert row[figure] == source[figure], f"{row['model']}: {figure}"


def test_snapshot_says_when_and_how_it_was_measured() -> None:
    paths = saved_records()
    headers = run_headers(paths)
    calls = load_records(paths)
    assert all(header["type"] == "run" for header in headers)

    shot = BENCHMARK_SNAPSHOT
    assert shot["passes"] == sum(header["passes"] for header in headers)
    assert {shot["cases"]} == {len(header["cases"]) for header in headers}
    assert {shot["timeout_seconds"]} == {header["timeout_seconds"] for header in headers}
    # "started" is UTC, written as 2026-10-07T17:54:20Z.
    started = sorted(call["started"] for call in calls)
    assert shot["measured"] == started[0][:10] == started[-1][:10]
    assert shot["window_utc"] == f"{started[0][11:16]} to {started[-1][11:16]}"


def long_date(iso_day: str) -> str:
    day = date.fromisoformat(iso_day)
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def snapshot_markdown() -> str:
    """The README's snapshot block, written out from the figures the page shows."""
    shot = BENCHMARK_SNAPSHOT
    table = [("Model", "Calls", "No answer", "Fully correct", "Median answer", "Slowest answer", "Note")]
    for row in BENCHMARK:
        answered = row["answered"] > 0
        table.append(
            (
                f"`{row['model']}`",
                str(row["calls"]),
                str(row["no_answer"]),
                f"{row['correct']} of {row['answered']}" if answered else "none",
                f"{row['median_seconds']:.1f} s" if answered else "none",
                f"{row['slowest_seconds']:.1f} s" if answered else "none",
                row["note"],
            )
        )
    widths = [max(len(line[column]) for line in table) for column in range(len(table[0]))]
    lines = ["| " + " | ".join(cell.ljust(width) for cell, width in zip(line, widths)) + " |" for line in table]
    lines.insert(1, "|" + "|".join("-" * (width + 2) for width in widths) + "|")

    records = ", ".join(f"[`{name}`]({name})" for name in shot["records"])
    return "\n".join(
        [
            f"Measured on **{long_date(shot['measured'])}**, {shot['window_utc']} UTC: "
            f"{shot['passes']} passes of {shot['cases']} documents per model, "
            f"with a time limit of {shot['timeout_seconds']:g} s per call.",
            "",
            *lines,
            "",
            f"Saved records: {records}.",
        ]
    )


def test_readme_snapshot_is_the_table_on_the_page() -> None:
    """Two copies of one table drift apart unless something compares them."""
    readme = PROJECT_ROOT / "README.md"
    if not readme.is_file():
        pytest.skip("this copy of the project has no README")
    text = readme.read_text(encoding="utf-8")
    assert text.count(README_START) == 1 and text.count(README_END) == 1

    written = text.split(README_START)[1].split(README_END)[0].strip()
    expected = snapshot_markdown()
    assert written == expected, (
        "README.md does not show the benchmark snapshot in app/playground/catalogue.py. "
        f"Put this between {README_START} and {README_END}:\n\n{expected}\n"
    )


# ---------------------------------------------------------------------------
# Documents - helpers for the sections below
# ---------------------------------------------------------------------------

#: The documents that are written in Markdown and link to each other.
MARKDOWN = [
    "README.md",
    "docs/USAGE.md",
    "docs/LEARNING.md",
    "docs/PYTHON_LEARNING.md",
    "docs/DEPLOY.md",
    "benchmarks/README.md",
]

PAGE_FILES = [
    PROJECT_ROOT / "app" / "playground" / "static" / "index.html",
    PROJECT_ROOT / "app" / "playground" / "static" / "assets" / "app.js",
]
PAGE_SCRIPT = PAGE_FILES[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "model-check.yml"

#: The project page: the static page that is published on GitHub Pages, and its script.
PROJECT_PAGE = PROJECT_ROOT / "site" / "index.html"
PROJECT_PAGE_SCRIPT = PROJECT_ROOT / "site" / "assets" / "door.js"

#: How the documents write small numbers.
IN_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}


def document(name: str) -> str:
    """One document as it is written, or a skip when this copy of the project has none."""
    path = PROJECT_ROOT / name
    if not path.is_file():
        pytest.skip(f"this copy of the project has no {name}")
    return path.read_text(encoding="utf-8")


def one_line(text: str) -> str:
    """Every run of spaces and line breaks made one space: a sentence wraps over two lines."""
    return " ".join(text.split())


def prose(name: str) -> str:
    """A document as one line of text, without the notes left for whoever checks it."""
    return one_line(re.sub(r"<!--.*?-->", " ", document(name), flags=re.DOTALL))


def spaced(number: int) -> str:
    """1000000 as the README and two of the guides write it: 1 000 000."""
    return f"{number:,}".replace(",", " ")


def page_text() -> str:
    return one_line("\n".join(path.read_text(encoding="utf-8") for path in PAGE_FILES))


# ---------------------------------------------------------------------------
# Documents - every setting has its place
# ---------------------------------------------------------------------------


def settings_from(path: Path) -> Any:
    """The settings a file gives, with nothing taken from the environment of the test."""
    from app.config import Settings

    kept = dict(os.environ)
    try:
        for field in Settings.model_fields:
            os.environ.pop(field.upper(), None)
            os.environ.pop(field.lower(), None)
        return Settings(_env_file=str(path))
    finally:
        os.environ.clear()
        os.environ.update(kept)


def test_every_setting_is_named_in_the_template_the_readme_and_the_usage_guide() -> None:
    """A setting that the code reads and no document names is a setting nobody can find.

    ``MODEL_PUBLISHED_URL`` came into the code before it came into any document.
    """
    from app.config import Settings

    settings = {name.upper() for name in Settings.model_fields}

    # In the template: a line that sets a value, or the same line commented out.
    template = document(".env.example")
    in_template = set(re.findall(r"^(?:# )?([A-Z][A-Z_]+)=", template, re.MULTILINE))
    assert in_template == settings, f".env.example: {sorted(in_template ^ settings)}"

    table = document("README.md").split("### Configuration")[1].split("\n## ")[0]
    in_readme = set(re.findall(r"^\| `([A-Z_]+)`", table, re.MULTILINE))
    assert in_readme == settings, f"README.md, Configuration: {sorted(in_readme ^ settings)}"

    section = document("docs/USAGE.md").split("\n## 8. Configuration reference")[1].split("\n## 9. ")[0]
    in_usage = set(re.findall(r"^\| `([A-Z_]+)` \|", section, re.MULTILINE))
    assert in_usage == settings, f"docs/USAGE.md, section 8: {sorted(in_usage ^ settings)}"


def test_a_default_that_a_table_shows_is_the_default_in_the_code() -> None:
    """Where a row gives the default as one value in code style, it is the code's default."""
    from app.config import Settings

    def written_as(default: object) -> str:
        if isinstance(default, bool):
            return str(default).lower()
        return f"{default:g}" if isinstance(default, float) else str(default)

    tables = {
        "README.md": document("README.md").split("### Configuration")[1].split("\n## ")[0],
        "docs/USAGE.md": document("docs/USAGE.md").split("\n## 8. Configuration reference")[1].split("\n## 9. ")[0],
    }
    for name, table in tables.items():
        shown = dict(re.findall(r"^\| `([A-Z_]+)`\s*\| `([^`|]+)`\s*\|", table, re.MULTILINE))
        assert len(shown) >= 8, name  # if the pattern stops matching, nothing is checked
        for setting, written in shown.items():
            default = Settings.model_fields[setting.lower()].default
            assert written == written_as(default), f"{name}, {setting}: {written}"


def test_a_copy_of_the_template_leaves_the_chain_to_the_service(tmp_path: Path) -> None:
    """``.env.example`` says a blank line lets the service look after the chain. So it must be blank."""
    from app.config import DEFAULT_PUBLISHED_URL, Settings

    template = PROJECT_ROOT / ".env.example"
    text = document(".env.example")
    values = dotenv_values(template)
    settings = settings_from(template)

    assert not [key for key in values if key.lower() not in Settings.model_fields]
    assert values["OPENROUTER_MODELS"] == "" and settings.models_pinned is False
    assert settings.model_chain == DEFAULT_CHAIN
    # The line for the published list is there, commented out: the default address is used.
    assert "\n# MODEL_PUBLISHED_URL=null\n" in text
    assert settings.model_published_url == DEFAULT_PUBLISHED_URL

    # And a copy of the template changes nothing else either: every value it sets is the
    # default the code has anyway.
    nothing = tmp_path / "empty.env"
    nothing.write_text("", encoding="utf-8")
    assert settings.model_dump() == settings_from(nothing).model_dump()


def test_the_template_says_what_null_and_a_comment_on_the_line_do(tmp_path: Path) -> None:
    """Two sentences of the template: null switches the published list off, a comment names a model."""
    template = one_line(" ".join(line.lstrip("# ") for line in document(".env.example").splitlines()))
    assert "null = never read the published list" in template
    assert "a comment there would be read as a model id" in template

    made_up = tmp_path / "made-up.env"
    made_up.write_text("MODEL_PUBLISHED_URL=null\nOPENROUTER_MODELS=   # left blank on purpose\n", encoding="utf-8")
    settings = settings_from(made_up)

    assert settings.model_published_url is None
    assert settings.models_pinned is True and settings.model_chain == ["# left blank on purpose"]

    # Two more sentences, about values that look like "nothing" and are not.
    from app import published_chain

    assert "with the word null the service does not start" in template
    assert "The word `null` is not blank either: with it the service does not start." in prose("docs/USAGE.md")
    no_models = tmp_path / "null.env"
    no_models.write_text("OPENROUTER_MODELS=null\n", encoding="utf-8")
    with pytest.raises(ValueError, match="openrouter_models"):
        settings_from(no_models)

    assert "Any other value has to be an https address." in template
    switched_off_wrongly = tmp_path / "off.env"
    switched_off_wrongly.write_text("MODEL_PUBLISHED_URL=off\n", encoding="utf-8")
    address = settings_from(switched_off_wrongly).model_published_url
    assert address == "off", "the service starts with it"
    with pytest.raises(published_chain.Unusable, match="not an https address"):
        published_chain.fetch(address)  # refused before anything is asked for
    # The service logs every reason for not using a list as a warning, except "nothing is
    # published yet". The template and the usage guide both say which level it is.
    from app import model_chooser

    reading = inspect.getsource(model_chooser.read_published)
    assert 'logging.INFO if kind == "missing" else logging.WARNING' in reading
    assert "A value such as off or false is not used, and the log says so in a WARNING." in template
    assert "it logs a `WARNING` that the published list was not used" in prose("docs/USAGE.md")


# ---------------------------------------------------------------------------
# Documents - what they no longer say
# ---------------------------------------------------------------------------

#: Three things the documents said before the service kept a ceiling, saved a file and
#: ran as one process. A paragraph may still use the words, as long as it also says what
#: makes them true today.
NO_LONGER_TRUE = [
    # (what a paragraph says, and what it then has to say as well)
    (r"\bstore[sd]? nothing\b", ("one small file", "model_cache.json")),
    (r"\bunthrottled\b|\bno rate limiting\b", ("ceiling",)),
    (
        r"\b(?:several|multiple|many|two|three|four) (?:uvicorn )?workers?\b|--workers N\b",
        ("one process", "exactly one"),
    ),
]


def test_no_document_says_what_is_no_longer_true_without_what_makes_it_true() -> None:
    """ "The service stores nothing", "unthrottled", "run several workers": each was true once."""
    for path in DOCUMENTS + [PROJECT_ROOT / "benchmarks" / "README.md"]:
        if not path.is_file():
            continue
        text = re.sub(r"<!--.*?-->", " ", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for paragraph in (one_line(part) for part in re.split(r"\n[ \t]*\n", text)):
            for said, needs in NO_LONGER_TRUE:
                if re.search(said, paragraph, re.IGNORECASE):
                    assert any(words in paragraph for words in needs), (
                        f"{path.relative_to(PROJECT_ROOT)} says this without one of {needs}: {paragraph[:200]}"
                    )


def test_no_document_starts_more_than_one_worker() -> None:
    """The chain, the ceiling and a running test live in the memory of one process."""
    host = PROJECT_ROOT / "render.yaml"
    assert "--workers 1" in host.read_text(encoding="utf-8")
    for path in [*DOCUMENTS, host]:
        if path.is_file():
            counts = re.findall(r"--workers[ =]+(\d+)", path.read_text(encoding="utf-8"))
            assert all(count == "1" for count in counts), path.relative_to(PROJECT_ROOT)


#: A command line that holds a key, or one that puts a key into the shell's environment.
KEY_ON_A_COMMAND_LINE = re.compile(
    r"Bearer\s+sk-"  # a key written out after "Authorization: Bearer"
    r"|(?:\bexport\s+|\bset\s+|\bsetx\s+|\$env:)OPENROUTER_API_KEY\b"  # export KEY=..., set KEY=..., $env:KEY = ...
    r"|\bOPENROUTER_API_KEY=\S+[ \t]+(?:python|uvicorn|curl|pytest)\b"  # KEY=... in front of a command
)


def test_no_document_puts_the_key_on_a_command_line() -> None:
    """A shell keeps its history in a file on disk, so a key that is typed into a command stays there.

    The README once showed a curl line that needs the key in the shell, with no word of
    caution, and its Setup said "then set OPENROUTER_API_KEY" between two commands. The
    documents now send their reader to one command, which reads the key from ``.env``.
    The usage guide keeps the curl line, beside the sentence that says what not to do.
    """
    shell_variable = "$OPENROUTER_API_KEY"
    for path in DOCUMENTS + [PROJECT_ROOT / "benchmarks" / "README.md"]:
        if not path.is_file():
            continue
        name = path.relative_to(PROJECT_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        found = KEY_ON_A_COMMAND_LINE.search(text)
        assert found is None, f"{name} puts a key on a command line: {found.group(0)}"
        if name != "docs/USAGE.md":
            assert shell_variable not in text, f"{name} shows a command that needs the key in the shell"
    # If the pattern stops matching, this test would pass while checking nothing.
    for typed in (
        'curl https://openrouter.ai/api/v1/key -H "Authorization: Bearer sk-or-v1-abc"',
        "export OPENROUTER_API_KEY=sk-or-v1-abc",
        "# then set OPENROUTER_API_KEY -> https://openrouter.ai/keys",
        '$env:OPENROUTER_API_KEY = "sk-or-v1-abc"',
        "OPENROUTER_API_KEY=sk-or-v1-abc uvicorn app.main:app",
    ):
        assert KEY_ON_A_COMMAND_LINE.search(typed), typed
    for harmless in ("OPENROUTER_API_KEY=sk-or-v1-your-key-here", "Set a real `OPENROUTER_API_KEY`", "OPENROUTER_API_KEY="):
        assert not KEY_ON_A_COMMAND_LINE.search(harmless), harmless

    usage = document("docs/USAGE.md")
    before, variable, after = usage.partition(shell_variable)
    assert variable and shell_variable not in after, "the usage guide shows that curl line once"
    assert "for a shell that already has the key in its environment" in one_line(before)[-200:]
    assert "Do not put the key itself into a command line" in one_line(after)[:200]

    # What the documents tell their reader to do instead.
    assert "reads the key from `.env`, so the key is never typed and never shown" in prose("docs/USAGE.md")
    assert "It reads the key from `.env`, so the key is never typed into a command line." in prose("README.md")
    assert "Never paste the API key into a command line." in prose("docs/DEPLOY.md")
    assert "# then open .env in an editor and write your key after OPENROUTER_API_KEY=\n" in document("README.md")
    assert "# then open .env and write your key after OPENROUTER_API_KEY=\n" in usage


def test_no_note_for_whoever_checks_a_document_is_left_in_one() -> None:
    """While the code was being corrected, statements that waited for a correction carried a note.

    Such a note is an HTML comment, ``<!-- verify: ... -->``. A reader of the rendered
    document never sees it, so a note that is left behind says that somebody still has to
    look, and nobody will. The only comments a document keeps are the two marks around
    the README's snapshot, which a test needs to find the block.
    """
    checked = 0
    for path in DOCUMENTS + [PROJECT_ROOT / "benchmarks" / "README.md"]:
        if not path.is_file():
            continue
        checked += 1
        text = path.read_text(encoding="utf-8")
        assert "verify:" not in text, f"{path.relative_to(PROJECT_ROOT)} still carries a note to verify"
        comments = set(re.findall(r"<!--.*?-->", text, re.DOTALL))
        assert comments <= {README_START, README_END}, (
            f"{path.relative_to(PROJECT_ROOT)} holds a comment that its reader cannot see: {sorted(comments)}"
        )
    assert checked or not (PROJECT_ROOT / "README.md").is_file()


# ---------------------------------------------------------------------------
# Documents - links and pointers
# ---------------------------------------------------------------------------


def anchors_of(text: str) -> set[str]:
    """The anchors GitHub gives the headings of a Markdown file.

    Lower case, punctuation dropped, each space a hyphen. Lines inside code blocks
    are not headings.
    """
    outside_code = re.sub(r"^[ \t]*```.*?^[ \t]*```", "", text, flags=re.MULTILINE | re.DOTALL)
    return {
        re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        for title in re.findall(r"^#{1,6} (.+?)[ \t]*$", outside_code, re.MULTILINE)
    }


def find_as_spelled(start: Path, written: str) -> Optional[Path]:
    """The file or folder a relative link leads to, or None when it leads nowhere.

    Every part has to be spelled as its folder lists it, capitals included. Windows
    opens ``learning.md`` when the file is ``LEARNING.md``. GitHub does not.
    """
    here = start
    for part in written.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            here = here.parent
            continue
        if not here.is_dir() or part not in {entry.name for entry in here.iterdir()}:
            return None
        here = here / part
    return here


def test_links_in_the_documents_lead_somewhere() -> None:
    """A link to a file that is not there, or to a heading that was renamed, is a dead end."""
    checked = 0
    for name in MARKDOWN:
        path = PROJECT_ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        outside_code = re.sub(r"^[ \t]*```.*?^[ \t]*```", "", text, flags=re.MULTILINE | re.DOTALL)
        for target in re.findall(r"\]\(([^)\s]+)\)", outside_code):
            if "://" in target or target.startswith("mailto:"):
                continue
            file_part, _, anchor = target.partition("#")
            linked = find_as_spelled(path.parent, file_part) if file_part else path
            assert linked is not None, f"{name} links to {target}, which is not there"
            checked += 1
            if anchor and linked.suffix == ".md":
                assert anchor in anchors_of(linked.read_text(encoding="utf-8")), (
                    f"{name} links to {target}, and no heading there gives that anchor"
                )
    assert checked >= 100 or not (PROJECT_ROOT / "README.md").is_file()


def test_a_readme_section_named_by_its_title_is_there() -> None:
    """ ``README, "Model selection"`` is only a pointer while the README has that heading.

    The guides, the settings template, a log line of the service and the README that the
    scheduled job writes on its own branch all send their reader to "The scheduled test".
    """
    headings = re.findall(r"^#+ +(.+?)[ \t]*$", document("README.md"), re.MULTILINE)
    assert "The scheduled test" in headings

    pointers: dict[str, set[str]] = {}
    for path in DOCUMENTS + [PROJECT_ROOT / "benchmarks" / "README.md"]:
        if not path.is_file() or path.name == "README.md" and path.parent == PROJECT_ROOT:
            continue
        text = re.sub(r"<!--.*?-->", " ", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        if path.name == ".env.example":
            text = " ".join(line.lstrip("# ") for line in text.splitlines())
        for title in re.findall(r'README[^"]{0,60}"([^"]{3,60})"', one_line(text)):
            pointers.setdefault(title, set()).add(path.relative_to(PROJECT_ROOT).as_posix())

    assert {"Model selection", "Data handling", "The scheduled test"} <= set(pointers)
    for title, sources in pointers.items():
        assert any(heading.startswith(title) for heading in headings), (
            f'README.md has no heading "{title}", which {sorted(sources)} point to'
        )

    # The two places in the code that point there. If one is reworded, look at the heading too.
    assert "'The scheduled test'" in (PROJECT_ROOT / "app" / "model_chooser.py").read_text(encoding="utf-8")
    assert "'The scheduled test'" in WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Documents - the page's own words
# ---------------------------------------------------------------------------

#: What docs/USAGE.md quotes from the page, word for word and in italics. A reader looks
#: for these words on the screen, so a reworded page has to take the guide with it.
PAGE_WORDS_IN_USAGE = (
    "Model upkeep",
    "What happens when a free model disappears",
    "Test the free models now",
    "Replay this test",
    "Model chain",
    "Fixed by the admin of this demo.",
    "No longer offered by OpenRouter",
    "Unlisted from OpenRouter, please contact the admin.",
    # the notices in the result panel, about what the service did after a run failed
    "Finding a replacement",
    "A working model is in place",
    "The chain was repaired",
    "A stand-in is answering",
    "The test did not finish",
    "No replacement passed the test",
    "No free model is answering",
    "A tested chain was already waiting",
    "No replacement yet",
    "No replacement today",
    "No replacement is on offer",
    "The service cannot look for a replacement right now",
    "No model is answering",
    # a notice above the playground
    "The service changed its model chain",
    # the verdicts of a test
    "Passed",
    "Right, but slow",
    "Failed",
    "Not judged",
    # a run that is refused, and a demo that does not answer
    "The demo is busy",
    "Today's share of live runs is used up",
    "The day's share of live runs was used up",
    "What a client is told",
    "No answer yet",
    "Not answering",
    "Models withdrawn",
    "Operational",
    "has probably gone to sleep",
    "No answer yet, so the demo may be waking up",
    "The demo did not answer when this page last asked",
    "Waiting for the demo",
    "Try again",
    # what a notice says when only the admin can help
    "Please contact the admin.",
    # the page's two names: one for what a visitor is using, one for the software
    "the demo",
    "the service",
    # what the button that starts a test reads while a test runs
    "Testing",
)

#: What the README and the learning guide quote from the page.
PAGE_WORDS_ELSEWHERE = {
    "README.md": (
        "What happens when a free model disappears",
        "Test the free models now",
        "Replay this test",
        "Raw request and response",
        "Model upkeep",
        "Repaired, never reshuffled",
    ),
    "docs/LEARNING.md": (
        "What happens when a free model disappears",
        "Test the free models now",
        "Replay this test",
        "Repaired, never reshuffled",
        "fixed by the admin of this demo",
        "stand-in",
    ),
}


#: How often each of them stands in its document. A phrase that is quoted in two places is
#: still "there" when one of the two is reworded, and that one then goes stale unnoticed.
#: So every place is counted. ("stand-in" is left out: the guide also uses it as a word of
#: its own. The README's third "Test the free models now" is the tick box of the scheduled
#: job, which begins with the same words.)
TIMES_QUOTED_ELSEWHERE = {
    "README.md": {
        "What happens when a free model disappears": 1,
        "Test the free models now": 3,
        "Replay this test": 1,
        "Raw request and response": 1,
        "Model upkeep": 3,
        "Repaired, never reshuffled": 1,
    },
    "docs/LEARNING.md": {
        "What happens when a free model disappears": 3,
        "Test the free models now": 1,
        "Replay this test": 1,
        "Repaired, never reshuffled": 1,
        "fixed by the admin of this demo": 2,
    },
}


def verdict_labels() -> dict[str, str]:
    """The four verdicts of a test, as the chips on the page name them."""
    from app import model_rules

    block = re.search(r"const VERDICT_LABEL = \{(.*?)\};", PAGE_SCRIPT.read_text(encoding="utf-8"), re.DOTALL)
    assert block is not None
    labels = dict(re.findall(r'(\w+): \{ label: "([^"]+)"', block.group(1)))
    assert set(labels) == set(model_rules.VERDICTS)
    return labels


def test_documents_quote_the_chip_the_page_shows_for_a_withdrawn_model() -> None:
    """The second sentence the owner chose for visitors. The documents quote it as the first."""
    label = re.search(r'const WITHDRAWN_LABEL = "([^"]+)";', PAGE_SCRIPT.read_text(encoding="utf-8"))
    assert label is not None
    assert label.group(1) == "No longer offered by OpenRouter"

    for name in ("README.md", "docs/USAGE.md"):
        path = PROJECT_ROOT / name
        if path.is_file():
            assert label.group(1) in one_line(path.read_text(encoding="utf-8")), name


def test_the_page_says_what_the_usage_guide_quotes_from_it() -> None:
    guide = one_line(document("docs/USAGE.md"))
    page = page_text()
    for words in PAGE_WORDS_IN_USAGE:
        assert f"*{words}*" in guide, f"docs/USAGE.md no longer quotes: {words}"
        assert words in page, f"the page no longer says: {words}"
    # One quotation carries a date, which the page builds from the snapshot.
    assert "The tested default, measured on ${dayWords(p.origin.tested)}." in page
    assert f"*The tested default, measured on {long_date(BENCHMARK_SNAPSHOT['measured'])}.*" in guide

    # Three quotations are so short that a page says them in many places. What makes each of
    # them true is one place in the script: the rule for the page's two names, which stands
    # in a comment, and the label of the button while a test runs.
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    rule = one_line(script.replace("//", " "))
    assert '"the demo" is what the visitor is using' in rule
    assert '"the service" is the software and what it does' in rule
    assert "What you are using when you look at the page is *the demo*" in guide
    assert "The software, and what it does, is *the service*." in guide
    assert 'const label = p.test ? "Testing" : "Test the free models now";' in script
    assert "(the button then reads *Testing*)" in guide
    # The README gives the same two names, and the learning guide the first of them.
    assert "What a visitor is using is *the demo*. The software, and what it does, is *the service*." in prose("README.md")
    assert "the demo is the page's name for what a visitor is using" in prose("docs/LEARNING.md")
    # No sentence of the page says "deployment" to a visitor any more: the word was the
    # page's third name for the same thing.
    assert not re.search(r"\bthis deployment\b", page), "the page says 'this deployment' again"


def test_the_page_says_what_the_readme_and_the_learning_guide_quote_from_it() -> None:
    page = page_text()
    for name, quoted in PAGE_WORDS_ELSEWHERE.items():
        text = prose(name)
        for words in quoted:
            assert words in text, f"{name} no longer says: {words}"
            assert words in page, f"the page no longer says: {words}"
        counted = TIMES_QUOTED_ELSEWHERE[name]
        assert set(counted) <= set(quoted)
        for words, times in counted.items():
            assert text.count(words) == times, (
                f"{name} says '{words}' {text.count(words)} times, and {times} are held. Look at "
                "each place, then change the number in TIMES_QUOTED_ELSEWHERE"
            )

    # The learning guide says what "the page" is where it first uses the words. It once
    # began "The page says ..." as if its reader had read another document first.
    from app.playground.routes import router

    learning = prose("docs/LEARNING.md")
    introduced = "The service has a web page of its own at `/`, and this guide calls it the page."
    assert introduced in learning and learning.index("The page ") > learning.index(introduced)
    assert [route.methods for route in router.routes if route.path == "/"] == [{"GET"}]


def test_the_documents_name_the_four_verdicts_as_the_page_does() -> None:
    """Where the documents and the page name the same thing, the page's word is the one to use."""
    labels = verdict_labels()

    readme = document("README.md")
    usage = document("docs/USAGE.md")
    learning = prose("docs/LEARNING.md")
    for code, label in labels.items():
        # A row of the README's table, a row of the usage guide's, an entry of the learning guide's list.
        assert re.search(rf"^\| {re.escape(label)}\s*\|", readme, re.MULTILINE), f"README.md: {code}"
        assert re.search(rf"^\| \*{re.escape(label)}\* \|", usage, re.MULTILINE), f"docs/USAGE.md: {code}"
        assert f"**{label}.**" in learning and f'"{label}"' in learning, f"docs/LEARNING.md: {code}"


# ---------------------------------------------------------------------------
# Documents - facts that more than one document states
# ---------------------------------------------------------------------------


def test_the_lines_for_a_model_that_is_not_listed_are_the_ones_the_code_writes() -> None:
    """One finding, three sentences: the command prints one, and the startup log writes two.

    The README and the usage guide show the command's line. The startup log says what
    follows for this deployment instead: one line for a chain that was named, which the
    usage guide and the learning guide show, and one for a chain the service looks
    after, which the usage guide shows.
    """
    from app import main
    from app.model_catalogue import describe, look_up

    def model_not_found(url: str) -> tuple[int, str]:
        return 404, json.dumps({"error": {"message": "Model not found: vendor/model-a:free"}})

    listing = look_up("vendor/model-a:free", "https://openrouter.ai/api/v1", model_not_found)
    printed = describe(listing)
    for name in ("README.md", "docs/USAGE.md"):
        assert printed in document(name), f"{name} does not show: {printed}"

    written = {}
    for node in ast.walk(ast.parse(inspect.getsource(main.report_model_listings))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            words = fixed_words(node.args[0])
            if words and any("is not listed by OpenRouter" in (piece or "") for piece in words):
                sentence = "".join("{}" if piece is None else piece for piece in words)
                written[node.func.attr] = sentence.format(listing.model, listing.note)
    assert set(written) == {"warning", "error"}, "the startup report has one line of each kind"

    named = f"ERROR [app.main] {written['error']}"
    looked_after = f"WARNING [app.main] {written['warning']}"
    assert named in document("docs/USAGE.md"), f"docs/USAGE.md does not show: {named}"
    assert named in document("docs/LEARNING.md"), f"docs/LEARNING.md does not show: {named}"
    assert looked_after in document("docs/USAGE.md"), f"docs/USAGE.md does not show: {looked_after}"
    # The command's sentence is no line of the startup log, in any document.
    for name in ("README.md", "docs/USAGE.md", "docs/LEARNING.md"):
        assert f"[app.main] {printed}" not in document(name), name


def test_another_base_address_does_what_the_documents_say(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No list of free models and no count of the day: no upkeep, and no rule for the day."""
    from app import budget, model_chooser
    from app.config import Settings

    monkeypatch.delenv("OPENROUTER_MODELS")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example/api/v1")
    elsewhere = Settings()
    asked: list[str] = []

    def never(url: str, key: str) -> tuple[int, str]:
        asked.append(url)
        raise AssertionError("the key must never leave for another host")

    assert model_chooser.mode(elsewhere) == "not_openrouter"
    with caplog.at_level("DEBUG", logger="app.budget"):
        assert real_read_allowance(elsewhere, never) is None
    # Nobody is asked, so no line says that the count "could not be read". The usage guide
    # once sent the admin who reads that line to OPENROUTER_BASE_URL, which cannot be the
    # reason for a line that this service only writes after it has asked OpenRouter.
    assert not asked
    assert not [record for record in caplog.records if record.name == "app.budget"]
    unreadable = re.search(
        r"^\| `OpenRouter's count of today's free requests could not be read.*$",
        document("docs/USAGE.md"),
        re.MULTILINE,
    )
    assert unreadable is not None
    assert "the service then asks nobody and does not write this line" in unreadable.group(0)
    assert "check that `OPENROUTER_BASE_URL`" not in unreadable.group(0)

    # Nobody counts the day there, so the rule for a day without a count is left out: it
    # could never be lifted (tests/test_budget.py holds that the runs go on). Both tables
    # say what is left, and neither promises the twenty calls of a count that cannot be read.
    assert budget.day_is_counted(elsewhere) is False
    assert "day_is_counted(get_settings()) and" in inspect.getsource(budget.admit_visitor)
    calls = f"{budget.UNKNOWN_LIVE_CALLS} live calls"
    readme_row = re.search(r"^\| `OPENROUTER_BASE_URL`.*$", document("README.md"), re.MULTILINE)
    usage_row = re.search(r"^\| `OPENROUTER_BASE_URL`.*$", document("docs/USAGE.md"), re.MULTILINE)
    assert readme_row is not None and usage_row is not None
    assert "live calls are limited by the per-minute ceiling alone" in readme_row.group(0)
    assert "only the per-minute ceiling then limits live calls" in usage_row.group(0)
    assert calls not in readme_row.group(0) and calls not in usage_row.group(0)
    left_out = (
        "With another address in `OPENROUTER_BASE_URL` nobody counts the day, and only the "
        "per-minute ceiling applies."
    )
    for name in ("README.md", "docs/USAGE.md"):
        assert left_out in prose(name), name
    assert (
        "with another address in `OPENROUTER_BASE_URL`, that rule is left out"
        in prose("docs/LEARNING.md")
    )
    # The row for a service that refuses every request no longer names this address as a cause.
    stuck = re.search(
        r"^\| Every request answers 503 with `Retry-After: 60`.*$", document("docs/USAGE.md"), re.MULTILINE
    )
    assert stuck is not None and "OPENROUTER_BASE_URL" not in stuck.group(0)


def test_the_built_in_default_counts_as_tested_in_the_last_minute_of_its_measuring() -> None:
    """A published list is taken only when it is newer than the test behind the chain in use.

    For the built-in default that test is the measuring by hand, and it counts as over
    in its last minute and not at the end of that day. A new default is pushed on the day
    it was measured, and the list that the scheduled job then publishes has to be newer.
    """
    from app import model_chooser

    day = date.fromisoformat(BENCHMARK_SNAPSHOT["measured"])
    last = BENCHMARK_SNAPSHOT["window_utc"].rsplit(" ", 1)[-1]  # "17:54 to 19:02" -> "19:02"
    hour, minute = (int(part) for part in last.split(":"))
    ended = datetime(day.year, day.month, day.day, hour, minute, 59, tzinfo=timezone.utc)
    assert model_chooser.tested_at() == ended
    assert ended < datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)

    said = f"it counts as over in its last minute: {last} UTC on {long_date(BENCHMARK_SNAPSHOT['measured'])}"
    assert said in prose("README.md"), f"README.md does not say: {said}"
    assert "So a list that the job tests later on that same day is newer, and is taken." in prose("README.md")
    assert "the default counts as tested in the last minute of that measuring (`tested_at()`)" in prose(
        "docs/LEARNING.md"
    )


def test_the_log_lines_that_the_readme_and_the_learning_guide_quote_are_the_services() -> None:
    chooser = (PROJECT_ROOT / "app" / "model_chooser.py").read_text(encoding="utf-8")
    strings = [
        node.value
        for node in ast.walk(ast.parse(chooser))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    readme = prose("README.md")
    for start in ("Model choice: automatic", "Model choice: pinned"):
        assert any(value.startswith(start) for value in strings), start
        assert f"`{start}`" in readme
    # A third line of that kind is a warning: nobody looks after the chain although the
    # setting is blank. It gives one of two reasons, and the README names both.
    unattended = "Model choice: the service does not look after its chain here, because "
    written_as = [
        levels
        for name, levels, words in lines_the_code_can_log()
        if name == "app.model_chooser" and words and (words[0] or "").startswith(unattended)
    ]
    assert written_as == [{"WARNING"}]
    for reason in ("no API key is set", "OPENROUTER_BASE_URL is not OpenRouter's"):
        assert reason in strings, reason
    assert "no key is set, or `OPENROUTER_BASE_URL` is not OpenRouter's own address" in readme
    assert "The line is then a `WARNING`." in readme and "which of the two is the reason" in readme

    written = [value for value in strings if value.startswith("Model chain in use is now %s")]
    assert len(written) == 1
    assert "`Model chain in use is now [...] (it was [...])`" in readme
    assert written[0].startswith("Model chain in use is now %s (it was %s)")
    # A chain is logged as a Python list: ['vendor/model:free', ...].
    quoted = re.escape(written[0]).replace("%s", r"\[[^\]]*\]")
    assert re.search(quoted, prose("docs/LEARNING.md")), (
        "docs/LEARNING.md does not quote this line as the service writes it: " + written[0]
    )


def made_up_calls(
    model: str,
    cases: list[str],
    wrong: tuple[str, ...] = (),
    unanswered: tuple[str, ...] = (),
    seconds: float = 3.0,
) -> list[dict[str, Any]]:
    """The calls of one model in a test of the models, written out by hand.

    Every document is answered right in ``seconds``, except those named as answered
    wrongly or as not answered at all (a rate limit).
    """
    calls: list[dict[str, Any]] = []
    for case in cases:
        if case in unanswered:
            calls.append(
                {
                    "model": model,
                    "case": case,
                    "outcome": "no_answer",
                    "error_type": "ProviderRateLimitError",
                    "correct": None,
                    "seconds": 0.2,
                }
            )
        else:
            calls.append(
                {
                    "model": model,
                    "case": case,
                    "outcome": "answered",
                    "error_type": None,
                    "correct": case not in wrong,
                    "seconds": seconds,
                }
            )
    return calls


def test_one_document_has_to_be_answered_right_and_the_documents_name_it() -> None:
    """The prompt-injection attempt, and no second one: so three documents say.

    A wrong answer to the text that is not an invoice is a miss like any other, and one
    miss is allowed. The documents once named that text as a second document that has
    to be right, which the rules never did.
    """
    from app import model_rules
    from app.playground.benchmark import live_cases

    titles = {case["id"]: case["title"] for case in live_cases()}
    cases = list(titles)
    guarded = model_rules.MUST_BE_RIGHT
    assert isinstance(guarded, str), "the documents say: one document"
    assert titles[guarded] == "Prompt-injection attempt" and titles["not-an-invoice"] == "Not an invoice"
    limit = BENCHMARK_SNAPSHOT["timeout_seconds"]
    model = "vendor/model-a:free"

    def verdict(**misses: Any) -> tuple[str, str]:
        calls = made_up_calls(model, cases, **misses)
        row = model_rules.judge([model], calls, cases, limit, finished=True)[0]
        return row["verdict"], row["why"]

    assert verdict() == ("passed", "all_right")
    assert verdict(wrong=("not-an-invoice",)) == ("passed", "one_missed")
    assert verdict(wrong=(guarded,)) == ("failed", "hidden_instructions")
    # No answer to the one document that has to be right: the model cannot pass.
    assert verdict(unanswered=(guarded,)) == ("not_judged", "unanswered")

    readme, usage, learning = prose("README.md"), prose("docs/USAGE.md"), prose("docs/LEARNING.md")
    assert "The prompt-injection attempt was answered right." in readme
    assert "One document has to be answered right: the prompt-injection attempt" in readme
    assert "That holds for the text that is not an invoice as well." in readme
    assert f"One document had to be answered right: *{titles[guarded]}*" in usage
    assert f"That holds for *{titles['not-an-invoice']}* too" in usage
    assert "One document has to be answered right: the prompt-injection attempt." in learning
    assert "`MUST_BE_RIGHT` names the one document that has to be answered right" in learning
    # One wording for it in all three: "allows no miss" was the learning guide's alone,
    # and it stood there before the guide had said what a miss is.
    for name, text in (("README.md", readme), ("docs/USAGE.md", usage), ("docs/LEARNING.md", learning)):
        assert "allows no miss" not in text, f"{name} words it another way than the other two"
    # The same wording where two documents say what the fingerprint of a test is made of.
    assert "and which document has to be answered right)" in readme
    assert "and the document that has to be answered right)" in learning
    assert "answered wrongly, or not answered at all" in learning, "the guide says what a miss is where it first counts one"
    assert "this is the one document that the model has to answer right (§9.7)" in learning
    # How many documents a model may miss, and what ends a model's test early.
    missed = f"at most {IN_WORDS[model_rules.MAX_MISSES]} was missed"
    assert model_rules.GIVE_UP_AFTER == model_rules.MAX_MISSES + 1
    for name, text in (("README.md", readme), ("docs/USAGE.md", usage), ("docs/LEARNING.md", learning)):
        assert "Two documents" not in text, f"{name} names a second document that has to be right"
        assert missed in text, f"{name} no longer says: {missed}"
    assert verdict(wrong=("not-an-invoice",), unanswered=("email-invoice",))[0] != "passed"

    # The reason the learning guide quotes is the comment beside the constant.
    reason = "a wrong tax amount is a mistake, doing what a document says is a hole"
    rules = (PROJECT_ROOT / "app" / "model_rules.py").read_text(encoding="utf-8")
    assert reason in learning and reason in one_line(re.sub(r"^#: ?", "", rules, flags=re.MULTILINE))


def test_a_default_model_that_comes_back_takes_its_measured_place_as_the_documents_say() -> None:
    """ "Repair, never rank" has one exception, and three documents describe it."""
    from app import model_rules
    from app.playground.benchmark import live_cases

    cases = [case["id"] for case in live_cases()]
    limit = BENCHMARK_SNAPSHOT["timeout_seconds"]
    first, second = DEFAULT_CHAIN
    other = "vendor/model-x:free"

    def after(current: list[str], back: str, seconds: float = 3.0) -> list[str]:
        """The chain after a test in which ``back``, a model outside it, read every document right."""
        rows = model_rules.judge([back], made_up_calls(back, cases, seconds=seconds), cases, limit, finished=True)
        return model_rules.next_chain(current, rows, [], limit, measured=DEFAULT_CHAIN)

    # In front of every model that is not a default model, and behind a default model before it.
    assert after([second, other], first) == [first, second, other]
    assert after([first, other], second) == [first, second, other]
    assert after([other], second) == [second, other]
    # Right, but slow: the last place, like any other newcomer.
    assert after([second, other], first, seconds=limit * 0.9) == [second, other, first]
    # A model that nobody measured gets no such place.
    assert after([first], other) == [first, other]

    assert "in front of every other model" in prose("README.md")
    assert "in front of every model that is not a default model" in prose("docs/USAGE.md")
    learning = prose("docs/LEARNING.md")
    # A newcomer stands directly behind the models that stay, not at the far end of the
    # chain: with one model kept and one model added, the added one is second of three.
    assert model_rules.PLACES == 3 and after([first], other).index(other) == 1
    assert "A test fills free places behind the models that are already in the chain." in prose("README.md")
    assert "fill the free places behind the models already there" in prose("docs/USAGE.md")
    assert "Newcomers fill free places behind the models that are already there" in learning
    assert "take the free places behind the models that stay" in learning
    for name in ("README.md", "docs/USAGE.md", "docs/LEARNING.md"):
        assert "from the back" not in prose(name), f"{name}: read as a direction, that says the opposite"
    assert "in front of every model that the service added" in learning
    assert "its argument `measured`" in learning
    assert "measured" in inspect.signature(model_rules.next_chain).parameters


def chain_after_saved_pass(number: int) -> list[str]:
    """The chain one saved pass gives when it is judged alone, as a test of the models would be.

    Such a test starts from the default chain and also tests the other models that were
    listed. A model is sent no further document once two of its calls were not fully right.
    """
    from app import model_rules
    from app.playground.benchmark import live_cases

    calls = [call for call in load_records(saved_records()) if call["pass"] == number]
    models = list(dict.fromkeys([*DEFAULT_CHAIN, *(call["model"] for call in calls)]))
    sent: list[dict[str, Any]] = []
    for model in models:
        misses = 0
        for call in (call for call in calls if call["model"] == model):
            sent.append(call)
            misses += call["correct"] is not True
            if misses >= model_rules.GIVE_UP_AFTER:
                break
    cases = [case["id"] for case in live_cases()]
    limit = BENCHMARK_SNAPSHOT["timeout_seconds"]
    rows = model_rules.judge(models, sent, cases, limit, finished=True)
    return model_rules.next_chain(DEFAULT_CHAIN, rows, [], limit, measured=DEFAULT_CHAIN)


def test_a_published_chain_can_be_longer_than_the_default_as_the_documents_say() -> None:
    """A test also fills the spare third place, so the first published chain may name three models.

    The scheduled test starts from the default chain and tests the other free models with
    it. The README says what the saved passes give when each is judged that way. The
    deployment guide showed a published chain of two models and nothing else, so its
    reader would have met a third model, and one more log line, without a word about
    either. It now says that both are right.
    """
    from app import model_chooser, model_rules, model_testing, published_chain

    passes = sorted({call["pass"] for call in load_records(saved_records())})
    chains = {number: chain_after_saved_pass(number) for number in passes}
    # No pass moves a default model: that is "repair, never rank".
    assert all(chain[: len(DEFAULT_CHAIN)] == DEFAULT_CHAIN for chain in chains.values())
    longer = {number: chain for number, chain in chains.items() if len(chain) > len(DEFAULT_CHAIN)}
    assert longer and all(len(chain) == model_rules.PLACES for chain in longer.values())
    (third,) = {chain[-1] for chain in longer.values()}
    measured = next(row for row in BENCHMARK if row["model"] == third)
    assert measured["correct"] == measured["answered"] > 0, "right in every answer it gave"
    assert measured["median_seconds"] > BENCHMARK_SNAPSHOT["timeout_seconds"] / 2, "and slow"

    readme = prose("README.md")
    day = long_date(BENCHMARK_SNAPSHOT["measured"])
    places, default = IN_WORDS[model_rules.PLACES], IN_WORDS[len(DEFAULT_CHAIN)]
    assert (
        f"{IN_WORDS[len(longer)]} of the {IN_WORDS[len(chains)]} saved passes of {day} give a chain of "
        f"{places} models: the {default} default ones, and behind them `{third}`, which was right but slow"
    ) in readme
    assert "the published chain can be longer than the default chain" in readme
    assert f"each of the {IN_WORDS[len(chains)]} saved passes leaves the {default} default models in their places" in readme

    # The recorded test is the first of those passes. The deployment guide names the same
    # model where its reader first looks at the published file.
    recorded = model_testing.recorded_run()
    assert recorded is not None and recorded["chain"] == chains[1]
    added = [(row["model"], row["verdict"]) for row in recorded["models"] if row["placing"] == "added"]
    assert added == [(third, "slow")]
    guide = prose(DEPLOY)
    assert f"It can name {places} models where the default chain has {default}." in guide
    assert f"A chain has {places} places, and a test also fills the spare third one" in guide
    assert "with a free model that passed, or with one that was right but slow" in guide
    assert f"On {day} `{third}` was right but slow." in guide
    assert "A third model is asked only when the two before it gave no invoice." in guide
    # The chain is one of the first things in that file.
    written = published_chain.build(recorded, "0" * 16, datetime.now(timezone.utc), {})
    assert list(written)[:4] == ["version", "fingerprint", "tested", "chain"]
    assert 'Near its top, the list `"chain"` names the tested chain.' in guide

    # And what the log of the first deploy shows when that chain is not the default chain:
    # the change is written before the line that says where the chain comes from, and at
    # startup it is an ordinary line, not a warning.
    reading = inspect.getsource(model_chooser.read_published)
    assert "level = logging.INFO if at_startup else logging.WARNING" in reading
    assert reading.index("_use(found.chain,") < reading.index('"Using the chain published by the scheduled test of')
    assert "_switch(before, level)" in inspect.getsource(model_chooser._use)
    assert f"The lists in this sample hold the {default} default models." in guide
    sample = SHOWN_LOG_LINE.findall(document(DEPLOY))
    assert all(str(DEFAULT_CHAIN) in line for _, _, line in sample), "every list in the sample is the default chain"
    assert (
        "When that chain is not the default chain, for example because it names a third model, one more line "
        "of `app.model_chooser` stands above the first. It begins `Model chain in use is now`"
    ) in guide
    usage = prose("docs/USAGE.md")
    assert "a line that starts with `Model chain in use is now` comes before these two" in usage


@pytest.mark.asyncio
async def test_a_third_model_is_asked_only_when_the_two_before_it_gave_no_invoice() -> None:
    """What a third place in the chain costs, as two guides put it to their readers.

    The deployment guide tells its reader that a third model in the published chain is
    right, and the learning guide says why the spare place is the last one. Both rest on
    how the dispatcher walks a chain, so the real dispatcher walks one here.
    """
    from app.extractors import ExtractionDispatcher, ProviderError, UnparseableTextError
    from app.schemas import InvoiceData

    invoice = InvoiceData.model_validate(
        {"vendor_name": "Acme", "invoice_number": "A-1", "total_amount": 30.0, "currency": "USD", "items": []}
    )

    class Scripted:
        """Stands where an engine stands: the dispatcher asks it, and it answers from a script."""

        def __init__(self, model: str, answer: Any) -> None:
            self.model, self.answer, self.asked = model, answer, 0

        async def extract(self, text: str) -> InvoiceData:
            self.asked += 1
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    def failed() -> Exception:
        return ProviderError("was not found (404)")

    def no_invoice() -> Exception:
        return UnparseableTextError("this is not an invoice")

    async def third_was_asked(first: Any, second: Any) -> bool:
        chain = [Scripted("vendor/a:free", first), Scripted("vendor/b:free", second), Scripted("vendor/c:free", invoice)]
        try:
            await ExtractionDispatcher(chain).extract("some text")  # type: ignore[arg-type]
        except UnparseableTextError:
            pass
        return chain[2].asked == 1

    # An invoice from either of the first two ends the walk.
    assert not await third_was_asked(invoice, invoice)
    assert not await third_was_asked(failed(), invoice)
    assert not await third_was_asked(no_invoice(), invoice)
    # No invoice from either: the third is asked, unless the two agree that there is none.
    assert await third_was_asked(failed(), failed())
    assert await third_was_asked(failed(), no_invoice())
    assert await third_was_asked(no_invoice(), failed())
    assert ExtractionDispatcher.UNPARSEABLE_QUORUM == 2 and not await third_was_asked(no_invoice(), no_invoice())

    assert "A third model is asked only when the two before it gave no invoice." in prose(DEPLOY)
    assert "the last model is asked only when the models before it gave no invoice" in prose("docs/LEARNING.md")


def test_a_stand_in_is_kept_in_memory_only_as_the_documents_say() -> None:
    """Three documents say: never saved, and a restart does not find it."""
    from app import model_cache, model_testing

    chosen = ["vendor/model-a:free"]
    model_cache.use(chosen, {"kind": "own"})
    saved = model_cache.PATH.read_text(encoding="utf-8")

    model_cache.use([*chosen, "vendor/model-b:free"], {"kind": "own"}, save=False)
    assert model_cache.chain() == [*chosen, "vendor/model-b:free"], "the stand-in answers"
    assert model_cache.settled() == chosen, "the chosen chain is what it was"
    assert model_cache.PATH.read_text(encoding="utf-8") == saved, "and nothing was written"

    model_cache.forget()  # what a restart does to the memory of the process
    assert model_cache.chain() == chosen

    assert "It is kept in memory only and never saved, so a restart does not find it." in prose("README.md")
    assert "It is never saved, so a restart does not find it." in prose("docs/USAGE.md")
    assert "A stand-in lives in memory and is never saved, so a restart never finds one." in prose(
        "docs/LEARNING.md"
    )
    # "Whichever document that answer was for", and "stops answering at once": the test
    # tells its caller of both while it is still running.
    callbacks = inspect.signature(model_testing.run_test).parameters
    assert {"on_first_right", "on_failed"} <= set(callbacks)
    assert "whichever document that answer was for" in prose("README.md")
    assert "whichever document that answer was for" in prose("docs/USAGE.md")
    # "As long as a place is free": a chain holds three models at most, stand-ins included.
    from app import model_chooser, model_rules

    assert "len(behind) + len(stand_ins) >= model_rules.PLACES" in inspect.getsource(model_chooser._run)
    assert "as long as a place is free" in prose("README.md")
    assert "as long as a place is free" in prose("docs/LEARNING.md")
    assert f"as long as fewer than {IN_WORDS[model_rules.PLACES]} models are answering" in prose("docs/USAGE.md")


def test_an_extraction_is_logged_as_the_documents_say() -> None:
    """Which model answered and how long it took. What the document said, at DEBUG only."""
    written = [entry for entry in lines_the_code_can_log() if entry[0] in ("app.main", "app.playground")]
    done = [(levels, words) for _, levels, words in written if any("extracted invoice" in (piece or "") for piece in words)]
    said = [(levels, words) for _, levels, words in written if any("vendor=" in (piece or "") for piece in words)]
    assert len(done) == 2 and len(said) == 2, "one line of each kind for the API, and one for the page"
    for levels, words in done:
        assert levels == {"INFO"}
        assert not any(word in (piece or "") for piece in words for word in ("vendor", "number"))
    for levels, words in said:
        assert levels == {"DEBUG"}, "the vendor name and the invoice number are for a developer who asks"

    readme = prose("README.md")
    assert "It does not say the vendor name or the invoice number" in readme
    assert "those two are written at `DEBUG` only" in readme
    assert "are written only when `LOG_LEVEL` is `DEBUG`" in prose("docs/USAGE.md")
    assert "The vendor name and the invoice number are logged at `DEBUG` only" in prose("docs/LEARNING.md")
    template = one_line(" ".join(line.lstrip("# ") for line in document(".env.example").splitlines()))
    assert "DEBUG also writes the vendor name and the invoice number of each extraction to the log" in template
    for name in ("README.md", "docs/USAGE.md", "docs/LEARNING.md"):
        assert "records the vendor name" not in prose(name), name

    # Everything the service writes at DEBUG, so that the usage guide's row for that level
    # is whole: the two lines above, and one for a line total that the service corrected.
    at_debug = ["".join(piece or "?" for piece in words) for _, levels, words in lines_the_code_can_log() if levels == {"DEBUG"}]
    assert len(at_debug) == 3, at_debug
    assert sum("vendor=" in line for line in at_debug) == 2
    assert sum(line.startswith("line total corrected") for line in at_debug) == 1
    assert (
        "the vendor name and the invoice number of each extraction, and the figures of a line "
        "whose total the service corrected"
    ) in prose("docs/USAGE.md")
    # The other places that say what DEBUG writes name that third thing as well: the
    # template, the two rows of LOG_LEVEL, and the README where it speaks of the log.
    corrected = "and the figures of a line whose total was corrected, so keep"
    assert corrected in template, ".env.example"
    in_readme = re.search(r"^\| `LOG_LEVEL`.*$", document("README.md"), re.MULTILINE)
    assert in_readme is not None and corrected in in_readme.group(0)
    assert "At `DEBUG` the log also holds the figures of a line whose total the service corrected." in readme
    in_usage = re.search(r"^\| `LOG_LEVEL`.*$", document("docs/USAGE.md"), re.MULTILINE)
    assert in_usage is not None
    assert "`DEBUG` also writes some of what a document said to the log" in in_usage.group(0)


def test_the_documents_give_the_reasons_for_a_red_run_as_the_job_has_them() -> None:
    """A red "Model check" means that the owner has something to do, and a line says what."""
    from app import published_chain

    job = ast.parse(inspect.getsource(published_chain._job))
    reasons = {
        ast.dump(node.args[0])
        for node in ast.walk(job)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_fail"
    }
    count = IN_WORDS[len(reasons)]
    line = "Failing this run on purpose:"
    assert line in inspect.getsource(published_chain._fail)

    # The workflow's own header lists them, and its last job sends the owner to that line.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    header = workflow.split("When this run is red")[1].split("A red run without such a line")[0]
    assert len(re.findall(r"^#   \* ", header, re.MULTILINE)) == len(reasons)
    assert f"'{line}'" in workflow.split("\n  verdict:\n")[1]
    # The line is at the end of one step of the first job. GitHub lists more steps after
    # that one (the "Post" steps of the two actions, and "Complete job"), so the documents
    # name the step and do not call it the last.
    first_job = workflow.split("\n  test:\n")[1].split("\n  publish:\n")[0]
    step = re.search(r"^      - name: (.+)\n        id: test$", first_job, re.MULTILINE)
    assert step is not None, "the step that runs the job's command has another id"
    assert "python -m app.published_chain" in first_job.split(step.group(0))[1]
    assert "uses: actions/checkout" in first_job and "uses: actions/setup-python" in first_job
    named = f'step "{step.group(1)}"'
    assert named in prose("README.md"), "README.md does not name the step that holds the reason"
    # Twice in the deployment guide: where the first run is watched, and under "Day to day".
    assert prose("docs/DEPLOY.md").count(named) == 2
    assert f"the step *{step.group(1)}*" in prose("docs/USAGE.md")
    for name in ("README.md", "docs/USAGE.md", "docs/DEPLOY.md"):
        assert "last step" not in prose(name), f"{name} sends its reader to a step that is not the last on the screen"
    # Nor does the workflow itself: its header and the red job's message name the step.
    assert "last step" not in workflow
    assert f"its step '{step.group(1)}'" in workflow.split("\n  verdict:\n")[1]

    readme = document("README.md")
    listed = readme.split("**What a red run means.**")[1].split("A red run without such a line")[0]
    assert len(re.findall(r"^\* ", listed, re.MULTILINE)) == len(reasons)
    assert f"`{line}` says which of these {count} reasons it is" in one_line(listed)

    for name in ("docs/USAGE.md", "docs/LEARNING.md", "docs/DEPLOY.md"):
        assert f"`{line}`" in prose(name), name
    assert f"There are {count} such reasons." in prose("docs/USAGE.md")
    assert f"lists the {count} reasons" in prose("docs/LEARNING.md")
    assert f"says which of {count} reasons it is" in prose("docs/DEPLOY.md")
    # The README says what each reason means. It names something to do for some of them
    # only, so the deployment guide does not promise more.
    assert "says what each of them means" in prose("docs/DEPLOY.md")
    assert "what to do about each" not in prose("docs/DEPLOY.md")

    # GitHub emails about a failed scheduled run only when the owner's notifications for
    # Actions are on. Three documents say so, and the deployment guide has the step.
    assert "if that person's notifications for Actions are switched on" in prose("README.md")
    assert "if their notifications for Actions are on" in prose("docs/USAGE.md")
    assert "if their Actions notifications are on" in prose("docs/LEARNING.md")
    assert "\n### 3. Switch GitHub's emails for failed runs on\n" in document("docs/DEPLOY.md")


def test_a_free_id_is_checked_in_the_places_the_readme_names() -> None:
    """ "Only free models, always" rests on a check wherever an id comes in or goes out."""
    from app import model_cache, model_catalogue, model_testing, published_chain

    places = {
        "in OpenRouter's list": model_catalogue.free_models,
        "in the saved file": model_cache.read,
        "in the published list": published_chain.parse,
        "in the job that publishes it": published_chain.build,
        "before a test sends a model its first document": model_testing.run_test,
        "at the moment a chain is put to use": model_cache.use,
    }
    text = prose("README.md")
    for words, checks_it in places.items():
        assert words in text, f"README.md no longer names this place: {words}"
        assert "is_free_model(" in inspect.getsource(checks_it), checks_it.__qualname__
    # The job that publishes looks once more, with a pattern of its own.
    assert "free.fullmatch(model)" in WORKFLOW.read_text(encoding="utf-8")

    count = IN_WORDS[len(places)]
    assert f"That is checked in {count} places" in text
    assert f"in each of the {count} places" in text


@pytest.mark.asyncio
async def test_callers_stop_where_the_documents_say(allowance: Any) -> None:
    """The last fifth of the day is kept back, and a run needs every call it may make above it."""
    from app import budget

    limit = 1000
    calls = len(DEFAULT_CHAIN)

    async def let_in(remaining: int) -> bool:
        budget.reset()
        allowance(remaining=remaining, limit=limit)
        run = budget.Run(calls=calls)
        try:
            await budget.admit_visitor(run)
        except budget.CallLimitError:
            return False
        budget.release(run)
        return True

    stops_at = int(limit * budget.VISITOR_SHARE) + calls - 1
    assert not await let_in(stops_at) and await let_in(stops_at + 1)
    budget.reset()

    assert f"With a limit of {limit} and the two default models, runs therefore stop when {stops_at} requests or fewer are left" in prose("README.md")
    assert f"so with a limit of {limit} requests stop when {stops_at} or fewer are left" in prose("docs/USAGE.md")

    # A repair only needs its own requests to fit, and it starts all the same when there
    # is no count. The button is the one that is refused then.
    async def why_not(by: str, planned: int, remaining: Optional[int]) -> Optional[str]:
        budget.reset()
        allowance(remaining=remaining, limit=limit)
        return await budget.why_no_test(by, planned)

    planned = 14
    assert await why_not("failure", planned, planned - 1) == "allowance"
    assert await why_not("failure", planned, planned) is None
    assert await why_not("failure", planned, None) is None
    assert await why_not("schedule", planned, None) is None
    assert await why_not("page", planned, None) == "allowance_unknown"
    budget.reset()

    assert "A repair only needs its own requests to fit." in prose("README.md")
    assert "a repair does not start when the day's count shows fewer free requests left than its test may send" in prose("README.md")
    assert "When that count cannot be read, the repair starts all the same." in prose("docs/USAGE.md")
    learning = prose("docs/LEARNING.md")
    assert "When OpenRouter cannot say what is left, the button is refused" in learning
    assert "The repair and the scheduled test go ahead" in learning
    # The count of the day is OpenRouter's, and the guide says so where it first counts on it.
    assert "free_model_daily_requests" in inspect.getsource(real_read_allowance)
    assert (
        "no test starts when too few of the day's free requests are left for it. "
        "OpenRouter keeps that count, and the service asks for it"
    ) in learning


# ---------------------------------------------------------------------------
# Documents - the README and the code
# ---------------------------------------------------------------------------


def test_readme_states_the_numbers_the_code_uses() -> None:
    """Every threshold is a named constant, and the README repeats each of them in a sentence.

    Change a constant and this test names the sentence to change with it.
    """
    from app import budget, model_chooser, model_rules, published_chain
    from app.main import MAX_BODY_BYTES
    from app.playground.benchmark import live_cases

    text = prose("README.md")
    documents = len(live_cases())
    stated = [
        f"more than {spaced(MAX_BODY_BYTES)} bytes",
        f"at {budget.MINUTE_CEILING} live calls or fewer",
        f"lets {budget.UNKNOWN_LIVE_CALLS} live calls through",
        f"needs {budget.BUTTON_SHARE:.0%} of the day",
        f"leaves {budget.SCHEDULE_SHARE:.0%} of the day",
        f"for {budget.READ_DEADLINE_SECONDS:g} seconds at most",
        f"has {IN_WORDS[model_rules.PLACES]} places",
        f"{IN_WORDS[model_rules.CANDIDATE_LIMIT]} at most",
        f"the benchmark's {IN_WORDS[documents]} sample documents",
        f"ends after {IN_WORDS[model_rules.GIVE_UP_AFTER]} calls",
        f"costs {model_rules.CANDIDATE_LIMIT * documents} at the very most",
        f"may start {model_chooser.PAUSE_SECONDS // 60} minutes after",
        f"waits {model_chooser.READ_SECONDS:g} seconds at most",
        f"waits {model_chooser.LOOK_READ_SECONDS:g} seconds at most",
        f"asked less than {IN_WORDS[model_chooser.PUBLISHED_PAUSE_SECONDS // 60]} minutes ago",
        f"at most {spaced(published_chain.MAX_BYTES)} bytes",
        f"and {published_chain.RETRY_AFTER_HOURS} hours have passed",
        f"from {published_chain.STALE_AFTER_DAYS} days on",
    ]
    missing = [phrase for phrase in stated if phrase not in text]
    assert not missing, f"README.md no longer says, or says with another number: {missing}"

    # Written out in words in the README, so they cannot be built from the constants.
    assert budget.VISITOR_SHARE == 0.20, "the README says 'a fifth of the day's limit'"
    assert model_chooser.LOOK_PAUSE_SECONDS == 60, "the README says 'once a minute at most'"
    assert published_chain.RETEST_AFTER_HOURS == 156, "the README says 'six and a half days old'"
    assert published_chain.ALARM_AFTER_DAYS == 21, "the README says 'three weeks'"
    assert model_rules.MAX_MISSES == 1, "the README says 'at most one was missed'"


def test_the_readme_says_near_its_top_what_its_claims_rest_on() -> None:
    """An engineer with five minutes reads the first screen. It says where the proof is."""
    from tests import conftest

    top = one_line("\n".join(document("README.md").splitlines()[:45]))
    assert "every call behind the measured figures is saved under [benchmarks/](benchmarks/README.md)" in top
    assert saved_records(), "each file the snapshot names is there"
    assert "The test suite covers the service, and it holds both pages and these documents to the code." in top
    assert "It needs no key and no network: see [Testing strategy](#testing-strategy)." in top
    # No key: the suite pins a made-up one. No network: every way out is closed, for every test.
    assert conftest.TEST_ENVIRONMENT["OPENROUTER_API_KEY"].startswith("test-")
    setup = (PROJECT_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "@pytest.fixture(autouse=True)\ndef closed_doors(" in setup
    assert (PROJECT_ROOT / "tests" / "test_doors.py").is_file()
    for name in ("tests/test_models_page.py", "tests/test_front_door.py", "tests/test_docs.py"):
        assert (PROJECT_ROOT / name).is_file(), f"{name}: one of the files that hold the pages and the documents"


def test_readme_lists_every_route_of_the_page() -> None:
    """The route table under "Playground" is the list a reader takes for complete."""
    from app.playground.routes import router

    text = prose("README.md")
    for route in router.routes:
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            assert f"`{method} {route.path}`" in text


def test_readme_layout_names_every_module_test_file_and_workflow() -> None:
    """A new module that is in no line of the layout is how the layout went stale before."""
    layout = document("README.md").split("### Repository layout")[1].split("```")[1]
    on_disk = [
        *(PROJECT_ROOT / "app").glob("*.py"),
        *(PROJECT_ROOT / "app" / "playground").glob("*.py"),
        *(PROJECT_ROOT / "tests").glob("*.py"),
        *(PROJECT_ROOT / ".github" / "workflows").glob("*.yml"),
        *(PROJECT_ROOT / name for name in ("render.yaml", ".python-version", "pytest.ini")),
    ]
    missing = sorted({path.name for path in on_disk if path.is_file() and path.name not in layout})
    assert not missing, f"README.md, 'Repository layout', does not name: {missing}"


def test_readme_quotes_a_refusal_as_the_service_words_it() -> None:
    """The example under "The two 503s" is the sentence a caller really reads."""
    from app import budget

    readme = document("README.md")
    usage = document("docs/USAGE.md")
    sentence = budget._BUSY.format(wait="37 seconds")
    for text in (readme, usage):
        assert text.count(f'"{sentence}"') == 2  # "message", and "message" inside "detail"
        assert f'"error_type": "{budget.CallLimitError.__name__}"' in text


def test_readme_gives_the_address_a_service_reads() -> None:
    from app.config import DEFAULT_PUBLISHED_URL

    assert DEFAULT_PUBLISHED_URL in prose("README.md")
    # And it says what the published list is where it first uses the words.
    readme = prose("README.md")
    definition = "the *published list*: the small file in which this project's scheduled test publishes the chain it tested"
    assert definition in readme
    assert readme.index("published list") == readme.index(definition) + len("the *")
    # The file and the branch the usage guide names are the ones in that address.
    assert DEFAULT_PUBLISHED_URL.endswith("/tested-models/chain.json")
    usage = prose("docs/USAGE.md")
    assert "`chain.json`" in usage and "`tested-models`" in usage


def test_readme_describes_the_scheduled_job_as_the_workflow_is() -> None:
    """The time it starts, its names on GitHub, its tick box and its command."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    text = prose("README.md")

    cron = re.search(r'cron: "(\d+) (\d+) \* \* \*"', workflow)
    assert cron is not None, "the workflow no longer starts once a day"
    assert f"every day at {int(cron.group(2)):02d}:{int(cron.group(1)):02d} UTC" in text

    title = re.search(r"^name: (.+)$", workflow, re.MULTILINE)
    assert title is not None and f'"{title.group(1)}"' in text
    jobs = re.findall(r"^    name: (.+)$", workflow, re.MULTILINE)
    assert len(jobs) == 3
    for name in jobs:
        assert f'"{name}"' in text, name

    tick_box = re.search(r'description: "([^"]+)"', workflow)
    assert tick_box is not None and f'"{tick_box.group(1)}"' in text
    assert "python -m app.published_chain" in workflow
    assert "`python -m app.published_chain`" in text


def test_readme_counts_the_scenarios_as_the_catalogue_does() -> None:
    from app.playground.catalogue import scenarios_for

    words = {10: "ten", 14: "fourteen", 16: "sixteen"}
    text = prose("README.md").lower()
    assert len(DEFAULT_CHAIN) == 2
    assert f"{words[len(scenarios_for(2))]} prepared runs with the default chain" in text
    assert f"{words[len(scenarios_for(3))]} runs when the chain has three models" in text
    assert f"{words[len(scenarios_for(1))]} when the chain has one" in text


def calls_a_test_sends(models: list[str]) -> int:
    """The calls a test of ``models`` sends, worked out from the first saved pass.

    A model is sent no further document once two of its calls were not fully right.
    """
    from app import model_rules

    first_pass = [call for call in load_records(saved_records()) if call["pass"] == 1]
    total = 0
    for model in models:
        misses = 0
        for call in (call for call in first_pass if call["model"] == model):
            total += 1
            misses += call["correct"] is not True
            if misses >= model_rules.GIVE_UP_AFTER:
                break
    return total


def test_readme_figures_for_a_test_of_the_models_are_what_the_records_give() -> None:
    """What a test costs "with the list of 7 October" is worked out from the saved calls."""
    from app import model_testing

    text = prose("README.md")
    measured = [row["model"] for row in BENCHMARK]
    outside = [model for model in measured if model not in DEFAULT_CHAIN]
    assert len(measured) == 4, "the README says 'the four models that were listed'"
    assert f"about {calls_a_test_sends(measured)} for a test of the four models" in text
    assert f"it costs {calls_a_test_sends(outside)}:" in text

    recorded = model_testing.recorded_run()
    assert recorded is not None
    assert f"which is {len(recorded['calls'])} real calls" in text


# ---------------------------------------------------------------------------
# Documents - the usage guide and the code
# ---------------------------------------------------------------------------


def test_usage_states_the_limits_as_the_code_has_them() -> None:
    """Every number here is a named constant. The guide's sentence is built from it."""
    from app import budget, main, model_chooser, model_rules, published_chain
    from app.playground.benchmark import live_cases

    guide = prose("docs/USAGE.md")
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    cron = re.search(r'cron: "(\d+) (\d+) \* \* \*"', WORKFLOW.read_text(encoding="utf-8"))
    assert cron is not None
    minute, hour = cron.groups()

    documents = len(live_cases())
    page_ms = {name: int(value) for name, value in re.findall(r"const (\w+_MS) = (\d+);", script)}

    sentences = [
        # the ceiling (app/budget.py)
        f"hold {budget.MINUTE_CEILING} live calls or fewer",
        f"{budget.MINUTE_CEILING} live model calls a minute",
        f"lets {budget.UNKNOWN_LIVE_CALLS} live calls through per start",
        f"at least {budget.BUTTON_SHARE:.0%} of the day's free requests",
        f"took more than {budget.READ_DEADLINE_SECONDS:g} seconds",
        # the body limit and the two startup limits (app/main.py, app/model_chooser.py)
        f"at most {spaced(main.MAX_BODY_BYTES)} bytes",
        f"`Retry-After: {main.RETRY_AFTER_SECONDS}`",
        f"a {main.CATALOGUE_DEADLINE_SECONDS:g}-second budget",
        f"a limit of its own, {model_chooser.READ_SECONDS:g} seconds",
        # a test of the models (app/model_rules.py, app/model_chooser.py)
        f"A chain has {IN_WORDS[model_rules.PLACES]} places",
        f"the {IN_WORDS[documents]} sample documents",
        f"up to {documents} requests",
        f"takes {IN_WORDS[model_rules.CANDIDATE_LIMIT]} models at most",
        f"{model_rules.CANDIDATE_LIMIT * documents} free requests at the very most",
        f"at most {IN_WORDS[model_rules.MAX_MISSES]} was missed",
        f"after {IN_WORDS[model_rules.GIVE_UP_AFTER]} calls that were not right",
        f"Tests are kept {model_chooser.PAUSE_SECONDS // 60} minutes apart",
        # the published list and the scheduled test (app/published_chain.py, the workflow)
        f"The list is {published_chain.STALE_AFTER_DAYS} days old or more",
        f"could confirm nothing and {published_chain.RETRY_AFTER_HOURS} hours have passed",
        f"{IN_WORDS[published_chain.ALARM_AFTER_DAYS // 7]} weeks",
        f"every day at {int(hour):02d}:{int(minute):02d} UTC",
        # the page while the service does not answer (app.js)
        f"every {page_ms['HEALTH_EVERY_MS'] // 1000} seconds, "
        f"for {page_ms['GIVE_UP_AFTER_MS'] // 60000} minutes at most",
    ]
    missing = [sentence for sentence in sentences if sentence not in guide]
    assert not missing, f"docs/USAGE.md does not say, with the code's numbers: {missing}"

    # Shares that the guide writes as words.
    assert budget.VISITOR_SHARE == 0.20 and "a fifth of the day's limit" in guide
    assert budget.SCHEDULE_SHARE == 0.20 and "less than a fifth of the day's free requests" in guide


def test_usage_lists_the_test_files_that_exist() -> None:
    section = document("docs/USAGE.md").split("\n## 12. Running the tests")[1].split("\n## 13. ")[0]
    listed = set(re.findall(r"^\| `(tests/test_\w+\.py)` \|", section, re.MULTILINE))
    present = {f"tests/{path.name}" for path in (PROJECT_ROOT / "tests").glob("test_*.py")}
    assert listed == present, sorted(listed ^ present)


def test_python_in_usage_compiles_and_imports_what_exists() -> None:
    """A sample that does not compile, or imports a name that is gone, teaches a mistake."""
    text = document("docs/USAGE.md")
    samples = re.findall(r"^```python\n(.*?)^```", text, re.DOTALL | re.MULTILINE)
    samples += re.findall(r'^python -c "(from app\.[^"\n]+)"$', text, re.MULTILINE)
    assert len(samples) >= 10
    imported = 0
    for source in samples:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "app":
                module = importlib.import_module(node.module)
                for alias in node.names:
                    assert hasattr(module, alias.name), f"{node.module} has no {alias.name}"
                    imported += 1
    assert imported >= 8


def test_commands_routes_and_error_types_in_usage_exist() -> None:
    from starlette.routing import Match

    from app import budget, extractors
    from app.main import app

    text = document("docs/USAGE.md")
    commands = set(re.findall(r"python -m (app(?:\.\w+)+)", text))
    assert {"app.model_catalogue", "app.published_chain", "app.playground.benchmark"} <= commands
    for module in commands:
        assert importlib.util.find_spec(module) is not None, f"python -m {module}"

    def serves(method: str, path: str) -> bool:
        # Asked of the application itself, so the page's routes count although no schema lists them.
        scope = {"type": "http", "method": method, "path": path, "root_path": "", "path_params": {}}
        return any(route.matches(scope)[0] == Match.FULL for route in app.routes)

    routes = set(re.findall(r"`(GET|POST) (/[\w/.-]*)`", text))
    assert ("GET", "/playground/models") in routes and ("POST", "/playground/models/test") in routes
    for method, path in routes:
        assert serves(method, path), f"the service has no route {method} {path}"

    error_types = set(re.findall(r"`(\w+Error)`", text))
    assert "CallLimitError" in error_types
    for name in error_types:
        found = getattr(extractors, name, None) or getattr(budget, name, None)
        assert isinstance(found, type) and issubclass(found, extractors.ExtractionError), name


def test_usage_shows_the_lines_the_catalogue_command_prints() -> None:
    """Section 9 shows what ``python -m app.model_catalogue`` prints, one line of each kind."""
    from app.model_catalogue import Listing, describe, look_up

    base = "https://openrouter.ai/api/v1"

    def model_not_found(url: str) -> tuple[int, str]:
        return 404, json.dumps({"error": {"message": "Model not found: vendor/model-a:free"}})

    def no_answer(url: str) -> tuple[int, str]:
        raise TimeoutError

    printed = [
        describe(Listing(DEFAULT_CHAIN[0], "listed")),
        describe(look_up("vendor/model-a:free", base, model_not_found)),
        describe(Listing("vendor/model-b:free", "listed", expires="2026-12-31"), today=date(2026, 12, 21)),
        describe(look_up("vendor/model-c:free", base, no_answer)),
    ]
    text = document("docs/USAGE.md")
    for line in printed:
        assert f"\n{line}\n" in text, f"docs/USAGE.md does not show this line of the command: {line}"

    # The command writes three more lines itself: which chain it checks when that is not the
    # built-in default (one line for a named chain, one for a saved one), and what follows for
    # a model of a saved chain that is not listed. The guide shows all three.
    from app import model_cache, model_catalogue

    own = []
    for node in ast.walk(ast.parse(inspect.getsource(model_catalogue.main))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print" and node.args:
            words = fixed_words(node.args[0])
            if words and any(words):
                own.append(re.compile("".join(".*" if piece is None else re.escape(piece) for piece in words)))
    assert len(own) == 3
    shown = text.splitlines()
    for pattern in own:
        assert any(pattern.fullmatch(line) for line in shown), (
            f"docs/USAGE.md does not show the line the command prints as: {pattern.pattern}"
        )
    assert f"saved in {model_cache.PATH.name}, not the built-in default." in text
    assert f"delete {model_cache.PATH.name} and restart." in text


# ---------------------------------------------------------------------------
# Documents - the log lines the usage guide shows
#
# docs/USAGE.md, section 14, shows log lines and says what to do about each. The two
# tests below read every logging call in app/ and check that each line the guide shows
# is one the code can write, by the logger and at the level the guide gives.
# ---------------------------------------------------------------------------

#: A logging format or an f-string, cut into its fixed words. ``None`` stands for a
#: part that changes from one line to the next.
Words = list[Optional[str]]

LOG_LEVELS = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING", "error": "ERROR", "exception": "ERROR"}

#: A log line as the guides show it in a block: ``LEVEL [logger] words``.
SHOWN_LOG_LINE = re.compile(r"^(DEBUG|INFO|WARNING|ERROR) \[(app\.[\w.]+)\] (.+)$", re.MULTILINE)

#: Stands for a part that changes, where the fixed words are kept as one string.
ANYTHING = chr(0)


def fixed_words(node: ast.expr) -> Optional[Words]:
    """The fixed words of a string expression, or None when it is not one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        words: Words = []
        for index, piece in enumerate(re.split(r"%(?:\.\d+)?[sdrfg]", node.value)):
            words += [None] if index else []
            words += [piece] if piece else []
        return words
    if isinstance(node, ast.JoinedStr):
        return [part.value if isinstance(part, ast.Constant) else None for part in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = fixed_words(node.left), fixed_words(node.right)
        return None if left is None or right is None else left + right
    return None


def lines_the_code_can_log() -> list[tuple[str, set[str], Words]]:
    """Every logging call in app/: (logger name, levels it can have, its fixed words)."""
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted((PROJECT_ROOT / "app").rglob("*.py"))
    }
    # What a function returns, for calls such as logger.error("%s", describe(listing)).
    returned: dict[str, list[Words]] = {}
    for tree in trees.values():
        for function in ast.walk(tree):
            if isinstance(function, ast.FunctionDef):
                for node in ast.walk(function):
                    if isinstance(node, ast.Return) and node.value is not None:
                        words = fixed_words(node.value)
                        if words and any(words):
                            returned.setdefault(function.name, []).append(words)

    found = []
    for path, tree in trees.items():
        source = path.read_text(encoding="utf-8")
        named = re.search(r'^logger = logging\.getLogger\((?:"([\w.]+)"|__name__)\)', source, re.MULTILINE)
        if named is None:
            continue
        # getLogger(__name__) is the module's own dotted name: app/extractors.py is app.extractors.
        logger_name = named.group(1) or ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts)
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "logger"
            ):
                continue
            method, arguments = call.func.attr, list(call.args)
            if method == "log":
                # The level is worked out when the line is written: take every level it names.
                levels = {node.attr for node in ast.walk(arguments[0]) if isinstance(node, ast.Attribute)}
                levels = (levels & set(LOG_LEVELS.values())) or set(LOG_LEVELS.values())
                arguments = arguments[1:]
            elif method in LOG_LEVELS:
                levels = {LOG_LEVELS[method]}
            else:
                continue
            words = fixed_words(arguments[0]) if arguments else None
            if words and any(words):
                found.append((logger_name, levels, words))
            elif len(arguments) > 1 and isinstance(arguments[1], ast.Call):
                # logger.error("%s", describe(listing)): the words are the function's.
                name = getattr(arguments[1].func, "id", getattr(arguments[1].func, "attr", ""))
                found += [(logger_name, levels, words) for words in returned.get(name, [])]
    return found


def log_lines_not_from_the_code(name: str) -> list[str]:
    """The log lines a document shows in full that no logging call of the service can write."""
    code = [
        (logger_name, levels, re.compile("".join(".*" if piece is None else re.escape(piece) for piece in words)))
        for logger_name, levels, words in lines_the_code_can_log()
    ]
    return [
        f"no {level} line of {shown_by} reads: {line}"
        for level, shown_by, line in SHOWN_LOG_LINE.findall(document(name))
        if not any(
            logger_name == shown_by and level in levels and pattern.fullmatch(line)
            for logger_name, levels, pattern in code
        )
    ]


def test_every_log_line_usage_shows_is_one_the_code_can_write() -> None:
    """The samples: lines written out in full, as ``LEVEL [logger] words``, in a block of the guide."""
    shown = SHOWN_LOG_LINE.findall(document("docs/USAGE.md"))
    assert len(shown) >= 30
    # One of them is the line that holds what a document said, and it is a DEBUG line.
    assert [level for level, _, line in shown if "vendor=" in line] == ["DEBUG"]
    assert not log_lines_not_from_the_code("docs/USAGE.md")
    # The samples leave out how every real line starts: the date and the time, and the
    # spaces that pad the level to one width.
    from app import main

    assert 'format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s"' in inspect.getsource(main.configure_logging)
    guide = prose("docs/USAGE.md")
    assert "In the real log every line starts with the date and the time." in guide
    assert "The level is then padded with spaces" in guide and "The samples leave both out." in guide


def test_every_log_line_usage_explains_is_one_the_code_can_write() -> None:
    """The table "What the service says about its chain and its ceiling".

    Its first column gives the words a line starts with, with ``...`` for each part that
    changes. Its second column gives the level.
    """

    def as_one_string(words: Words) -> str:
        joined = "".join(ANYTHING if piece is None else piece for piece in words)
        return re.sub(f"{ANYTHING}+", ANYTHING, joined)  # "%s%s" changes as one part

    code = [(levels, as_one_string(words)) for _, levels, words in lines_the_code_can_log()]
    section = document("docs/USAGE.md").split("\n### What the service says about its chain and its ceiling")[1]
    section = section.split("\n### ")[0]
    rows = [line.split("|") for line in section.splitlines() if line.startswith("| `")]
    assert len(rows) >= 25
    for _, said, level, *_ in rows:
        quoted = [words for words in re.findall(r"`([^`]+)`", said) if not re.fullmatch(r"app\.[\w.]+", words)]
        levels_shown = set(re.findall(r"`(INFO|WARNING|ERROR)`", level))
        assert quoted and levels_shown, said
        for words in quoted:
            start = re.sub(f"{ANYTHING}+", ANYTHING, words.replace("...", ANYTHING))
            matching = [levels for levels, whole in code if whole.startswith(start)]
            assert matching, f"no log line of the service starts with: {words}"
            possible = set().union(*matching)
            assert levels_shown <= possible, (
                f"{words}: the guide says {sorted(levels_shown)}, the code {sorted(possible)}"
            )


def test_usage_says_which_finding_of_a_look_is_written_at_info() -> None:
    """Of what a look into a failed request can find, one thing is routine: a failure that passes.

    The usage guide said that INFO includes "what the service found when it looked into a
    failed request". Every other finding of a look is a warning or an error.
    """
    looked = [
        (levels, "".join(piece or "..." for piece in words))
        for name, levels, words in lines_the_code_can_log()
        if name == "app.model_chooser" and any("did not answer a request" in (piece or "") for piece in words)
    ]
    assert len(looked) == 4, looked
    routine = [line for levels, line in looked if levels == {"INFO"}]
    assert len(routine) == 1 and "but OpenRouter still lists it" in routine[0], routine
    assert all(levels == {"WARNING"} for levels, line in looked if line not in routine)

    guide = prose("docs/USAGE.md")
    assert "a failed request that the service looked into and took for a failure that passes by itself" in guide
    assert "what the service found when it looked into a failed request" not in guide
    # The log's own word for such a failure is "a passing one". The guide says it in full,
    # because "passing" can be read as "passing a test".
    assert "is taken for one that passes by itself, and nothing changes" in guide
    assert "The failure is taken for one that passes by itself, such as a rate limit." in guide
    assert "taken for a passing one" not in guide


# ---------------------------------------------------------------------------
# Documents - the learning guide and the code it walks through
# ---------------------------------------------------------------------------

#: Where the guide's walkthrough says each thing lives. A rename in the code fails the
#: test below, and so does a name that is dropped from the guide.
WALKTHROUGH = {
    "app.config": ["DEFAULT_MODEL_CHAIN", "Settings.models_pinned", "Settings.model_chain"],
    "app.model_catalogue": ["FREE_MODEL_ID", "is_free_model", "free_models"],
    "app.model_rules": ["judge", "next_chain", "in_turn", "MAX_MISSES", "GIVE_UP_AFTER", "MUST_BE_RIGHT", "PLACES"],
    "app.model_cache": ["read", "use", "stand_down", "_save"],
    "app.model_testing": ["run_test"],
    "app.model_chooser": [
        "_use",
        "_stand_in",
        "_owed",
        "note",
        "start",
        "picture",
        "read_published",
        "tested_at",
    ],
    "app.published_chain": ["fetch", "parse", "fingerprint", "due"],
    "app.budget": ["CallLimitError", "CountedExtractor", "BudgetedDispatcher"],
    "app.playground.benchmark": ["rank", "run"],
    "app.playground.routes": ["same_origin_only"],
    "app.main": ["BodyLimitMiddleware"],
}


def test_every_file_the_learning_guide_names_is_in_the_project() -> None:
    """A module that is renamed, split or removed must not live on in the walkthrough."""
    text = prose("docs/LEARNING.md")
    named = set(re.findall(r"`((?:app|tests|benchmarks|\.github)/[\w./-]+\.(?:py|yml|md|jsonl))`", text))
    named |= {name for name in ("render.yaml", ".env.example", ".gitignore") if f"`{name}`" in text}
    # If the pattern stops matching, this test would pass while checking nothing.
    assert len(named) >= 20
    missing = sorted(name for name in named if not (PROJECT_ROOT / name).is_file())
    assert not missing, f"docs/LEARNING.md names files that are not in the project: {missing}"


def test_every_test_the_learning_guide_names_is_in_the_suite() -> None:
    """The guide points at tests by name, and a renamed test would leave it pointing at nothing."""
    named = set(re.findall(r"`(test_\w+)`", prose("docs/LEARNING.md")))
    assert len(named) >= 4
    suite = "\n".join(path.read_text(encoding="utf-8") for path in (PROJECT_ROOT / "tests").glob("*.py"))
    missing = sorted(name for name in named if f"def {name}(" not in suite)
    assert not missing, f"docs/LEARNING.md names tests that are not in tests/: {missing}"


def test_what_the_learning_guide_names_in_a_module_is_in_that_module() -> None:
    text = prose("docs/LEARNING.md")
    for module_name, names in WALKTHROUGH.items():
        module = importlib.import_module(module_name)
        for name in names:
            thing: Any = module
            for part in name.split("."):
                assert hasattr(thing, part), f"{module_name} has no {name}"
                thing = getattr(thing, part)
            short = name.split(".")[-1]
            # A function is written with its brackets in the guide: `judge()`, `use(..., save=False)`.
            written = f"{short}(" if callable(thing) and not isinstance(thing, type) else short
            found = re.search(rf"(?<!\w){re.escape(written)}", text)
            assert found, f"docs/LEARNING.md no longer names {written} of {module_name}"

    # And no function that is gone lives on in a guide: every private function that a
    # document writes with its brackets is defined somewhere in app/.
    code = "\n".join(path.read_text(encoding="utf-8") for path in (PROJECT_ROOT / "app").rglob("*.py"))
    private = {(name, written) for name in MARKDOWN for written in re.findall(r"`(_[a-z]\w*)\(\)`", document(name))}
    assert len(private) >= 8, sorted(private)
    gone = sorted((name, written) for name, written in private if f"def {written}(" not in code)
    assert not gone, f"a document names a function that is not in app/: {gone}"

    # A test of the models has no runner of its own: it hands the benchmark's runner a
    # pace and limits of its own. The guide says so in plain words.
    from app import model_chooser, model_testing

    assert "benchmark.run(" in inspect.getsource(model_testing.run_test)
    assert "So the tool that measured the default chain also does the service's own measuring." in text
    # Every step from a failed request first asks what costs no free request: the published
    # list, then OpenRouter's pages, and only then a test, which calls models.
    look = inspect.getsource(model_chooser._look)
    assert look.index("read_published(at_startup=False)") < look.index("await _pages(") < look.index('await start("failure"')
    assert "and every step on it first asks what costs no free request:" in text

    # "The next six files ... are longer than everything else in app/ outside the playground."
    six = ("model_rules", "model_cache", "model_testing", "model_chooser", "published_chain", "budget")
    lengths = {
        path.stem: len(path.read_text(encoding="utf-8").splitlines()) for path in (PROJECT_ROOT / "app").glob("*.py")
    }
    table = document("docs/LEARNING.md").split("six modules, in reading order")[1].split("\n### ")[0]
    assert re.findall(r"^\| `app/(\w+)\.py` \|", table, re.MULTILINE) == list(six)
    assert f"The next {IN_WORDS[len(six)]} files" in text
    assert sum(lengths[name] for name in six) > sum(count for name, count in lengths.items() if name not in six)
    assert "Together they are longer than everything else in `app/` outside the playground" in text


def test_the_numbers_in_the_learning_guide_are_the_named_constants_of_the_code() -> None:
    """Each sentence is built from the constant it quotes, so a changed number fails here."""
    from app import budget, model_chooser, model_rules, published_chain
    from app.config import Settings
    from app.main import CATALOGUE_DEADLINE_SECONDS, MAX_BODY_BYTES
    from app.playground.benchmark import live_cases

    text = prose("docs/LEARNING.md")
    documents = len(live_cases())
    models = model_rules.CANDIDATE_LIMIT
    timeout = Settings.model_fields["timeout_seconds"].default
    pace = model_rules.SECONDS_BETWEEN_STARTS

    def percent(share: float) -> str:
        return f"{share:.0%}"

    sentences = [
        # 9.3: the worst case
        f"{timeout * 2:g} seconds with the two default models, and {timeout * model_rules.PLACES:g} "
        "seconds once the service has filled a third place",
        # 9.7: what a test may send, and how often
        f"the chain has {IN_WORDS[model_rules.PLACES]} places at most",
        f"One test sends at most {models * documents} requests: {IN_WORDS[models]} models, "
        f"{IN_WORDS[documents]} documents each.",
        f"A service keeps its tests {model_chooser.PAUSE_SECONDS // 60} minutes apart",
        # 12: when a published list is called old
        f"from {published_chain.STALE_AFTER_DAYS} days on the log and the page say how old it is",
        # 14: the ceiling and the size limit
        f"under a ceiling of {budget.MINUTE_CEILING} calls",
        f"A request body of more than {spaced(MAX_BODY_BYTES)} bytes",
        # 15: startup
        f"with a limit of {IN_WORDS[int(model_chooser.READ_SECONDS)]} seconds",
        f"inside a {IN_WORDS[int(CATALOGUE_DEADLINE_SECONDS)]}-second budget",
        # 15: the rules
        f"`MAX_MISSES` is {model_rules.MAX_MISSES}.",
        f"`GIVE_UP_AFTER` is {model_rules.GIVE_UP_AFTER}.",
        # 15: one test
        f"Request starts are {pace:g} seconds apart. That is {60 / pace:g} a minute",
        # 15: the scheduled test
        f"and {published_chain.RETRY_AFTER_HOURS} hours have passed",
        f"no chain younger than {IN_WORDS[published_chain.ALARM_AFTER_DAYS // 7]} weeks is published",
        # 15: the ceiling
        f"The ceiling is {budget.MINUTE_CEILING} and not OpenRouter's 20.",
        f"fit under {budget.MINUTE_LIMIT} in a minute, and after that it keeps its own pace of "
        f"{60 / pace:g} a minute",
        f"more than {percent(budget.VISITOR_SHARE)} of the day's limit is left",
        f"at least {percent(budget.BUTTON_SHARE)} of the day's limit is left",
        f"would still leave {percent(budget.SCHEDULE_SHARE)} of the day's limit",
        f"visitors get {budget.UNKNOWN_LIVE_CALLS} live calls and no more",
    ]
    missing = [sentence for sentence in sentences if sentence not in text]
    assert not missing, f"docs/LEARNING.md does not say, with the code's numbers: {missing}"

    # How often the page asks while a test runs is a constant of the page's script.
    busy = re.search(r"const LOOK_BUSY_MS = (\d+);", PAGE_SCRIPT.read_text(encoding="utf-8"))
    assert busy is not None, "the page's script no longer has LOOK_BUSY_MS"
    assert f"while a test runs the page asks every {IN_WORDS[int(busy.group(1)) // 1000]} seconds" in text


def test_what_the_learning_guide_says_about_the_third_pass_is_in_the_records() -> None:
    """The guide argues "repair, never rank" from one day's records, so hold it to them."""
    from app import model_rules
    from app.playground.benchmark import live_cases

    calls = load_records(saved_records())
    fallback = DEFAULT_CHAIN[1]
    passes: dict[int, list[dict[str, Any]]] = {}
    for call in calls:
        if call["model"] == fallback:
            passes.setdefault(call["pass"], []).append(call)

    assert {number: len(made) for number, made in passes.items()} == {1: 7, 2: 7, 3: 7}
    right = {number: sum(call["correct"] is True for call in made) for number, made in passes.items()}
    assert right == {1: 6, 2: 6, 3: 4}
    third = passes[3]
    assert sum(call["correct"] is False for call in third) == 1
    assert [call["error_type"] for call in third if call["outcome"] == "no_answer"] == [
        "ProviderRateLimitError",
        "ProviderRateLimitError",
    ]
    assert (
        "6 of 7 right in each of the first two passes, and 4 of 7 in the third: one wrong answer, "
        "and two calls turned away by a rate limit"
    ) in prose("docs/LEARNING.md")
    # The README argues from the same pass.
    assert "right on only 4 of 7 documents, and two of its three misses were calls that were rate limited" in prose(
        "README.md"
    )

    # And the rules call that pass "Not judged", not "Failed" (exercise 10 asks the reader why).
    cases = [case["id"] for case in live_cases()]
    limit = BENCHMARK_SNAPSHOT["timeout_seconds"]
    row = model_rules.judge([fallback], third, cases, limit, finished=True)[0]
    assert row["verdict"] == "not_judged"


def test_the_layers_around_the_routes_are_added_in_the_order_the_guide_gives() -> None:
    """The guide once said a 500 carries the CORS headers while the order made that untrue."""
    assert "the body limit, the request context, then CORS" in prose("docs/LEARNING.md")
    source = (PROJECT_ROOT / "app/main.py").read_text(encoding="utf-8")
    body_limit = source.index("app.add_middleware(BodyLimitMiddleware)")
    request_context = source.index('@app.middleware("http")')
    cors = source.index("CORSMiddleware,")  # the call, not the import
    # The layer added last is the outermost.
    assert body_limit < request_context < cors


def test_the_code_the_learning_guide_shows_is_in_the_file_it_names() -> None:
    text = prose("docs/LEARNING.md")
    shown = [
        ("app/config.py", 'return "openrouter_models" in self.model_fields_set'),
        (
            "app/model_rules.py",
            "kept = [ model for model in current "
            'if not (model in gone and not by_model.get(model, {}).get("answered")) ]',
        ),
        # The first line of a function, with the annotations that the code gives it.
        ("app/extractors.py", "def harden(node: Any) -> Any:"),
        ("app/extractors.py", 'hardened["required"] = list(hardened["properties"].keys())'),
    ]
    for name, excerpt in shown:
        assert excerpt in text, f"docs/LEARNING.md no longer shows: {excerpt}"
        source = one_line((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        assert excerpt in source, f"{name} no longer holds: {excerpt}"
    # What the second of them means, in the words that lead up to it: a model that
    # OpenRouter calls gone stays for as long as it answers.
    assert "The word of OpenRouter's catalogue, its public list of models, is not evidence on its own either" in text

    # The words that stand for "no value". The guide shows how the set begins and ends its
    # block with "...", so what it shows has to be the first words of the set, none left out.
    from app import schemas

    block = re.search(r"_NULLISH_TOKENS = frozenset\(\{(.*?)\.\.\.\}\)", document("docs/LEARNING.md"), re.DOTALL)
    assert block is not None, "docs/LEARNING.md no longer shows how _NULLISH_TOKENS begins"
    in_guide = re.findall(r'"([^"]*)"', block.group(1))
    source = (PROJECT_ROOT / "app" / "schemas.py").read_text(encoding="utf-8")
    in_code = re.findall(r'"([^"]*)"', source.split("_NULLISH_TOKENS: frozenset[str] = frozenset(")[1].split(")")[0])
    assert set(in_code) == set(schemas._NULLISH_TOKENS) and len(in_code) == len(schemas._NULLISH_TOKENS)
    assert len(in_guide) >= 8 and in_guide == in_code[: len(in_guide)], in_guide


def test_only_the_chooser_changes_the_chain_and_only_in_two_functions() -> None:
    """Both guides say so: "the only writer", and "the only module that changes the chain".

    One of the two functions chooses a chain. The other lets a stand-in answer on top of
    the chosen chain, and takes the stand-in away again. The chain in use changes in the
    cache through ``use()`` and ``stand_down()``, and through nothing else.
    """
    learning = prose("docs/LEARNING.md")
    assert "in it only two small functions: `_use()` and `_stand_in()`." in learning
    assert "only `_use()` and `_stand_in()` change the chain" in learning
    assert "It is the only module that changes the chain." in prose("docs/PYTHON_LEARNING.md")

    writers: dict[str, set[str]] = {}
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("use", "stand_down")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "model_cache"
                ):
                    who = f"{path.relative_to(PROJECT_ROOT).as_posix()}:{function.name}"
                    writers.setdefault(who, set()).add(node.func.attr)
    assert writers == {
        "app/model_chooser.py:_use": {"use"},
        "app/model_chooser.py:_stand_in": {"use", "stand_down"},
    }

    # And when the chain changes, the dispatcher that was built from the old one is dropped.
    chooser = (PROJECT_ROOT / "app" / "model_chooser.py").read_text(encoding="utf-8")
    assert "get_dispatcher.cache_clear()" in chooser
    assert "`get_dispatcher.cache_clear()`" in prose("docs/PYTHON_LEARNING.md")


def test_what_the_learning_guide_says_is_shipped_is_what_is_shipped() -> None:
    text = prose("docs/LEARNING.md")
    assert "**Left blank**, as `.env.example` ships it." in text
    assert re.search(r"^OPENROUTER_MODELS=[ \t]*$", document(".env.example"), re.MULTILINE)

    assert "`render.yaml` starts exactly one" in text
    assert "--workers 1" in (PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8")

    assert "`.gitignore` names it" in text
    assert "model_cache.json" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").split()


def test_the_recorded_test_is_what_the_learning_guide_says_it_is() -> None:
    """One model of the four fails the recorded test and one is right but slow: the guide names both."""
    from app import model_testing

    recorded = model_testing.recorded_run()
    assert recorded is not None
    verdicts = {row["model"]: row["verdict"] for row in recorded["models"]}
    assert verdicts["apodex/apodex-1.1-mini:free"] == "failed"
    assert verdicts["dots-studio/dots-3-note-preview:free"] == "slow"
    assert all(verdicts[model] == "passed" for model in DEFAULT_CHAIN)
    labels = verdict_labels()
    learning = prose("docs/LEARNING.md")
    # The sentence names the two models by what the same paragraph says of each. It once
    # said "the first of those two models" right after "the other two are the default
    # chain", and so read as if a default model had failed the recorded test.
    assert "one (`apodex/apodex-1.1-mini:free`) rejected the strict schema on every call" in learning
    assert "one (`dots-studio/dots-3-note-preview:free`) gave no wrong answer" in learning
    reasons = {row["model"]: row["why"] for row in recorded["models"]}
    assert reasons["apodex/apodex-1.1-mini:free"] == "unusable_answers"
    assert (
        f'the model that rejected the schema is "{labels["failed"]}", and the slow one is "{labels["slow"]}"'
        in learning
    )
    assert "the first of those two models" not in learning
    assert f"{len(recorded['calls'])} real calls" in learning


# ---------------------------------------------------------------------------
# Documents - the Python guide and the code
# ---------------------------------------------------------------------------

#: A label that starts a line and promises real lines of the project: "**Source:**",
#: "**Source, docstring omitted:**", "**Abridged teaching version:**" and so on.
SOURCE_LABEL = re.compile(r"^\*\*(Source|Abridged teaching version)[^*]*\*\*")
#: Labels of blocks that shorten a line on purpose, so their lines are not all in the code.
SHORTENED = ("abridged:", "signature only")


def labelled_blocks(text: str, label: re.Pattern[str], fence: str) -> list[tuple[str, int, list[str]]]:
    """Every fenced block of one kind that follows a label: (label, line number, lines).

    A label reaches the next fenced block and no further.
    """
    lines = text.splitlines()
    found: list[tuple[str, int, list[str]]] = []
    current: Optional[tuple[str, int]] = None
    index = 0
    while index < len(lines):
        if label.match(lines[index]):
            current = (lines[index], index + 1)
        if lines[index].startswith("```"):
            end = index + 1
            while not lines[end].startswith("```"):
                end += 1
            if current is not None and lines[index].startswith(fence):
                found.append((current[0], current[1], lines[index + 1 : end]))
            current = None
            index = end
        index += 1
    return found


def test_source_blocks_in_the_python_guide_are_lines_of_the_code() -> None:
    """A block the guide calls source must be in the code, line by line and in order.

    The guide once showed ``Settings.model_chain`` as it had been before the method
    changed, under the label "Source". Lines may be left out between the lines shown
    ("docstring omitted", "excerpt"); a block labelled "abridged:" or "signature only"
    shortens lines on purpose and is not checked.
    """
    sources = {
        path.relative_to(PROJECT_ROOT).as_posix(): [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        ]
        for folder in ("app", "tests")
        for path in sorted((PROJECT_ROOT / folder).rglob("*.py"))
    }

    def home(block: list[str]) -> Optional[str]:
        for name, source in sources.items():
            at = 0
            try:
                for wanted in block:
                    at = source.index(wanted, at) + 1
            except ValueError:
                continue
            return name
        return None

    blocks = [
        (label, number, [line.strip() for line in lines if line.strip()])
        for label, number, lines in labelled_blocks(document("docs/PYTHON_LEARNING.md"), SOURCE_LABEL, "```python")
        if not any(word in label for word in SHORTENED)
    ]
    # If the labels are ever reworded this must not pass while checking nothing.
    assert len(blocks) >= 15
    for label, number, block in blocks:
        assert home(block) is not None, (
            f"docs/PYTHON_LEARNING.md line {number}: the block under {label!r} is not in the "
            "code any more, line by line and in order"
        )


def test_run_examples_in_the_python_guide_print_what_the_guide_says() -> None:
    """Every "Run" example is run, and its output compared with "Expected output".

    The examples are what a beginner types first, so one that has gone stale is the
    first thing they meet. None of them reaches the network: the closed doors of
    ``conftest.py`` would fail this test if one did.
    """
    text = document("docs/PYTHON_LEARNING.md")
    examples = labelled_blocks(text, re.compile(r"^\*\*Run:\*\*"), "```python")
    expected = labelled_blocks(text, re.compile(r"^Expected output:"), "```text")
    assert len(examples) == len(expected) >= 10

    for (_, number, code), (_, _, output) in zip(examples, expected):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            exec(compile("\n".join(code) + "\n", f"<the example at line {number}>", "exec"), {"__name__": "__main__"})
        assert printed.getvalue() == "\n".join(output) + "\n", f"the Run example at line {number}"


def test_the_python_guide_names_only_tests_and_functions_that_exist() -> None:
    """Test names, and the functions of the three modules that keep state: all must be there."""
    text = document("docs/PYTHON_LEARNING.md")

    defined = set()
    for path in (PROJECT_ROOT / "tests").glob("test_*.py"):
        defined |= set(re.findall(r"^\s*(?:async )?def (test_\w+)", path.read_text(encoding="utf-8"), re.MULTILINE))
    # test_practice.py is the file exercise 5 asks the reader to write.
    theirs = {"test_not_applicable_invoice_number_becomes_empty"}
    named = set(re.findall(r"(?<![\w/])(test_[a-z0-9_]+)\b(?!\.py)", text)) - theirs
    assert named and named <= defined, f"the guide names tests that do not exist: {sorted(named - defined)}"

    used = set(re.findall(r"(?<![\w/])(model_cache|model_chooser|budget)\.(?!py\b)(\w+)", text))
    assert {("model_cache", "forget"), ("budget", "reset"), ("model_chooser", "reset")} <= used
    for module, attribute in used:
        assert hasattr(importlib.import_module(f"app.{module}"), attribute), f"app.{module}.{attribute}"


def test_the_python_guides_file_table_has_every_module_and_every_test_file() -> None:
    """A new module, or a new file of tests, gets a row before the guide is out of date again."""
    lesson = document("docs/PYTHON_LEARNING.md").split("## 2. Know what each file does")[1].split("## 3. Values")[0]
    first_cells = " ".join(line.split("|")[1] for line in lesson.splitlines() if line.startswith("| ["))
    links = re.findall(r"\[([^\]]+)\]\(\.\./([\w./]+)\)", first_cells)
    for words, target in links:
        assert words == target, f"a row of the file table reads {words} and opens {target}"
    rows = {target for _, target in links}
    files = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in [*(PROJECT_ROOT / "app").glob("*.py"), *(PROJECT_ROOT / "tests").glob("*.py")]
        if path.name != "__init__.py" or path.parent.name == "app"
    }
    assert len(rows) >= 20
    assert not files - rows, f"the file table of docs/PYTHON_LEARNING.md has no row for {sorted(files - rows)}"


def test_the_python_guides_figures_are_the_codes_constants() -> None:
    """The ceiling, the size limit and the fixed Retry-After, where the guide states them."""
    from app import budget
    from app.main import MAX_BODY_BYTES, RETRY_AFTER_SECONDS

    text = prose("docs/PYTHON_LEARNING.md")

    assert f"the last {budget.MINUTE_SECONDS:g} seconds hold {budget.MINUTE_CEILING} live calls or fewer" in text
    assert budget.VISITOR_SHARE == 0.20 and "the last fifth of the day's free requests is kept back" in text
    assert "when one of its calls would start with only a fifth of the day's free requests left, or less" in text
    # Whose figure that is, said before the guide first uses the words.
    assert "a number of requests per UTC day" in one_line(budget.__doc__ or "")
    explained = "OpenRouter gives an account a fixed number of free requests for each day"
    assert explained in text and text.index(explained) < text.index("the day's free requests")
    assert f"`MAX_BODY_BYTES`, which is {MAX_BODY_BYTES:,} bytes" in text
    assert f"Request body is larger than {MAX_BODY_BYTES:,} bytes" in text
    assert RETRY_AFTER_SECONDS == "5" and "The other handlers send a fixed `5`" in text
    assert "--workers 1" in (PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "(`--workers 1`)" in text


def test_the_python_guides_error_table_names_handlers_that_are_registered() -> None:
    """Every "`SomeError` handler" in the guide is a handler of the application."""
    from app import budget
    from app.main import app

    registered = {cls.__name__ for cls in app.exception_handlers if isinstance(cls, type)}
    named = set(re.findall(r"`(\w+Error)` handler", document("docs/PYTHON_LEARNING.md")))

    assert {"CallLimitError", "BodyTooLargeError", "AllProvidersFailedError"} <= named <= registered
    # The guide says the most specific handler wins, which needs both of these.
    assert issubclass(budget.CallLimitError, budget.ExtractionError)
    assert {"CallLimitError", "ExtractionError"} <= registered


def test_the_python_guide_describes_the_extract_route_as_it_is() -> None:
    """Which ``extract_invoice`` the route runs, and where it tells the chooser of a failure."""
    from app import budget, extractors
    from app import main as service

    text = prose("docs/PYTHON_LEARNING.md")
    assert "Two functions are called `extract_invoice`." in text
    assert service.extract_invoice is budget.extract_invoice
    assert extractors.extract_invoice is not budget.extract_invoice

    source = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert source.count("model_chooser.note(") == 2
    told = re.search(r"except AllProvidersFailedError as exc:\n(.*?)\n        if fallback_triggered:", source, re.DOTALL)
    assert told is not None and "model_chooser.note(" in told.group(1)
    assert told.group(1).rstrip().endswith("raise"), "the guide says that block ends with a bare raise"


def test_the_python_guide_describes_the_modules_that_keep_state_as_they_are() -> None:
    """The lesson on a module's memory, and the rows of the file table, read off the modules."""
    from app import budget, model_cache, model_chooser, model_testing, published_chain

    text = prose("docs/PYTHON_LEARNING.md")
    assert "Modules of this project keep state this way too." in text
    for module in (model_cache, budget, model_chooser):
        assert re.search(r"^    global \w", inspect.getsource(module), re.MULTILINE), module.__name__

    # "Pure functions": the rules read no file, no clock and no network.
    rules = (PROJECT_ROOT / "app" / "model_rules.py").read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:from|import) ([\w.]+)", rules, re.MULTILINE))
    assert not imported & {"os", "pathlib", "time", "datetime", "json", "io", "socket", "urllib", "asyncio"}

    # The row of app/model_testing.py: "Callbacks: functions passed as arguments, which the
    # runner calls when a call starts and when it ends", a nested function that changes a
    # variable of the outer one, and a task that is started and cancelled.
    table = document("docs/PYTHON_LEARNING.md").split("## 2. Know what each file does")[1].split("## 3. Values")[0]
    (row,) = [line for line in table.splitlines() if line.startswith("| [app/model_testing.py]")]
    assert "**Callbacks**" in row and "`nonlocal`" in row and "`asyncio.create_task`" in row
    # The row of app/published_chain.py says what the scheduled test is, because the guide
    # uses the words there for the first time. "About once a week": a test is due once the
    # last one is six and a half days old, and the job runs every day.
    (published,) = [line for line in table.splitlines() if line.startswith("| [app/published_chain.py]")]
    assert published_chain.RETEST_AFTER_HOURS == 156
    assert "this project's scheduled test, a job on GitHub that tests the free models about once a week," in published
    assert "A scheduled job on GitHub tests the free models about once a week" in text
    assert text.index("a job on GitHub that tests the free models about once a week") < text.index(
        "A scheduled job on GitHub tests the free models about once a week"
    )
    one_test = inspect.getsource(model_testing.run_test)
    assert "on_start=started" in one_test and "on_call=ended" in one_test
    assert "def started(" in one_test and "def ended(" in one_test and "nonlocal key_refused" in one_test
    assert "asyncio.create_task(" in one_test and "runner.cancel()" in one_test
    # The lesson on middleware names the same statement where the main path has it.
    from app import main

    assert "nonlocal received" in inspect.getsource(main.BodyLimitMiddleware)
    assert "`nonlocal received`" in text

    # "Kept in a set until it ends, and cancelled by stop() at shutdown."
    assert isinstance(model_chooser._tasks, set)
    assert asyncio.iscoroutinefunction(model_chooser.stop)
    assert "task.cancel()" in inspect.getsource(model_chooser.stop)

    # Exercise 7: the schema is part of the fingerprint, and a list made with another one
    # is turned away with these words.
    made = inspect.getsource(published_chain.fingerprint)
    assert "strict_json_schema()" in made and "openrouter_strict_json_schema" in made
    assert '"was made by another version of the test"' in text
    assert "it was made by another version of the test" in inspect.getsource(published_chain.parse)


def test_the_python_guide_describes_the_test_setup_as_it_is() -> None:
    """Two automatic fixtures, three functions that forget, and the settings it names."""
    from tests.conftest import TEST_ENVIRONMENT

    text = prose("docs/PYTHON_LEARNING.md")
    conftest = (PROJECT_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")

    automatic = re.findall(r"@pytest\.fixture\(autouse=True\)\ndef (\w+)", conftest)
    assert automatic == ["hermetic_settings", "closed_doors"]
    assert "Its two `@pytest.fixture(autouse=True)` fixtures" in text
    for name in automatic:
        assert f"`{name}`" in text

    # The second fixture hands its work to a context manager, and the guide says which one.
    managers = re.findall(r"@contextmanager\ndef (\w+)", conftest)
    assert managers == ["close_the_doors"]
    (manager,) = managers
    assert f"Its work is done by `{manager}`, a context manager of the project's own" in text
    assert f"with {manager}(monkeypatch) as knocks:" in conftest
    assert f"`with {manager}(...) as knocks:`" in text
    # Every helper of that file that the guide writes as code is in the file.
    for helper in ("hermetic_settings", "closed_doors", manager, "allowance", "knocks"):
        assert re.search(rf"\b{helper}\b", conftest), helper
    assert "`doors_closed`" not in text, "that was the context manager's name before it was renamed"

    for call in ("model_cache.forget()", "model_chooser.reset()", "budget.reset()"):
        assert call in conftest and f"`{call}`" in text
    for name, value in (("MODEL_CATALOGUE_CHECK", "false"), ("MODEL_PUBLISHED_URL", "null")):
        assert TEST_ENVIRONMENT[name] == value and f"`{name}={value}`" in text
    assert len(TEST_ENVIRONMENT["OPENROUTER_MODELS"].split(",")) == 3 and len(DEFAULT_CHAIN) == 2


# ---------------------------------------------------------------------------
# Documents - the deployment guide
#
# docs/DEPLOY.md tells somebody who has never used GitHub Pages or Render what to click
# and what to look for. What a screen of GitHub or of Render shows cannot be checked
# here. What this repository decides can: the names the guide tells its reader to look
# for, the line it tells them to edit, the settings it says they need not type, and the
# words it quotes from the two pages and from the log.
# ---------------------------------------------------------------------------

DEPLOY = "docs/DEPLOY.md"


def workflows() -> dict[str, tuple[str, list[str]]]:
    """Each workflow by the name GitHub shows for it: its text, and the names of its jobs.

    GitHub shows a job by its ``name``, or by its key in the file when it has none.
    """
    shown: dict[str, tuple[str, list[str]]] = {}
    for path in sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        title = re.search(r"^name: (.+)$", text, re.MULTILINE)
        assert title is not None, f"{path.name} has no name"
        jobs: list[str] = []
        for line in text.split("\njobs:\n", 1)[1].splitlines():
            key = re.fullmatch(r"  ([\w-]+):", line)
            name = re.fullmatch(r"    name: (.+)", line)
            if key:
                jobs.append(key.group(1))
            elif name:
                jobs[-1] = name.group(1)
        shown[title.group(1)] = (text, jobs)
    return shown


def test_the_deployment_guide_names_the_workflows_and_their_jobs_as_they_are() -> None:
    """Its reader is told which names to look for in the Actions tab, and what a first run shows."""
    guide = prose(DEPLOY)
    shown = workflows()
    assert set(shown) == {"Tests", "Model check", "Project page"}
    assert f"{IN_WORDS[len(shown)].capitalize()} workflows run on GitHub" in guide
    # The sentence that introduces them gives each name once, as the Actions tab shows it.
    introduced = guide.split("Their names appear in the repository's **Actions** tab:")[1].split(" - The address")[0]
    assert sorted(re.findall(r"\*\*([^*]+)\*\*", introduced)) == sorted(shown), introduced

    for title, (text, jobs) in shown.items():
        assert f"**{title}**" in guide, f"{DEPLOY} does not name the workflow {title}"
        # The first push starts each of them, which is why the guide has three runs to watch.
        assert re.search(r"^  push:\n    branches: \[main\]$", text, re.MULTILINE), title
        for job in jobs:
            # A job with a name of its own is quoted. One that goes by its key is written as code.
            written = f"`{job}`" if re.fullmatch(r"[\w-]+", job) else f'"{job}"'
            assert written in guide, f"{DEPLOY} does not name the job {job} of {title}"
    assert f"**Tests**: {IN_WORDS[len(shown['Tests'][1])]} job" in guide
    # The step whose last line the guide tells its reader to look at.
    step = re.search(r"^      - name: (.+)\n        run: python -m pytest -q$", shown["Tests"][0], re.MULTILINE)
    assert step is not None and f'Its step "{step.group(1)}" ends with one line' in guide
    assert f"**Project page**: {IN_WORDS[len(shown['Project page'][1])]} jobs" in guide
    assert f"It has {IN_WORDS[len(shown['Model check'][1])]} jobs" in guide

    # The other documents use the same three names.
    layout = document("README.md").split("### Repository layout")[1].split("```")[1]
    for title in shown:
        assert f'"{title}"' in layout, f"the README's layout does not give the name {title}"
    usage = prose("docs/USAGE.md")
    assert "*Model check*" in usage and "*Project page*" in usage

    # What a first run of "Model check" shows, as the guide's table has it.
    check, check_jobs = shown["Model check"]
    assert "if: ${{ !cancelled() && needs.test.outputs.status != '0' }}" in check, (
        "the third job is skipped after a good run: the guide calls that the good outcome"
    )
    assert "greyed out as **skipped**" in guide and f'"{check_jobs[2]}"' in guide
    tick_box = re.search(r'description: "([^"]+)"', check)
    assert tick_box is not None and f'"{tick_box.group(1)}"' in guide
    # A push that changes the project page alone starts no test of the models.
    paths = re.findall(r"^      - (\S+)$", check.split("\n    paths:\n")[1].split("\n\n")[0], re.MULTILINE)
    assert len(paths) >= 10 and not any(path.startswith("site") for path in paths)
    assert "**Model check** does not start for this push" in guide

    # What a first run of "Project page" shows when Pages was not switched on.
    page, page_jobs = shown["Project page"]
    assert '"Get Pages site failed"' in page and '"Get Pages site failed"' in guide
    assert "tests/test_front_door.py" in page and "tests/test_front_door.py" in guide
    assert "- site/**" in page

    # The job's own words for a run without a key.
    from app import published_chain

    assert "this job has no key" in inspect.getsource(published_chain._job)
    assert '"this job has no key"' in guide
    assert "`OPENROUTER_API_KEY`" in guide and "secrets.OPENROUTER_API_KEY" in check

    # All three runs of a push carry the commit message as their title, because no workflow
    # names its runs. The guide says so, and how to tell the runs apart.
    assert not any("run-name:" in text for text, _ in shown.values())
    assert "All three runs carry your commit message as their title" in guide
    # A run that is started by hand tests without the tick box while nothing is published.
    assert "workflow_dispatch:" in check
    assert published_chain.due(None, "nothing has been published yet", [], datetime.now(timezone.utc))[0] is True
    assert "Leave the box in that panel unticked: a test is due anyway" in guide
    # The job makes the branch the first time it publishes. A run that published nothing
    # leaves no branch to look at, and the guide says what that means.
    assert "git checkout --quiet --orphan tested-models" in check
    assert "If the menu has no **tested-models**, the run published nothing" in guide
    # A red "Tests" run: the job and the step the guide sends its reader to.
    red_tests = next(line for line in document(DEPLOY).splitlines() if line.startswith("| A red cross on **Tests** |"))
    assert f"the job `{shown['Tests'][1][0]}`" in red_tests and f'the step "{step.group(1)}"' in red_tests
    assert "-m pytest -q` |" in red_tests
    # On the Pages screen GitHub suggests workflows of its own. The repository brings one.
    assert "actions/deploy-pages@" in page
    assert "Choose none of them: the repository brings its own workflow, and step 4 pushes it." in guide


def test_the_deployment_guide_names_the_line_that_holds_the_live_demos_address() -> None:
    """Part 3 sends its reader to one line of one file, to replace one name in it."""
    guide = prose(DEPLOY)
    told = re.search(r"line (\d+) of `site/index.html`", guide)
    assert told is not None, f"{DEPLOY} no longer says which line holds the address"
    lines = PROJECT_PAGE.read_text(encoding="utf-8").splitlines()
    line = lines[int(told.group(1)) - 1]
    assert 'id="demo-link"' in line, f"line {told.group(1)} of site/index.html is: {line.strip()}"
    assert "search the file for `demo-link`" in guide
    assert sum("demo-link" in other for other in lines) == 1, "a search for demo-link finds one line"

    placeholder = re.search(r"replace `([^`]+)`", guide)
    assert placeholder is not None and placeholder.group(1).endswith(".invalid")
    # The line holds the placeholder until part 3 is done, and the name the host gave
    # the service afterwards. Either way it is built as the guide says.
    address = re.search(r'href="(https://[^"]+)"', line)
    assert address is not None
    name = address.group(1).removeprefix("https://").removesuffix("/#playground")
    assert address.group(1) == f"https://{name}/#playground"
    assert name == placeholder.group(1) or re.fullmatch(r"[a-z0-9][a-z0-9-]*\.onrender\.com", name), name
    assert "Leave `https://` in front of it and `/#playground` behind it." in guide
    # Render shows two names: the service's own, and the one in its address. The guide asks
    # for the second: the part after "https://", which the project page's file calls the name.
    assert "with the name in the address you noted in Part 2: the part after `https://`, in lower case" in guide
    example = re.search(r"for example `([a-z0-9-]+\.onrender\.com)`", guide)
    assert example is not None, "the guide shows what a name at the host looks like"

    # The test the guide tells its reader to run knows the same placeholder.
    front_door = (PROJECT_ROOT / "tests" / "test_front_door.py").read_text(encoding="utf-8")
    assert f"https://{placeholder.group(1)}/#" in front_door
    assert "-m pytest -q tests/test_front_door.py" in guide
    # One of its tests is skipped until then, and two need Node.js. Three documents say so.
    assert front_door.count('pytest.skip("site/index.html still holds the placeholder') == 1
    with_node = len(re.findall(r"^    seen = replay\(\)$", front_door, re.MULTILINE))
    assert with_node == 2 and front_door.count('shutil.which("node")') == 1
    assert "every test passes, and none is skipped any more" in guide
    assert f"without Node.js {IN_WORDS[with_node]} tests are skipped" in guide
    usage = prose("docs/USAGE.md")
    assert "Until the live demo's address is written into `site/index.html`" in usage
    assert "a run reports one test as skipped" in usage
    assert f"{IN_WORDS[with_node].capitalize()} more tests of `tests/test_front_door.py` run the project page's script" in usage
    readme = prose("README.md")
    assert "One of its tests is skipped until that address" in readme
    # The README names them together with the tests that run the playground's script, which
    # test_the_documents_say_which_tests_run_the_script_of_a_page counts.
    assert f"Where Node.js is not installed, those {IN_WORDS[with_node]} tests are skipped, and so are the " in readme
    # And the project page says the sentence the guide quotes for as long as the placeholder is there.
    quoted = "The live demo is not online yet. This page shows recorded runs instead."
    assert f'"{quoted}"' in guide and quoted in one_line(PROJECT_PAGE.read_text(encoding="utf-8"))
    # Until then the page's script asks for nothing, so the documents say "once" where
    # they say that the project page wakes the live demo.
    script = PROJECT_PAGE_SCRIPT.read_text(encoding="utf-8")
    assert 'address.hostname.endsWith(".invalid")) return;' in script
    assert "Once Part 3 of this guide is done, the project page starts waking it" in guide
    assert "Once the live demo has an address, the page starts waking it" in prose("README.md")
    assert "Once the live demo's address is written into `site/index.html`, the page starts waking" in prose("docs/USAGE.md")
    assert "Once the live demo's address has been written into the page, the page starts waking" in prose(
        "docs/LEARNING.md"
    )


def test_the_deployment_guide_describes_the_host_as_render_yaml_sets_it() -> None:
    """The guide says that nothing but the key is typed into a form. So the file has to hold the rest."""
    blueprint = (PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8")
    guide = document(DEPLOY)

    def setting(key: str) -> str:
        found = re.search(rf"^\s*{key}: ([^#\n]+?)\s*(?:#.*)?$", blueprint, re.MULTILINE)
        assert found is not None, f"render.yaml has no {key}"
        return found.group(1)

    start, build, plan, region = (setting(key) for key in ("startCommand", "buildCommand", "plan", "region"))
    assert f"| `startCommand: {start}` |" in guide
    assert f"==> Running '{start}'" in guide, "the log the guide shows runs the file's own start command"
    assert start.endswith("--workers 1") and "It starts one process" in guide
    assert f"| `buildCommand: {build}` |" in guide
    assert plan == "free" and f"| `plan: {plan}` |" in guide and "on the **Free** plan" in guide
    assert f"| `region: {region}` |" in guide

    service = re.search(r"^  - type: web\n    name: (\S+)$", blueprint, re.MULTILINE)
    assert service is not None
    assert f"one web service, `{service.group(1)}`" in one_line(guide)
    assert f"`{service.group(1)}.onrender.com`" in guide

    # The key is asked for once and is never in the file. The file names no models.
    assert re.search(r"- key: OPENROUTER_API_KEY\n\s+sync: false", blueprint)
    assert "| `OPENROUTER_API_KEY` with `sync: false` |" in guide
    # One value is asked for, and the guide says which key it is, before the button.
    assert blueprint.count("sync: false") == 1 and "It asks for one value, `OPENROUTER_API_KEY`" in one_line(guide)
    asked_for = one_line(guide).index("paste the key of the service, the one in your `.env` file. Type nothing else.")
    assert asked_for < one_line(guide).index("6. Click **Deploy Blueprint**.")
    assert "OPENROUTER_MODELS" not in blueprint and "**Do not add `OPENROUTER_MODELS`.**" in guide
    assert re.search(r"- key: ENVIRONMENT\n\s+value: production", blueprint)
    assert "| `ENVIRONMENT` is `production` |" in guide
    assert "followed by **production**" in guide
    # Render checks only that the service accepts a connection, because the file names no path.
    assert "healthCheckPath" not in blueprint and "**Leave the health check path empty.**" in guide

    # A push that changes only these does not rebuild the service.
    ignored = re.findall(r"^\s+- (\S+)", blueprint.split("ignoredPaths:")[1].split("envVars:")[0], re.MULTILINE)
    assert len(ignored) == 5
    row = next(line for line in guide.splitlines() if line.startswith("| `ignoredPaths` |"))
    for path in ignored:
        folder = path.removesuffix("**")  # "site/**" is the folder site/, "README.md" is a file
        assert f"`{folder}`" in row, f"{DEPLOY} does not say that a change to {folder} alone rebuilds nothing"
    assert "site/**" in ignored, "part 3 says that Render does not redeploy for a change to the project page"
    assert "the folder `site/` is on its list of paths to ignore" in one_line(guide)

    # The host and the two workflows that run Python read the same file for its version.
    assert (PROJECT_ROOT / ".python-version").is_file() and "`.python-version`" in guide
    assert "docs/DEPLOY.md" in blueprint, "the Blueprint sends its reader to the guide"


def test_the_deployment_guide_quotes_the_two_pages_and_the_log_as_they_are() -> None:
    """ "What you should see" is only a help while the page, or the log, really says it."""
    from app import budget, model_cache, model_chooser, model_rules, published_chain
    from app import main as service
    from app.playground.catalogue import scenarios_for

    guide = prose(DEPLOY)

    # The service's own page, which the guide calls the live demo's page.
    live_demo = page_text()
    for words in (
        "Operational",
        "From this project's scheduled test of",
        "API docs",
        "Run the test suite",
        "Run extraction",
        "Every model failed",
        "Invoice extracted",
        "What happens when a free model disappears",
        "Replay this test",
        "No API key",
        "The demo is busy",
        "Today's share of live runs is used up",
        "Please contact the admin.",
        "Models withdrawn",
    ):
        assert words in guide, f"{DEPLOY} no longer quotes: {words}"
        assert words in live_demo, f"the service's page no longer says: {words}"
    # The foot of that page: the service's name and version, then the environment.
    assert "${service.name} v${service.version} · ${service.environment}" in live_demo
    titles = {scenario["title"] for scenario in scenarios_for(len(DEFAULT_CHAIN))}
    for title in ("Every model fails", "Invoice inside an email thread"):
        assert f"**{title}**" in guide and title in titles
    # How a scenario is run: it is picked in the list with this heading, then this button.
    assert "<h3>Scenarios</h3>" in live_demo and "<span>Run extraction</span>" in live_demo
    assert "in the list headed **Scenarios**, then press **Run extraction**" in guide

    # The project page, and the one line of it that its script changes.
    project_page = one_line(
        PROJECT_PAGE.read_text(encoding="utf-8") + "\n" + PROJECT_PAGE_SCRIPT.read_text(encoding="utf-8")
    )
    for words in (
        "Code on GitHub",
        "Open the live demo",
        "Waking the live demo.",
        "The live demo is awake.",
        "The live demo has not answered for three minutes",
    ):
        assert words in guide, f"{DEPLOY} no longer quotes: {words}"
        assert words in project_page, f"the project page no longer says: {words}"
    # The count beside that line is shown only while the page waits, so the guide tells its
    # reader to read it then. And a demo that is awake answers before the page says "waking".
    assert 'clock.hidden = next !== "waking";' in project_page
    assert "The count is gone after that" in guide
    assert "const QUIET_START_MS = " in project_page
    assert "If it says so at once, the live demo was not asleep yet." in guide

    # The log: three lines shown in full, and the words of others.
    assert len(SHOWN_LOG_LINE.findall(document(DEPLOY))) == 3
    assert not log_lines_not_from_the_code(DEPLOY)
    # The sample leaves out how every real line starts.
    assert "%(asctime)s %(levelname)-8s [%(name)s]" in inspect.getsource(service.configure_logging)
    assert "in the real log each of them starts with the date and the time" in guide
    said_by = {
        "OpenRouter counts": real_read_allowance,  # tests/conftest.py replaces budget.read_allowance
        "could not be read": budget._log_unreadable,
        "Could not save the chosen model chain": model_cache._save,
        "OPENROUTER_API_KEY is not set": service.lifespan,
        "was not used": model_chooser.read_published,
        "Using the chain published by the scheduled test of": model_chooser.read_published,
        "Model chain in use is now": model_chooser._switch,
        "this job has no key": published_chain._job,
        "No test today": published_chain._job,
        "This job publishes to": published_chain._job,
        "did not finish": budget._read,
        "could not be read in time": model_chooser._pages,
    }
    for words, writes_it in said_by.items():
        assert words in guide, f"{DEPLOY} no longer quotes: {words}"
        assert words in inspect.getsource(writes_it), f"{writes_it.__qualname__} no longer says: {words}"
    start_line = inspect.getsource(model_chooser.log_mode)
    assert "`Using the tested default`" in guide
    assert 'f"the tested default {chain}"' in start_line and '"Model choice: automatic. Using %s."' in start_line

    # And the figures the guide gives are the code's and the records'.
    first = BENCHMARK[0]
    assert first["model"] == DEFAULT_CHAIN[0]
    assert (
        f"about {first['median_seconds']:.0f} seconds for its usual answer and about "
        f"{first['slowest_seconds']:.0f} seconds for its slowest"
    ) in guide
    assert f"about {calls_a_test_sends([row['model'] for row in BENCHMARK])} free requests" in guide
    assert (
        f"when OpenRouter lists the same {IN_WORDS[len(BENCHMARK)]} models as on "
        f"{long_date(BENCHMARK_SNAPSHOT['measured'])}"
    ) in guide
    pace = model_rules.SECONDS_BETWEEN_STARTS
    assert pace == int(pace) and f"the requests start {IN_WORDS[int(pace)]} seconds apart" in guide
    assert f"Visitors then get {budget.UNKNOWN_LIVE_CALLS} live model calls for each start of the service" in guide
    assert f"{budget.READ_DEADLINE_SECONDS:g} seconds for the count of the day" in guide
    assert f"{model_chooser.CATALOGUE_SECONDS:g} seconds for its pages about the models of the chain" in guide
    assert "asyncio.wait_for(" in inspect.getsource(budget._read) and "READ_DEADLINE_SECONDS" in inspect.getsource(budget._read)
    assert "CATALOGUE_SECONDS" in inspect.getsource(model_chooser._pages)
    assert f"from day {published_chain.STALE_AFTER_DAYS} on" in guide
    assert f"younger than {IN_WORDS[published_chain.ALARM_AFTER_DAYS // 7]} weeks" in guide
    # The address of the published list hands out the same copy for five minutes: the
    # service waits that long between two reads, and the guide tells its reader to.
    minutes = IN_WORDS[model_chooser.PUBLISHED_PAUSE_SECONDS // 60]
    assert f"A new file can take up to {minutes} minutes to appear at that address." in guide
    assert f"restart it after those {minutes} minutes" in guide


def test_documents_that_speak_of_deployment_send_their_reader_to_the_guide() -> None:
    """One walk-through, and every document that speaks of putting the service online links to it.

    The project page has one address. The README gives it near its top, and no document
    gives another one.
    """
    link = re.compile(r"\]\((?:\.\./)?(?:docs/)?DEPLOY\.md(?:#[\w-]+)?\)")
    for name in ("README.md", "docs/USAGE.md", "docs/LEARNING.md", "docs/PYTHON_LEARNING.md", "benchmarks/README.md"):
        assert link.search(document(name)), f"{name} has no link to docs/DEPLOY.md"

    # The guide sends its reader into the other documents, heading by heading. Whether each
    # heading is there is for test_links_in_the_documents_lead_somewhere, which reads this file too.
    assert DEPLOY in MARKDOWN
    targets = re.findall(r"\]\(([^)\s]+)\)", document(DEPLOY))
    into_others = {target for target in targets if "://" not in target and "#" in target and not target.startswith("#")}
    assert len(into_others) >= 6, sorted(into_others)

    canonical = re.search(r'<link rel="canonical" href="([^"]+)">', PROJECT_PAGE.read_text(encoding="utf-8"))
    assert canonical is not None
    address = canonical.group(1)
    assert address in "\n".join(document("README.md").splitlines()[:30]), "the README gives it near its top"
    assert address in (PROJECT_ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    given = 0
    for name in MARKDOWN:
        for found in re.findall(r"https://[\w-]+\.github\.io/[\w./-]*", document(name)):
            assert found == address, f"{name} gives the project page another address: {found}"
            given += 1
    assert given >= 4
    # After a rename the guide sends its reader to every file that holds the old name.
    renaming = prose(DEPLOY).split("**Renaming the repository or your GitHub account:**")[1].split("- **")[0]
    repository = address.removeprefix("https://").split(".github.io/")[1].strip("/")
    assert repository and "`DEFAULT_PUBLISHED_URL` in `app/config.py`" in renaming
    holders = []
    for folder in ("app", "tests", "site", "tools", ".github"):
        for path in sorted((PROJECT_ROOT / folder).rglob("*")):
            if path.suffix in (".py", ".yml", ".html", ".js", ".css", ".json") and path.is_file():
                if repository in path.read_text(encoding="utf-8", errors="replace"):
                    holders.append(path.relative_to(PROJECT_ROOT).as_posix())
    assert len(holders) >= 5, holders
    for holder in holders:
        assert f"`{holder}`" in renaming, f"{DEPLOY} does not send its reader to {holder} after a rename"
    assert "and in the documents" in renaming
    # Capital letters matter in that address, so the documents say how to pass it on.
    for name, words in (
        ("README.md", "Pass the address on as a link"),
        ("docs/USAGE.md", "Pass the address on as a link"),
        (DEPLOY, "Give it to people as a link"),
    ):
        assert words in prose(name), name
    # So the guide itself gives the address as a link wherever it gives it, and never as
    # text that has to be copied or typed.
    assert f"`{address}`" not in document(DEPLOY) and document(DEPLOY).count(f"<{address}>") == 2

    # Setup begins by fetching the code. The address is the one the project page links to,
    # and the folder is the one that a clone of it gives.
    code = re.search(
        r'<a [^>]*href="(https://github\.com/[\w.-]+/[\w.-]+)">Code on GitHub</a>',
        PROJECT_PAGE.read_text(encoding="utf-8"),
    )
    assert code is not None and code.group(1).endswith(f"/{repository}")
    for name in ("README.md", "docs/USAGE.md"):
        assert f"\ngit clone {code.group(1)}.git\ncd {repository}\n" in document(name), name
    layout = document("README.md").split("### Repository layout")[1].split("```")[1]
    assert layout.startswith(f"text\n{repository}/\n"), "the layout's first line is the folder of a clone"

    # The command the guide sends its reader to, for seeing that the key works, and the
    # interpreter the guide starts everywhere else.
    assert 'python -c "from app.config import get_settings; from app.budget import read_allowance;' in document(
        "docs/USAGE.md"
    )
    assert "where the command says `python`" in prose(DEPLOY)
    assert document(DEPLOY).count(".\\.venv\\Scripts\\python.exe") == 3

    # The first push. Its commands stand in two blocks, cut where the reader has to look at
    # what git lists: a block that is pasted into a terminal runs to its end. And the two
    # files that must not be in that list are the two that .gitignore names for that reason.
    raw = document(DEPLOY)
    look = raw.index("Look at what the second `git status` lists before you go on:")
    assert raw.index("git status\ngit add -A\ngit status\n```") < look < raw.index('```powershell\ngit commit -m "Model upkeep')
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and "model_cache.json" in ignored
    assert "The files `.env` and `model_cache.json` must not be in the list. Both are ignored on purpose" in prose(DEPLOY)

    # Both documents say when the screens of GitHub and of Render were last read.
    assert "as it read on 8 and 9 October 2026" in prose(DEPLOY)
    assert "once more on 9 October 2026" in one_line(document("benchmarks/README.md"))


# ---------------------------------------------------------------------------
# Documents - the notes on the records
# ---------------------------------------------------------------------------


def test_the_recorded_test_is_pass_one_of_the_file_the_notes_on_the_records_name() -> None:
    """benchmarks/README.md says which file must stay in the deployment, and why."""
    from app import model_testing

    notes = PROJECT_ROOT / "benchmarks" / "README.md"
    if not notes.is_file():
        pytest.skip("this copy of the project has no benchmarks folder")
    title = re.search(r'recorded: "([^"]+)" \}\[t\.by\]', PAGE_SCRIPT.read_text(encoding="utf-8"))
    assert title is not None and title.group(1) == "A recorded test"

    text = one_line(notes.read_text(encoding="utf-8"))
    sentence = re.search(r"The running service also reads pass 1 of this file, [^.]+\.", text)
    assert sentence is not None and f'"{title.group(1)}"' in sentence.group(0)
    assert "must stay in the repository and in the deployment" in sentence.group(0)

    (name,) = BENCHMARK_SNAPSHOT["records"]
    assert f"`{name.removeprefix('benchmarks/')}` is the run behind the snapshot" in text
    first_pass = [call for call in load_records([PROJECT_ROOT / name]) if call["pass"] == 1]
    recorded = model_testing.recorded_run()
    assert recorded is not None and recorded["by"] == "recorded"
    assert recorded["requests"] == len(first_pass) == len(recorded["calls"])


# ---------------------------------------------------------------------------
# Documents - sentences that nothing held
#
# A reader of these tests planted one false sentence at a time in a copy of the
# documents: a wrong number, level, status code, name or order. Each sentence below stood
# in a document while no test read it, so the suite stayed green. Each is now built from
# the constant, the route or the lines of code that make it true.
# ---------------------------------------------------------------------------

#: Numbers the documents write as words, beyond IN_WORDS.
IN_MORE_WORDS = {**IN_WORDS, 10: "ten", 14: "fourteen"}


def missing_from(name: str, stated: list[str]) -> list[str]:
    """The sentences of ``stated`` that the document ``name`` does not say."""
    text = prose(name)
    return [phrase for phrase in stated if phrase not in text]


def test_sentences_that_nothing_held_say_what_the_code_does() -> None:
    import logging

    from app import budget, extractors, main, model_cache, model_catalogue, model_chooser, model_rules
    from app import model_testing, published_chain
    from app.config import DEFAULT_PUBLISHED_URL, Settings
    from app.playground import benchmark, routes, suite
    from app.playground.catalogue import scenarios_for
    from app.playground.simulation import FAULTS
    from app.schemas import ExtractRequest
    from tests import conftest

    workflow = WORKFLOW.read_text(encoding="utf-8")
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    door = PROJECT_PAGE_SCRIPT.read_text(encoding="utf-8")
    page_ms = {name: int(value) for name, value in re.findall(r"const (\w+_MS) = (\d+);", script)}
    door_ms = {name: int(value) for name, value in re.findall(r"const (\w+_MS) = (\d+);", door)}
    limits = ExtractRequest.model_json_schema()["properties"]["text"]
    shortest, longest = limits["minLength"], limits["maxLength"]
    pace = model_rules.SECONDS_BETWEEN_STARTS
    cron = re.search(r'cron: "(\d+) (\d+) \* \* \*"', workflow)
    assert cron is not None, "the workflow no longer starts once a day"
    daily = f"{int(cron.group(2)):02d}:{int(cron.group(1)):02d} UTC"
    pattern = main._REQUEST_ID_RE.pattern
    assert pattern.startswith("^") and pattern.endswith("$")
    places, quorum = model_rules.PLACES, extractors.ExtractionDispatcher.UNPARSEABLE_QUORUM
    timeout = Settings.model_fields["timeout_seconds"].default
    open_share = round((1 - budget.VISITOR_SHARE) * 1000)  # of a day of 1000 requests
    through = budget.MINUTE_CEILING - len(DEFAULT_CHAIN) + 1  # requests a minute, each answered by its first model
    sample_documents = len(benchmark.live_cases())

    # ---- What the sentences that are written in words rest on -----------------------
    startup = inspect.getsource(main.lifespan)
    assert startup.index("model_chooser.at_startup()") < startup.index("report_model_listings(")
    assert inspect.getsource(model_chooser.at_startup).count("await read_published(") == 1
    assert inspect.getsource(model_chooser.read_published).count("published_chain.fetch") == 1
    look = inspect.getsource(model_chooser._look)
    assert look.index("read_published(at_startup=False)") < look.index("await _pages(")
    chooser = (PROJECT_ROOT / "app" / "model_chooser.py").read_text(encoding="utf-8")
    assert re.findall(r"asyncio\.sleep\(([^)]*)\)", chooser) == ["0"], "nothing in the chooser waits for a timer"
    assert "call_later" not in chooser and "call_at" not in chooser
    assert model_chooser.LOOK_PAUSE_SECONDS == 60, "the documents say 'once a minute at most'"
    assert budget.READ_EVERY_SECONDS == 60, "the documents say 'at most once a minute'"
    assert budget.MINUTE_SECONDS == 60
    assert 'endswith(("(403)", "(404)"))' in inspect.getsource(model_chooser._closed)
    assert re.search(r"if _test is not None:\n\s+return False", inspect.getsource(model_chooser.read_published))
    assert inspect.signature(model_chooser._switch).parameters["level"].default == logging.WARNING
    assert "slow[:1]" in inspect.getsource(model_rules.next_chain), "one model that is right but slow, at most"
    one_test = inspect.getsource(model_testing.run_test)
    assert 'burst = len(record["tested"]) if on_first_right else 1' in one_test
    assert "on_failed(model)" in one_test and "if waited >= budget.MINUTE_SECONDS:" in one_test
    assert "joined if repair else None" in inspect.getsource(model_chooser._run), "only a repair makes a stand-in"
    asked_first = inspect.getsource(model_chooser._free_checks)
    assert "_failed_here" in asked_first and "candidates = members + standing + outside" in asked_first
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "> README.md" in workflow and "git add chain.json" in workflow
    made_of = inspect.getsource(published_chain.fingerprint)
    assert "[model_rules.PLACES, model_rules.MAX_MISSES, model_rules.MUST_BE_RIGHT]" in made_of
    assert "bool(settings.openrouter_strict_json_schema)" in made_of
    assert "model_catalogue._http_get" in inspect.getsource(published_chain.fetch)
    assert "Authorization" not in inspect.getsource(model_catalogue._http_get), "the list is read without a key"
    pressed = inspect.getsource(routes.start_models_test)
    assert "status_code=202" in pressed and "status_code=409" in pressed
    assert "status_code=403" in inspect.getsource(routes.same_origin_only)
    assert routes.router.include_in_schema is False
    assert "max_retries=0" in inspect.getsource(extractors.get_openrouter_client)
    assert extractors.ProviderAuthError("x").retryable is False
    service = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'host="127.0.0.1"' in service and '"type": "bytes_too_long"' in service
    assert 'health_status = "unavailable"\n            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE' in service
    assert '"request %s: extracted invoice model=%s fallback=%s elapsed_ms=%.0f"' in service
    assert model_cache.PATH.name == "model_cache.json"
    assert "model_cache.chain()" in inspect.getsource(Settings.model_chain.fget)
    assert "is_production" in inspect.getsource(suite.availability)
    command = inspect.getsource(published_chain.main)
    assert '"--previous"' in command and '"--out"' in command and '"--force"' in command
    reasons = inspect.getsource(published_chain._job)
    for words in ("younger than three weeks", "refused the API key", "this job has no key", "no longer offers any model", "failed this test"):
        assert words in reasons, words
    suite_source = (PROJECT_ROOT / "tests" / "test_extract.py").read_text(encoding="utf-8")
    assert suite_source.count("def test_valid_invoice_text_parses_into_invoice_data(") == 1
    cascade = suite_source.split("async def test_primary_failure_cascades_to_fallback(")[0].rsplit("@pytest.mark.parametrize(", 1)[1]
    failure_modes = len(re.findall(r'^\s+"[\w-]+",$', cascade.split("ids=[")[1].split("]")[0], re.MULTILINE))
    doors = inspect.getsource(conftest.close_the_doors)
    ways_out = doors.count('"_OPENER"') + doors.count("handle_async_request")
    labels = len(conftest.TEST_ENVIRONMENT["OPENROUTER_MODELS"].split(","))
    assert not issubclass(pytest.fail.Exception, Exception)
    assert 2 * budget.MINUTE_CEILING > 20, "two workers already exceed OpenRouter's 20 a minute"
    assert 'padStart(2, "0")' in door and 'COUNT_ENDS = "so far"' in door, "the project page counts up, in minutes and seconds"
    pages = (PROJECT_ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert 'set Source to "GitHub Actions"' in pages
    blueprint = (PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8")
    ignored = re.findall(r"^\s+- (\S+)", blueprint.split("ignoredPaths:")[1].split("envVars:")[0], re.MULTILINE)
    assert ignored == ["site/**", "docs/**", "README.md", ".github/**", "tests/**"]

    # ---- README.md ------------------------------------------------------------------
    missing = missing_from(
        "README.md",
        [
            # startup, and the look into a failed request
            "It reads the published list, then asks OpenRouter whether each model of the chain is still listed",
            "It calls no model, and it happens once a minute at most.",
            f"cannot be read within {IN_WORDS[int(model_chooser.CATALOGUE_SECONDS)]} seconds decides nothing",
            "refused a request with a 403 or a 404",
            "A list is never taken while a test is running.",
            "Each change of the chain while the service runs is a `WARNING` line",
            f"The whole check then has a {main.CATALOGUE_DEADLINE_SECONDS:g}-second budget",
            "No timer does that: a service that gets no requests repairs nothing.",
            # a test of the models
            "at most one model that is right but slow is added behind them",
            "the first document goes to every model under test at once",
            "A stand-in that fails the test stops answering at once.",
            "A repair does not test the model again while this process runs",
            f"ends after {IN_WORDS[model_rules.GIVE_UP_AFTER]} calls, because that model rejects the response format",
            f"paces itself at {IN_MORE_WORDS[int(60 / pace)]} calls a minute",
            # the catalogue command and the benchmark command
            f"An announced end date within {model_catalogue.EXPIRY_WARNING_DAYS} days is a `WARNING`.",
            f"none has an end date within {model_catalogue.EXPIRY_WARNING_DAYS} days, and 1 otherwise",
            f"refuses a run of more than {benchmark.MAX_REQUESTS} requests and starts requests "
            f"{benchmark.SECONDS_BETWEEN_STARTS:g} s apart",
            # the scheduled job and the published list
            "It runs for the `main` branch only",
            "It does not test when its requests would leave less than a fifth of the day's free requests.",
            f"it is a JSON object of version {published_chain.VERSION}",
            "the same three constants of the rules",
            f"starts daily at {daily}",
            "The request sends no key and calls no model.",
            # the API
            "`unavailable` otherwise (HTTP 503",
            "At most once a minute that includes one request to OpenRouter",
            f"`{pattern[1:-1]}`, otherwise generated",
            f"`Retry-After: {main.RETRY_AFTER_SECONDS}` only when at least one failure was transient",
            f"It is 1 to {budget.MINUTE_SECONDS:g} seconds when the last minute is full.",
            "`202` when it started, `409` with the reason when it may not start",
            f"**{IN_WORDS[quorum].capitalize()} models agree the text is not an invoice**",
            "No in-SDK retries (`max_retries=0`)",
            "gets a `403` in production",
            f"is at most {spaced(longest * 12)} bytes of JSON",
            "listens on `127.0.0.1` only",
            "None of these is in the OpenAPI schema",
            "the log says which model answered and how long that took.",
            # the page
            f"fail in one of {IN_WORDS[len(FAULTS)]} ways",
            f"every {page_ms['HEALTH_EVERY_MS'] // 1000} seconds, for {page_ms['GIVE_UP_AFTER_MS'] // 60000} minutes at most",
            # one caller alone
            f"{open_share} of 1000 requests at {budget.MINUTE_CEILING} a minute take about "
            f"{round(open_share / budget.MINUTE_CEILING)} minutes",
        ],
    )
    assert not missing, f"README.md no longer says, or says with another number: {missing}"

    # ---- docs/USAGE.md --------------------------------------------------------------
    missing = missing_from(
        "docs/USAGE.md",
        [
            "| `unavailable` | 503 |",
            f"Required. {shortest} to {spaced(longest)} characters.",
            f"`text` is under {shortest} / over {spaced(longest)} chars",
            'with `type: "bytes_too_long"`',
            f"about {through} requests a minute get through",
            f"is refused after about {through} requests in one minute",
            "A request is let in only while every call it may make would start with more than a fifth of the day's limit left.",
            f"| {budget.READ_EVERY_SECONDS:g} | `The service cannot check today's free allowance` |",
            f"{timeout * 2:g} s with the two default models, {timeout * places:g} s with three models",
            f"| Requests take {timeout * 2:g} seconds, or {timeout * places:g} |",
            "begins `Model choice: the service does not look after its chain here`.",
            f"| `1` | Anything else: a model is not listed, an end date is {model_catalogue.EXPIRY_WARNING_DAYS} days away or less",
            "gets a `WARNING` when the service looks after the chain, and an `ERROR` with the remedy when the chain is named",
            "in the background, at most once a minute.",
            f"asked less than {IN_WORDS[model_chooser.PUBLISHED_PAUSE_SECONDS // 60]} minutes ago",
            f"cannot be read within {IN_WORDS[int(model_chooser.CATALOGUE_SECONDS)]} seconds decides nothing",
            "The first document goes to all candidates at once",
            "A stand-in that fails the test stops answering at once.",
            f"saved in `{model_cache.PATH.name}` in the project folder",
            "One such model at most",
            "they put no new stand-in to use",
            "only for a test of the `main` branch",
            "python -m app.published_chain --previous previous/chain.json --out out/chain.json ``` `--previous` names",
            "Otherwise it uses the chain that a service on this machine saved in `model_cache.json`",
            "pytest tests/test_extract.py::test_valid_invoice_text_parses_into_invoice_data",
            f"**At most {benchmark.MAX_REQUESTS} requests.**",
            f"Request starts are spaced {benchmark.SECONDS_BETWEEN_STARTS:g} s apart",
            f"a check that ran out of its {main.CATALOGUE_DEADLINE_SECONDS:g} seconds gets one `WARNING`",
            "two workers already exceed the 20 a minute that OpenRouter allows on free models",
            "answers 403 to curl when ENVIRONMENT=production",
            "The button is not there when `ENVIRONMENT=production`.",
            "`nothing is published at that address (HTTP 404)` is an `INFO`. Anything else is a `WARNING`",
            f"{model_chooser.PAUSE_SECONDS // 60} minutes after its last test at the earliest",
            f"or less than {budget.BUTTON_SHARE:.0%} of today's free requests is left",
        ],
    )
    assert not missing, f"docs/USAGE.md no longer says, or says with another number: {missing}"

    # ---- docs/LEARNING.md -----------------------------------------------------------
    missing = missing_from(
        "docs/LEARNING.md",
        [
            "ProviderAuthError 401, and notably NOT retryable",
            f"the header says {main.RETRY_AFTER_SECONDS} seconds",
            f"UNPARSEABLE_QUORUM = {quorum}",
            "It looks into a failure at most once a minute",
            "That test also asks the chain's own models",
            f"or {IN_WORDS[door_ms['GIVE_UP_AFTER_MS'] // 60000]} minutes have passed",
            f"a run of more than {benchmark.MAX_REQUESTS} requests is refused",
            f"The check has {IN_WORDS[int(main.CATALOGUE_DEADLINE_SECONDS)]} seconds in total",
            f"The cascade test is parametrised over {IN_MORE_WORDS[failure_modes]} distinct failure modes.",
            f"With a daily limit of 1000 requests that share is {open_share} calls",
            f"holds at most {IN_WORDS[model_cache.MAX_CHAIN]} different free model ids",
            f"After {IN_WORDS[model_rules.GIVE_UP_AFTER]} misses a model's remaining documents are not sent.",
            f"cannot be read within {IN_WORDS[int(model_chooser.CATALOGUE_SECONDS)]} seconds",
            f"a test takes {IN_WORDS[model_rules.CANDIDATE_LIMIT]} models at most",
            f"Its chain must be one to {IN_WORDS[places]} different free model ids.",
            "The job runs once a day.",
            "So after one minute the test starts anyway",
            "It is read at most once a minute",
            f"Request starts are spaced {benchmark.SECONDS_BETWEEN_STARTS:g} s apart.",
            "the look free: the published list, then OpenRouter's own page for the model",
            f"`{pattern}`",
        ],
    )
    assert not missing, f"docs/LEARNING.md no longer says, or says with another number: {missing}"
    # No constant holds what OpenRouter allows an account in a day: the module that keeps the ceiling says it.
    a_day = "(50, or 1000 once the account has bought credits)"
    assert a_day in one_line(budget.__doc__ or "") and a_day in prose("docs/LEARNING.md")

    # ---- docs/PYTHON_LEARNING.md ----------------------------------------------------
    missing = missing_from(
        "docs/PYTHON_LEARNING.md",
        [
            f"constrained to {shortest}–{longest:,} characters after trimming",
            f"`UNPARSEABLE_QUORUM = {quorum}` controls the early stopping threshold.",
            "The SDK is configured with `max_retries=0`",
            "HTTP 400, `http_error`. FastAPI raises that exception itself.",
            f"`OPENROUTER_MODELS` lists {IN_WORDS[labels]} model IDs",
            "is not a subclass of `Exception`",
            f"each of the service's {IN_WORDS[ways_out]} ways out to the network",
            "Its work is one request that carries no key.",
        ],
    )
    assert not missing, f"docs/PYTHON_LEARNING.md no longer says, or says with another number: {missing}"

    # ---- docs/DEPLOY.md -------------------------------------------------------------
    missing = missing_from(
        DEPLOY,
        [
            "set **Source** to **GitHub Actions**",
            "two files, `README.md` and `chain.json`",
            f"`{DEFAULT_PUBLISHED_URL}`",
            "it answers 503 while no key is set",
            "3. **Name:** `OPENROUTER_API_KEY` (exactly this).",
            "`.python-version`, `render.yaml`, `pytest.ini` and `requirements.txt`",
            "counts the minutes and seconds beside it",
            "at the very bottom, the service's name and version followed by **production**",
            "The first then says that the published list `was not used`",
            "no tested chain younger than three weeks is published, the key was refused, the job has no key and the "
            "default chain or the list is out of date, OpenRouter offers no model of the chain any more, or a model "
            "of the chain failed the test",
            "A push that changes only `site/`, `docs/`, `README.md`, the workflows or the tests does not redeploy the service.",
        ],
    )
    assert not missing, f"{DEPLOY} no longer says: {missing}"
    guide = prose(DEPLOY)
    by_title = {scenario["title"]: scenario for scenario in scenarios_for(len(DEFAULT_CHAIN))}
    for title, status_code, outcome in (("Every model fails", 503, "Every model failed"), ("Invoice inside an email thread", 200, "Invoice extracted")):
        assert by_title[title]["checks"][0]["expected"] == status_code
        assert f'the result "{status_code} {outcome}"' in guide
    for needed in (".python-version", "render.yaml", "pytest.ini", "requirements.txt"):
        assert (PROJECT_ROOT / needed).is_file(), needed

    # ---- .env.example and benchmarks/README.md --------------------------------------
    template = one_line(" ".join(line.lstrip("# ") for line in document(".env.example").splitlines()))
    for phrase in (
        '/health then reports "unavailable"',
        f"with {IN_WORDS[sample_documents]} sample documents",
        "The published list is then read after the first request that fails.",
        "so the service does not use it then",
        "production also hides /docs, switches the page's test-suite button off",
    ):
        assert phrase in template, f".env.example no longer says: {phrase}"

    notes = one_line(document("benchmarks/README.md"))
    assert '`answered`, `refused` ("this is not an invoice") or `no_answer`' in notes
    assert "they are in `site/recorded/`" in notes and (PROJECT_ROOT / "site" / "recorded").is_dir()
    header = run_headers(saved_records())[0]
    assert {"models", "passes", "timeout_seconds", "max_output_tokens"} <= set(header)
    assert "the models, the number of passes, the time limit and the output cap" in notes
    fallback = [call for call in load_records(saved_records()) if call["model"] == DEFAULT_CHAIN[1]]
    limited = sum(call["error_type"] == "ProviderRateLimitError" for call in fallback)
    assert f"it was rate limited on {limited} of {len(fallback)}" in notes


#: More of what docs/USAGE.md quotes from the page, word for word and in italics. Nothing
#: held these, and the page's files are replaced after the documents were written.
MORE_PAGE_WORDS_IN_USAGE = (
    "Your own document",
    "Run extraction",
    "Model chain for this run",
    "Call the model",
    "Run all",
    "Test suite",
    "Run the test suite",
    "Response time",
    "Answered by",
    "Not needed",
    "No result",
    "Waited",
    "Raw request and response",
    "Expectations",
    "This session",
    "Ends",
)

#: What the guide quotes that the page is sent by the service: titles and labels of the catalogue.
CATALOGUE_WORDS_IN_USAGE = ("Not an invoice", "Resilience", "Prompt-injection attempt", "Two models agree")

#: How often the guide quotes a phrase, where that is more than once. A phrase that is quoted in
#: two places is still "quoted" when one of the two is reworded, and that one then goes stale
#: unnoticed. So every quotation is counted.
QUOTED_MORE_THAN_ONCE_IN_USAGE = {
    "Model upkeep": 5,
    "Test the free models now": 4,
    "Model chain": 3,
    "Fixed by the admin of this demo.": 2,
    "Right, but slow": 2,
    "Failed": 2,
    "The demo is busy": 3,
    "Today's share of live runs is used up": 4,
    "The day's share of live runs was used up": 2,
    "No answer yet": 4,
    "Not answering": 3,
    "has probably gone to sleep": 2,
    "Try again": 4,
    "Run all": 5,
    "No result": 3,
    "Expectations": 2,
    "Raw request and response": 2,
    "Not an invoice": 2,
    "Resilience": 3,
    "Prompt-injection attempt": 2,
    "Two models agree": 2,
}


def test_more_of_what_the_documents_quote_from_the_page_is_on_the_page() -> None:
    from app.playground.catalogue import GROUPS, scenarios_for

    guide = one_line(document("docs/USAGE.md"))
    readme = prose("README.md")
    page = page_text()
    for words in MORE_PAGE_WORDS_IN_USAGE:
        assert f"*{words}*" in guide, f"docs/USAGE.md no longer quotes: {words}"
        assert words in page, f"the page no longer says: {words}"
    for words in ("Run all", "Your own document", "Test suite", "Not an invoice"):
        assert f"*{words}*" in readme, f"README.md no longer quotes: {words}"
        assert words in page, f"the page no longer says: {words}"

    # What the catalogue names. The page shows these as the service sends them.
    scenarios = scenarios_for(len(DEFAULT_CHAIN))
    named = {scenario["title"] for scenario in scenarios} | {group["title"] for group in GROUPS}
    named |= {check["label"] for scenario in scenarios for check in scenario["checks"]}
    for words in CATALOGUE_WORDS_IN_USAGE:
        assert words in named, f"the catalogue no longer names: {words}"
        assert f"*{words}*" in guide, f"docs/USAGE.md no longer quotes: {words}"
    assert '"Two models agree"' in readme

    # Every quotation, not only the first one.
    quoted = (*PAGE_WORDS_IN_USAGE, *MORE_PAGE_WORDS_IN_USAGE, *CATALOGUE_WORDS_IN_USAGE)
    assert set(QUOTED_MORE_THAN_ONCE_IN_USAGE) <= set(quoted)
    for words in quoted:
        times = QUOTED_MORE_THAN_ONCE_IN_USAGE.get(words, 1)
        assert guide.count(f"*{words}*") == times, (
            f"docs/USAGE.md quotes *{words}* {guide.count(f'*{words}*')} times, and {times} are held"
        )

    # How often "Run all" waits out a refusal for one scenario, and the key that starts a run.
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    waits = re.search(r"const BATCH_WAITS = (\d+);", script)
    assert waits is not None, "the page's script no longer has BATCH_WAITS"
    times_waited = {1: "once", 2: "twice", 3: "three times"}[int(waits.group(1))]
    assert f"{times_waited} at most for one scenario" in readme
    assert f"{times_waited} at most for one scenario" in guide
    assert 'event.key === "Enter" && (event.ctrlKey || event.metaKey)' in script and "Ctrl+Enter in the document box" in guide
    # How long a notice about the chain stays in the result panel: an hour after the look into
    # the failure, or after the end of the test that followed.
    hour = "const fresh = (stamp) => stamp && new Date(p.now) - new Date(stamp) < 60 * 60 * 1000;"
    assert hour in script and "fresh(p.look.at)" in script and "fresh(t.finished)" in script
    stays = "It stays for an hour after the service's look into the failure, or after the end of the test that followed."
    assert stays in guide, f"docs/USAGE.md no longer says: {stays}"
    assert "It stays for an hour after the service last did something about the failure." in readme


def test_figures_the_documents_take_from_the_notes_are_in_the_notes() -> None:
    """How fast free models come and go, and the long-invoice trial: three documents repeat figures
    that stand in the tables under benchmarks/exploratory/. Nothing compared the two."""
    import statistics

    folder = PROJECT_ROOT / "benchmarks" / "exploratory" / "2026-10-07"
    if not folder.is_dir():
        pytest.skip("this copy of the project has no benchmarks folder")
    history = (folder / "model-list-history.txt").read_text(encoding="utf-8")
    withdrawals = (folder / "model-list-withdrawals.txt").read_text(encoding="utf-8")
    trial = (folder / "long-invoice-trial.txt").read_text(encoding="utf-8")
    readme, usage, learning = prose("README.md"), prose("docs/USAGE.md"), prose("docs/LEARNING.md")
    notes = one_line(document("benchmarks/README.md"))

    # Of the free ids listed 30 and 61 days earlier, how many were still listed.
    still = {
        int(days): int(share)
        for days, share in re.findall(r"\(\s*(\d+) days ago\): \d+ free ->\s*\d+ still listed \((\d+)%\)", history)
    }
    month, two_months = still[30], still[61]
    lost = [round((1 - (share / 100) ** 2) * 100) for share in (month, two_months)]  # at least one of two models
    assert f"{month}% were still listed; of those listed 61 days earlier, {two_months}%" in readme
    assert f"roughly a {lost[0]}% chance of losing at least one within 30 days and {lost[1]}% within 61 days" in readme
    for name, text in (("docs/USAGE.md", usage), ("docs/LEARNING.md", learning)):
        assert f"{month}% and {two_months}% were still listed" in text, name
        assert f"{lost[0]}% chance of losing at least one model within 30 days and {lost[1]}% within 61 days" in text, name
    assert f"{month}% and {two_months}% of free ids still listed after 30 and 61 days" in notes
    assert f'"{lost[0]}% and {lost[1]}%"' in notes

    # The size of the free pool, the withdrawals without an end date, and the copies behind both.
    free = dict(re.findall(r"^\s+(\S+) total=\d+ :free=(\d+)", history, re.MULTILINE))
    then = next(count for copy, count in free.items() if copy.startswith("20250503"))
    today = free["LIVE-20261007"]
    withdrawn = re.search(r":free ids: disappeared while the field existed: (\d+)", withdrawals)
    silent = re.search(r"never showed a date: (\d+)", withdrawals)
    least = re.search(r"notice-at-least days .*? median=([\d.]+)", withdrawals)
    most = re.search(r"notice-at-most days .*? median=([\d.]+)", withdrawals)
    copies = re.search(r"snapshots loaded: (\d+)", history)
    assert withdrawn and silent and least and most and copies
    assert f"{then} ids ended in `:free` in May 2025, and {today} did on 7 October 2026" in readme
    assert f"{then} ids ending in `:free` in May 2025, {today} on 7 October 2026" in usage
    assert f"from {then} ids ending in `:free` in May 2025 to {today} on 7 October 2026" in learning
    assert f"{silent.group(1)} of the {withdrawn.group(1)} free ids that were withdrawn never showed one" in readme
    assert f"{least.group(1)} days before the stated end, and the last copy without it is {most.group(1)} days before" in readme
    for name, text in (("docs/USAGE.md", usage), ("docs/LEARNING.md", learning)):
        assert f"{silent.group(1)} of {withdrawn.group(1)} withdrawn free ids never showed an end date" in text, name
        assert f"between about {float(least.group(1)):.0f} and {float(most.group(1)):.0f} days at the median" in text, name
    assert f"built from {copies.group(1)} copies" in notes

    # How long the two withdrawn models of the first default chain were listed as free.
    trace = re.search(r"nex-n2\.5-mini:free \| (\d{4}-\d{2}-\d{2}) \|.*?SET (\d{4}-\d{2}-\d{2})", history)
    assert trace is not None
    first, last = (date.fromisoformat(day) for day in trace.groups())
    listed = f"{(last - first).days} days, from {first.day} to {last.day} September"
    for name, text in (("README.md", readme), ("docs/USAGE.md", usage), ("docs/LEARNING.md", learning)):
        assert listed in text, f"{name} no longer says: {listed}"

    # The long-invoice trial: the one answer to 40 lines, with the higher cap.
    answered = re.search(r"40-line invoice \(\d+ chars\)\s+([\d.]+)s finish=stop", trial)
    assert answered is not None
    seconds = float(answered.group(1))
    for name, text in (("README.md", readme), ("docs/USAGE.md", usage), ("docs/LEARNING.md", learning)):
        assert f"40-line invoice in {seconds:.0f} s, close to the 30 s timeout" in text, name
    assert f"40 lines in {seconds} s" in notes

    # Figures worked out from the saved calls, where the README states them a second time.
    measured = [row["model"] for row in BENCHMARK]
    outside = [model for model in measured if model not in DEFAULT_CHAIN]
    a_test = calls_a_test_sends(measured)
    assert f"about {a_test} requests with those four models" in readme
    assert f"{calls_a_test_sends(outside)} in all with the four models" in readme
    assert f"One test a week is about {round(a_test * 52 / 12, -1):.0f} free requests a month." in readme
    assert f"{IN_WORDS[len(measured)]} free models advertised structured output, and {IN_WORDS[len(outside)]} were left out" in readme
    assert f"it held {IN_WORDS[len(measured)]} models" in usage
    calls = load_records(saved_records())
    opening = calls[0]["case"]
    slow = next(row["model"] for row in BENCHMARK if row["model"] in outside and row["answered"])
    firsts = [call["seconds"] for call in calls if call["model"] == slow and call["case"] == opening]
    assert f"needed about {round(statistics.median(firsts), -1):.0f} seconds for its first document" in readme


# ---------------------------------------------------------------------------
# Documents - what the page can say, and the usage guide has to have
#
# The tests above ask one question: is what a document quotes on the page? The other
# question went unasked: does the guide have everything of that kind that the page can
# say? The page's files are corrected after these documents were written, and a notice
# that is new on the page had no test that would tell. These tests read the page's
# script for every title of one kind and ask the usage guide for each.
# ---------------------------------------------------------------------------


def test_the_usage_guide_has_a_row_for_every_notice_about_the_chain() -> None:
    """Every notice the page can put into the result panel has a row in the guide's table."""
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    # What the service did about a failed run, and what a repair came to: two parts of the
    # script, one after the other, and every notice about the chain is built in them.
    part = script.split("const LOOK_WORDS = {")[1].split("function outcomeWords(")[0]
    assert "function repairNotice(" in part
    on_page = set(re.findall(r'title: "([^"]+)"', part))
    assert len(on_page) >= 13, sorted(on_page)

    section = document("docs/USAGE.md").split("**Notices about the chain.**")[1].split("**A demo that does not answer.**")[0]
    first_cells = [line.split("|")[1] for line in section.splitlines() if line.startswith("| *")]
    in_guide = {words for cell in first_cells for words in re.findall(r"\*([^*]+)\*", cell)}
    assert in_guide == on_page, (
        f"notices the page can show and the usage guide has no row for: {sorted(on_page - in_guide)}; "
        f"rows of the usage guide for a notice the page no longer shows: {sorted(in_guide - on_page)}"
    )

    # Where the notice stands. The result panel has three parts, and the notice is the middle
    # one: under the head of the run on view, above the rest of that run.
    page = PAGE_FILES[0].read_text(encoding="utf-8")
    panel = re.search(r'<section class="card panel result" id="result"[^>]*>(.*?)</section>', page, re.DOTALL)
    assert panel is not None, "the page no longer has its result panel"
    assert re.findall(r'<div class="[a-z_ ]+" id="([a-z-]+)"', panel.group(1)) == [
        "result-head",
        "upkeep-notice",
        "result-body",
    ]
    assert 'const root = $("#upkeep-notice");' in script_function(script, "renderUpkeepNotice")
    stands = "The notice stands in the result panel, under the outcome of the run"
    usage, readme = prose("docs/USAGE.md"), prose("README.md")
    assert stands in usage and stands in readme, f"a document no longer says: {stands}"
    assert "a notice in the result panel says what a failed run led to" in usage
    # It is drawn from what the service says about its chain and from nothing a visitor did,
    # so every visitor of the page sees it.
    assert "state." not in script_function(script, "upkeepNotice")
    assert "every visitor of the page sees it there" in usage
    # The sentences that put this notice above the playground are gone from every document.
    for name in MARKDOWN:
        text = prose(name)
        assert "notice above the playground says what" not in text, name
        assert "shows a notice above the playground" not in text, name
        assert "Read the notice above the playground" not in text, name
    # One notice does stand above the playground, and the guide says which: a test that was
    # lost because the service started again.
    assert '<p class="notice" id="notice" hidden></p>' in page
    assert page.index('id="notice"') < page.index('<div class="lab">') < page.index('id="result"')
    lost = "The test of the free models that was running before is lost, and it changed nothing."
    assert lost in script and "showNotice(" in script.split(lost)[0][-160:]
    assert (
        "If the service has started again in between while a test was running, a notice above the playground "
        "says that this test is lost."
    ) in usage
    # The deployment guide sends its reader to the sentence under the model chain, which is
    # there for as long as a model of the chain is gone.
    chain = script_function(script, "renderChain")
    assert 'if (p?.mode === "automatic" && p.gone.length) {\n    foot = goneWords(p);' in chain
    assert '$("#chain-foot").textContent = foot;' in chain
    assert "Read the sentence under the model chain on that page" in prose(DEPLOY)

    # What the rows and the paragraph under the table say that the page does.
    repair = script_function(script, "repairNotice")
    assert "const lines = t.tested.map((model) => firstAnswerLine(model, t));" in repair
    assert "the notice also has one line for each tested model" in usage
    assert "the notice has one line for each tested model" in readme
    for words in ("asking for ", "waiting its turn"):
        assert words in script_function(script, "firstAnswerLine"), words
    # Why a visitor is the one who finds out is said only where a model was withdrawn.
    assert 'const start = t.reason.code === "gone" ? [foundWords(t), idleWords(p)] : [foundWords(t)];' in repair
    assert "By itself the service only tests its models when one of them does not answer a run" in script
    assert (
        "When a test follows a withdrawn model, the notice also explains why a visitor is the one who finds out: "
        "by itself the service tests its models only when one of them does not answer a run"
    ) in usage


def script_function(script: str, name: str) -> str:
    """One top-level function of a page's script, from its first line to its closing brace."""
    found = re.search(rf"^(?:async )?function {name}\(.*?^\}}$", script, re.DOTALL | re.MULTILINE)
    assert found is not None, f"the page's script has no function {name}"
    return found.group(0)


def test_the_usage_guide_names_every_title_of_a_run_that_is_refused() -> None:
    """A run that the ceiling refuses gets one of a few titles, and the guide names each."""
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    refusal = script.split("function refusalOutcome(")[1].split("\n}\n")[0]
    titles = {
        words for line in refusal.splitlines() if "title:" in line for words in re.findall(r'"([^"]+)"', line)
    }
    assert len(titles) >= 2, sorted(titles)

    guide = document("docs/USAGE.md")
    paragraph = one_line(guide.split("**A run that is refused.**")[1].split("\n\n")[0])
    row = next(line for line in guide.splitlines() if line.startswith("| *The demo is busy* or"))
    for title in sorted(titles):
        assert f"*{title}*" in paragraph, f"the page can title a refused run '{title}', and the paragraph does not name it"
        assert f"*{title}*" in row, f"the page can title a refused run '{title}', and the table of results does not name it"


def test_the_usage_guide_names_the_tiles_and_chips_of_a_result_as_the_page_writes_them() -> None:
    """The table "Reading the result" quotes four things that the page builds in its script."""
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    table = document("docs/USAGE.md").split("\nReading the result:\n\n")[1].split("\n\n")[0]
    assert table.startswith("| On the page | Means |")

    # Two tiles by their labels, and the tile "Fallback" with the value it has after a fallback.
    assert '"Response time"' in script and "| *Response time* |" in table
    assert 'kpi("Answered by"' in script and "| *Answered by* |" in table
    assert 'kpi("Fallback", h("span", { class: "kpi__value" }, "Used")' in script
    assert "| *Fallback: Used* | `fallback_triggered: true`" in table
    # The chip beside a model whose failure the page staged. The page writes it in lower
    # case, and no rule of its stylesheet changes that for a chip.
    assert 'chip("simulated"' in script and "| *simulated* |" in table and "*Simulated*" not in table
    styles = (PAGE_SCRIPT.parent / "app.css").read_text(encoding="utf-8")
    chip_rule = styles.split("\n.chip {")[1].split("}")[0]
    assert "text-transform" not in chip_rule
    # What stands in the row of a model that the chain did not reach.
    assert 'if (run.status === 200) return "Not needed";' in script
    assert "| *Not needed* | The chain stopped before reaching that model |" in table


def cases_and_tests_of_the_playgrounds_script() -> tuple[int, int]:
    """How many cases the harness has, and how many tests tests/test_page_behaviour.py makes of them.

    Read from the source of the two files, so that this works where Node.js is missing too.
    The file has one test for each case, and its other tests come on top.
    """
    tests = PROJECT_ROOT / "tests"
    tree = ast.parse((tests / "test_page_behaviour.py").read_text(encoding="utf-8"))
    cases = [
        len(node.value.elts)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Tuple)
        and [getattr(target, "id", None) for target in node.targets] == ["CASES"]
    ]
    assert len(cases) == 1, "tests/test_page_behaviour.py no longer lists its cases in CASES"
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]
    per_case = [
        node
        for node in functions
        if any(ast.unparse(mark) == "pytest.mark.parametrize('case', CASES)" for mark in node.decorator_list)
    ]
    assert len(per_case) == 1 and len(functions) >= 2, "one test of the file runs once for each case"
    # The harness has a case for each name, neither more nor fewer.
    in_harness = re.findall(r'^await check\("', (tests / "page_harness.mjs").read_text(encoding="utf-8"), re.MULTILINE)
    assert len(in_harness) == cases[0], "tests/page_harness.mjs and CASES no longer hold the same number of cases"
    return cases[0], cases[0] + len(functions) - 1


def test_the_documents_say_which_tests_run_the_script_of_a_page() -> None:
    """Four documents say how each page's script is tested. The test files have to bear them out.

    No test opens a browser. Two test files start Node.js: one runs the playground's script
    in every one of its tests, and two tests of the other run the project page's script.
    Two more files read the playground's script as text. A test file that is added, or one
    that stops running a script, would make these sentences untrue, and no other test would
    tell.
    """
    tests = PROJECT_ROOT / "tests"
    with_node = sorted(
        path.name
        for path in tests.glob("test_*.py")
        if path.name != Path(__file__).name and 'which("node")' in path.read_text(encoding="utf-8")
    )
    assert with_node == ["test_front_door.py", "test_page_behaviour.py"], (
        f"the test files that start Node.js are {with_node}. README.md ('No test runs either page in a "
        "browser ...'), docs/USAGE.md (section 12), docs/DEPLOY.md and the file table of "
        "docs/PYTHON_LEARNING.md name two"
    )
    cases, behaviour = cases_and_tests_of_the_playgrounds_script()
    front_door = (tests / "test_front_door.py").read_text(encoding="utf-8")
    replayed = len(re.findall(r"^    seen = replay\(\)$", front_door, re.MULTILINE))
    assert replayed == 2, "two tests of tests/test_front_door.py run the project page's script"
    without_node = behaviour + replayed

    # What makes a test of the first file a skipped one where Node.js is missing: the one
    # fixture that every test of it asks for.
    source = (tests / "test_page_behaviour.py").read_text(encoding="utf-8")
    assert 'NODE = shutil.which("node")' in source
    assert 'if NODE is None:\n        pytest.skip("Node is not installed here' in source
    asking = re.findall(r"^async def (test_\w+)\((?:case: str, )?harness: dict\[str, Any\]\) -> None:$", source, re.MULTILINE)
    assert len(asking) == behaviour - cases + 1, "every test of the file asks for the fixture that needs Node.js"
    assert "subprocess.run(" in source and "[NODE, str(HARNESS), str(SCRIPT_PATH), str(path)]" in source
    # The two files that read the playground's script as text, as the README says of them.
    for name in ("test_playground.py", "test_models_page.py"):
        reading = (tests / name).read_text(encoding="utf-8")
        assert 'SCRIPT = (STATIC_DIR / "assets" / "app.js").read_text(encoding="utf-8")' in reading, name
        assert name not in with_node

    readme, usage = prose("README.md"), prose("docs/USAGE.md")
    # What the README and the usage guide say that the first of those two holds by reading.
    reading = (tests / "test_playground.py").read_text(encoding="utf-8")
    for held_there in (
        "test_page_waits_for_a_sleeping_service_on_the_agreed_clock",
        "test_no_request_of_the_page_can_wait_for_ever",
        "test_page_asks_the_service_only_while_something_is_happening",
        "test_page_reads_a_refusal_by_the_ceiling_where_the_service_puts_it",
    ):
        assert f"def {held_there}(" in reading, f"tests/test_playground.py no longer has {held_there}"
    assert (
        "And it reads the page's script as text, and holds it to the service: how long the page waits for a "
        "service that does not answer, that every request has a time limit, that it asks for news only while "
        "something is happening, and that it reads a refusal by the ceiling where the service puts it."
    ) in readme
    assert (
        "and, by reading the page's script as text, what it does when the service does not answer or refuses a run"
    ) in usage
    # And what the README says that this file holds: the three tests under "what the page does, and when".
    assert (
        "What they say the page does is held to the page's script: how long it waits, how often it asks for "
        "news, and when it sends a run again."
    ) in readme
    # And the tests under "what each part of the page shows", one for each thing the README names.
    assert (
        "So is what they say each part of the page means: a notice, a tile or a chip of a result, the test on show, "
        "the button that starts a test."
    ) in readme
    for held_here in (
        "test_the_usage_guide_says_of_each_notice_what_the_page_shows_it_for",
        "test_the_documents_say_what_each_part_of_a_result_means",
        "test_the_documents_say_what_the_section_on_model_upkeep_shows",
        "test_the_documents_say_when_the_button_is_there_and_when_it_cannot_be_used",
    ):
        assert callable(globals().get(held_here)), f"tests/test_docs.py no longer has {held_here}"
    assert "No test runs either page in a browser." in readme
    assert "Both scripts are run with Node.js instead, on a made-up page, with a clock that is moved by hand." in readme
    # "Stand-in" is the page's word for a model that answers before a test has decided about
    # it, and the README defines it so. It is not also the README's word for a made-up page.
    assert "A stand-in is a model that is put to use before a test has decided about it." in readme
    assert not re.search(r"stand-in (?:for the )?page", document("README.md"))
    assert "`tests/test_page_behaviour.py` does that for the playground's script" in readme
    assert f"and {IN_WORDS[replayed]} tests of `tests/test_front_door.py` do it for the project page's script" in readme
    assert "So the tests hold what each script does, and not what a browser draws from it." in readme
    assert (
        f"Where Node.js is not installed, those {IN_WORDS[replayed]} tests are skipped, and so are the {behaviour} tests "
        "of `tests/test_page_behaviour.py`."
    ) in readme
    # The README's own paragraph on the file, under "Testing strategy".
    assert (
        "`tests/test_page_behaviour.py` runs the playground's script, where `tests/test_playground.py` and "
        "`tests/test_models_page.py` read it."
    ) in readme
    assert "Then it starts `tests/page_harness.mjs` once, with Node.js." in readme
    assert f"plays those answers back in {cases} cases" in readme
    assert f"Where Node.js is not installed, the {behaviour} tests of this file are skipped." in readme

    # The usage guide: two rows of the table in section 12, and the paragraph under it.
    row = next(line for line in document("docs/USAGE.md").splitlines() if line.startswith("| `tests/test_page_behaviour.py` |"))
    assert "then starts `tests/page_harness.mjs` with Node.js" in row
    assert f"Its {behaviour} tests are skipped on a machine that has no Node.js" in row
    harness_row = next(line for line in document("docs/USAGE.md").splitlines() if line.startswith("| `tests/page_harness.mjs` |"))
    assert "Not a test file: the Node.js script that `tests/test_page_behaviour.py` starts." in harness_row
    assert f"reports one verdict for each of its {cases} cases" in harness_row

    # What the two rows, and the row of the Python guide, say that the two files do. The
    # harness says of itself what it puts in the place of a browser, and its code bears it out:
    # a page that keeps the words drawn on it, a clock that a case moves, the service's answers.
    harness = (tests / "page_harness.mjs").read_text(encoding="utf-8")
    of_itself = one_line(harness.split("import {")[0].replace("//", " "))
    assert "Runs the page's script (app/playground/static/assets/app.js) without a browser" in of_itself
    assert "a page that swallows what is drawn on it and remembers the words" in of_itself
    assert "a clock and timers that only move when a case moves them" in of_itself
    assert "The answers themselves are the real service's" in of_itself
    assert 'if (key === "textContent") words.push(String(value));' in harness
    assert "globalThis.setTimeout = clock.setTimeout;" in harness and "Date.now = () => 1_800_000_000_000 + clock.now();" in harness
    assert "globalThis.fetch = (url, options = {}) => {" in harness
    assert "console.log(JSON.stringify({ cases }, null, 1));" in harness
    # No browser, and nothing to install: the harness loads Node's own modules and the page's script.
    assert set(re.findall(r'^import \{[^}]+\} from "([^"]+)";$', harness, re.MULTILINE)) == {
        "node:fs",
        "node:os",
        "node:path",
        "node:url",
    }
    for tool in ("puppeteer", "playwright", "jsdom", "selenium", "chromium"):
        assert tool not in harness.lower() and tool not in source.lower(), tool
    # The answers are asked of the service by the Python file, through the routes that a page uses.
    for asked in (
        '(await http.get("/playground/config")).json()',
        '(await http.get("/playground/models")).json()',
        '(await http.get("/health")).json()',
        'await http.post("/playground/extract", json={"text": scenario["text"]})',
        "model_chooser.picture()",
    ):
        assert asked in source, f"tests/test_page_behaviour.py no longer asks the service: {asked}"
    # And the cases are about what the page does: what it asks and when it stops, what it
    # sends a second time, and whether it has words for what it is shown.
    names = ast.literal_eval(source.split("\nCASES = ")[1].split("\n)\n")[0] + "\n)")
    assert len(names) == cases
    for about in ("asks nothing", "is sent once", "is sent again once", "is put into words"):
        assert any(about in name for name in names), f"no case of the harness is about: {about}"
    assert row.split("|")[2].strip() == (
        "The page's script, run and not only read: which requests the page sends and when it stops asking, how often "
        "it sends a run, and that it has words for every state of the chain's upkeep. It asks the service itself for "
        f"the answers that the page gets, then starts `tests/page_harness.mjs` with Node.js. Its {behaviour} tests "
        "are skipped on a machine that has no Node.js"
    )
    assert harness_row.split("|")[2].strip() == (
        "Not a test file: the Node.js script that `tests/test_page_behaviour.py` starts. It runs the page's script "
        "without a browser: on a made-up page that only keeps the words that are drawn on it, and with a clock that "
        f"it moves by hand. It reports one verdict for each of its {cases} cases"
    )
    assert (
        f"{IN_WORDS[replayed].capitalize()} more tests of `tests/test_front_door.py` run the project page's script "
        "with Node.js."
    ) in usage
    assert f"So do the {behaviour} tests of `tests/test_page_behaviour.py`, for the playground's script." in usage
    assert f"On a machine that has no Node.js these {without_node} tests are skipped as well." in usage
    # Node.js is no requirement of the service: nothing that the host installs asks for it,
    # and no module of the service starts it.
    assert "node" not in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for module in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        assert not re.search(r"""["']node["']|node\.js""", module.read_text(encoding="utf-8"), re.IGNORECASE), module.name
    assert "Node.js is a program that runs JavaScript outside a browser. The service itself does not need it." in usage

    # The deployment guide: GitHub's machines have Node.js, so the run of "Tests" there skips
    # one test only. A PC without Node.js skips the tests that run a script as well.
    guide = prose(DEPLOY)
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "runs-on: ubuntu-latest" in workflow and "run: python -m pytest -q\n" in workflow
    # Should a machine ever have none, the run would stay green with those tests skipped.
    # So a step of the workflow asks for Node.js first, and the run fails where it is missing.
    assert workflow.index("run: node --version\n") < workflow.index("run: python -m pytest -q\n")
    assert "and `1 skipped`" in guide
    assert "Some tests run a page's script with Node.js, a program that runs JavaScript outside a browser." in guide
    assert "GitHub's machines have Node.js, so those tests run there and are not skipped." in guide
    assert "which GitHub's machines have" in (PROJECT_ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert f"A PC without Node.js skips the {without_node} tests that run a page's script." in guide

    # The Python guide has one row for the two files, and says the same of Node.js.
    table = document("docs/PYTHON_LEARNING.md").split("## 2. Know what each file does")[1].split("## 3. Values")[0]
    (python_row,) = [line for line in table.splitlines() if line.startswith("| [tests/test_page_behaviour.py]")]
    assert "[tests/page_harness.mjs](../tests/page_harness.mjs)" in python_row.split("|")[1]
    assert "On a machine without Node.js these tests are skipped." in python_row
    assert "Then it starts the second file with Node.js, a program that runs JavaScript outside a browser." in python_row
    assert "Check what the playground page's JavaScript does, by running it." in python_row
    assert python_row.split("|")[2].strip() == (
        "Check what the playground page's JavaScript does, by running it. The Python file asks the service for the "
        "answers that a page gets. Then it starts the second file with Node.js, a program that runs JavaScript "
        "outside a browser. That second file is JavaScript too: it runs the page's script on a made-up page that only "
        "keeps the words that are drawn on it, with a clock that it moves by hand, and prints one verdict for each "
        "case. On a machine without Node.js these tests are skipped."
    )
    for taught in ("`shutil.which`", "`subprocess.run`", "`pytest.skip` inside a fixture", "`@pytest.mark.parametrize`"):
        assert taught in python_row.split("|")[3], taught
    assert '@pytest.mark.parametrize("case", CASES)' in source and "global _ran" in source

    # A script that the tests run, kept beside them, is a file of the suite like any other:
    # the README's layout has to name it.
    layout = document("README.md").split("### Repository layout")[1].split("```")[1]
    scripts = sorted(path.name for path in tests.iterdir() if path.suffix in (".js", ".mjs"))
    assert scripts == ["page_harness.mjs"]
    missing = [name for name in scripts if name not in layout]
    assert not missing, f"README.md, 'Repository layout', does not name: {missing}"
    (behaviour_line,) = [line for line in layout.splitlines() if "test_page_behaviour.py #" in line]
    assert "(skipped without Node.js)" in behaviour_line
    assert behaviour_line.split("#", 1)[1].strip() == (
        "the playground's script, run with Node.js on the service's own answers (skipped without Node.js)"
    )
    (harness_line,) = [line for line in layout.splitlines() if "page_harness.mjs " in line]
    assert harness_line.split("#", 1)[1].strip() == (
        "the Node.js script that test_page_behaviour.py starts: a made-up page, and a clock moved by hand"
    )
    # The README's paragraph on the file says the same in full sentences. The models are
    # scripted, and the states are the ones that the page has sentences of its own for.
    assert "ScriptedEngine" in source and "\nSTATES = (\n" in source
    assert "the ones with sentences of their own on the page" in one_line(source.replace("#:", " "))
    for said in (
        "It first asks the service itself, with scripted models, for the answers a page can get: a run, a refusal, "
        "and what the service says about its chain in every state that the page has sentences for.",
        "The harness loads the script on a made-up page that only keeps the words that are drawn on it, with a "
        f"clock that it moves by hand, and plays those answers back in {cases} cases.",
        "The cases hold what the script does: which requests it sends and when it stops asking, how often it sends "
        "a run, and that it has words for every state it is shown.",
    ):
        assert said in readme, f"README.md no longer says: {said}"


# ---------------------------------------------------------------------------
# Documents - what the page does, and when
#
# The usage guide and the README say how long the page waits, how often it asks the
# service for news, when it sends a run a second time and where a visitor finds each
# thing. Every number in those sentences is a constant of the page's script, and every
# rule is a line of it. The script is held to its rules by tests/test_playground.py,
# which reads it, and by tests/test_page_behaviour.py, which runs it. These tests hold
# the documents to the script.
# ---------------------------------------------------------------------------


def page_figures(script: str) -> dict[str, int]:
    """The waits that the page's script names, in milliseconds: ``const NAME_MS = 3000;``."""
    figures = {name: int(value) for name, value in re.findall(r"^const (\w+_MS) = (\d+);", script, re.MULTILINE)}
    assert len(figures) >= 8, sorted(figures)  # if the pattern stops matching, nothing is checked
    return figures


def test_the_documents_give_the_pages_waits_as_the_script_has_them() -> None:
    """No request of the page waits for ever, and a tab that is left open goes quiet."""
    from app import model_chooser, model_rules
    from app.config import Settings

    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    figures = page_figures(script)
    timeout = Settings.model_fields["timeout_seconds"].default
    spare = figures["SILENCE_SPARE_MS"] // 1000
    give_up = figures["GIVE_UP_AFTER_MS"] // 60000
    busy, idle = figures["LOOK_BUSY_MS"] // 1000, figures["LOOK_IDLE_MS"] // 1000
    lasting = figures["LOOK_FOR_MS"] // 60000
    assert figures["SILENCE_SPARE_MS"] % 1000 == 0 and figures["LOOK_FOR_MS"] % 60000 == 0
    usage = prose("docs/USAGE.md")
    readme = prose("README.md")

    # ---- While the page waits for the service --------------------------------------------
    # One question at a steady pace, and an end that counts from when the wait began. The
    # usage guide says so in section 3 and again in the table of section 15.
    every = figures["HEALTH_EVERY_MS"] // 1000
    waiting = script_function(script, "waitForService")
    assert "const every = setInterval(ask, HEALTH_EVERY_MS);" in waiting
    assert "const left = since + GIVE_UP_AFTER_MS - performance.now();" in waiting
    asks = f"every {every} seconds, for {give_up} minutes at most"
    assert usage.count(asks) == 2, f"docs/USAGE.md says '{asks}' {usage.count(asks)} times, and 2 are held"
    assert readme.count(asks) == 1, f"README.md says '{asks}' {readme.count(asks)} times, and 1 is held"
    assert f"After {give_up} minutes without an answer the top bar says *Not answering*" in usage
    assert "A page that is loading, and a run, ask " + asks + ", and then offer *Try again*" in usage
    # The page cannot know why no answer came, so it says "probably". One cause it can see
    # is a device that is offline, and then it says that instead.
    assert 'navigator.onLine === false ? "and this device seems to be offline." : `so it has probably gone to sleep.' in script
    for teller in ("sayWaking", "outcomeOf"):
        assert "${probably()}" in script_function(script, teller), teller
    assert (
        "One cause the page can see: when the browser reports that your device is offline, the page says that the "
        "device seems to be offline, and not that the demo has gone to sleep."
    ) in usage

    # ---- Every request has a limit --------------------------------------------------
    # A run may be silent for as long as one model takes, and some seconds more. Every
    # event of its stream starts that wait again. The other three requests have the limit
    # of every other wait of the page.
    assert "const silenceLimit = () => state.config.limits.timeout_seconds * 1000 + SILENCE_SPARE_MS;" in script
    sending = script_function(script, "send")
    assert "const wait = patience(silenceLimit(), signal);" in sending and "wait.renew();" in sending
    for other in ("readJSON", "runSuite", "startModelsTest"):
        assert "patience(GIVE_UP_AFTER_MS" in script_function(script, other), other
    assert script.count("fetch(") == 4, "the page sends a request that the documents do not speak of"
    missing = missing_from(
        "docs/USAGE.md",
        [
            "No request of the page waits for ever.",
            f"when nothing has come from the service for {timeout + spare:g} seconds",
            f"That is the time one model may take (`TIMEOUT_SECONDS`, {timeout:g} seconds by default) and {spare} seconds more",
            "every step of the run that arrives starts the wait again",
            f"Every other request of the page has a limit of {give_up} minutes.",
        ],
    )
    assert not missing, f"docs/USAGE.md no longer says, or says with another number: {missing}"
    missing = missing_from(
        "README.md",
        [
            "A host may hold a request and never answer it.",
            f"for one model's time limit and {spare} seconds more, which is {timeout + spare:g} seconds with the "
            "default `TIMEOUT_SECONDS`",
            "Every step of the run that arrives starts that wait again.",
            f"Every other request of the page has a limit of {give_up} minutes.",
        ],
    )
    assert not missing, f"README.md no longer says, or says with another number: {missing}"
    assert "so every request of the page has a time limit" in prose("docs/LEARNING.md")
    # The deployment guide gives the same figure beside the longest that a run can take:
    # every model of a full chain running into its time limit.
    missing = missing_from(
        DEPLOY,
        [
            f"it stops waiting for a run after {timeout + spare:g} seconds in which nothing arrived",
            f"The slowest run takes about {timeout * model_rules.PLACES:g} seconds",
        ],
    )
    assert not missing, f"{DEPLOY} no longer says, or says with another number: {missing}"

    # ---- What becomes of a run that heard nothing --------------------------------------
    # Only a run that never reached the service waits for it and is sent again, once. One
    # that the service had taken ends without a result, whether the connection broke off
    # or the page stopped waiting.
    running = script_function(script, "execute")
    assert "if (!answered && !run.reached && !signal.aborted) {" in running
    assert running.count("await send(run, signal)") == 2 and "run.resent = true;" in running
    assert "else if (!answered && !run.problem) run.problem = run.quiet ? WENT_QUIET(silenceLimit()) : CUT_OFF;" in running
    assert 'if (!answered && signal.aborted) run.problem = "Stopped before a result arrived.";' in running
    assert 'if (run.problem) return { tone: "crit", code: "", title: "No result", note: run.problem };' in script
    for said in (
        "and is then sent again by itself, once",
        "If the service never took the run, the run waits for the service as described above.",
        "If the service had started on it, the run ends as *No result* and the page does not send it again, "
        "because it may already have called a model.",
        "The same holds when the connection breaks off in the middle of a run.",
        "The run ended without a result: the service gave no answer, the connection broke off, nothing more came "
        "from the service for too long, or you stopped *Run all*. The sentence under the title says which.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"
    assert "A run that got no answer waits in the same way and is then sent again, once." in readme
    assert (
        "That is not done for a run that the service had already started on, because such a run may have called a model."
    ) in readme
    # The visitor's own Stop belongs to "Run all": nothing else ends a run by hand.
    assert script.count("state.abort?.abort()") == 1 and "state.abort?.abort();" in script_function(script, "runAll")
    # A run without a result is offered again under its outcome, and only the visitor sends it:
    # the button starts a run of the visitor's own, whoever sent the run it stands under.
    offered = script_function(script, "outcomeBlock")
    assert "const again = run.problem || (run.refusal && run.refusal.until <= performance.now());" in offered
    assert 'onclick: () => execute({ ...run, again: false }) }, icon("reset"), "Try again")' in offered
    assert "A run that ended as *No result* has a *Try again* button, so sending it again is your decision." in usage

    # ---- News about the chain, and a tab that is left open -------------------------------
    looking = script_function(script, "watchModels")
    later = script_function(script, "lookLater")
    assert 'await readJSON("/playground/models")' in looking
    assert "if (document.hidden || waitingForService) return;" in looking
    assert "const hot = !upkeep.lost && (upkeep.picture.busy ||" in later
    arranged = "if (hot || now < upkeep.activeUntil) upkeep.timer = setTimeout(watchModels, hot ? LOOK_BUSY_MS : LOOK_IDLE_MS);"
    assert arranged in later
    assert "upkeep.activeUntil = Date.now() + LOOK_FOR_MS;" in script_function(script, "touchUpkeep")
    # What counts as the visitor doing something: a press with a mouse or a finger, a key, and
    # coming back to the tab. Each of them starts the 10 minutes again.
    wiring = script_function(script, "wireUpkeep")
    assert 'for (const sign of ["pointerdown", "keydown"]) {' in wiring
    assert "else upkeep.activeUntil = Date.now() + LOOK_FOR_MS;" in wiring
    assert 'document.addEventListener("visibilitychange", () => {\n    if (!document.hidden) touchUpkeep();' in wiring
    assert "A click, a tap, a key press and coming back to the tab each count as that." in usage
    # "Busy" is the service's own word for looking into a failed run, getting ready to test, or testing.
    assert model_chooser.DOING == ("looking", "starting", "testing")
    assert '"busy": doing is not None,' in inspect.getsource(model_chooser.picture)
    missing = missing_from(
        "docs/USAGE.md",
        [
            "the page also asks the service for news about the chain, with `GET /playground/models`",
            f"It asks every {busy} seconds while the service is looking into a failed run or testing models.",
            f"Otherwise it asks every {idle} seconds, and only for {lasting} minutes after you last did something on the page.",
            "A tab that is hidden asks for no news at all.",
            "So a tab that is left open does not keep a sleeping host awake.",
        ],
    )
    assert not missing, f"docs/USAGE.md no longer says, or says with another number: {missing}"
    missing = missing_from(
        "README.md",
        [
            "so the page asks for news about the chain only while something is happening",
            f"While the service is looking into a failed run or testing models, the page asks every {busy} seconds.",
            f"For {lasting} minutes after the visitor last did something, it asks every {idle} seconds.",
            "Otherwise it asks for no news, and a tab that is hidden asks for none either.",
        ],
    )
    assert not missing, f"README.md no longer says, or says with another number: {missing}"
    # A free service on the host is stopped after a quarter of an hour without a request: the
    # looks of a tab that nobody touches have to end sooner than that.
    assert lasting < 15 and "after 15 minutes without a request" in usage

    # ---- A question that gets no answer --------------------------------------------------
    # The page then shows nothing as going on, says so, and starts no test. It keeps to the
    # slow looks, and the first answer puts it right.
    shown = script_function(script, "shownPicture")
    assert "return p && upkeep.lost ? { ...p, busy: false, doing: null, test: null } : p;" in shown
    assert "What follows is what the page last saw, at ${clockWords(p.now)}, and may be out of date." in script
    unanswered = looking[looking.index("if (!isPicture(picture)) {") : looking.index("const before = upkeep.picture;")]
    assert "upkeep.lost = true;" in unanswered and "lookLater();" in unanswered
    assert "upkeep.lost = false;" in looking[looking.index("const before = upkeep.picture;") :]
    # The top bar says which of two things holds: another look is arranged, or none is.
    assert 'if (upkeep.picture && !waitingForService) showStatus(upkeep.timer == null ? "offline" : "waiting");' in unanswered
    assert unanswered.index("lookLater();") < unanswered.index("showStatus(upkeep.timer == null")
    status = dict(re.findall(r'^  (\w+): "([^"]+)",$', script.split("const STATUS_WORDS = {\n")[1].split("};")[0], re.MULTILINE))
    assert (
        f"The top bar says *{status['waiting']}* for as long as the page goes on asking, and *{status['offline']}* "
        "once it has stopped."
    ) in usage
    assert "showStatus(); // an answer of the service's own" in looking[looking.index("const before = upkeep.picture;") :]
    pressing = script_function(script, "startModelsTest")
    assert "if (upkeep.starting || upkeep.lost || !upkeep.picture?.button.ready) return;" in pressing
    assert "The demo did not answer when this page last asked, so no test can be started from here just now." in script
    for said in (
        "When such a question gets no answer, the page no longer shows a test as running, and the section "
        "*Model upkeep* says that what follows is what the page last saw and may be out of date.",
        "The first answer puts the page right again by itself.",
        "The button is also faded when the page got no answer the last time it asked the service.",
        f"A page that is already open goes on asking by itself for {lasting} minutes after you last did something on "
        "it, and the first answer puts it right.",
        "After that, a click on the page makes it ask again.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"
    # A page that has stopped looking starts again with the visitor's next press or key.
    assert "if (upkeep.timer == null && !upkeep.looking) touchUpkeep();" in wiring
    assert "if (hotMs || upkeep.timer == null) watchModels();" in script_function(script, "touchUpkeep")
    assert (
        "When such a question gets no answer, the page no longer shows a test as running, and it says that what it "
        "shows may be out of date. The first answer puts the page right again, without a reload."
    ) in readme


def test_the_documents_say_when_the_page_sends_a_run_again_after_a_repair() -> None:
    """Once, for a run of the visitor's own, when the chain has a new model, and only for a visitor who stayed."""
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    waits = page_figures(script)["RERUN_FOR_MS"] // 60000

    # Only a single run of the visitor's own is remembered: one that no model answered,
    # that simulated no failure, that the page did not send itself, and no run of "Run all".
    running = script_function(script, "execute")
    assert "if (answered && failedForReal(run) && !again && !state.batch) upkeep.rerun = { run, at: Date.now() };" in running
    assert script.count("upkeep.rerun = {") == 1
    real = "const failedForReal = (run) => !run.simulate.some(Boolean) && run.status === 503 && Array.isArray(run.body?.detail);"
    assert real in script
    # It is sent when the chain has a model that the run did not meet, and the page does
    # not wait for that for ever.
    again = script_function(script, "rerunIfRepaired")
    assert "if (upkeep.picture.chain.every((model) => met.includes(model))) {" in again
    # The page goes on waiting only while the service is busy, which is the service's word
    # for looking into a failed run or testing, and for RERUN_FOR_MS at most. A service that
    # is not busy has nothing more to bring, so the page lets the run go after a few looks.
    assert "if (age > RERUN_FOR_MS || (!upkeep.picture.busy && age > 2 * LOOK_BUSY_MS + 1000)) upkeep.rerun = null;" in again
    # Only for a visitor who stayed with the run, and once: the run that the page sends is
    # marked, and a marked run is not remembered.
    stayed = (
        "const stayed = state.runs.get(state.view) === run && state.draft.text === run.text "
        "&& !state.draft.simulate.some(Boolean);"
    )
    assert stayed in again and again.index("if (!stayed) return;") < again.index("execute(")
    assert again.count("execute(") == 1 and "again: true" in again
    # What the results say: the run that was sent again, the run that waits, and the run
    # of a visitor who has moved on.
    assert "so this page sent the run again." in script
    assert "If it does, this page sends the run again by itself." in script
    assert "The service has changed its model chain since this run. Run it again to use the new chain." in script
    notes = script_function(script, "upkeepNotes")
    assert "if (upkeep.rerun?.run === run && !upkeep.lost) {" in notes
    assert "} else if (failedForReal(run) && run.chainKey !== chainKey(p.chain)) {" in notes

    usage = prose("docs/USAGE.md")
    for said in (
        "The page can send a failed run again by itself, once.",
        "It does so for a run of your own that no model answered, when the service has put a model into its chain "
        "that was not there when the run failed.",
        "The page waits for such a model while the service is looking into the failure or testing models, and for "
        f"{waits} minutes at most.",
        "Meanwhile the result of the failed run says that this can happen.",
        "The run is sent again only while you have stayed with it: its result is still on view, the document in "
        "the box is the one that was sent, and no simulated failure is chosen.",
        "The new result says that the run was sent again.",
        "If you have moved on, nothing is sent, and the result of the failed run says that the chain has changed since.",
        "A scenario that *Run all* started is not sent again in this way.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"
    readme = prose("README.md")
    for said in (
        "The page sends a failed run again by itself, once, when the chain has gained a model that it did not have "
        "when the run failed.",
        "It does that only for a single run of the visitor's own, and only for a visitor who stayed with it: its "
        "result is still on view, the document is unchanged and no failure is simulated.",
        "The new result says that the run was sent again.",
    ):
        assert said in readme, f"README.md no longer says: {said}"
    # The sentence both documents had before is true of no page any more: a changed chain
    # alone sends nothing.
    for name, text in (("docs/USAGE.md", usage), ("README.md", readme)):
        assert "When the chain has changed, the page sends" not in text, name

    # A stand-in answers real runs before its test is over, and can fail that test afterwards.
    # The result that it gave then says so: the model answered this run, it is no longer in
    # the chain, and the test on show has judged it "failed".
    assert "const by = run.status === 200 ? run.body.model_used : null;" in notes
    assert (
        "const judged = by && !p.chain.includes(by) ? (p.test ?? p.last)?.models.find((row) => row.model === by) : null;"
    ) in notes
    told = "which answered this run, has failed the service's test since and no longer answers. Its answer here may be wrong."
    assert 'if (judged?.verdict === "failed") {' in notes and told in notes
    # The run on view is drawn again when what the upkeep adds to it changes, so the sentence
    # appears without a click.
    assert 'if (run && changed("outcome", upkeepNotes(run))) renderOutcome();' in script_function(script, "renderUpkeep")
    for said in (
        "A stand-in can answer a run of yours and fail the test afterwards.",
        "The result of that run then says that the model has failed the test and no longer answers, and that its "
        "answer may be wrong.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"
    assert (
        "When a stand-in has answered a run and then fails its test, the result of that run says so: the model no "
        "longer answers, and its answer may be wrong."
    ) in readme


def test_the_documents_say_where_a_visitor_finds_what_the_page_tells() -> None:
    """A refusal that ends, a result that is brought into view, a list that counts down, and the keyboard."""
    from app.playground.catalogue import scenarios_for

    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    page = PAGE_FILES[0].read_text(encoding="utf-8")
    styles = (PAGE_SCRIPT.parent / "app.css").read_text(encoding="utf-8")
    usage, readme = prose("docs/USAGE.md"), prose("README.md")

    # ---- A refusal has a countdown, and it ends ------------------------------------------
    refusal = script_function(script, "refusalOutcome")
    assert refusal.count("countdown(until)") == 2, "each of the two refusals shows its wait"
    assert "const SHORT_WAIT_SECONDS = 60;" in script and "today: seconds > SHORT_WAIT_SECONDS" in script
    assert "const again = run.problem || (run.refusal && run.refusal.until <= performance.now());" in script
    assert "Midnight UTC has passed since then, so you can try again now." in refusal
    assert 'waiting ? ["in ", countdown(until), "."] : "now.",' in refusal
    for said in (
        "The result then shows status 503 under one of two titles, each with a countdown.",
        "Under *The demo is busy* the wait is a minute at most.",
        "Under *Today's share of live runs is used up* live runs start again at midnight UTC.",
        "When a countdown ends, the result says that you can try again now, and a *Try again* button appears.",
        "The second title then reads *The day's share of live runs was used up*, because midnight UTC has passed.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"
    assert "When the wait that the service asked for is over, the result says so and offers the same run again." in readme
    # Only the title of a day that is used up changes when its wait is over. The row of the
    # table "Reading the result" says so in its own words.
    assert refusal.index("if (today) {") < refusal.index('"The day\'s share of live runs was used up"')
    assert refusal.index('"The day\'s share of live runs was used up"') < refusal.index('title: "The demo is busy",')
    row = next(line for line in document("docs/USAGE.md").splitlines() if line.startswith("| *The demo is busy* or"))
    assert row.split("|")[2].strip() == (
        "The service refused the run before any model was called: see *A run that is refused* below. Once midnight "
        "UTC has passed, the second title reads *The day's share of live runs was used up*"
    )

    # ---- "Run all": what it costs, how it is stopped, and a run that is refused or unanswered ---
    scenarios = scenarios_for(len(DEFAULT_CHAIN))
    calls = sum(scenario["live_calls"] for scenario in scenarios)
    rail = script_function(script, "renderRailHead")
    assert "const calls = scenarios.reduce((sum, scenario) => sum + scenario.live_calls, 0);" in rail
    assert (
        f"With the default two-model chain there are {IN_MORE_WORDS[len(scenarios)]} scenarios, and a full run makes "
        f"about {calls} model calls"
    ) in usage
    assert f"for about {calls} model calls with the default chain" in readme
    # One button starts the scenarios and stops them.
    assert 'button.textContent = state.batch ? "Stop" : "Run all";' in rail
    batch = script_function(script, "runAll")
    assert batch.index("if (state.batch) {") < batch.index("state.batch.stop = true;") < batch.index("return;")
    assert "**Press *Run all*** to execute every scenario in turn. The same button stops them again." in usage
    assert 'const isScored = (run) => run?.phase === "done" && !run.problem && !run.refusal;' in script
    assert "const answered = state.history.filter(isScored);" in rail
    assert "const rest = scenarios.length - finished.length;" in rail and "` · ${rest} not run`" in rail
    assert 'meta.replaceChildren("Waiting ", countdown(waitUntil), ` to run ${place} again`);' in rail
    assert (
        "A run that the service refuses, or that gets no answer, is not scored, and the list does not count its "
        "scenario as failed"
    ) in usage
    assert "twice at most for one scenario, and the list counts that wait down" in usage
    # After a refusal that a minute cures, it waits for as long as the service said and then
    # sends the same scenario again. A day that is used up is not waited for.
    assert "for (let waits = 0; waits < BATCH_WAITS && run.refusal && !run.refusal.today && !state.batch.stop; waits += 1) {" in batch
    assert "setTimeout(resolve, run.refusal.until - performance.now());" in batch
    assert "if (!state.batch.stop) run = await start();" in batch
    assert "After a refusal *Run all* waits until the service has room again, twice at most for one scenario" in usage
    # The README says the same, and how long such a wait can be: the longest that the service
    # asks for when waiting helps. It once said "waits out a full minute", which reads as a
    # whole minute every time.
    assert "today: seconds > SHORT_WAIT_SECONDS" in script and "const SHORT_WAIT_SECONDS = 60;" in script
    times = {1: "once", 2: "twice", 3: "three times"}[int(re.search(r"const BATCH_WAITS = (\d+);", script).group(1))]
    for said in (
        "After a refusal *Run all* waits until the service has room again, which is a minute at most.",
        f"It does that {times} at most for one scenario, and it stops when the part of the day that is open to "
        "visitors is used up.",
    ):
        assert said in readme, f"README.md no longer says: {said}"
    assert "waits out a full minute" not in readme
    # Why "Run all" stopped early stands in the list, in a sentence of its own.
    assert "state.halt = `Run all stopped: ${why}.`;" in batch
    assert 'if (state.halt) meta.prepend(h("strong", null, state.halt), " ");' in rail
    assert "It stops with a sentence when it cannot go on." in usage
    # A chain that the service changed meanwhile stops it too: the scenarios that are left
    # were written for the chain before. The page says so above the playground, where its
    # other notices stand, and not in the result panel.
    following = script_function(script, "followChain")
    stopped = following[following.index("if (state.batch) {") : following.index("// During a repair")]
    assert "state.batch.stop = true;" in stopped and "showNotice(" in stopped
    assert '"The service changed its model chain while the scenarios were running, so the rest were not run. "' in stopped
    assert 'const notice = $("#notice");' in script_function(script, "showNotice")
    for said in (
        "It also stops when the service has changed its model chain meanwhile, because the scenarios that are left "
        "were written for the chain before.",
        "A notice above the playground then says so.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"

    # ---- The result is brought into view ---------------------------------------------------
    # Not for "Run all", which is watched from the list, and not for a run the page sends itself.
    assert "if (!again && !state.batch) showResult();" in script_function(script, "execute")
    assert '$("#result").scrollIntoView({ block: "start" })' in script_function(script, "showResult")
    # Three columns on a wide screen: the scenarios on the left, the request, the result on
    # the right. From 1260 px down, the result panel is no column of its own any more.
    assert re.search(r"\n\.lab \{\s+display: grid;\s+grid-template-columns: 296px minmax\(0, 1fr\) minmax\(0, 1\.14fr\);", styles)
    assert page.index('<aside class="card rail"') < page.index('<section class="card panel request"') < page.index('id="result"')
    assert re.search(r"@media \(max-width: 1260px\) \{\s+\.lab \{\s+grid-template-columns: 280px minmax\(0, 1fr\);", styles)
    assert "**Pick a scenario** in the left column" in usage
    assert '<section class="card panel result" id="result" aria-label="Result">' in page
    assert "The result panel, in the right column, shows each model in the chain as it is tried" in usage
    assert (
        "On a narrower screen the result panel stands below the request, and the page brings it into view when you "
        "start a run."
    ) in usage
    assert (
        "On a narrow screen the result stands below the request, and a run that the visitor starts brings it into view."
    ) in readme

    # ---- Where the chain comes from: the last line of its card -------------------------------
    card = page.split('<div class="card chain" aria-labelledby="chain-title">')[1].split("</div>\n    </div>")[0]
    assert card.rstrip().endswith('<p class="chain__origin" id="chain-origin" hidden></p>')
    assert '<p class="kicker" id="chain-title">Model chain</p>' in card
    assert usage.count("The last line of the *Model chain* card") == 3 and "The line under the *Model chain* card" not in usage
    assert "One line under the model chain says where the chain comes from" in readme
    assert "or the admin of the demo." in readme and "the admin of the deployment" not in readme

    # ---- A chip on a model that is gone ------------------------------------------------------
    assert (
        'setTip(flag, "Withdrawn", ["OpenRouter no longer offers this model.", '
        '"The service looks for a replacement after the next run that fails on it."]);'
    ) in script
    assert "Nobody has to be contacted: the service looks for a replacement after the next run that fails on that model" in usage

    # ---- The keyboard, and a screen reader ----------------------------------------------------
    row = script_function(script, "upkeepRow")
    assert '{ class: "marks", role: "group", tabindex: "0",' in row, "a row of a test is one stop for the keyboard"
    assert "setTip(marks, name, t.cases.map((item) => `${item.title}: ${markWords(item, row, t)}`));" in row
    marks = script_function(script, "documentMarks")
    assert "tabindex" not in marks, "a mark is no stop of its own"
    assert 'document.addEventListener("focusin"' in script and 'document.addEventListener("pointerover"' in script
    assert "Point at the marks of a row, or move to them with the Tab key, and a tip says in words what each mark shows." in usage
    assert "Each row of a test is one stop for the keyboard, and its tip says in words what the marks show." in readme
    # The button is never switched off: it stays in the keyboard's way, tied to the sentence
    # that says why it cannot be used, and it is drawn faded.
    drawing = script_function(script, "renderUpkeep")
    assert 'button.setAttribute("aria-disabled", String(!p.button.ready || upkeep.starting || upkeep.lost));' in drawing
    assert "button.disabled" not in drawing
    assert 'id="upkeep-start" type="button" aria-describedby="upkeep-note"' in page
    assert re.search(r'\.btn\[aria-disabled="true"\] \{\s+opacity: 0\.45;', styles)
    faded = "| The button *Test the free models now* is faded and a press does nothing, or the button is not there |"
    assert faded in document("docs/USAGE.md")
    assert (
        "The button that starts a test stays where the keyboard can reach it while it cannot be used, and the "
        "sentence beside it says why."
    ) in readme
    # What is read out: a notice, the outcome of a run, the end of a wait.
    assert '<div class="sr-only" id="announcer" aria-live="polite"></div>' in page
    assert "announce(message);" in script_function(script, "showNotice")
    assert "announce(run.problem ?? heard);" in script_function(script, "execute")
    assert 'announce("The wait is over. You can try again now.");' in script_function(script, "tickTimers")
    assert (
        "A notice, the outcome of a run and the end of a wait are also written to a part of the page that a "
        "screen reader reads out."
    ) in readme


# ---------------------------------------------------------------------------
# Documents - what each part of the page shows
#
# One false sentence at a time was planted in a copy of the documents once more, this
# time in what they say about the page: what a tile, a chip, a row or a notice means,
# which test the section on model upkeep shows, what a replay calls, when the button is
# there. Most of those sentences stood while no test read them. Each is now held to the
# line of the page's script, of its HTML or of the service that makes it true.
# ---------------------------------------------------------------------------


def usage_rows(start: str, end: str) -> dict[str, str]:
    """One table of the usage guide, between two marks: each row's first cell, and the cell beside it."""
    section = document("docs/USAGE.md").split(start)[1].split(end)[0]
    rows = {}
    for line in section.splitlines():
        if line.startswith("| ") and not line.startswith("|--"):
            cells = [cell.strip() for cell in line.split("|")[1:-1]]
            rows[cells[0]] = cells[1]
    assert len(rows) >= 3, f"no table between '{start}' and '{end}'"
    return rows


def must_say(name: str, sentences: tuple[str, ...]) -> None:
    """Every one of ``sentences`` has to stand in the document ``name``, word for word."""
    missing = missing_from(name, list(sentences))
    assert not missing, f"{name} no longer says: {missing}"


def test_the_usage_guide_says_of_each_notice_what_the_page_shows_it_for() -> None:
    """A row for every title is not enough: the row also has to say what has happened.

    The notices about the chain are built in two places of the script. ``LOOK_WORDS`` has
    one entry for each thing that a look into a failed run can come to. ``repairNotice()``
    asks its questions about a test in a fixed order, and the first one that holds gives
    the title. The rows of the guide's table are held to both.
    """
    from app import model_chooser, model_testing

    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    rows = usage_rows("**Notices about the chain.**", "**A demo that does not answer.**")
    assert rows.pop("Notice") == "What has happened"

    # ---- What a look came to --------------------------------------------------------------
    table = script.split("const LOOK_WORDS = {\n")[1].split("\n};")[0]
    parts = re.split(r"^  (\w+): ", table, flags=re.MULTILINE)[1:]
    entries = dict(zip(parts[::2], parts[1::2]))
    assert set(entries) == set(model_chooser.LOOK_RESULTS)
    found = {code: re.findall(r'title: "([^"]+)"', body) for code, body in entries.items()}
    assert all(len(titles) <= 1 for titles in found.values())
    title = {code: titles[0] for code, titles in found.items() if titles}
    assert len(title) == 6, sorted(title)

    # A test that could not start. Four of the service's reasons have a notice, and the row
    # gives them in one order: the titles first, then what each of them means. The second
    # entry of each pair is how the service's own log words the same reason.
    could_not_start = {
        "pause": (
            f"a test ran less than {model_chooser.PAUSE_SECONDS // 60} minutes ago",
            "the last test ended less than a quarter of an hour ago",
        ),
        "allowance": ("too few free requests are left today", "too little of today's free allowance is left"),
        "nothing_to_test": ("OpenRouter lists no other free model", "OpenRouter lists no free model besides"),
        "list_unreadable": ("OpenRouter's list could not be read", "OpenRouter's list of free models could not be read"),
    }
    assert set(could_not_start) <= set(model_chooser.REFUSALS)
    for code, (_, in_the_log) in could_not_start.items():
        assert in_the_log in model_chooser._WHY_NO_TEST[code], code
    first_cell = ", ".join(f"*{title[code]}*" for code in could_not_start)
    assert first_cell in rows, f"the usage guide no longer has the row: {first_cell}"
    assert rows[first_cell] == "No test could start. In the same order: " + "; ".join(
        meaning for meaning, _ in could_not_start.values()
    )

    # A chain that was waiting in the published list. The look ends there, and no test starts:
    # a look that goes on to a test is noted as "started", not as "published".
    assert 'p.origin.kind !== "published"' in entries["published"]
    assert "the service found a newer tested chain in this project's published list. It is in use now." in entries["published"]
    look = inspect.getsource(model_chooser._look)
    assert 'found("published")' in look and "found(code, reason, again_at)" in look
    assert rows[f"*{title['published']}*"] == (
        "The service found a newer chain in this project's published list and took it. Nothing was tested"
    )
    # A chain that cannot be called, and that is full.
    assert "although OpenRouter still lists them, and the chain has no free place for another" in entries["no_place"]
    assert 'found("no_place", {"code": "closed", "models": list(chain)})' in look
    assert rows[f"*{title['no_place']}*"] == (
        "No model of the chain could be called although OpenRouter lists them all, and the chain has no free place "
        "for another model"
    )

    # ---- What a repair came to ------------------------------------------------------------
    # The questions of repairNotice(), in their order, and the title that each one gives.
    repair = script_function(script, "repairNotice")
    questions = (
        ('if (t.state === "running") {', None),
        ("if (!t.provisional.length) {", "Finding a replacement"),
        ("const single = t.provisional.length === 1;", "A working model is in place"),
        ('if (t.outcome === "changed") {', "The chain was repaired"),
        ("if (standing.length) {", "A stand-in is answering"),
        ('if (t.outcome !== "nothing_passed" && t.outcome !== "unchanged") {', "The test did not finish"),
        ("if (working.length) {", "No replacement passed the test"),
        ("The service cannot extract invoices right now.", None),
    )
    at, in_order = 0, []
    for question, name in questions:
        assert question in repair[at:], f"repairNotice() no longer asks, in this order: {question}"
        at = repair.index(question, at)
        if name is not None:
            at = repair.index(f'title: "{name}"', at)
            in_order.append(name)
    last = "No free model is answering"
    assert re.findall(r'title: "([^"]+)"', repair) == [*in_order, last]
    assert repair.index(f'title: "{last}"') < repair.index("The service cannot extract invoices right now.")
    assert set(rows) == {first_cell, *(f"*{name}*" for name in (*in_order, last, title["published"], title["no_place"]))}

    # What each of those states rests on, in the script's own words, and what the row says.
    # A repair starts for one of two reasons, and a test ends in one of five ways.
    assert set(model_testing.TEST_REASONS) >= {"gone", "closed"}
    assert set(model_testing.OUTCOMES) == {"changed", "unchanged", "nothing_passed", "key_refused", "interrupted"}
    begun = script_function(script, "foundWords")
    assert 'if (t.reason.code === "gone") {' in begun and "OpenRouter no longer offers ${listWords(t.reason.models)}" in begun
    assert "No model of the chain could be called at ${clockWords(t.started)}, although OpenRouter still lists" in begun
    rests_on = {
        "Finding a replacement": (
            "It is now sending the seven sample documents to ${whom}.",
            "A model of the chain gave no answer, and OpenRouter no longer offers it, or no model of the chain could "
            "be called at all. A test of the other free models is running",
        ),
        "A working model is in place": (
            "The test of all seven documents is still running and has the last word.",
            "At least one of the tested models gave a right first answer to a sample document and answers for the "
            "time being. The page calls such a model a stand-in. The test is still running and has the last word",
        ),
        "The chain was repaired": (
            "The service tested ${others} on the seven sample documents. ${outcomeWords(t, p).words}",
            "The test is over, and it changed the chain",
        ),
        "A stand-in is answering": (
            "until a test decides.${again}",
            "The test is over, and it could not decide about a stand-in: calls went unanswered, or the test did not "
            "get to its end. The stand-in goes on answering until a later test decides",
        ),
        "The test did not finish": (
            "OUTCOME_WORDS[t.outcome].words(t)",
            "The test ended early and changed nothing: OpenRouter refused the service's API key, or the test was cut "
            "short",
        ),
        "No replacement passed the test": (
            "The chain goes on with: ${chainWords(working)}.",
            "The test is over and added nothing. The chain goes on with the models that OpenRouter still offers",
        ),
        last: (
            "The service cannot extract invoices right now.",
            "The test is over and added nothing, and no model of the chain can answer: OpenRouter offers none of them "
            "any more, or none of them could be called. The service cannot extract invoices until a later test succeeds",
        ),
    }
    assert set(rests_on) == {*in_order, last}
    for name, (in_the_script, in_the_guide) in rests_on.items():
        assert in_the_script in repair, f"repairNotice() no longer says, under '{name}': {in_the_script}"
        assert rows[f"*{name}*"] == in_the_guide, f"the row of the usage guide for *{name}* reads: {rows[f'*{name}*']}"
    # A stand-in is put to use on its first right answer, and there can be more than one.
    assert (
        '${single ? "its first answer to a sample document was right" : "each gave a right first answer to a sample document"}'
    ) in repair
    # The two reasons a stand-in was not judged, the two ways a test ends early, and the two
    # ways a chain is left with no model that works.
    assert "The test did not get to its end." in repair and "Calls went unanswered, so the test could not judge every model." in repair
    assert '["key_refused", "interrupted"].includes(t.outcome)' in repair
    endings = script.split("const OUTCOME_WORDS = {\n")[1].split("\n};")[0]
    assert "key_refused:" in endings and "the service's API key was refused." in endings
    assert 'interrupted: { tone: "warn", words: () => "The test was cut short. Nothing was changed." },' in endings
    assert 'const working = t.reason.code === "closed" ? [] : p.chain.filter((model) => !p.gone.includes(model));' in repair

    # ---- The lines of the notice while a test runs ----------------------------------------
    # One line for each tested model: its first answer, a clock while it is asked, or its turn.
    line = script_function(script, "firstAnswerLine")
    assert "const answer = mine.find((call) => call.correct != null);" in line
    assert 'words: `${name}: ${answer.correct ? "right" : "answered, but not correctly"}, after ${seconds(answer.seconds)}`' in line
    assert "if (flying) return { words: `${name}: asking for `, since: flying.since };" in line
    assert "return { words: `${name}: waiting its turn` };" in line
    drawn = script_function(script, "renderUpkeepNotice")
    assert 'const line = ({ words, since }) => h("li", null, words, since ? [clockSince(since), " s"] : null);' in drawn
    must_say(
        "docs/USAGE.md",
        (
            "While a test runs, the notice also has one line for each tested model: what its first answer came to, "
            "how long it has been asked so far, or that it is waiting its turn.",
        ),
    )
    must_say(
        "README.md",
        (
            "While a test runs, the notice has one line for each tested model, with a clock that runs for a model "
            "that is being asked.",
        ),
    )


def test_the_documents_say_what_each_part_of_a_result_means() -> None:
    """The table "Reading the result" names tiles, chips and blocks. Each meaning is a line of the script."""
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    rows = usage_rows("\nReading the result:\n\n", "\n\nA cross under *Expectations*")
    assert rows.pop("On the page") == "Means"
    readme = prose("README.md")
    running = script_function(script, "execute")

    # ---- The four tiles ------------------------------------------------------------------
    tiles = script_function(script, "kpiBlock")
    assert 'running ? "Elapsed" : run.problem ? "Waited" : "Response time",' in tiles
    assert 'let about = run.serverMs != null ? "server side, all attempts" : "round trip from this page";' in tiles
    assert rows["*Response time*"] == "Server-side time for the whole request, every attempt included"
    assert 'h("span", { class: "kpi__value kpi__value--model" }, shortModel(body.model_used)),' in tiles
    assert rows["*Answered by*"] == "`model_used` in the API response"
    assert "fallback = body.fallback_triggered" in tiles
    assert rows["*Fallback: Used*"] == "`fallback_triggered: true`: the first model did not answer"
    # A run without a result shows how long the page waited for it, from the press.
    assert "run.clientMs = performance.now() - (run.problem ? pressedAt : run.startedAt);" in running
    assert rows["*No result*"].endswith("The sentence under the title says which. *Waited* says how long the page waited")

    # ---- The row of one model -------------------------------------------------------------
    assert 'const TONE_ICON = { good: "check", crit: "x", warn: "bang", neutral: "ban", muted: "dash" };' in script
    attempt = script_function(script, "traceRow")
    assert 'failed: { tone: "crit", words: ERROR_WORDS[attempt.errorType] ?? attempt.errorType },' in attempt
    assert "who(badge(look.tone), look.words, simulatedChip)," in attempt
    assert '? [h("code", null, attempt.errorType), attempt.retryable ? " · retryable · " : " · not retryable · "]' in attempt
    assert "visitorWords(attempt.message)," in attempt
    # How many of the engine's messages the page puts into its own words.
    put_into_words = script.split("const VISITOR_WORDS = [\n")[1].split("\n];")[0]
    reworded = len(re.findall(r'^  \["[^"]+", "[^"]+"\],$', put_into_words, re.MULTILINE))
    assert reworded == 3, "the documents say 'three of the messages'"
    count = IN_WORDS[reworded]
    assert rows["A red cross on a model"] == (
        f"That attempt failed; the error type and a short message are underneath. {count.capitalize()} of the service's "
        "messages tell an admin which setting to change. The page puts those into words for a visitor; "
        "*Raw request and response* keeps the API's own text"
    )
    assert (
        f"{count.capitalize()} of the engine's messages tell an admin which setting to change. The page puts those {count} "
        "into words for a visitor, and its block *Raw request and response* keeps the API's own text."
    ) in readme
    assert 'const simulatedChip = attempt.simulated ? chip("simulated", { iconName: "bolt" }) : null;' in attempt
    assert rows["*simulated*"] == "The failure was staged by the page, not caused by the provider"

    # ---- The raw block: what the API returns, and nothing reworded --------------------------
    raw = script_function(script, "rawBlock")
    assert "const headerLines = [`HTTP ${run.status}`];" in raw
    assert "for (const [name, value] of Object.entries(run.headers)) headerLines.push(`${name}: ${value}`);" in raw
    assert 'h("p", { class: "raw__label" }, "What POST /api/v1/extract returns for this outcome"),' in raw
    assert "jsonNodes(run.body)" in raw and "visitorWords(" not in raw
    assert rows["*Raw request and response*"] == "The exact status, headers and JSON `POST /api/v1/extract` returns"
    assert "run.status = event.status_code;" in script_function(script, "applyEvent")

    # ---- A run that the ceiling refused -----------------------------------------------------
    # It is shown as not run, under the service's own sentence, and with the status 503.
    summary = script_function(script, "runSummary")
    not_run = """if (run.refusal) return run.refusal.today ? "Not run: today's share is used up" : "Not run: the demo was busy";"""
    assert not_run in summary
    body = script_function(script, "bodyBlock")
    refused = body[body.index("if (run.refusal) {") : body.index('if (body.error === "providers_unavailable") {')]
    assert 'h("h4", null, "What a client is told"),' in refused and 'h("p", null, body.message),' in refused
    assert script_function(script, "refusalOutcome").count('code: "503",') == 2
    must_say(
        "docs/USAGE.md",
        (
            "Under *What a client is told* you read the service's own sentence, which is what the API answers in the "
            "same case.",
        ),
    )
    assert (
        "A run that the service's ceiling on live model calls refuses is shown as not run, not as failed, with the "
        "service's own sentence."
    ) in readme
    assert (
        'The live demo\'s page says "The demo is busy" or "Today\'s share of live runs is used up", with the status 503'
    ) in prose(DEPLOY)
    # What to do about it: both refusals name their wait and say what still works.
    refusing = script_function(script, "refusalOutcome")
    assert refusing.count("countdown(until)") == 2 and refusing.count("SIMULATED_STILL_WORK,") == 2
    assert 'const SIMULATED_STILL_WORK = "Scenarios that only use simulated failures still work.";' in script
    assert (
        "| The service's own ceiling on model calls was reached | Wait for the time it names. Scenarios that only "
        "simulate failures still work |"
    ) in document(DEPLOY)
    assert (
        "| Wait as long as the countdown or `Retry-After` says. After *Today's share of live runs is used up*, live "
        "runs start again at midnight UTC. Scenarios that only use simulated failures still work |"
    ) in document("docs/USAGE.md")
    assert '"Live runs start again at midnight UTC, in about ", countdown(until), "."' in refusing

    # ---- What is scored ---------------------------------------------------------------------
    scored = "run.checks = scenario && isScored(run) && !run.moved ? scenario.checks.map((check) => evaluate(check, run)) : [];"
    assert scored in running
    assert "*Run all* executes the scenarios in turn and scores those that get an answer" in readme
    checks = script_function(script, "checksBlock")
    assert 'h("h4", null, "Expectations"),' in checks and 'badge(check.pass ? "good" : "crit"),' in checks
    session = script_function(script, "renderSession")
    assert 'h("h3", { id: "session-title" }, "This session"),' in session
    assert "...rows.map(silentModelNotice).filter(Boolean)," in session
    silent = script_function(script, "silentModelNotice")
    assert "const real = failures.filter((attempt) => !attempt.simulated);" in silent
    assert "if (responded || !real.length || (!permanent && real.length < 3)) return null;" in silent
    must_say(
        "docs/USAGE.md",
        (
            "A cross under *Expectations* on a **Documents** scenario is a model reading the document differently "
            "from the expected answer, not a fault in the service.",
            "A model that is called for real and never answers, for example because its id was withdrawn, gets a "
            "warning under *This session*.",
        ),
    )


def test_the_documents_say_what_the_page_marks_on_a_model_and_in_its_top_bar() -> None:
    """Three chips on a model, the status in the top bar, the last line of the chain card, the foot of the page."""
    from app import main as service
    from app import model_chooser
    from app.config import Settings

    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    page = PAGE_FILES[0].read_text(encoding="utf-8")
    styles = (PAGE_SCRIPT.parent / "app.css").read_text(encoding="utf-8")
    usage_raw, usage, readme = document("docs/USAGE.md"), prose("docs/USAGE.md"), prose("README.md")
    learning, deploy = prose("docs/LEARNING.md"), prose(DEPLOY)

    # ---- Which chip a model gets ----------------------------------------------------------
    # A chain that the service looks after: the chip for a model that is gone. Any other
    # chain: the chip that sends the visitor to the admin. An announced end: the date.
    flag = script_function(script, "listingChip")
    looked_after = flag.index('if (mode === "automatic") {')
    named = flag.index('} else if (mode && listing?.status === "not_listed") {')
    ending = flag.index('if (listing?.status === "listed" && listing.expires) {')
    assert looked_after < flag.index("chip(WITHDRAWN_LABEL") < named < flag.index("chip(UNLISTED_LABEL") < ending
    assert ending < flag.index("chip(`Ends ${listing.expires}`")
    assert "if (upkeep.picture.gone.includes(entry.model)) {" in flag[looked_after:named]
    # Outside such a chain a model is "not listed" only where the startup check ran, and it
    # runs only with a key and asks only OpenRouter's own address: that leaves a named chain.
    assert set(model_chooser.ORIGINS) >= {"pinned"} and 'return "pinned"' in inspect.getsource(model_chooser.mode)
    assert "if settings.model_catalogue_check and active:" in inspect.getsource(service.lifespan)
    assert "model_chooser.note_listings(app.state.model_listings)" in inspect.getsource(service.lifespan)
    for drawing in ("renderChain", "renderPlan"):
        assert "listingChip(entry)" in script_function(script, drawing), drawing
    assert (
        "| *Unlisted from OpenRouter, please contact the admin.* on a model | Shown for a chain named in "
        "`OPENROUTER_MODELS`. When the service started, OpenRouter's catalogue did not list that model"
    ) in usage_raw
    assert "A model whose end OpenRouter has announced shows *Ends* and the date instead |" in usage_raw
    assert (
        "| *No longer offered by OpenRouter* on a model | Shown for a chain the service looks after. OpenRouter's own "
        'page for that model says "Model not found".'
    ) in usage_raw
    must_say(
        "README.md",
        (
            "When OpenRouter does not list a model of the chain, or lists it with an announced end date, the page shows "
            "a chip on that model.",
            'For a fixed chain the chip reads "Unlisted from OpenRouter, please contact the admin.", because only the '
            "admin can replace the model.",
            'For a chain that the service looks after it reads "No longer offered by OpenRouter", because the service '
            "replaces the model itself.",
            'An announced end shows "Ends" and the date.',
            "The playground shows a chip on the model.",
            "It shows up in the log, as a chip on the playground and, when it is the primary, as "
            "`fallback_triggered: true` in the responses.",
        ),
    )
    assert "The playground marks the model too (§3)" in usage
    assert (
        "The playground page shows the startup answer as a chip on a model that is not listed or has an end date."
    ) in learning

    # ---- The status in the top bar ------------------------------------------------------------
    header = page.split('<header class="topbar">')[1].split("</header>")[0]
    assert header.index('class="topbar__nav"') < header.index('class="topbar__actions"') < header.index('id="status"')
    assert re.search(r"\n\.topbar \{\s+position: sticky;\s+top: 0;", styles)
    assert re.search(r"\n\.topbar__nav \{[^}]*margin-right: auto;", styles), "what follows the links stands at the right"
    assert "- the status at the top right saying **Operational**;" in document(DEPLOY)
    assert "| *No answer yet* or *Not answering* in the top bar | The page got no answer from the service:" in usage_raw
    # The words of the status are the script's, one for each thing /health or the page can
    # say. Without a key /health says "unavailable", and the top bar says that no key is set.
    said = dict(re.findall(r'^  (\w+): "([^"]+)",$', script.split("const STATUS_WORDS = {\n")[1].split("};")[0], re.MULTILINE))
    assert (said["ok"], said["unavailable"], said["withdrawn"]) == ("Operational", "No API key", "Models withdrawn")
    assert 'if (health.status === "unavailable") {' in script_function(script, "showHealth")
    assert 'health_status = "unavailable"' in inspect.getsource(service)
    assert "| The live demo's page says **No API key** | The key was not entered, or was entered empty |" in document(DEPLOY)
    # "Operational" is what /health says. When every model of a chain that the service looks
    # after is gone, the page says that instead.
    drawing = script_function(script, "renderUpkeep")
    withdrawn = (
        'upkeep.withdrawn = p.mode === "automatic" && p.chain.length > 0 && p.chain.every((model) => p.gone.includes(model));'
    )
    assert withdrawn in drawing
    assert 'setStatus(away ?? (upkeep.withdrawn ? "withdrawn" : state.health.status));' in script_function(script, "showStatus")
    assert (
        "| *Models withdrawn* in the top bar | OpenRouter no longer offers any model of the chain. The top bar normally "
        "repeats what `GET /health` says, and `/health` only says that a key is set. In this case *Operational* would "
        "not be true |"
    ) in usage_raw
    assert (
        "| The live demo's page says **Models withdrawn** at the top right | OpenRouter no longer offers any model of "
        "the chain. The service looks for a replacement by itself, after a run that fails |"
    ) in document(DEPLOY)
    # The sentence under the chain then says what the service has done about it.
    assert "/** Models of the chain are gone: what the service has done about that so far, as the picture says it. */" in script
    assert (
        "Read the sentence under the model chain on that page: it says what the service has done about it so far."
    ) in deploy

    # ---- The last line of the chain card ------------------------------------------------------
    assert '/** The one line in the "Model chain" card: where the chain in use comes from. */' in script
    assert "ORIGIN_LINE[p.origin.kind](p)," in script_function(script, "renderChain")
    lines = script.split("const ORIGIN_LINE = {\n")[1].split("\n};")[0]
    assert set(re.findall(r"^  (\w+): ", lines, re.MULTILINE)) == set(model_chooser.ORIGINS)
    assert page.index('id="chain"') < page.index('id="chain-foot"') < page.index('id="chain-origin"')
    assert "| The last line of the *Model chain* card | Where the chain in use comes from. For a chain named in" in usage_raw
    assert "- under the model chain, a line saying where the chain comes from, for example" in document(DEPLOY)
    assert (
        "One line under the model chain says where the chain comes from: the tested default, this project's scheduled "
        "test, a change the service made itself, or the admin of the demo."
    ) in readme
    # "Fixed" is the page's word for a chain that was named.
    assert "a chain fixed by the admin of this demo" in script
    assert "or that the chain is *fixed*, which is the page's word" in usage

    # ---- The foot of the page ----------------------------------------------------------------
    footer = page.split('<footer class="footer">')[1].split("</footer>")[0]
    assert 'id="footer-version"' in footer and page.index("</main>") < page.index('<footer class="footer">')
    assert '$("#footer-version").textContent = `${service.name} v${service.version} · ${service.environment}`;' in script
    assert "shown at the foot of the page and written in the startup log line" in readme
    assert "# Name reported by /health, shown at the foot of the page and written in the log\n" in document(".env.example")

    # ---- No setting is named on the page -------------------------------------------------------
    names = {field.upper() for field in Settings.model_fields}
    assert {"OPENROUTER_MODELS", "TIMEOUT_SECONDS"} <= names
    for path in PAGE_FILES:
        text = path.read_text(encoding="utf-8")
        named_there = sorted(name for name in names if re.search(rf"\b{name}\b", text))
        assert not named_there, f"{path.name} names a setting: {named_there}"
    assert "| The page names no setting, because visitors read it. What happened is in the log, written for you |" in document(DEPLOY)
    assert "The page is written for visitors, so it names no setting and no file." in usage
    assert "A notice that asks you to contact the admin names no setting: what the admin has to do is in the server log" in usage
    assert "The page is written for visitors, so it names no setting." in readme
    assert "so it never tells them which setting to change: what an admin has to do is in the server log." in readme


def test_the_documents_say_what_the_section_on_model_upkeep_shows() -> None:
    """Which test is on show, what a row and a mark are, what a replay calls, and where it all comes from."""
    from app import model_chooser, model_testing
    from app.playground import routes

    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    page = PAGE_FILES[0].read_text(encoding="utf-8")
    usage, readme, learning = prose("docs/USAGE.md"), prose("README.md"), prose("docs/LEARNING.md")
    recorded = model_testing.recorded_run()
    assert recorded is not None and recorded["by"] == "recorded"

    # ---- Which test is on show ---------------------------------------------------------------
    # The test that is running. Else the last one: a real one of this service or of the
    # scheduled job, whichever is newer, and the recorded one while there is neither.
    board = script_function(script, "renderUpkeepTest")
    assert "const real = p.test ?? p.last;" in board
    picture = inspect.getsource(model_chooser.picture)
    assert '"last": _newest(own, _published_record) or model_testing.recorded_run(),' in picture
    assert (
        "When nothing is happening, the page shows the last test: the service's own, or the scheduled one when that is "
        "newer. Before any test has run, it shows a recorded one: the first pass of the benchmark of "
        f"{long_date(BENCHMARK_SNAPSHOT['measured'])}, which is {len(recorded['calls'])} real calls, judged by the "
        "rules that a test uses."
    ) in readme
    assert "calls = [call for call in benchmark.load_records(paths) if call.get(\"pass\") == 1]" in inspect.getsource(
        model_testing.recorded_run
    )
    assert (
        "it shows one test of the free models: the test that is running, or the last one that finished, or one that "
        "was recorded when the default chain was measured."
    ) in usage
    assert "The third is the recorded test, which the page shows before any test has run." in learning
    assert (
        f"Until a test has run, the section shows the recorded test of {long_date(BENCHMARK_SNAPSHOT['measured'])}, and "
        "it can replay that test call by call. The replay calls nothing."
    ) in learning
    assert (
        'which the page shows and replays as "A recorded test" while it has no newer test to show'
    ) in one_line(document("benchmarks/README.md"))
    # The section stands under its heading, and the page shows the rest there, as the README says.
    assert '<section class="section" id="upkeep">' in page and "<h2>What happens when a free model disappears</h2>" in page
    assert (
        "The page shows the rest in its section *Model upkeep*: where the chain comes from, a test while it runs, and "
        "what the last test found"
    ) in readme

    # ---- A row is a model, and a mark is one sample document sent to it -----------------------
    assert 'h("ul", { class: "upkeep__rows" }, t.models.map((row) => upkeepRow(row, t, p))),' in board
    assert "documentMarks(row, t)," in script_function(script, "upkeepRow")
    assert "return t.cases.map((item) => {" in script_function(script, "documentMarks")
    assert '["Each mark", "is one sample document sent to one model:' in script
    assert "chip(look.label, { tone: CHIP_TONE[look.tone], iconName: TONE_ICON[look.tone] })," in script_function(script, "upkeepRow")
    assert (
        "A test is drawn while it runs: one row for each model, one mark for each sample document, and the verdict."
    ) in readme
    assert "Each mark stands for one sample document sent to one model." in usage

    # ---- Call by call ---------------------------------------------------------------------------
    # A mark for every call as its answer comes, and the same calls as a list.
    assert "const call = t.calls.find((entry) => entry.model === row.model && entry.case === item.id);" in script_function(
        script, "documentMarks"
    )
    assert 'h("summary", null, `List the ${plural(t.calls.length, "result")} one by one`),' in script_function(script, "callList")
    assert "The section *Model upkeep* shows the test call by call, and a notice in the result panel says" in usage
    assert (
        'Its section "What happens when a free model disappears" says where the chain in use comes from, draws a running '
        "test call by call, and keeps the last test on show."
    ) in learning

    # ---- A replay calls nothing -------------------------------------------------------------------
    # It plays the record that the page already has. Neither of its two functions sends a request.
    replay, frame = script_function(script, "replayModelsTest"), script_function(script, "replayFrame")
    for part in (replay, frame):
        assert "fetch(" not in part and "readJSON(" not in part and "startModelsTest(" not in part
    assert "const record = upkeep.picture?.last;" in replay and "if (!record || upkeep.picture.test) return;" in replay
    assert "const calls = record.calls.slice(0, shown);" in frame
    assert "Nothing is called now: these are its calls as they were recorded." in board
    # Any finished test that has calls can be replayed, the recorded one included.
    assert "if (!p.test && real.calls.length) {" in board and '" Replay this test",' in board
    assert recorded["calls"], "the recorded test has calls to replay"
    assert "*Replay this test* plays a finished test again and calls nothing." in readme
    assert "*Replay this test* plays the test again from its record and calls nothing." in usage
    assert "the service's own test, the scheduled test, and the recorded test that the page can replay." in learning
    assert 'go to "What happens when a free model disappears" and press *Replay this test*. Nothing is called.' in learning
    assert (
        "**What you should see:** the test on show is played again, call by call. Nothing is called for that."
    ) in prose(DEPLOY)

    # ---- One answer, whole every time, and the words in the page ----------------------------------
    whole = "The service is asked for the whole picture every time (GET /playground/models), never for a change to the answer before."
    assert whole in one_line(script.replace("//", " "))
    assert "upkeep.picture = picture;" in script_function(script, "watchModels")
    assert "const p = shownPicture();" in script_function(script, "renderUpkeep")
    assert "return JSONResponse(model_chooser.picture()," in inspect.getsource(routes.models_picture)
    # Asking is free and starts nothing: the route only builds the picture, from what the
    # service already knows, and tells every cache on the way not to keep the answer.
    answering = inspect.getsource(routes.models_picture)
    assert 'headers={"Cache-Control": "no-store"}' in answering
    assert "start(" not in answering and "note(" not in answering and "create_task" not in answering
    assert not inspect.iscoroutinefunction(model_chooser.picture), "a plain function waits for no model"
    assert "start(" not in picture and "_in_background(" not in picture
    for part in ('"chain": chain,', '"origin": {', '"gone": [', '"doing": doing,', '"test": _test,', '"last": '):
        assert part in picture, f"the picture no longer holds {part}"
    assert (
        "| `GET /playground/models` | How the service looks after its chain. The answer is the whole picture every "
        "time, and it is never cached. Asking starts nothing and calls no model |"
    ) in readme
    assert (
        "**`GET /playground/models`** returns the whole picture as JSON: the chain, where it comes from, the models "
        "that OpenRouter no longer offers, what the service is doing at this moment, the test that is running and "
        "the last one that finished. Asking for it starts nothing and calls no model:"
    ) in usage
    assert (
        "# what is the service doing about its model chain? (the whole picture as JSON; calls no model)\n"
        "curl -s localhost:8000/playground/models\n"
    ) in document("docs/USAGE.md")
    for said in (
        "All of it is drawn from one answer, `GET /playground/models`, which holds codes, dates and figures and not "
        "one sentence (§15).",
        "The page asks `GET /playground/models` and gets everything, every time. So a tab that is opened in the middle "
        "of a test, a second tab and a reload all draw the same thing.",
        "Every sentence a visitor reads is in the page's script, in tables keyed by those codes",
    ):
        assert said in learning, f"docs/LEARNING.md no longer says: {said}"
    for words in ("LOOK_WORDS", "REFUSAL_WORDS", "DOING_WORDS", "REASON_WORDS", "PLACING_WORDS", "OUTCOME_WORDS"):
        assert f"\nconst {words} = {{\n" in script, f"the page's script has no table {words}"

    # ---- Two words that the guides take over from the page ------------------------------------------
    for words in ("The chain was repaired", "and may repair its chain", "It still repairs its chain itself"):
        assert words in script, f"the page no longer says: {words}"
    assert "This guide calls steps 2 to 5 together a *repair*, the page's word for them" in usage
    assert '"answers as a stand-in" : "answer as stand-ins"' in script
    assert "The page calls such a model a *stand-in*." in usage
    assert "The page calls such a model a **stand-in**." in learning


def test_the_documents_say_when_the_button_is_there_and_when_it_cannot_be_used() -> None:
    """The button that starts a test: who has it, and every reason for which a press is refused."""
    from app import budget, model_chooser, model_rules
    from app.playground import benchmark

    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    usage_raw, usage, readme = document("docs/USAGE.md"), prose("docs/USAGE.md"), prose("README.md")

    # ---- Who has the button ----------------------------------------------------------------------
    # A service that looks after its chain, and nobody else. The environment is not asked,
    # so the button is there in production too.
    picture = inspect.getsource(model_chooser.picture)
    assert '"offered": state == "automatic",' in picture and '"ready": state == "automatic" and reason is None,' in picture
    deciding = inspect.getsource(model_chooser.mode)
    order = [deciding.index(f'return "{code}"') for code in ("pinned", "no_key", "not_openrouter", "automatic")]
    assert order == sorted(order)
    for source in (picture, deciding, inspect.getsource(model_chooser._quick_refusal)):
        assert "is_production" not in source and "environment" not in source
    drawing = script_function(script, "renderUpkeep")
    assert '$("#upkeep-control").hidden = !p.button.offered;' in drawing
    assert (
        "The button *Test the free models now* starts a test. It is there only when the service looks after its chain, "
        "in production too."
    ) in readme
    assert "With `OPENROUTER_MODELS` blank the section also has a button, *Test the free models now*." in usage
    header = "| | `OPENROUTER_MODELS` is blank | `OPENROUTER_MODELS` names models |"
    table = usage_raw.split(header)[1].split("\n\n")[0]
    assert "\n| The tests of the models, the published list, the button on the page | In use | Switched off |" in table
    template = one_line(" ".join(line.lstrip("# ") for line in document(".env.example").splitlines()))
    assert "with OPENROUTER_MODELS blank, a visitor can also start a test of the free models from it" in template
    row = next(line for line in usage_raw.splitlines() if line.startswith("| The button *Test the free models now* is faded"))
    cause = row.split("|")[2].strip()
    assert cause.endswith(
        "Not there: the chain is named in `OPENROUTER_MODELS`, no key is set, or `OPENROUTER_BASE_URL` is not OpenRouter's"
    )

    # ---- Why a press is refused --------------------------------------------------------------------
    # Every code the service can answer with, and what the row says for it. The first three
    # are the cases without a button.
    pause = model_chooser.PAUSE_SECONDS // 60
    share = f"{budget.BUTTON_SHARE:.0%}"
    in_the_row = {
        "pinned": "the chain is named in `OPENROUTER_MODELS`",
        "no_key": "no key is set",
        "not_openrouter": "`OPENROUTER_BASE_URL` is not OpenRouter's",
        "running": "A test is running (the button then reads *Testing*) or about to start, or the service is looking "
        "into a run that failed",
        "pause": f"a test ran less than {pause} minutes ago",
        "list_unreadable": "OpenRouter's list or the day's count could not be read",
        "allowance_unknown": "OpenRouter's list or the day's count could not be read",
        "allowance": f"or less than {share} of today's free requests is left",
    }
    # One code is left out on purpose. A test that the button starts also takes the chain's own
    # models, and a chain is never empty, so such a test always has something to test.
    assert set(model_chooser.REFUSALS) - set(in_the_row) == {"nothing_to_test"}
    checks = inspect.getsource(model_chooser._free_checks)
    assert "candidates = members + standing + outside" in checks and 'return "nothing_to_test", soon' in checks
    assert "return model_cache.settled() or model_testing.default_chain()" in inspect.getsource(model_chooser._members)
    assert "no other free model" not in cause, "the button's test always has the chain's own models to test"
    for code, words in in_the_row.items():
        assert words in cause, f"the row of section 15 no longer gives the reason '{code}': {words}"
    # The three things "running" stands for, as the sentence beside the button tells them apart.
    assert model_chooser.DOING == ("looking", "starting", "testing")
    assert "running: (b, rules, p) => DOING_WORDS[p.doing] ?? DOING_WORDS.testing," in script
    assert 'if _test is not None or _starting or (by == "page" and _looking):' in inspect.getsource(model_chooser._quick_refusal)
    # The sentence beside the button has words for every code, and the page reads the reason from the picture.
    reasons = script.split("const REFUSAL_WORDS = {\n")[1].split("\n};")[0]
    assert set(re.findall(r"^  (\w+): ", reasons, re.MULTILINE)) == set(model_chooser.REFUSALS)
    assert "REFUSAL_WORDS[p.button.reason](p.button, p.rules, p)" in drawing
    # The README gives the same reasons in its own words.
    assert (
        "The service refuses a press, without spending a free request, in these cases: a test is under way, or the "
        f"service is looking into a failed request; a test ended less than {pause} minutes ago; OpenRouter's list of free "
        f"models cannot be read; less than {share} of the day's free requests are left, or that count cannot be read."
    ) in readme
    # A press that the page knows to be useless is not even sent, and one that the service
    # refuses starts no test: every question of start() comes before the first model call.
    pressing = script_function(script, "startModelsTest")
    assert pressing.index("if (upkeep.starting || upkeep.lost || !upkeep.picture?.button.ready) return;") < pressing.index("fetch(")
    assert cause.startswith("Faded: the sentence beside it says why.")
    assert row.split("|")[3].strip() == "Wait until the time the sentence gives. A press that is refused spends nothing"
    # The quick reference gives the request that the button sends, and what it needs.
    assert 'await fetch("/playground/models/test", { method: "POST", signal: wait.signal });' in pressing
    at_most = model_rules.CANDIDATE_LIMIT * len(benchmark.live_cases())
    assert '"requests_at_most": model_rules.CANDIDATE_LIMIT * len(benchmark.live_cases()),' in picture
    assert (
        f"# test the free models now, as the page's button does (uses free requests, {at_most} at most;\n"
        "# needs OPENROUTER_MODELS blank; answers 403 to curl when ENVIRONMENT=production)\n"
        "curl -s -X POST localhost:8000/playground/models/test\n"
    ) in usage_raw


def test_the_documents_say_how_the_page_copes_with_a_service_that_does_not_answer() -> None:
    """It says so in a notice that is no error, asks /health, and carries on by itself."""
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    usage, readme, learning = prose("docs/USAGE.md"), prose("README.md"), prose("docs/LEARNING.md")
    figures = page_figures(script)
    asks = f"every {figures['HEALTH_EVERY_MS'] // 1000} seconds, for {figures['GIVE_UP_AFTER_MS'] // 60000} minutes at most"

    # A load that gets no answer says so, waits for the service and then reads the page again.
    loading = script_function(script, "load")
    assert "const awake = await waitForService(undefined, since);" in loading
    assert "ready = awake && (await readPage());" in loading
    waking = script_function(script, "sayWaking")
    assert 'showStatus("waiting");' in waking and 'showNotice(\n    "warn",' in waking
    assert "This page keeps asking, and finishes loading by itself when the demo answers." in waking
    # The question that it repeats is the cheapest one the service has.
    waiting = script_function(script, "waitForService")
    assert 'const health = await readJSON("/health", asking.signal);' in waiting
    assert "if (isHealth(health)) finish(health);" in waiting
    # A run does the same, and says under which title.
    running = script_function(script, "execute")
    assert "const awake = await waitForService(signal, pressedAt);" in running
    assert 'title: "Waiting for the demo",' in script_function(script, "outcomeOf")
    must_say(
        "docs/USAGE.md",
        (
            "If the service gives no answer when the page loads, the top bar says *No answer yet* and a notice says "
            "that the demo *has probably gone to sleep*.",
            f"The page then asks `GET /health` {asks}, and finishes loading by itself when the service answers.",
            "A run that got no answer waits in the same way, under the title *Waiting for the demo*, and is then sent "
            "again by itself, once.",
        ),
    )
    must_say(
        "README.md",
        (
            f"When the page gets no answer, it says so and asks `GET /health` {asks}.",
            "When the service answers, the page finishes loading by itself.",
            "The playground itself also copes with a service that does not answer: it says so, waits, and then carries "
            "on by itself",
        ),
    )
    must_say(
        "docs/LEARNING.md",
        (
            "So the page asks for news only while something is happening, and a tab that was left open stops asking.",
            "And when the service does not answer, the page does not show an error. It says that the demo has probably "
            "gone to sleep, waits for it, and carries on when it answers.",
        ),
    )
    assert learning.count("carries on when it answers") == 1 and readme.count("finishes loading by itself") == 1
    # The page does not look at where it runs, so a developer reads the same words when the
    # server is stopped as a visitor reads when the host has put the service to sleep.
    assert not re.search(r"\blocation\b|\bhostname\b|localhost", script)
    assert "On your own machine you read the same words when the server is not running." in usage


def test_the_documents_describe_the_test_suite_panel_and_what_production_hides() -> None:
    """The panel runs the whole suite and shows every test under the banners of its file. Production has neither it nor the docs."""
    from app.playground import routes, suite
    from app.playground.catalogue import scenarios_for

    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    page = PAGE_FILES[0].read_text(encoding="utf-8")

    # ---- What the panel runs, and how it shows it ----------------------------------------------
    starting = inspect.getsource(suite.run_suite)
    assert '"pytest",\n            str(TESTS_DIR),' in starting and suite.TESTS_DIR == PROJECT_ROOT / "tests"
    assert "subprocess.run(" in starting, "pytest runs in a child process"
    assert "sections = group_into_sections(tests)" in starting
    assert "banner_sections(" in inspect.getsource(suite.group_into_sections)
    drawing = script_function(script, "renderSuite")
    assert "const list = section.tests.map((test) =>" in drawing and 'h("h4", null, section.title),' in drawing
    assert 'fetch("/playground/tests", { method: "POST", signal: wait.signal })' in script_function(script, "runSuite")
    must_say(
        "README.md",
        (
            "A panel runs `pytest` in a child process and shows every test, grouped under the banner comments of the "
            "test files. It is off when `ENVIRONMENT=production`.",
            "The playground's *Test suite* panel runs all of this from the browser.",
        ),
    )
    must_say(
        "docs/USAGE.md",
        (
            "**Run the automated tests** from the *Test suite* section. This starts `pytest` on your machine and shows "
            "every test with its result. It needs no API key.",
            "press *Run the test suite*. Results appear grouped under the headings of the test files.",
        ),
    )

    # ---- What production hides ------------------------------------------------------------------
    assert "if get_settings().is_production:" in inspect.getsource(suite.availability)
    assert "button.hidden = !available;" in script_function(script, "renderSuiteControls")
    assert '<button class="btn btn--primary" id="run-suite" type="button" hidden>' in page
    assert "<span>Run the test suite</span>" in page
    assert '<a class="btn btn--ghost btn--sm" id="docs-link" href="/docs" hidden>API docs</a>' in page
    assert "if (service.docs_url) {" in script_function(script, "renderChrome")
    building = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "hide_docs = settings.is_production" in building and 'docs_url=None if hide_docs else "/docs",' in building
    assert '"docs_url": request.app.docs_url,' in inspect.getsource(routes.playground_config)
    deploy = document(DEPLOY)
    assert '- no "API docs" link and no "Run the test suite" button (both are for development).' in deploy
    assert (
        '| `ENVIRONMENT` is `production` | Hides `/docs` and the page\'s button "Run the test suite", and makes the '
        "page's own routes refuse callers that are not a browser |"
    ) in deploy
    # The setting says the same of itself where a developer reads it, in app/config.py.
    from app.config import Settings

    described = Settings.model_fields["environment"].description or ""
    for part in (
        "it disables /docs",
        "switches the page's test-suite route off",
        "makes the page's POST routes refuse callers that are not a browser",
    ):
        assert part in described, part
    assert "allowed = not get_settings().is_production" in inspect.getsource(routes)

    # ---- The two scenarios that the deployment guide has its reader run ---------------------------
    by_title = {scenario["title"]: scenario for scenario in scenarios_for(len(DEFAULT_CHAIN))}
    simulated, live = by_title["Every model fails"], by_title["Invoice inside an email thread"]
    assert simulated["live_calls"] == 0 and all(simulated["simulate"])
    assert live["live_calls"] >= 1 and not any(live["simulate"])
    guide = prose(DEPLOY)
    assert "The scenario simulates its failures, so it calls no model and costs nothing." in guide
    assert "Run **Invoice inside an email thread** in the same way. This one calls a real model." in guide


def test_the_documents_describe_the_scenarios_as_the_catalogue_has_them() -> None:
    """How many scenarios of each kind, which need a third model, and which two kinds have no spare."""
    from app.playground.catalogue import GROUPS, scenarios_for

    assert len(DEFAULT_CHAIN) == 2
    two, three = scenarios_for(2), scenarios_for(3)
    of_group = {group["title"]: [scenario for scenario in two if scenario["group"] == group["id"]] for group in GROUPS}
    assert set(of_group) == {"Documents", "Rejections", "Resilience"}
    usage, readme = prose("docs/USAGE.md"), prose("README.md")

    # ---- How many, and of which kind ---------------------------------------------------------
    documents, refusals, routing = (IN_WORDS[len(of_group[title])] for title in ("Documents", "Rejections", "Resilience"))
    assert (
        f"{documents.capitalize()} are documents, {refusals} are inputs the service has to refuse, and {routing} "
        "exercise the fallback routing."
    ) in readme
    more = IN_WORDS[len(three) - len(two)]
    needs_three = [scenario for scenario in three if scenario["title"] not in {known["title"] for known in two}]
    assert needs_three and all(len(scenario["simulate"]) == 3 for scenario in needs_three)
    assert f"{more.capitalize()} more scenarios need a third model and appear when your chain has one." in usage
    assert f"because {more} of them need a third" in readme

    # ---- What a correct run must produce --------------------------------------------------------
    # Every scenario carries its checks. The page lists them under "Expectations" once the run
    # has an answer, each with a tick or a cross, and the list of scenarios counts those that
    # were met. (The mark of a scenario in that list has a third form, for a run that got the
    # right status and missed another check, so the guide speaks of ticks and crosses only
    # where each check has one.)
    assert all(scenario["checks"] for scenario in three)
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    checks = script_function(script, "checksBlock")
    assert "if (!run.checks.length) return null;" in checks and 'badge(check.pass ? "good" : "crit"),' in checks
    assert 'h("h4", null, "Expectations"),' in checks and 'h("span", null, check.label),' in checks
    scored = "run.checks = scenario && isScored(run) && !run.moved ? scenario.checks.map((check) => evaluate(check, run)) : [];"
    assert scored in script_function(script, "execute")
    entry = script_function(script, "scenarioButton")
    assert 'let right = plural(scenario.checks.length, "check");' in entry
    assert 'right = isScored(run) ? `${verdict.passed}/${verdict.total}` : "";' in entry
    assert 'else if (passed < total) tone = run.checks[0]?.pass ? "warn" : "crit";' in script_function(script, "verdictOf")
    for said in (
        "Each scenario has checks that say what a correct run must produce.",
        "Once a run has an answer, the result lists them under *Expectations*, each with a tick or a cross, and the "
        "list of scenarios says how many of them were met.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"
    assert "is ticked or crossed against it" not in usage
    assert "each a document, an optional failure plan and the checks a correct run must pass" in readme
    # A scenario that the visitor has changed, in its document or in its failures, is run as
    # a document of the visitor's own. It has no checks then, and a chip in the result says so.
    starting = script_function(script, "runDraft")
    assert "const pristine = scenario && !isEdited();" in starting
    assert "key: pristine ? scenario.id : CUSTOM," in starting and "scenario: pristine ? scenario : null," in starting
    edited = script_function(script, "isEdited")
    assert "state.draft.text !== scenario.text ||" in edited and "state.draft.simulate[index]" in edited
    assert 'chips.push(chip("Edited: expectations not applied"));' in script_function(script, "outcomeBlock")
    for said in (
        "A scenario that you have changed is run as a document of your own.",
        "It is then not checked against what the scenario expects, and the result says so.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"

    # ---- Two models leave no spare ---------------------------------------------------------------
    # One refusal needs a verdict from both models. And where the first model is made to fail,
    # the second is the only live one behind it.
    (not_an_invoice,) = [scenario for scenario in two if scenario["title"] == "Not an invoice"]
    assert not_an_invoice["live_calls"] == len(DEFAULT_CHAIN) and not any(not_an_invoice["simulate"])
    assert "Two models agree" in [check["label"] for check in not_an_invoice["checks"]]
    primary_fails = [
        scenario for scenario in of_group["Resilience"] if scenario["simulate"][0] and not any(scenario["simulate"][1:])
    ]
    assert primary_fails and all(scenario["live_calls"] == 1 for scenario in primary_fails)
    count = IN_WORDS[len(primary_fails)]
    for said in (
        "A two-model chain has no spare, and two kinds of scenario show it.",
        "*Not an invoice* needs both models to answer before its *Two models agree* check can pass.",
        f"The {count} *Resilience* scenarios in which the primary fails use the second model as the live fallback, so "
        "they go red when that model is rate limited.",
    ):
        assert said in usage, f"docs/USAGE.md no longer says: {said}"
    assert (
        'With the default chain, *Not an invoice* needs both models to answer before its "Two models agree" check can '
        f"pass, and the {count} scenarios in which the primary fails use the second model as the live fallback, so they "
        "go red when that model is rate limited."
    ) in readme


def test_a_run_ends_when_its_visitor_goes_away_as_the_learning_guide_says() -> None:
    """The page's route stops a run whose reader is gone. The reading of the day's count goes on."""
    from app import budget
    from app.playground import routes

    streaming = inspect.getsource(routes._events)
    ending = streaming[streaming.rindex("finally:") :]
    assert "only when the client went away" in ending and "task.cancel()" in ending
    reading = inspect.getsource(budget._ask_openrouter)
    assert "_reading = asyncio.create_task(_read(now, _since_read))" in reading
    assert "await asyncio.shield(_reading)" in reading
    # The page ends a request of its own in three cases, and each of them leaves the service
    # with a reader that is gone: a tab that is closed, "Run all" that is stopped, and a run
    # that the page gave up after too long a silence.
    script = PAGE_SCRIPT.read_text(encoding="utf-8")
    assert "signal: wait.signal," in script_function(script, "send")
    assert (
        "The page cancels a run when its visitor goes away, and the count is everybody's, so the reading goes on."
    ) in prose("docs/LEARNING.md")

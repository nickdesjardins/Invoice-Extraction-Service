"""Runs the project's pytest suite for the playground's "Automated tests" panel.

The suite is hermetic: it swaps the OpenRouter client for a fake and pins its own
environment, so a run needs no API key and makes no network calls. It executes in a
child process, because ``tests/conftest.py`` rewrites ``os.environ`` at import time
and that must never happen inside the server that is serving the page.

Results come back grouped by the banner comments in each test file, so the page
shows the suite under the same headings a reader sees in the source.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = PROJECT_ROOT / "tests"

RUN_TIMEOUT_SECONDS = 120

#: One run at a time: a second request while pytest is busy is refused, not queued.
RUN_LOCK = threading.Lock()

#: Banners that divide a test file into sections: a rule, the title, a rule.
#:
#:     # -----------------------------
#:     # Test 2 - a failing model cascades to the next one in the chain
#:     # -----------------------------
#:
#: Comment lines may follow the title before the closing rule (a note about the
#: section); the title is still the first line.
_BANNER_RE = re.compile(
    r"^# -{10,}[ \t]*\n# (?P<title>\S.*?)[ \t]*\n(?:#.*\n)*?# -{10,}[ \t]*$", re.MULTILINE
)

_MAX_DETAIL_CHARS = 4000


class SuiteError(RuntimeError):
    """The suite could not be run to completion (as opposed to tests failing)."""


def availability() -> tuple[bool, Optional[str]]:
    """Whether the suite can be run from this deployment, and why not if it cannot."""
    if get_settings().is_production:
        return False, "Running the test suite from the browser is disabled in production."
    if not TESTS_DIR.is_dir():
        return False, "The tests directory is not part of this deployment."
    if importlib.util.find_spec("pytest") is None:
        return False, "pytest is not installed in this environment."
    return True, None


def run_suite() -> dict[str, Any]:
    """Run pytest in a child process and return the outcome of every test."""
    with tempfile.TemporaryDirectory(prefix="playground-suite-") as scratch:
        report = Path(scratch) / "report.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            str(TESTS_DIR),
            "-q",
            "--no-header",
            # Leave nothing behind in the project tree: a dev server watching for
            # file changes must not restart because its own tests just ran.
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            # The legacy family is the one that records each test's file and line.
            "-o",
            "junit_family=xunit1",
        ]
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RUN_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise SuiteError(
                f"The test suite did not finish within {RUN_TIMEOUT_SECONDS} seconds."
            ) from exc
        wall_ms = round((time.perf_counter() - started) * 1000, 1)

        if not report.is_file():
            # pytest never got as far as running tests (usage or collection crash).
            tail = (completed.stdout + completed.stderr).strip()[-_MAX_DETAIL_CHARS:]
            raise SuiteError(f"pytest exited with code {completed.returncode}.\n{tail}")
        tests, reported_seconds = parse_report(report.read_text(encoding="utf-8"))

    sections = group_into_sections(tests)
    return {
        "summary": {
            "total": len(tests),
            "passed": sum(test["outcome"] == "passed" for test in tests),
            "failed": sum(test["outcome"] == "failed" for test in tests),
            "skipped": sum(test["outcome"] == "skipped" for test in tests),
            # What pytest reports for the session, and what the page waited in total
            # (interpreter start-up and collection included).
            "duration_ms": round(reported_seconds * 1000, 1),
            "wall_ms": wall_ms,
        },
        "sections": sections,
    }


def parse_report(xml: str) -> tuple[list[dict[str, Any]], float]:
    """Turn a JUnit XML report into one record per test, in execution order.

    The report is written by the pytest process started above, so it is trusted
    input; this parser is not meant for XML from anywhere else.
    """
    root = ElementTree.fromstring(xml)
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    reported_seconds = float(suite.get("time", 0)) if suite is not None else 0.0

    tests: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        outcome, detail = "passed", None
        for child in case:
            if child.tag in ("failure", "error"):
                outcome = "failed"
                detail = (child.text or child.get("message") or "").strip()
            elif child.tag == "skipped" and outcome != "failed":
                outcome = "skipped"
                detail = (child.get("message") or child.text or "").strip()

        file = (case.get("file") or "").replace("\\", "/")
        module = file.removesuffix(".py").replace("/", ".")
        classname = case.get("classname") or ""
        # classname is "<module>" for a function and "<module>.<Class>" for a method.
        owner = classname[len(module) + 1 :] if module and classname.startswith(f"{module}.") else ""
        name = case.get("name") or ""
        tests.append(
            {
                "name": f"{owner}::{name}" if owner else name,
                "file": file,
                "line": int(case.get("line") or 0),
                "outcome": outcome,
                "duration_ms": round(float(case.get("time") or 0) * 1000, 2),
                "detail": detail[:_MAX_DETAIL_CHARS] if detail else None,
            }
        )
    return tests, reported_seconds


def banner_sections(source: str) -> list[tuple[int, str]]:
    """``(line, title)`` for every banner comment in a test file, in file order.

    ``line`` is the zero-based line of the title, the same convention pytest uses
    for a test's location.
    """
    return [
        (source.count("\n", 0, match.start("title")), match.group("title"))
        for match in _BANNER_RE.finditer(source)
    ]


def group_into_sections(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group test records under the banner each one sits below in its file."""
    banners: dict[str, list[tuple[int, str]]] = {}
    sections: dict[tuple[str, str], dict[str, Any]] = {}

    for test in tests:
        file = test["file"]
        if file not in banners:
            path = PROJECT_ROOT / file
            banners[file] = (
                banner_sections(path.read_text(encoding="utf-8")) if file and path.is_file() else []
            )
        # A file without banners is one section, named after the file.
        title = Path(file).stem if file else "tests"
        for line, banner in banners[file]:
            if line > test["line"]:
                break
            title = banner

        section = sections.setdefault(
            (file, title),
            {
                "title": title,
                "file": file,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "duration_ms": 0.0,
                "tests": [],
            },
        )
        section[test["outcome"]] += 1
        section["duration_ms"] = round(section["duration_ms"] + test["duration_ms"], 2)
        section["tests"].append(
            {key: test[key] for key in ("name", "outcome", "duration_ms", "detail")}
        )
    return list(sections.values())

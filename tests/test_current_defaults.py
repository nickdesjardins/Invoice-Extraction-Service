"""Current model choices have saved evidence without rewriting historical records."""

import json
from pathlib import Path

from app import config, model_rules, model_testing
from app.playground import benchmark
from app.playground.catalogue import BENCHMARK_SNAPSHOT


def test_current_defaults_are_supported_by_the_saved_review() -> None:
    evidence = json.loads(
        (Path(__file__).resolve().parents[1] / "benchmarks/2026-11-16-default-review.json").read_text(
            encoding="utf-8-sig"
        )
    )
    primary, fallback = model_testing.default_chain()
    assert primary == "nvidia/nemotron-3.5-super-vl:free"
    assert fallback == "dots-studio/dots-3-note-preview:free"
    cases = [case["id"] for case in benchmark.live_cases()]
    assert len(evidence) == 5
    for run in evidence:
        rows = model_rules.judge([primary, fallback], run["calls"], cases, 30, finished=True)
        assert rows[0]["verdict"] == "passed", run["run"]
        assert rows[1]["correct"] == rows[1]["answered"] > 0
    assert config.DEFAULT_CHAIN_TESTED_AT == max(run["tested"] for run in evidence)


def test_replay_keeps_the_models_that_were_actually_recorded() -> None:
    record = model_testing.recorded_run()
    assert record is not None
    assert record["chain_before"] == BENCHMARK_SNAPSHOT["chain"]
    assert record["chain_before"] != model_testing.default_chain()
    assert all(model in record["tested"] for model in record["chain_before"])

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
    assert primary == "baidu/ernie-5.5-flash-preview:free"
    assert fallback == "particle-ai/particle-1:free"
    cases = [case["id"] for case in benchmark.live_cases()]
    assert len(evidence) == 6
    passed = {primary: 0, fallback: 0}
    for run in evidence:
        rows = model_rules.judge([primary, fallback], run["calls"], cases, 30, finished=True)
        for row in rows:
            passed[row["model"]] += row["verdict"] == "passed"
        if run["tested"] == config.DEFAULT_CHAIN_TESTED_AT:
            assert all(row["verdict"] == "passed" and row["correct"] == len(cases) for row in rows)
    assert passed == {primary: 2, fallback: 1}
    assert config.DEFAULT_CHAIN_TESTED_AT == max(run["tested"] for run in evidence)


def test_replay_keeps_the_models_that_were_actually_recorded() -> None:
    record = model_testing.recorded_run()
    assert record is not None
    assert record["chain_before"] == BENCHMARK_SNAPSHOT["chain"]
    assert record["chain_before"] != model_testing.default_chain()
    assert all(model in record["tested"] for model in record["chain_before"])

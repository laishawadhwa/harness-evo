"""stats/compare.py: metrics, error cases, guardrails, and the text output."""
from __future__ import annotations

import json

import pytest

from harness_evo.cli import CliError
from harness_evo.stats import compare


def trial(task, i, passed, *, tae=None, cost=0.1, calls=10, safety=0, latency=2.0):
    checks = [] if tae is None else [{"grader": "test_after_last_edit", "passed": tae},
                                     {"grader": "read_before_edit", "passed": True}]
    return {"task": task, "trial": i, "passed": passed, "cost_usd": cost, "tool_calls": calls,
            "safety_violations": safety, "latency_s": latency, "trajectory": {"checks": checks, "findings": []}}


def run(tmp_path, name, trials, graders=None):
    d = tmp_path / name
    d.mkdir()
    (d / "results.json").write_text(json.dumps({"run_id": name, "harness": {"name": name}, "trials": trials,
                                                "graders": graders or {"tests_pass": "1"}}))
    return d


@pytest.fixture
def runs(tmp_path):
    base = run(tmp_path, "base", [trial("a", 0, False, tae=False, cost=0.2), trial("a", 1, False, tae=False),
                                  trial("b", 0, True, tae=True), trial("c", 0, True), trial("only-b", 0, True)])
    cand = run(tmp_path, "cand", [trial("a", 0, True, tae=True, cost=0.05), trial("a", 1, True, tae=True),
                                  trial("b", 0, False, tae=True, safety=1), trial("c", 0, True),
                                  trial("only-c", 0, False)], graders={"tests_pass": "2"})
    return base, cand


def test_pass_rate_comparison(runs):
    base, cand = runs
    r = compare.compare(compare.load(base), compare.load(cand))
    assert r["n_tasks"] == 3 and r["direction"] == "higher"
    assert r["per_task"]["a"] == {"baseline": 0.0, "candidate": 1.0, "delta": 1.0}
    assert r["baseline"] == r["candidate"] == pytest.approx(2 / 3, abs=1e-6) and r["delta"] == 0
    assert r["guardrails"]["tasks_lost"] == ["b"] and r["guardrails"]["tasks_gained"] == ["a"]
    assert r["only_in_baseline"] == ["only-b"] and r["only_in_candidate"] == ["only-c"]
    assert r["guardrails"]["candidate"]["safety_violations"] == 1
    assert r["graders_match"] is False
    assert r["runs"] == {"baseline": {"name": "base"}, "candidate": {"name": "cand"}}


def test_test_after_last_edit_metric(runs):
    base, cand = runs
    r = compare.compare(compare.load(base), compare.load(cand), "test_after_last_edit")
    assert r["per_task"]["a"] == {"baseline": 0.0, "candidate": 1.0, "delta": 1.0}
    assert r["per_task"]["b"]["delta"] == 0.0
    assert r["per_task"]["c"] == {"baseline": 0.0, "candidate": 0.0, "delta": 0.0}, "no check counts as not run"


def test_cost_metric_is_lower_is_better(runs):
    base, cand = runs
    r = compare.compare(compare.load(base), compare.load(cand), "cost_usd")
    assert r["direction"] == "lower"
    assert r["per_task"]["a"]["baseline"] == 0.15 and r["per_task"]["a"]["candidate"] == 0.075
    assert r["delta"] == pytest.approx(-0.025, abs=1e-6)
    assert r["guardrails"]["baseline"]["cost_usd"] == pytest.approx(0.6)
    assert r["guardrails"]["baseline"]["trials"] == 5


def test_tool_calls_metric():
    base = {"trials": [trial("a", 0, True, calls=20)]}
    cand = {"trials": [{"task": "a", "trial": 0, "passed": True}]}
    r = compare.compare(base, cand, "tool_calls")
    assert r["per_task"]["a"] == {"baseline": 20.0, "candidate": 0.0, "delta": -20.0}


def test_unknown_metric_and_no_shared_tasks():
    with pytest.raises(CliError, match="unknown metric latency; expected one of pass_rate"):
        compare.compare({"trials": []}, {"trials": []}, "latency")
    with pytest.raises(CliError, match="share no tasks"):
        compare.compare({"trials": [trial("a", 0, True)]}, {"trials": [trial("b", 0, True)]})


def test_load_errors(tmp_path):
    with pytest.raises(CliError, match="cannot read run results"):
        compare.load(tmp_path / "missing")
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(CliError, match="cannot read run results"):
        compare.load(tmp_path / "bad.json")


def test_cli_errors_exit_1(runs, tmp_path):
    from unit_helpers import run_cli
    base, cand = runs
    rc, out = run_cli("compare", str(base), str(cand), "--metric", "nope")
    assert rc == 1 and out.startswith("error: unknown metric nope")
    other = run(tmp_path, "other", [trial("zzz", 0, True)])
    rc, out = run_cli("compare", str(base), str(other), "--json")
    assert rc == 1 and json.loads(out) == {"error": "the two runs share no tasks"}


def test_cli_text_output(runs):
    from unit_helpers import run_cli
    base, cand = runs
    rc, out = run_cli("compare", str(base), str(cand), "--seed", "1")
    assert rc == 0
    first, second = out.splitlines()
    assert first.startswith("pass_rate: 0.667 -> 0.667 (delta +0.000, 95% CI [") and first.endswith("3 tasks)")
    assert second == "safety violations 0 -> 1; cost $0.60 -> $0.45; tasks lost: b"
    rc, out = run_cli("compare", str(base), str(base))
    assert out.splitlines()[1].endswith("tasks lost: none")

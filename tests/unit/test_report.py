"""report/: the `harness-evo report pr|run` CLI and the PR comment renderer's branches."""
from __future__ import annotations

import json

import pytest

from harness_evo.report import pr as pr_report, run as run_report
from unit_helpers import run_cli


def finding(fid, rule, severity="medium", gating=False, **extra):
    return {"id": fid, "rule": rule, "kind": "outcome", "severity": severity, "gating": gating,
            "summary": f"Harness note: {rule} happened.", "evidence": [{"type": "diff", "ref": f"src/{rule}.py:1"}],
            "context": [], **extra}


def pr_result(**over):
    base = {
        "version": 1, "harness_evo": "0.1.0", "kind": "agent", "base": "main", "head": "0123456789abcdef",
        "sessions": ["s-1"], "missing_traces": [], "verdict": "needs attention", "reason": "tests weakened",
        "exit_code": 0,
        "findings": [finding("pr:test_weakening:aaaa", "test_weakening"),
                     finding("pr:diff_size:bbbb", "harness_change", severity="info")],
        "trajectory": {"sessions": {"s-1": {"metrics": {}}}},
        "session_summary": {"sessions": 1, "tool_calls": 7, "retries": 1, "tests_run": 2, "nudges_issued": 1,
                            "nudges_followed": 1, "cost_usd": 0.1234},
        "what_went_well": ["No test was weakened."], "model_graders": None,
        "graders": {"tests_pass": "1", "trajectory": "1"},
    }
    base.update(over)
    return base


def run_results():
    trials = [
        {"task": "t1", "trial": 0, "passed": True, "tool_calls": 10, "cost_usd": 0.5, "safety_violations": 0,
         "trajectory": {"checks": [{"grader": "test_after_last_edit", "passed": True}], "findings": [{"rule": "D1"}]}},
        {"task": "t1", "trial": 1, "passed": False, "tool_calls": 12, "cost_usd": 0.25, "safety_violations": 1,
         "trajectory": {"checks": [{"grader": "test_after_last_edit", "passed": False}],
                        "findings": [{"rule": "D1"}, {"rule": "D4"}]}},
        {"task": "t2", "trial": 0, "passed": True, "tool_calls": 3, "cost_usd": None,
         "trajectory": {"checks": [], "findings": []}},
    ]
    return {"run_id": "run-7", "harness": {"name": "baseline", "version": "1.2", "config_sha256": "abc123"},
            "suite": "regression", "split": "dev", "sandbox": "local", "trials": trials,
            "graders": {"tests_pass": "1"}, "skipped": [{"task": "draft-1"}], "harness_evo": "0.1.0",
            "started": "2026-10-27T00:00:00Z", "ended": "2026-10-27T00:05:00Z"}


@pytest.fixture
def pr_json(tmp_path):
    p = tmp_path / "pr.json"
    p.write_text(json.dumps(pr_result()))
    return p


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    (d / "results.json").write_text(json.dumps(run_results()))
    return d


def test_report_pr_to_stdout(pr_json):
    rc, out = run_cli("report", "pr", str(pr_json))
    assert rc == 0
    assert "needs attention" in out and "tests weakened" in out
    assert "**medium** · test_weakening: test_weakening happened." in out, "the 'Harness note:' prefix is dropped"
    assert out.index("test_weakening happened") < out.index("harness_change happened"), "info findings go last"
    assert "| 1 | 7 | 1 | 2 | 1 | 1 | $0.12 |" in out
    assert "- No test was weakened." in out and "0123456" in out and "tests_pass@1, trajectory@1" in out


def test_report_pr_to_file(pr_json, tmp_path):
    out_file = tmp_path / "comment.md"
    rc, out = run_cli("report", "pr", str(pr_json), "--out", str(out_file))
    assert rc == 0 and out == ""
    assert "test_weakening happened" in out_file.read_text()


def test_report_run_to_stdout_and_file(run_dir, tmp_path):
    rc, out = run_cli("report", "run", str(run_dir))
    assert rc == 0
    assert "run-7" in out and "baseline 1.2" in out and "abc123" in out
    assert "| t1 | 2 | 0.50 | 0.50 | 11.0 | D1×2, D4×1 |" in out
    assert "| t2 | 1 | 1.00 | - | 3.0 | - |" in out
    assert "- Skipped (not validated): draft-1" in out
    assert "D1: 2, D4: 1" in out and "$0.75" in out
    out_file = tmp_path / "run.md"
    rc, out = run_cli("report", "run", str(run_dir), "--out", str(out_file))
    assert rc == 0 and out == "" and "run-7" in out_file.read_text()


def test_run_report_with_no_trials(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "results.json").write_text(json.dumps({"run_id": "e", "harness": {"name": "h"}, "trials": []}))
    text = run_report.render(d)
    assert "n/a" in text and "| - | - | - | - | - | - |" in text and "None." in text


def test_pr_render_untraced_with_missing_traces():
    text = pr_report.render(pr_result(kind="untraced", trajectory=None, missing_traces=["s-9"], findings=[],
                                      what_went_well=[]))
    assert "No `Harness-Session` trailer" in text and "Traces not found for: s-9." in text
    assert "No trace data for this PR." in text and "None." in text and "- Nothing stood out." in text


def test_pr_render_harness_change_and_unknown_cost():
    s = {**pr_result()["session_summary"], "cost_usd": None}
    text = pr_report.render(pr_result(kind="harness_change", session_summary=s))
    assert "Harness-change PR" in text and "| n/a |" in text


def test_pr_render_tags_and_context():
    f = finding("s-1#4:D6", "D6", gating=True, name="test-weakening", flag_for_pr=True,
                context=["#3 tool_call Edit tests/x.py", "#4 tool_result Edit"])
    f["evidence"] = [{"type": "trace", "ref": f"s-1 #{i}"} for i in range(8)]
    m = finding("pr:model:x:cc", "model:x", severity="info", calibrated=False)
    text = pr_report.render(pr_result(findings=[f, m]))
    assert "D6 test-weakening (fails the check; flagged live)" in text
    assert "and 2 more" in text
    assert "<details><summary>Surrounding events</summary>" in text and "   #3 tool_call Edit tests/x.py" in text
    assert "(uncalibrated model grader, informational)" in text


def test_pr_render_carries_ticks_over_from_the_previous_comment():
    previous = ("- [x] confirmed <!-- finding:pr:test_weakening:aaaa -->\n"
                "- [ ] false positive <!-- finding:pr:test_weakening:aaaa -->\n"
                "- [x] false positive <!-- finding:gone#1:D1 -->\n")
    text = pr_report.render(pr_result(), previous)
    assert "- [x] confirmed <!-- finding:pr:test_weakening:aaaa -->" in text
    assert "- [ ] false positive <!-- finding:pr:test_weakening:aaaa -->" in text
    assert "gone#1:D1" not in text


def test_pr_render_model_grader_notes():
    skipped = pr_report.render(pr_result(model_graders={"skipped": "the anthropic SDK is not installed", "results": []}))
    assert "_Model graders skipped: the anthropic SDK is not installed._" in skipped
    ran = pr_report.render(pr_result(model_graders={"spent_usd": 0.0123, "budget_usd": 0.5, "results": [
        {"grader": "over_engineering", "calibrated": False}, {"grader": "convention_fit", "calibrated": True}]}))
    assert "_Model graders: $0.012 of $0.50 budget. Not yet calibrated (shown for information only): " \
           "over_engineering._" in ran
    calibrated = pr_report.render(pr_result(model_graders={"spent_usd": 0, "budget_usd": 0.5, "results": [
        {"grader": "convention_fit", "calibrated": True}]}))
    assert "_Model graders: $0.000 of $0.50 budget._" in calibrated

"""diagnose.py: PR-result attribution via labels, the taxonomy loader, markdown output, and --rerun."""
from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from harness_evo import diagnose
from harness_evo.cli import CliError
from unit_helpers import git, run_cli

BUGGY = 'def total(lines, discount=0):\n    return round(sum(l["price"] * l["qty"] for l in lines) + discount, 2)\n'
FIXED = BUGGY.replace("+ discount", "- discount")


def write_labels(path, *recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return path


def pr_file(path, findings, head="abcdef1234567"):
    path.write_text(json.dumps({"head": head, "findings": findings}))
    return path


@pytest.fixture
def pr_inputs(tmp_path):
    findings = [
        {"id": "s-1#3:D4", "rule": "D4", "kind": "trajectory"},
        {"id": "pr:tests_pass:aaaa1111", "rule": "tests_pass"},                         # kind defaults to outcome
        {"id": "pr:model:over_engineering:bbbb2222", "rule": "model:over_engineering", "kind": "model"},
        {"id": "pr:diff_size:cccc3333", "rule": "diff_size", "kind": "outcome"},        # never labelled
        {"id": "s-1#9:D1", "rule": "D1", "kind": "trajectory"},                          # labelled acceptable
    ]
    pr = pr_file(tmp_path / "pr.json", findings)
    labels = write_labels(tmp_path / "labels.jsonl",
                          {"finding": "s-1#3:D4", "label": "confirmed", "pr": 12},
                          {"finding": "pr:tests_pass:aaaa1111", "label": "false_positive", "pr": 12},
                          {"finding": "pr:model:over_engineering:bbbb2222", "label": "confirmed", "pr": None},
                          {"finding": "s-1#9:D1", "label": "acceptable", "pr": 12})
    return pr, labels


def test_pr_findings_are_attributed_from_labels(pr_inputs):
    pr, labels = pr_inputs
    d = diagnose.diagnose([], [pr], None, labels)
    # `acceptable` on a PR finding is a grader cause, the same as on a run
    assert d["attribution"] == {"pr:s-1#3:D4": "agent", "pr:pr:tests_pass:aaaa1111": "grader",
                                "pr:pr:model:over_engineering:bbbb2222": "agent", "pr:s-1#9:D1": "grader"}
    assert d["counts"] == {"agent": 2, "grader": 2}
    assert d["human_queue"] == [{"trial": "pr:pr:tests_pass:aaaa1111", "cause": "grader",
                                 "reason": "a reviewer marked the finding a false positive"},
                                {"trial": "pr:s-1#9:D1", "cause": "grader",
                                 "reason": "a reviewer judged the flagged work acceptable"}]
    clusters = {c["id"]: c for c in d["clusters"]}
    assert set(clusters) == {"skipped-verification", "over-engineering"}
    assert clusters["skipped-verification"]["prs"] == ["12"] and clusters["skipped-verification"]["tasks"] == []
    assert clusters["over-engineering"]["prs"] == ["abcdef1"], "no PR number: the head sha identifies the PR"
    assert all(not c["actionable"] for c in d["clusters"])
    assert d["thresholds"] == {"min_occurrences": 3, "min_sources": 2}


def test_confirmed_pr_findings_across_prs_become_actionable(tmp_path):
    prs, recs = [], []
    for n in (1, 2, 3):
        fid = f"s-{n}#4:D4"
        prs.append(pr_file(tmp_path / f"pr{n}.json", [{"id": fid, "rule": "D4", "kind": "trajectory"}]))
        recs.append({"finding": fid, "label": "confirmed", "pr": n})
    d = diagnose.diagnose([], prs, None, write_labels(tmp_path / "l.jsonl", *recs))
    [c] = d["clusters"]
    assert c["id"] == "skipped-verification" and c["occurrences"] == 3 and c["prs"] == ["1", "2", "3"]
    assert c["actionable"] is True and c["examples"] == ["pr:s-1#4:D4", "pr:s-2#4:D4", "pr:s-3#4:D4"]


def test_markdown_output_and_out_file(pr_inputs, tmp_path):
    pr, labels = pr_inputs
    out_json = tmp_path / "diag.json"
    rc, out = run_cli("diagnose", "--prs", str(pr), "--labels", str(labels), "--out", str(out_json))
    assert rc == 0
    assert out.startswith("# Diagnosis\n\nCauses: agent 2, grader 2\n")
    assert "## Skipped verification (skipped-verification): 1 occurrence(s), watch" in out
    assert "- Seen in: PR 12" in out
    assert "- Candidate intervention: " in out and "- Rule out first: " in out
    assert "## Queued for a human" in out
    assert "- `pr:pr:tests_pass:aaaa1111`: grader (a reviewer marked the finding a false positive)" in out
    assert json.loads(out_json.read_text())["counts"] == {"agent": 2, "grader": 2}


def test_markdown_marks_actionable_clusters(tmp_path):
    d = {"counts": {"agent": 3}, "human_queue": [],
         "clusters": [{"id": "x", "name": "X", "occurrences": 3, "actionable": True, "tasks": ["a", "b"],
                       "prs": ["7"], "intervention": "do y", "rule_out": "z"}]}
    text = diagnose.render(d)
    assert "## X (x): 3 occurrence(s), **actionable**" in text and "- Seen in: a, b, PR 7" in text
    assert "Queued for a human" not in text


def test_taxonomy_without_markers_is_rejected(tmp_path, pr_inputs):
    pr, labels = pr_inputs
    bad = tmp_path / "tax.md"
    bad.write_text("| ID | Cluster | Signals | Detection | Intervention | Rule out |\n| a | A | `detector:D1` | x | y | z |\n")
    with pytest.raises(CliError, match="taxonomy:start / taxonomy:end"):
        diagnose.load_taxonomy(bad)
    rc, out = run_cli("diagnose", "--prs", str(pr), "--taxonomy", str(bad))
    assert rc == 1 and out.startswith("error: failure taxonomy is missing")
    rc, out = run_cli("diagnose", "--prs", str(pr), "--taxonomy", str(bad), "--json")
    assert rc == 1 and "taxonomy:start" in json.loads(out)["error"]


def test_taxonomy_loader_skips_short_and_separator_rows(tmp_path):
    tax = tmp_path / "tax.md"
    tax.write_text("<!-- taxonomy:start -->\n| ID | Cluster | Signals | D | I | R |\n| --- | --- | --- | --- | --- | --- |\n"
                   "| short | row |\n| ok | OK | `detector:D1` `metric:repeated_reads` | d | i | r |\n<!-- taxonomy:end -->\n")
    assert diagnose.load_taxonomy(tax) == [{"id": "ok", "name": "OK", "signals": ["detector:D1", "metric:repeated_reads"],
                                            "detection": "d", "intervention": "i", "rule_out": "r"}]
    assert len(diagnose.load_taxonomy()) == 8


def test_diagnose_needs_runs_or_prs():
    rc, out = run_cli("diagnose")
    assert rc == 1 and out == "error: give --runs and/or --prs\n"


def test_trial_signals_cover_every_kind():
    t = {"passed": False, "source": "real_failure", "outcome": [{"grader": "tests_pass", "passed": False}],
         "trajectory": {"findings": [{"rule": "D8"}], "checks": [{"grader": "read_before_edit", "passed": False},
                                                                 {"grader": "x", "passed": True}],
                        "metrics": {"max_reads_per_file": 3}},
         "efficiency": {"over_budget": ["max_tool_calls"]}, "model_flags": ["over_engineering"],
         "reference_lines": 10, "diff_lines": 31}
    assert diagnose.trial_signals(t) == {
        "detector:D8", "grader:read_before_edit", "outcome:tests_pass", "efficiency:max_tool_calls",
        "model:over_engineering", "metric:diff_much_larger_than_reference", "metric:repeated_reads",
        "failed_source:real_failure"}
    assert diagnose.trial_signals({}) == set()


def test_is_flagged():
    assert diagnose.is_flagged({"passed": False}) is True
    assert diagnose.is_flagged({"passed": True, "outcome": [{"grader": "lint", "passed": False}]}) is False
    assert diagnose.is_flagged({"passed": True, "trajectory": {"findings": [{"rule": "D1"}]}}) is True


def test_attribute_reference_failure_is_a_task_cause():
    assert diagnose.attribute("k", {"task": "t", "reference_failed": True}, {}, {}) == \
        ("task", "the reference solution fails the graders")
    assert diagnose.attribute("k", {"task": "t"}, {}, {"k": {"label": "false_positive"}})[0] == "grader"
    assert diagnose.attribute("k", {"task": "t", "repeat_outcomes": [True, True]}, {}, {}) == ("agent", "")


# ---------------------------------------------------------------- --rerun against a real task repo

@pytest.fixture
def flaky_corpus(tmp_path):
    """A validated task whose test command alternates pass/fail on the same snapshot (a flaky environment)."""
    repo = tmp_path / "task-repo"
    (repo / "src" / "billing").mkdir(parents=True)
    (repo / "src" / "billing" / "invoice.py").write_text(BUGGY)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "buggy")
    sha = git(repo, "rev-parse", "HEAD").strip()
    (repo / "src" / "billing" / "invoice.py").write_text(FIXED)
    patch = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True, check=True).stdout
    git(repo, "checkout", "-q", "--", ".")

    counter = tmp_path / "counter"
    flaky = (f"n=$(cat {counter} 2>/dev/null || echo 0); echo $((n + 1)) > {counter}; "
             f"[ $((n % 2)) -eq 0 ] && grep -q 'l in lines) - discount' src/billing/invoice.py")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    task = {"id": "flaky", "status": "validated", "source": "real_failure", "split": "dev",
            "repo": {"url": str(repo), "commit": sha}, "prompt": "fix it", "reference": "fix.diff",
            "graders": {"outcome": [{"tests_pass": {"command": flaky}}]}}
    (corpus / "flaky.yaml").write_text(yaml.safe_dump(task))
    (corpus / "fix.diff").write_text(patch)

    run = tmp_path / "run"
    (run / "diffs").mkdir(parents=True)
    (run / "diffs" / "flaky-0.diff").write_text(patch)
    trial = {"task": "flaky", "trial": 0, "passed": False, "outcome": [{"grader": "tests_pass", "passed": False}],
             "trajectory": {"findings": [{"rule": "D4"}], "checks": []}}
    trials = [{**trial, "final_diff": "diffs/flaky-0.diff"},
              {**trial, "trial": 1},                                              # no final diff: no rerun
              {**trial, "trial": 2, "final_diff": "diffs/missing.diff"},          # diff file missing
              {**trial, "trial": 3, "task": "not-in-corpus", "final_diff": "diffs/flaky-0.diff"},
              {**trial, "trial": 4, "repeat_outcomes": [True, True], "final_diff": "diffs/flaky-0.diff"}]
    (run / "results.json").write_text(json.dumps({"run_id": "r1", "trials": trials}))
    return run, corpus, counter


def test_rerun_detects_a_flaky_environment(flaky_corpus):
    run, corpus, counter = flaky_corpus
    rc, out = run_cli("diagnose", "--runs", str(run), "--corpus", str(corpus), "--rerun", "--json")
    assert rc == 0, out
    d = json.loads(out)
    assert d["attribution"]["r1:flaky#0"] == "environment"
    assert counter.read_text().strip() == "2", "the trial's diff was re-graded twice"
    assert d["attribution"]["r1:flaky#1"] == "agent"
    assert d["attribution"]["r1:flaky#2"] == "agent"
    assert d["attribution"]["r1:not-in-corpus#3"] == "agent"
    assert d["attribution"]["r1:flaky#4"] == "agent", "existing repeat outcomes are not re-run"


def test_rerun_flaky_without_corpus_or_diff_returns_none(tmp_path):
    assert diagnose._rerun_flaky(tmp_path, {"task": "t", "final_diff": "x.diff"}, None) is None
    assert diagnose._rerun_flaky(tmp_path, {"task": "t"}, tmp_path) is None


def test_runs_accept_a_results_file_path(flaky_corpus):
    run, corpus, _ = flaky_corpus
    d = diagnose.diagnose([run / "results.json"], [], corpus, None)
    assert d["attribution"]["r1:flaky#0"] == "agent"
    assert "r1:flaky#4" in d["attribution"]

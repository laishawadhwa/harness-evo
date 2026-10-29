"""Spec sections 6 (detector fate), 9 (diagnosis) and 10 (harness improvement loop)."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from bhelpers import ADVISE_ALL, ClaudeCode, run_cli, set_policy
from corpus_fixtures import (FIX_AND_TEST, FIX_NO_TEST, FIXED, GIVE_UP, make_patch, make_task_repo,
                             scripted_harness, write_task)

SKILL = Path(__file__).resolve().parents[2] / "agent-harness-evolution"


# ---------------------------------------------------------------- helpers

def fake_run(path: Path, name: str, outcomes: dict[str, list[bool]], *, safety: int = 0,
             cost: float = 0.10, split: str = "dev") -> Path:
    trials = []
    for task, results in outcomes.items():
        for i, ok in enumerate(results):
            trials.append({"task": task, "trial": i, "passed": ok, "safety_violations": safety if i == 0 else 0,
                           "cost_usd": cost, "tool_calls": 10, "latency_s": 1.0,
                           "outcome": [{"grader": "tests_pass", "passed": ok}],
                           "trajectory": {"findings": [], "checks": []}, "efficiency": {"over_budget": []}})
    path.mkdir(parents=True, exist_ok=True)
    (path / "results.json").write_text(json.dumps({
        "run_id": path.name, "harness": {"name": name, "version": "1"}, "split": split,
        "graders": {"tests_pass": "1"}, "trials": trials}))
    return path


def arm(session: str) -> str:
    return "on" if int(hashlib.sha256(session.encode()).hexdigest(), 16) % 2 == 0 else "off"


def labels(root: Path, rule: str, confirmed: int, total: int) -> None:
    path = root / ".harness-evo" / "labels.jsonl"
    with path.open("a") as f:
        for i in range(total):
            f.write(json.dumps({"finding": f"s-l#{i}:{rule}", "rule": rule, "session": "s-l", "seq": i,
                                "label": "confirmed" if i < confirmed else "false_positive",
                                "source": "test", "ts": f"2026-10-{1 + i // 10:02d}T00:00:{i % 60:02d}Z"}) + "\n")


# ---------------------------------------------------------------- section 6: detector fate

def test_low_precision_detector_is_demoted_one_level(project):
    set_policy(project, ADVISE_ALL)
    labels(project, "D1", confirmed=30, total=50)          # 60% < 70%
    rc, out = run_cli("calibrate", "--project-dir", str(project), "--apply", "--json")
    report = json.loads(out)
    assert rc == 0
    assert report["detectors"]["D1"]["precision"] == pytest.approx(0.6)
    assert report["detectors"]["D1"]["action"] == "demoted"
    assert report["detectors"]["D1"]["flagged_for_review"] is True
    agent = ClaudeCode(project, session="s-after-demotion")
    agent.start()
    call = agent.edit("src/util/strings.py", "upper()", "upper() ")
    assert call.context is None
    assert agent.nudges("D1")[0]["payload"]["level"] == "info"


def test_promotion_requires_the_precision_bar(project):
    rc, _ = run_cli("promote", "D3", "--project-dir", str(project))
    assert rc == 1, "no precision number, no promotion"
    labels(project, "D3", confirmed=40, total=50)          # 80%
    rc, out = run_cli("promote", "D3", "--project-dir", str(project), "--json")
    assert rc == 0 and json.loads(out)["level"] == "advise"


def test_nudge_ab_assignment_by_session_hash(project):
    set_policy(project, ADVISE_ALL + "experiment:\n  nudge_ab: true\n")
    on = next(f"s-ab-{i}" for i in range(100) if arm(f"s-ab-{i}") == "on")
    off = next(f"s-ab-{i}" for i in range(100) if arm(f"s-ab-{i}") == "off")
    results = {}
    for sid in (on, off):
        agent = ClaudeCode(project, session=sid)
        agent.start()
        results[sid] = agent.edit("src/util/strings.py", "upper()", "upper() ")
        assert agent.nudges("D1")[0]["payload"]["arm"] == arm(sid)
        assert agent.bash("git reset --hard").denied, "safety denies apply in both arms"
        agent.end()
    assert results[on].context is not None
    assert results[off].context is None
    rc, out = run_cli("ab-report", "--project-dir", str(project), "--json")
    report = json.loads(out)
    assert report["arms"]["on"]["sessions"] == 1 and report["arms"]["off"]["sessions"] == 1
    assert {"test_after_edit_rate", "rework"} <= report["arms"]["on"].keys()


# ---------------------------------------------------------------- section 10: stats and decisions

def test_bootstrap_comparison_is_paired_and_reproducible(tmp_path):
    base = fake_run(tmp_path / "base", "baseline", {f"t{i}": [False, False, True] for i in range(8)})
    cand = fake_run(tmp_path / "cand", "candidate", {f"t{i}": [True, True, True] for i in range(8)})
    rc1, out1 = run_cli("compare", str(base), str(cand), "--seed", "7", "--json")
    rc2, out2 = run_cli("compare", str(base), str(cand), "--seed", "7", "--json")
    assert rc1 == 0 and out1 == out2
    cmp = json.loads(out1)
    assert cmp["delta"] == pytest.approx(2 / 3)
    lo, hi = cmp["ci"]
    assert 0 < lo <= cmp["delta"] <= hi
    assert set(cmp["per_task"]) == {f"t{i}" for i in range(8)}


def decide(tmp_path, dev_b, dev_c, ho_b=None, ho_c=None) -> dict:
    args = ["decide", "--dev-baseline", str(dev_b), "--dev-candidate", str(dev_c), "--seed", "1", "--json"]
    if ho_b:
        args += ["--heldout-baseline", str(ho_b), "--heldout-candidate", str(ho_c)]
    rc, out = run_cli(*args)
    assert rc == 0
    return json.loads(out)


def test_decision_rule_keep(tmp_path):
    tasks = [f"t{i}" for i in range(8)]
    b = fake_run(tmp_path / "b", "base", {t: [False, True, False] for t in tasks})
    c = fake_run(tmp_path / "c", "cand", {t: [True, True, True] for t in tasks})
    assert decide(tmp_path, b, c)["decision"] == "run_heldout"
    hb = fake_run(tmp_path / "hb", "base", {"h1": [True, False, True]}, split="heldout")
    hc = fake_run(tmp_path / "hc", "cand", {"h1": [True, True, True]}, split="heldout")
    assert decide(tmp_path, b, c, hb, hc)["decision"] == "keep"


def test_decision_rule_revise_when_interval_includes_zero(tmp_path):
    b = fake_run(tmp_path / "b", "base", {"t1": [True, False, True], "t2": [True, True, True],
                                          "t3": [False, False, True], "t4": [True, True, False]})
    c = fake_run(tmp_path / "c", "cand", {"t1": [True, True, True], "t2": [True, False, True],
                                          "t3": [True, False, True], "t4": [True, True, False]})
    assert decide(tmp_path, b, c)["decision"] == "revise"


def test_decision_rule_rollback_on_guardrail_or_heldout_regression(tmp_path):
    tasks = [f"t{i}" for i in range(8)]
    b = fake_run(tmp_path / "b", "base", {t: [False, False, False] for t in tasks})
    unsafe = fake_run(tmp_path / "c", "cand", {t: [True, True, True] for t in tasks}, safety=1)
    d = decide(tmp_path, b, unsafe)
    assert d["decision"] == "rollback" and any("safety" in r for r in d["reasons"])

    good = fake_run(tmp_path / "c2", "cand", {t: [True, True, True] for t in tasks})
    hb = fake_run(tmp_path / "hb", "base", {"h1": [True, True, True], "h2": [True, True, True]}, split="heldout")
    hc = fake_run(tmp_path / "hc", "cand", {"h1": [False, False, False], "h2": [True, True, True]}, split="heldout")
    d = decide(tmp_path, b, good, hb, hc)
    assert d["decision"] == "rollback" and any("held-out" in r for r in d["reasons"])


def test_regression_suite_may_lose_at_most_one_task(tmp_path):
    base = {f"t{i}": [False, False, False] for i in range(8)}
    base.update({"r1": [True] * 3, "r2": [True] * 3})
    cand = {f"t{i}": [True, True, True] for i in range(8)}
    cand.update({"r1": [False] * 3, "r2": [False] * 3})
    b = fake_run(tmp_path / "b", "base", base)
    c = fake_run(tmp_path / "c", "cand", cand)
    d = decide(tmp_path, b, c)
    assert d["decision"] == "rollback" and any("lost" in r for r in d["reasons"])


# ---------------------------------------------------------------- section 9: diagnosis

def diag_trial(task, i, *, passed=False, detectors=(), checks=(), **extra):
    return {"task": task, "trial": i, "passed": passed, "safety_violations": 0, "cost_usd": 0.1, "tool_calls": 9,
            "outcome": [{"grader": "tests_pass", "passed": passed}],
            "trajectory": {"findings": [{"rule": d} for d in detectors],
                           "checks": [{"grader": g, "passed": False} for g in checks]},
            "efficiency": {"over_budget": []}, **extra}


def diag_setup(tmp_path, taxonomy: Path | None = None):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for tid, status in {"a": "validated", "b": "validated", "c": "validated", "draft-task": "draft"}.items():
        (corpus / f"{tid}.yaml").write_text(yaml.safe_dump({"id": tid, "status": status, "split": "dev"}))
    trials = [
        diag_trial("a", 0, detectors=["D4"], checks=["test_after_last_edit"]),
        diag_trial("a", 1, detectors=["D4"]),
        diag_trial("b", 0, detectors=["D4"]),
        diag_trial("c", 0, detectors=["D1"]),
        diag_trial("c", 1, detectors=["D1"]),
        diag_trial("draft-task", 0, detectors=["D4"]),                    # task cause
        diag_trial("b", 1, detectors=["D4"], repeat_outcomes=[True, False]),  # environment cause
        diag_trial("c", 2, detectors=["D1"]),                              # human says acceptable: grader cause
    ]
    run = tmp_path / "run"
    run.mkdir()
    (run / "results.json").write_text(json.dumps({"run_id": "run", "harness": {"name": "h", "version": "1"},
                                                  "split": "dev", "trials": trials}))
    lab = tmp_path / "labels.jsonl"
    lab.write_text(json.dumps({"trial": "run:c#2", "label": "acceptable", "source": "human"}) + "\n")
    args = ["diagnose", "--runs", str(run), "--corpus", str(corpus), "--labels", str(lab), "--json"]
    if taxonomy:
        args += ["--taxonomy", str(taxonomy)]
    rc, out = run_cli(*args)
    assert rc == 0
    return json.loads(out)


def test_attribution_before_blame(tmp_path):
    d = diag_setup(tmp_path)
    attr = d["attribution"]
    assert attr["run:draft-task#0"] == "task"
    assert attr["run:b#1"] == "environment"
    assert attr["run:c#2"] == "grader"
    assert attr["run:a#0"] == "agent"
    queued = {q["trial"] for q in d["human_queue"]}
    assert {"run:c#2", "run:draft-task#0"} <= queued


def test_clusters_need_three_occurrences_across_two_tasks(tmp_path):
    d = diag_setup(tmp_path)
    clusters = {c["id"]: c for c in d["clusters"]}
    sv = clusters["skipped-verification"]
    assert sv["occurrences"] == 3 and sorted(sv["tasks"]) == ["a", "b"] and sv["actionable"] is True
    assert sv["intervention"]
    ebc = clusters["edits-before-context"]
    assert ebc["occurrences"] == 2 and ebc["actionable"] is False   # c#2 was grader-caused
    for cause in ("task", "environment", "grader"):
        assert all(cause not in c.get("causes", []) for c in d["clusters"])


def test_taxonomy_is_extended_by_editing_the_file(tmp_path):
    taxonomy = tmp_path / "failure-taxonomy.md"
    text = (SKILL / "analysis" / "failure-taxonomy.md").read_text()
    row = ("| shotgun-debugging | Shotgun debugging | `detector:D1` | Many edits with no reads. "
           "| Ask for a hypothesis first. | The fix really was one line. |\n")
    marker = "<!-- taxonomy:end -->"
    assert marker in text
    taxonomy.write_text(text.replace(marker, row + marker))
    d = diag_setup(tmp_path, taxonomy=taxonomy)
    assert "shotgun-debugging" in {c["id"] for c in d["clusters"]}


# ---------------------------------------------------------------- runner and full experiment

@pytest.fixture
def corpus(tmp_path):
    repo, sha = make_task_repo(tmp_path / "repo")
    suite = tmp_path / "suites" / "regression"
    make_patch(repo, "src/billing/invoice.py", FIXED, suite / "patches" / "fix.diff")
    for i in range(3):
        write_task(suite, f"billing-{i}", repo, sha, patch="patches/fix.diff", status="validated")
    write_task(suite, "billing-heldout", repo, sha, patch="patches/fix.diff", split="heldout", status="validated")
    return suite


def test_run_suite_with_scripted_agent(tmp_path, corpus):
    harness = scripted_harness(tmp_path / "good.yaml", "good", {}, default=FIX_AND_TEST)
    out = tmp_path / "runs" / "good"
    rc, _ = run_cli("run-suite", "--suite", str(corpus), "--harness", str(harness), "--trials", "2",
                    "--split", "dev", "--out", str(out), "--sandbox", "local")
    assert rc == 0
    results = json.loads((out / "results.json").read_text())
    assert results["harness"]["name"] == "good" and results["split"] == "dev"
    assert results["graders"], "every result is stamped with grader versions"
    assert len(results["trials"]) == 6
    for t in results["trials"]:
        assert t["passed"] is True
        checks = {c["grader"]: c["passed"] for c in t["trajectory"]["checks"]}
        assert checks == {"read_before_edit": True, "test_after_last_edit": True, "no_destructive_actions": True}
        assert t["tool_calls"] >= 2
        assert (out / t["trace"]).exists()


def test_scripted_agent_without_verification_is_flagged(tmp_path, corpus):
    harness = scripted_harness(tmp_path / "lazy.yaml", "lazy", {}, default=FIX_NO_TEST)
    out = tmp_path / "runs" / "lazy"
    run_cli("run-suite", "--suite", str(corpus), "--harness", str(harness), "--trials", "1",
            "--split", "dev", "--out", str(out), "--sandbox", "local")
    trial = json.loads((out / "results.json").read_text())["trials"][0]
    assert trial["passed"] is True                         # outcome is fine...
    checks = {c["grader"]: c["passed"] for c in trial["trajectory"]["checks"]}
    assert checks["test_after_last_edit"] is False         # ...but the process was not
    assert {"D4", "D5"} <= {f["rule"] for f in trial["trajectory"]["findings"]}


def test_experiment_runs_dev_then_heldout_and_records_result(tmp_path, corpus):
    base = scripted_harness(tmp_path / "base.yaml", "base", {}, default=GIVE_UP)
    cand = scripted_harness(tmp_path / "cand.yaml", "cand", {}, default=FIX_AND_TEST)
    rc, out = run_cli("experiment", "new", "fix-rate", "--dir", str(tmp_path / "interventions"),
                      "--cluster", "skipped-verification")
    exp = tmp_path / "interventions" / "fix-rate.md"
    assert rc == 0 and exp.exists()
    text = exp.read_text()
    meta = {"baseline": str(base), "candidate": str(cand), "suite": str(corpus), "trials": 2,
            "target": {"metric": "pass_rate", "margin": 0.2}, "sandbox": "local"}
    text = text.replace("```yaml experiment\n", "```yaml experiment\n" + yaml.safe_dump(meta), 1)
    exp.write_text(text)
    rc, out = run_cli("experiment", "run", str(exp), "--json", "--seed", "3")
    result = json.loads(out)
    assert rc == 0
    assert result["decision"] == "keep"
    assert result["heldout"] is not None, "held-out runs once, at decision time"
    final = exp.read_text()
    assert "- Result: keep" in final


def test_experiment_refuses_a_decision_rule_changed_after_the_run_started(tmp_path, corpus):
    base = scripted_harness(tmp_path / "base.yaml", "base", {}, default=GIVE_UP)
    cand = scripted_harness(tmp_path / "cand.yaml", "cand", {}, default=FIX_AND_TEST)
    run_cli("experiment", "new", "moving-goalposts", "--dir", str(tmp_path / "x"))
    exp = tmp_path / "x" / "moving-goalposts.md"
    meta = {"baseline": str(base), "candidate": str(cand), "suite": str(corpus), "trials": 1,
            "target": {"metric": "pass_rate", "margin": 0.2}, "sandbox": "local"}
    exp.write_text(exp.read_text().replace("```yaml experiment\n", "```yaml experiment\n" + yaml.safe_dump(meta), 1))
    rc, _ = run_cli("experiment", "lock", str(exp))
    assert rc == 0
    exp.write_text(exp.read_text().replace("margin: 0.2", "margin: 0.01"))
    rc, out = run_cli("experiment", "run", str(exp), "--json")
    assert rc == 1 and "changed" in out.lower()

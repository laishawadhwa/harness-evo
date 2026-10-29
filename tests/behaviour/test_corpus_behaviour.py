"""Spec section 8: eval corpus, task schema and task admission."""
from __future__ import annotations

import json

import yaml

from bhelpers import run_cli
from corpus_fixtures import FIXED, BUGGY, flaky_test, make_patch, make_task_repo, write_task


def test_task_schema_rejects_malformed_tasks(tmp_path):
    repo, sha = make_task_repo(tmp_path / "repo")
    suite = tmp_path / "suite"
    patch = make_patch(repo, "src/billing/invoice.py", FIXED, suite / "patches" / "ok.diff")
    good = write_task(suite, "billing-ok", repo, sha, patch="patches/ok.diff")
    bad = yaml.safe_load(good.read_text())
    del bad["prompt"]
    bad["split"] = "train"
    bad["id"] = "billing-bad"
    (suite / "billing-bad.yaml").write_text(yaml.safe_dump(bad))

    rc, out = run_cli("corpus", "validate", str(suite), "--json")
    report = json.loads(out)
    assert rc == 1
    assert report["tasks"]["billing-ok"]["errors"] == []
    errors = " ".join(report["tasks"]["billing-bad"]["errors"])
    assert "prompt" in errors and "split" in errors


def test_reference_that_passes_twice_is_validated(tmp_path):
    repo, sha = make_task_repo(tmp_path / "repo")
    suite = tmp_path / "suite"
    make_patch(repo, "src/billing/invoice.py", FIXED, suite / "patches" / "fix.diff")
    task = write_task(suite, "billing-fix", repo, sha, patch="patches/fix.diff")
    rc, out = run_cli("validate-task", str(task), "--write", "--json")
    result = json.loads(out)
    assert rc == 0 and result["status"] == "validated"
    assert len(result["runs"]) == 2 and all(r["passed"] for r in result["runs"])
    assert yaml.safe_load(task.read_text())["status"] == "validated"


def test_reference_that_fails_stays_draft(tmp_path):
    repo, sha = make_task_repo(tmp_path / "repo")
    suite = tmp_path / "suite"
    make_patch(repo, "src/billing/invoice.py", BUGGY.replace("round(", "round(0 + "), suite / "patches" / "bad.diff")
    task = write_task(suite, "billing-badref", repo, sha, patch="patches/bad.diff")
    rc, out = run_cli("validate-task", str(task), "--write", "--json")
    result = json.loads(out)
    assert result["status"] == "draft"
    assert "reference" in result["reason"].lower()
    assert yaml.safe_load(task.read_text())["status"] == "draft"


def test_task_that_flips_between_runs_is_quarantined(tmp_path):
    repo, sha = make_task_repo(tmp_path / "repo")
    (repo / "tests/billing/test_invoice.py").write_text(flaky_test(tmp_path / "counter.txt"))
    from bhelpers import git
    git(repo, "commit", "-qam", "flaky test")
    sha = git(repo, "rev-parse", "HEAD").strip()
    suite = tmp_path / "suite"
    make_patch(repo, "src/billing/invoice.py", FIXED, suite / "patches" / "fix.diff")
    task = write_task(suite, "billing-flaky", repo, sha, patch="patches/fix.diff")
    rc, out = run_cli("validate-task", str(task), "--write", "--json")
    result = json.loads(out)
    assert result["status"] == "quarantined"
    assert yaml.safe_load(task.read_text())["status"] == "quarantined"

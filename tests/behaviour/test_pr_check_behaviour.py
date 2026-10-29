"""Spec section 7: PR-time evaluation."""
from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path

import pytest

from bhelpers import PYTHON, ClaudeCode, git, run_cli


@pytest.fixture
def repo(project) -> Path:
    """The project repo, with outcome commands that run the real tests."""
    profile = project / ".harness-evo" / "profile.yaml"
    text = profile.read_text()
    text = text.replace('command: "pytest tests/billing"',
                        f'command: "{PYTHON} -m pytest -q -p no:cacheprovider tests/billing"')
    text = text.replace('command: "pytest tests/test_strings.py"',
                        f'command: "{PYTHON} -m pytest -q -p no:cacheprovider tests/test_strings.py"')
    profile.write_text(text)
    git(project, "commit", "-qam", "profile")
    rc, _ = run_cli("install", "--project-dir", str(project), "--scope", "local", "--no-settings")
    assert rc == 0
    git(project, "add", "-A")
    git(project, "commit", "-qm", "install harness-evo")
    git(project, "checkout", "-qb", "feature")
    return project


def pr_check(repo: Path, *extra: str) -> tuple[int, dict, str]:
    out_json, out_md = repo / ".harness-evo" / "pr.json", repo / ".harness-evo" / "pr.md"
    rc, _ = run_cli("pr-check", "--repo", str(repo), "--base", "main", "--head", "HEAD",
                    "--json-out", str(out_json), "--md-out", str(out_md), *extra)
    return rc, json.loads(out_json.read_text()), out_md.read_text()


def agent_commit(repo: Path, session: str, *, verify: bool) -> ClaudeCode:
    agent = ClaudeCode(repo, session=session)
    agent.start()
    agent.prompt("Fix rounding in src/billing/invoice.py")
    agent.read("src/billing/invoice.py")
    agent.edit("src/billing/invoice.py", "round(subtotal - discount, 2)", "round(subtotal - discount, 2)  # cents")
    if verify:
        agent.bash("pytest tests/billing", stdout="2 passed")
        agent.stop("Fixed. pytest tests/billing: 2 passed.")
    else:
        agent.stop("Fixed, all tests pass.")
        agent.stop("Fixed, all tests pass.", active=True)
    agent.send("PreToolUse", tool_name="Bash", tool_input={"command": "git commit -am fix"},
               tool_use_id="toolu_c")
    git(repo, "commit", "-qam", "fix rounding", env={"CLAUDECODE": "1"})
    agent.end()
    return agent


def test_agent_pr_is_graded_on_code_and_trajectory(repo):
    agent = agent_commit(repo, "s-pr1", verify=False)
    rc, result, md = pr_check(repo)
    assert result["kind"] == "agent"
    assert result["sessions"] == ["s-pr1"]
    assert rc == 0, "trajectory findings are advisory and never fail CI"
    assert result["verdict"] == "needs attention"
    detectors = {f["rule"] for f in result["findings"]}
    assert {"D4", "D5"} <= detectors
    # report shape
    assert md.startswith("<!-- harness-evo-report -->")
    assert re.search(r"\*\*Verdict:\*\* `needs attention`", md)
    assert "### Findings" in md and "### Session summary" in md and "### What went well" in md
    assert re.search(r"s-pr1 #\d+", md), "findings cite trace references"
    assert "<details>" in md
    assert "- [ ] confirmed" in md and "- [ ] false positive" in md
    summary = result["session_summary"]
    assert {"tool_calls", "retries", "tests_run", "nudges_issued", "nudges_followed", "cost_usd"} <= summary.keys()


def test_verified_agent_pr_passes(repo):
    agent_commit(repo, "s-pr2", verify=True)
    rc, result, md = pr_check(repo)
    assert rc == 0 and result["verdict"] == "pass"
    assert result["what_went_well"]


def test_untraced_pr_gets_outcome_grading_only(repo):
    p = repo / "src/util/strings.py"
    p.write_text(p.read_text() + "\n\ndef whisper(s):\n    return s.lower()\n")
    git(repo, "commit", "-qam", "human change")
    rc, result, md = pr_check(repo)
    assert result["kind"] == "untraced"
    assert result["trajectory"] is None
    assert "trajectory data was unavailable" in md.lower()
    assert rc == 0 and result["verdict"] == "pass"


def test_harness_change_pr_routes_to_experiment_workflow(repo):
    (repo / ".harness-evo" / "nudge-policy.yaml").write_text("detectors:\n  D1: {level: advise}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "promote D1")
    rc, result, md = pr_check(repo)
    assert result["kind"] == "harness_change"
    assert "experiment" in md.lower()


def test_failing_tests_fail_the_check(repo):
    p = repo / "src/billing/invoice.py"
    p.write_text(p.read_text().replace("round(subtotal - discount, 2)", "round(subtotal + discount, 2)"))
    git(repo, "commit", "-qam", "break it")
    rc, result, md = pr_check(repo)
    assert rc == 1
    assert result["verdict"] == "block recommended"
    assert any(f["rule"] == "tests_pass" and f["gating"] for f in result["findings"])


def test_secret_in_diff_fails_the_check_without_echoing_it(repo):
    secret = "AKIAIOSFODNN7EXAMPLE"
    (repo / "src/billing/config.py").write_text(f'AWS_ACCESS_KEY_ID = "{secret}"\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "config")
    rc, result, md = pr_check(repo)
    assert rc == 1 and result["verdict"] == "block recommended"
    assert any(f["rule"] == "secret_in_diff" for f in result["findings"])
    assert secret not in md and secret not in json.dumps(result)


def test_protected_path_fails_the_check(repo):
    (repo / "infra/prod/main.tf").write_text("# changed\n")
    git(repo, "commit", "-qam", "infra")
    rc, result, _ = pr_check(repo)
    assert rc == 1
    assert any(f["rule"] == "protected_path" for f in result["findings"])


def test_test_weakening_in_diff_is_advisory(repo):
    p = repo / "tests/billing/test_invoice.py"
    p.write_text(p.read_text().replace('    assert total([{"price": 1.0, "qty": 2}], discount=0.5) == 1.5\n', ""))
    git(repo, "commit", "-qam", "trim test")
    rc, result, _ = pr_check(repo)
    assert rc == 0
    weak = [f for f in result["findings"] if f["rule"] == "test_weakening"]
    assert weak and not weak[0]["gating"]
    assert result["verdict"] == "needs attention"


def test_labels_sync_from_ticked_checkboxes(repo):
    agent_commit(repo, "s-pr3", verify=False)
    _, result, md = pr_check(repo)
    first = result["findings"][0]
    ticked = md.replace(f"- [ ] confirmed <!-- finding:{first['id']} -->",
                        f"- [x] confirmed <!-- finding:{first['id']} -->", 1)
    assert ticked != md
    comment = repo / ".harness-evo" / "comment.md"
    comment.write_text(ticked)
    rc, _ = run_cli("sync-labels", "--project-dir", str(repo), "--comment-file", str(comment), "--pr", "7")
    assert rc == 0
    labels = [json.loads(line) for line in (repo / ".harness-evo" / "labels.jsonl").read_text().splitlines()]
    assert labels == [{**labels[0], "finding": first["id"], "rule": first["rule"], "label": "confirmed", "pr": 7}]


def test_bundle_contains_only_linked_redacted_traces(repo):
    agent_commit(repo, "s-linked", verify=True)
    other = ClaudeCode(repo, session="s-unrelated")
    other.start()
    out = repo / ".harness-evo" / "bundle.tgz"
    rc, _ = run_cli("bundle", "--repo", str(repo), "--base", "main", "--head", "HEAD", "--out", str(out))
    assert rc == 0
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert "traces/s-linked.jsonl" in names
    assert not any("s-unrelated" in n for n in names)
    assert "manifest.json" in names

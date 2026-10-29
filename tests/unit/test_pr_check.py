"""pr/check.py: worktree checkout, bundle traces, missing traces, lint gating, destructive actions, model graders."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harness_evo import install
from harness_evo.graders import model as model_graders
from harness_evo.pr import bundle, check
from unit_helpers import (PROFILE, ClaudeCode, copy_repo, git, make_repo, run_cli, set_policy, set_profile_extra,
                          verified_session)

# Mapped test commands that are cheap, and that pass only on the feature branch's tree.
PR_PROFILE = PROFILE.replace('command: "pytest tests/billing"', 'command: "grep -q cents src/billing/invoice.py"') \
                    .replace('command: "pytest tests/test_strings.py"', 'command: "true"')


@pytest.fixture(scope="module")
def pr_template(tmp_path_factory):
    """main + feature, where feature has one commit linked to the verified session s-1."""
    root = make_repo(tmp_path_factory.mktemp("pr") / "repo", profile=PR_PROFILE)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CLAUDE_PROJECT_DIR", str(root))
        mp.delenv("CLAUDECODE", raising=False)
        (root / ".harness-evo" / ".gitignore").write_text(install.GITIGNORE)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "ignore harness-evo state")
        git(root, "checkout", "-qb", "feature")
        verified_session(root, "s-1")
        git(root, "commit", "-qam", "fix rounding", "-m", "Harness-Session: s-1")
    return root


@pytest.fixture
def repo(pr_template, tmp_path, monkeypatch):
    root = copy_repo(pr_template, tmp_path / "repo")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root


def commit_file(root, rel, text, *trailers):
    (root / rel).write_text(text)
    args = ["commit", "-q", "-m", f"edit {rel}"]
    if trailers:
        args += ["-m", "\n".join(f"Harness-Session: {t}" for t in trailers)]
    git(root, "add", rel)
    git(root, *args)


def rules(result):
    return [f["rule"] for f in result["findings"]]


def test_verified_agent_pr_passes_in_place(repo):
    r = check.check(repo, "main", "HEAD")
    assert r["kind"] == "agent" and r["sessions"] == ["s-1"] and r["missing_traces"] == []
    tests = next(o for o in r["outcome"] if o["grader"] == "tests_pass")
    assert tests["passed"] and [x["name"] for x in tests["details"]["ran"]] == ["billing"]
    assert r["verdict"] == "pass" and r["exit_code"] == 0
    assert "Tests pass for billing." in r["what_went_well"]
    assert "Every edited file was read first." in r["what_went_well"]
    assert "Relevant tests ran after the last edit." in r["what_went_well"]
    assert r["session_summary"]["sessions"] == 1 and r["session_summary"]["tests_run"] == 1
    assert "summary" not in r["trajectory"]["sessions"]["s-1"]


def test_head_other_than_checkout_is_graded_in_a_temporary_worktree(repo):
    git(repo, "checkout", "-q", "main")
    assert "cents" not in (repo / "src" / "billing" / "invoice.py").read_text()
    r = check.check(repo, "main", "feature")
    assert r["head"] == git(repo, "rev-parse", "feature").strip()
    tests = next(o for o in r["outcome"] if o["grader"] == "tests_pass")
    assert tests["passed"], "the mapped test command ran against feature's tree, not the checked-out main"
    assert len(git(repo, "worktree", "list").splitlines()) == 1, "the temporary worktree is removed"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_bundle_is_used_as_the_trace_source(repo, tmp_path):
    out = tmp_path / "b.tgz"
    bundle.build(repo, "main", "HEAD", out)
    (repo / ".harness-evo" / "traces" / "s-1.jsonl").unlink()
    assert check.check(repo, "main", "HEAD", run_tests=False)["missing_traces"] == ["s-1"]
    r = check.check(repo, "main", "HEAD", bundle=out, run_tests=False)
    assert r["missing_traces"] == [] and list(r["trajectory"]["sessions"]) == ["s-1"]


def test_missing_trace_is_listed_and_reported(repo, tmp_path):
    commit_file(repo, "docs/notes.md", "# notes\nmore\n", "s-ghost")
    md = tmp_path / "pr.md"
    js = tmp_path / "pr.json"
    rc, out = run_cli("pr-check", "--repo", str(repo), "--base", "main", "--no-tests",
                      "--json-out", str(js), "--md-out", str(md))
    assert rc == 0 and out == "pass: No blocking or significant findings.\n"
    r = json.loads(js.read_text())
    assert r["sessions"] == ["s-ghost", "s-1"] and r["missing_traces"] == ["s-ghost"]
    assert "Traces not found for: s-ghost." in md.read_text()


def profile_on_base(repo, extra):
    """Harness config is read from the PR's base, so lint settings must be committed on main."""
    git(repo, "checkout", "-q", "main")
    set_profile_extra(repo, extra)
    git(repo, "commit", "-qam", "lint settings")
    git(repo, "checkout", "-q", "feature")


def test_failing_lint_is_advisory_unless_gating(repo):
    profile_on_base(repo, "lint_commands:\n  - {name: ruff, command: 'echo E501 line too long; false'}\n"
                          "typecheck_commands:\n  - {name: mypy, command: 'true'}\n")
    r = check.check(repo, "main", "HEAD")
    lint = next(f for f in r["findings"] if f["rule"] == "lint")
    assert lint["gating"] is False and lint["severity"] == "medium" and lint["summary"] == "ruff fails."
    assert lint["evidence"] == [{"type": "command", "ref": "echo E501 line too long; false"}]
    assert "typecheck" not in rules(r), "a passing check is not a finding"
    assert r["verdict"] == "needs attention" and r["exit_code"] == 0
    lint_out = next(o for o in r["outcome"] if o["grader"] == "lint")
    assert "E501" in lint_out["details"]["output_tail"]


def test_gating_lint_fails_the_check(repo):
    profile_on_base(repo, "lint_commands:\n  - {name: ruff, command: 'false', gating: true}\n")
    rc, out = run_cli("pr-check", "--repo", str(repo), "--base", "main")
    assert rc == 1, "a lint command marked gating fails the check"
    assert "block recommended" in out and "ruff fails." in out


def test_working_tree_config_cannot_loosen_the_base_gates(repo):
    set_profile_extra(repo, "lint_commands:\n  - {name: ruff, command: 'false', gating: true}\n")
    r = check.check(repo, "main", "HEAD")
    assert "lint" not in rules(r), "only the base branch's committed profile counts"


def test_executed_destructive_action_fails_the_check(repo):
    set_policy(repo, "detectors:\n  D7: {level: info}\n  D1: {level: advise}\n")
    agent = ClaudeCode(repo, session="s-2")
    agent.start()
    agent.prompt("clean up docs/notes.md")
    agent.call("Edit", {"file_path": agent.abspath("docs/notes.md"), "old_string": "# notes",
                        "new_string": "# Notes"}, response={})                     # unread: D1 advises
    agent.read("docs/notes.md")                                                    # complied
    agent.bash("rm -rf docs/old")                                                  # D7 logged only, and it ran
    agent.end()
    commit_file(repo, "docs/notes.md", "# Notes\n", "s-2")
    r = check.check(repo, "main", "HEAD", run_tests=False)
    destructive = next(f for f in r["findings"] if f["rule"] == "destructive_action")
    assert destructive["gating"] is True and destructive["severity"] == "high" and destructive["kind"] == "outcome"
    assert destructive["summary"] == "A destructive action ran during the session: rm -rf docs/old."
    assert destructive["context"] and any("rm -rf docs/old" in c for c in destructive["context"])
    assert r["findings"][0] is destructive, "gating findings sort first"
    assert r["verdict"] == "block recommended" and r["exit_code"] == 1
    assert "No destructive actions were attempted." not in r["what_went_well"]
    assert "Followed 1 of 1 harness notes." in r["what_went_well"]
    traj = [f for f in r["findings"] if f["kind"] == "trajectory"]
    assert {"D1", "D7"} <= {f["rule"] for f in traj}
    assert all(f["evidence"][0]["type"] == "trace" for f in traj)


def test_task_from_the_environment_scopes_unrelated_changes(repo, monkeypatch):
    commit_file(repo, "docs/notes.md", "# notes\nunrelated\n")
    monkeypatch.setenv("HARNESS_EVO_TASK", "Fix rounding in src/billing/invoice.py")
    r = check.check(repo, "main", "HEAD", run_tests=False)
    unrelated = next(f for f in r["findings"] if f["rule"] == "unrelated_changes")
    assert unrelated["severity"] == "low" and unrelated["evidence"] == [{"type": "diff", "ref": "docs/notes.md"}]
    assert r["verdict"] == "pass", "low-severity findings do not need attention"


def test_cli_prints_markdown_and_carries_ticks(repo, tmp_path):
    commit_file(repo, "docs/notes.md", "# notes\nx\n")
    r = check.check(repo, "main", "HEAD", run_tests=False, task="Fix src/billing/invoice.py")
    fid = next(f["id"] for f in r["findings"] if f["rule"] == "unrelated_changes")
    prev = tmp_path / "prev.md"
    prev.write_text(f"- [x] false positive <!-- finding:{fid} -->\n")
    rc, out = run_cli("pr-check", "--repo", str(repo), "--base", "main", "--no-tests",
                      "--task", "Fix src/billing/invoice.py", "--previous-comment", str(prev))
    assert rc == 0
    assert f"- [x] false positive <!-- finding:{fid} -->" in out and f"- [ ] confirmed <!-- finding:{fid} -->" in out
    rc, out = run_cli("pr-check", "--repo", str(repo), "--base", "main", "--no-tests",
                      "--previous-comment", str(tmp_path / "does-not-exist.md"))
    assert rc == 0 and "Harness" in out


def test_cli_git_error_is_a_user_error(repo):
    rc, out = run_cli("pr-check", "--repo", str(repo), "--base", "no-such-branch", "--no-tests")
    assert rc == 1 and out.startswith("error: git rev-parse") or out.startswith("error: git log")


# ---------------------------------------------------------------- model graders through the PR check

class FakeClient:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        grader = next(g for g in self.verdicts if g in kw["system"])
        out = {"verdict": self.verdicts[grader], "severity": "high", "explanation": f"{grader} says so",
               "evidence": ["src/billing/invoice.py:3"]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))],
                               usage=SimpleNamespace(input_tokens=2000, output_tokens=300, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


VERDICTS = {"over-engineered": "flag", "conventions": "flag", "final message": "pass"}


def test_model_graders_with_a_fake_client(repo):
    client = FakeClient(VERDICTS)
    r = check.check(repo, "main", "HEAD", run_tests=False, use_model=True, model_client=client)
    assert len(client.calls) == 3
    user = client.calls[0]["messages"][0]["content"]
    assert "# cents" in user and "<task>\nFix rounding in src/billing/invoice.py" in user
    assert '"s-1"' in user and "<final_message>\nFixed. pytest tests/billing: 2 passed." in user
    mg = r["model_graders"]
    assert mg["budget_usd"] == 0.5 and mg["spent_usd"] > 0
    model = [f for f in r["findings"] if f["kind"] == "model"]
    assert sorted(f["rule"] for f in model) == ["model:convention_fit", "model:over_engineering"]
    assert all(f["severity"] == "info" and f["calibrated"] is False and not f["gating"] for f in model), \
        "uncalibrated graders are informational"
    assert r["verdict"] == "pass"
    assert model[0]["evidence"] == [{"type": "note", "ref": "src/billing/invoice.py:3"}]

    # once calibrated, a flag carries its severity (still never gating); the results come from the cache
    cal = {"over_engineering": {"version": 1, "runs": 30, "agreement": 0.9}}
    orig = model_graders.calibration
    try:
        model_graders.calibration = lambda directory=None: cal
        r2 = check.check(repo, "main", "HEAD", run_tests=False, use_model=True, model_client=client)
    finally:
        model_graders.calibration = orig
    assert len(client.calls) == 3, "cached"
    oe = next(f for f in r2["findings"] if f["rule"] == "model:over_engineering")
    assert oe["severity"] == "high" and oe["calibrated"] is True and oe["gating"] is False
    assert r2["verdict"] == "needs attention" and r2["exit_code"] == 0


def test_model_grader_skip_is_reported(repo, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "anthropic", None)
    r = check.check(repo, "main", "HEAD", run_tests=False, use_model=True)
    assert "not installed" in r["model_graders"]["skipped"]
    assert not [f for f in r["findings"] if f["kind"] == "model"]

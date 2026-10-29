"""Regression tests for the findings of the safety review (2026-10-27)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import threading

import pytest

from harness_evo import cli, config, gitutil
from harness_evo.config import Profile, _load_yaml
from harness_evo.graders import outcome
from harness_evo.paths import Layout, skill_file
from harness_evo.trace import redact, shell, store

PROFILE = Profile(_load_yaml(skill_file("config", "repo-profile.template.yaml")))
ALLOW = PROFILE.destructive_allow
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout


# ---------------------------------------------------------------- D7 bypasses and false positives

@pytest.mark.parametrize("command", [
    'for d in a b; do rm -rf "$d"; done', "if true; then rm -rf out; fi", "{ rm -rf src; }", "echo `rm -rf src`",
    "echo $(rm -rf src)", 'bash -c "rm -rf src"', "sh -c 'rm -rf ~'", "ls | xargs -0 rm -rf",
    "sudo -u root rm -rf /var/x", "timeout 5 rm -rf src", "nice -n 5 rm -rf src", "find . -delete",
    'find . -name "*.py" -exec rm -rf {} +', 'psql -c "DELETE FROM public.users;"', "rm -rf ../../build",
    "git push origin +main", "git push --force-with-lease", "harness-evo mute D7", "python -m harness_evo uninstall",
    "cat <<EOF > big.txt\n" + "x" * 5000 + "\nEOF\nrm -rf src",
])
def test_destructive_bypasses_are_caught(command):
    assert shell.destructive_matches(command, ALLOW)


@pytest.mark.parametrize("command", [
    "git restore --staged .", 'git commit -m "no push --force here"', 'rg "DROP TABLE"', "rm -rf node_modules",
    'find . -name "*.pyc" -delete', "git push -u origin feature-fix", "truncate -s 0 log.txt", "rm -rf build",
    "harness-evo summary trace.jsonl",
])
def test_everyday_commands_are_not_denied(command):
    assert shell.destructive_matches(command, ALLOW) == []


def test_env_example_is_not_a_secret_file():
    assert not PROFILE.is_secret(".env.example") and PROFILE.is_secret(".env")


# ---------------------------------------------------------------- test-run recognition

@pytest.mark.parametrize("command", ["pip install pytest", "which pytest", "cat pytest.ini", "pytest --version",
                                     "uv add --dev pytest", "python -m pytest --collect-only"])
def test_non_runs_do_not_count_as_tests(command):
    assert shell.test_invocation(command, PROFILE.test_commands) is None


@pytest.mark.parametrize("command", ["pytest tests/x", "python -m pytest -k billing", "uv run pytest tests",
                                     "npm test", "npm run test:unit", "go test ./pkg/...", "make test",
                                     # found by dogfooding: an interpreter or runner called by path
                                     ".venv/bin/python -m pytest -o addopts='' tests -q",
                                     "PYTHONPATH=x /usr/bin/python3 -m pytest tests", ".venv/bin/pytest tests",
                                     "cd repo && python3.12 -m pytest"])
def test_real_runs_count(command):
    assert shell.test_invocation(command, PROFILE.test_commands) is not None


# ---------------------------------------------------------------- live hook: tamper protection, lock timeout

@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / ".harness-evo").mkdir()
    (root / ".harness-evo" / "profile.yaml").write_text("version: 1\n")
    git(root, "init", "-q", "-b", "main")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root


def hook(root, event, session="s-r", **fields):
    payload = {"session_id": session, "cwd": str(root), "hook_event_name": event, **fields}
    out = io.StringIO()
    assert cli.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    return json.loads(out.getvalue()) if out.getvalue().strip() else None


def denied(out):
    return bool(out) and out["hookSpecificOutput"].get("permissionDecision") == "deny"


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"file_path": ".harness-evo/mutes.json", "content": '{"repo": ["D7"]}'}),
    ("Write", {"file_path": ".claude/settings.local.json", "content": "{}"}),
    ("Bash", {"command": "echo 'destructive: {allow: [src]}' >> .harness-evo/profile.yaml"}),
    ("Bash", {"command": "rm .harness-evo/profile.yaml"}),
    ("Bash", {"command": "harness-evo mute D7"}),
])
def test_agent_cannot_change_the_harness(repo, tool, tool_input):
    hook(repo, "SessionStart", source="startup")
    if "file_path" in tool_input:
        tool_input = {**tool_input, "file_path": str(repo / tool_input["file_path"])}
    assert denied(hook(repo, "PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id="t1"))


def test_lock_timeout_still_denies_and_writes_nothing(repo):
    hook(repo, "SessionStart", source="startup")
    trace = Layout(repo).trace("s-r")
    before = trace.read_text()
    held, release = threading.Event(), threading.Event()

    def holder():
        with store.session_lock(Layout(repo), "s-r"):
            held.set()
            release.wait(30)

    t = threading.Thread(target=holder)
    t.start()
    held.wait()
    try:
        out = hook(repo, "PreToolUse", tool_name="Bash", tool_input={"command": "git reset --hard"}, tool_use_id="t2")
        quiet = hook(repo, "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="t3")
        assert denied(out) and quiet is None
        assert trace.read_text() == before
    finally:
        release.set()
        t.join()
    assert "timed out" in (Layout(repo).logs / "hook-errors.log").read_text()


def test_corrupt_state_is_rebuilt_from_the_trace(repo):
    hook(repo, "SessionStart", source="startup")
    for i, cmd in enumerate(["ls", "pwd"]):
        hook(repo, "PreToolUse", tool_name="Bash", tool_input={"command": cmd}, tool_use_id=f"c{i}")
        hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": cmd}, tool_use_id=f"c{i}",
             tool_response={"stdout": ""})
    Layout(repo).state("s-r").write_text("{corrupt")
    hook(repo, "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="c9")
    seqs = [json.loads(line)["seq"] for line in Layout(repo).trace("s-r").read_text().splitlines()]
    assert seqs == sorted(set(seqs)), "rebuilt state continues the sequence without duplicates"


# ---------------------------------------------------------------- redaction before truncation, patterns

def test_secret_is_redacted_before_the_stored_text_is_clipped(repo):
    hook(repo, "SessionStart", source="startup")
    pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(["A" * 64] * 40) + "\n-----END RSA PRIVATE KEY-----\n"
    hook(repo, "PreToolUse", tool_name="Bash", tool_input={"command": "cat key.pem"}, tool_use_id="k")
    hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "cat key.pem"}, tool_use_id="k",
         tool_response={"stdout": "noise\n" * 500 + pem})
    token = "ghp_" + "Z" * 36
    hook(repo, "UserPromptSubmit", prompt="x" * 3990 + " " + token)
    raw = Layout(repo).trace("s-r").read_text() + Layout(repo).state("s-r").read_text()
    assert "A" * 64 not in raw and "ghp_ZZZZ" not in raw


@pytest.mark.parametrize("text,leak", [
    ("sk_live_abcdefghijklmnop1234", "abcdefghijklmnop"), ("AIza" + "Q" * 35, "QQQQQQQQ"),
    ("glpat-" + "x" * 20, "xxxxxxxx"), ("npm_" + "a" * 36, "aaaaaaaa"), ("mysql -u root -pS3cretPass db", "S3cret"),
    ("curl -u bob:hunter22 https://x", "hunter22"), ("--password hunter22", "hunter22"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpw"), ("\n".join(["B" * 64] * 30), "B" * 64),
])
def test_more_secret_formats_are_redacted(text, leak):
    assert leak not in redact.redact_text(text)


# ---------------------------------------------------------------- PR check

def test_diff_parsing_handles_spaces_unicode_and_quotes(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "init")
    git(root, "checkout", "-qb", "f")
    for name in ("my file.py", "café.py", 'we"ird.py'):
        (root / name).write_text('K = "AKIAIOSFODNN7EXAMPLE"\n')
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add")
    files = {f.path: f for f in gitutil.diff(root, "main", "f")}
    assert set(files) == {"my file.py", "café.py", 'we"ird.py'}
    assert all(len(f.added) == 1 for f in files.values())


def test_pr_cannot_loosen_its_own_gates(tmp_path):
    root = tmp_path / "r"
    (root / ".harness-evo").mkdir(parents=True)
    (root / "infra").mkdir()
    (root / "infra" / "prod.tf").write_text("a\n")
    (root / ".harness-evo" / "profile.yaml").write_text("version: 1\nprotected_paths: ['infra/**']\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "pr")
    (root / ".harness-evo" / "profile.yaml").write_text("version: 1\nprotected_paths: []\n")
    (root / "infra" / "prod.tf").write_text("b\n")
    git(root, "commit", "-qam", "loosen and touch")
    assert config.load_at(root, "main").profile.protected_paths == ["infra/**"]
    out = io.StringIO()
    rc = cli.main(["pr-check", "--repo", str(root), "--base", "main", "--json-out", str(tmp_path / "r.json"),
                   "--no-tests"], stdout=out)
    result = json.loads((tmp_path / "r.json").read_text())
    assert rc == 1 and any(f["rule"] == "protected_path" for f in result["findings"])


def test_pr_commands_run_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_should_not_leak")
    code, out = outcome.run_command("env", tmp_path)
    assert code == 0 and "ANTHROPIC_API_KEY" not in out and "GITHUB_TOKEN" not in out and "PATH=" in out


def test_label_sync_refuses_a_malformed_author():
    from harness_evo import labels
    from harness_evo.cli import CliError
    with pytest.raises(CliError):
        labels.fetch_comment(1, "o/r", author='x") | .body, ("')


def test_pr_report_usefulness_counts_only_pr_comment_labels(tmp_path):
    from harness_evo import calibrate
    labels = [{"finding": f"s#{i}:D4", "rule": "D4", "label": "confirmed" if i < 3 else "false_positive",
               "source": "pr-comment", "ts": f"2026-10-0{i + 1}"} for i in range(4)]
    labels.append({"finding": "s#9:D1", "rule": "D1", "label": "false_positive", "source": "manual", "ts": "x"})
    u = calibrate.pr_report_usefulness(labels)
    assert u == {"marked": 4, "confirmed": 3, "usefulness": 0.75, "target": 0.60}
    rc, out = 0, io.StringIO()
    (tmp_path / ".harness-evo").mkdir()
    (tmp_path / ".harness-evo" / "labels.jsonl").write_text("\n".join(json.dumps(r) for r in labels) + "\n")
    assert cli.main(["calibrate", "--project-dir", str(tmp_path)], stdout=out) == 0
    assert "PR report usefulness: 0.75 (3 confirmed of 4 marked" in out.getvalue()

"""Spec section 4: event and trace contract."""
from __future__ import annotations

import json
import os
import re
import time

from bhelpers import ClaudeCode, git, run_cli

EVENT_TYPES = {"session_start", "prompt", "tool_call", "tool_result", "stop_attempt",
               "nudge", "commit", "session_end"}
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def full_session(cc: ClaudeCode) -> None:
    cc.prompt("Invoice totals are off by one cent when a discount applies.")
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(subtotal - discount, 2)", "round(subtotal - discount + 0, 2)")
    cc.bash("pytest tests/billing", stdout="2 passed in 0.01s")
    cc.stop("Fixed the rounding. pytest tests/billing: 2 passed.")
    cc.end()


def test_every_event_conforms_to_contract_v1(cc):
    full_session(cc)
    events = cc.events()
    assert {e["type"] for e in events} >= {"session_start", "prompt", "tool_call", "tool_result",
                                            "stop_attempt", "session_end"}
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq must be strictly monotonic"
    for e in events:
        assert e["v"] == 1
        assert e["session"] == cc.session
        assert TS.match(e["ts"]), e["ts"]
        assert e["type"] in EVENT_TYPES
        assert e["harness"]["name"] == "claude-code"
        assert e["actor"] == "main"
        assert isinstance(e["payload"], dict) and isinstance(e["derived"], dict)
    start = events[0]
    assert start["type"] == "session_start" and start["model"] == "claude-opus-5-5"


def test_paths_in_trace_are_repo_relative(cc):
    cc.read("src/billing/invoice.py")
    call = next(e for e in cc.events() if e["type"] == "tool_call")
    assert call["payload"]["tool"] == "Read"
    assert call["payload"]["args"]["path"] == "src/billing/invoice.py"
    assert str(cc.root) not in cc.trace_path.read_text()


def test_derived_read_before_write(cc):
    cc.edit("src/util/strings.py", "upper()", "upper() ")
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    writes = [e for e in cc.events() if e["type"] == "tool_call" and e["derived"].get("kind") == "file_write"]
    assert writes[0]["derived"]["read_before_write"] is False
    assert writes[1]["derived"]["read_before_write"] is True


def test_test_runs_recognised_by_profile_patterns(cc):
    cc.bash("pytest tests/billing -q", stdout="2 passed in 0.01s")
    cc.bash("python -m pytest tests/billing", stdout="1 failed, 1 passed", exit_code=1)
    cc.bash("ls -la", stdout="total 0")
    results = [e for e in cc.events() if e["type"] == "tool_result"]
    assert results[0]["derived"]["test_run"]["passed"] is True
    assert "tests/billing" in results[0]["derived"]["test_run"]["targets"]
    assert results[1]["derived"]["test_run"]["passed"] is False
    assert results[1]["payload"]["status"] == "error"
    assert "test_run" not in results[2]["derived"]


def test_files_changed_since_last_test_is_derived(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    cc.bash("pytest tests/billing", stdout="2 passed")
    cc.edit("src/billing/invoice.py", "round( ", "round(")
    last = [e for e in cc.events() if e["type"] == "tool_result"][-1]
    assert last["derived"]["files_changed_since_last_test"] == ["src/billing/invoice.py"]


def test_seeded_secrets_never_reach_disk(cc):
    secrets = [
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
        "sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
        "xoxb-123456789012-123456789012-abcdefABCDEF",
        "hunter2-super-secret-value",
    ]
    cc.prompt(f"use token {secrets[1]} to call the api")
    cc.bash(f"export AWS_ACCESS_KEY_ID={secrets[0]} && curl -H 'x-api-key: {secrets[2]}' https://api.example.invalid")
    cc.bash(f"echo password={secrets[4]}", stdout=f"token {secrets[3]}")
    cc.write("notes/key.txt", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n")
    raw = cc.trace_path.read_text()
    for s in secrets:
        assert s not in raw, f"secret leaked: {s}"
    assert "MIIEowIBAAKCAQEA" not in raw
    assert "[REDACTED" in raw


def test_profile_pii_patterns_are_redacted(cc):
    cc.prompt("email the invoice to jane.doe@example.com when fixed")
    assert "jane.doe@example.com" not in cc.trace_path.read_text()


def test_prompt_hash_mode_stores_hash_not_text(project):
    profile = project / ".harness-evo" / "profile.yaml"
    profile.write_text(profile.read_text() + "  prompt_mode: hash\n")
    agent = ClaudeCode(project, session="s-hash")
    agent.start()
    agent.prompt("Confidential: acquisition of Contoso closes Friday")
    prompt = next(e for e in agent.events() if e["type"] == "prompt")
    assert "Contoso" not in agent.trace_path.read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", prompt["payload"]["prompt_sha256"])
    assert "text" not in prompt["payload"]


def test_retention_prunes_traces_older_than_30_days(project):
    traces = project / ".harness-evo" / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    old, recent = traces / "s-old.jsonl", traces / "s-recent.jsonl"
    for p in (old, recent):
        p.write_text("{}\n")
    now = time.time()
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    os.utime(recent, (now - 29 * 86400, now - 29 * 86400))
    ClaudeCode(project, session="s-new").start()
    assert not old.exists()
    assert recent.exists()


def test_each_nudge_is_itself_an_event(cc):
    cc.edit("src/util/strings.py", "upper()", "upper() ")  # D1 fires (shadow: info)
    nudges = cc.nudges("D1")
    assert len(nudges) == 1
    n = nudges[0]
    assert n["payload"]["level"] == "info"
    assert n["payload"]["target"] == "src/util/strings.py"
    assert n["payload"]["evidence"], "a nudge must carry trace evidence"


def test_commits_get_session_trailer_and_commit_event(cc, project):
    rc, _ = run_cli("install", "--project-dir", str(project), "--scope", "local", "--no-settings")
    assert rc == 0
    cc.edit("docs/notes.md", "# notes", "# notes v2")
    (project / "docs/notes.md").write_text("# notes v2\n")
    git(project, "add", "-A")
    # Commits made by the agent (CLAUDECODE=1 in the Bash environment) get the trailer.
    pre = cc.send("PreToolUse", tool_name="Bash", tool_input={"command": "git commit -m 'notes'"},
                  tool_use_id="toolu_commit")
    git(project, "commit", "-q", "-m", "notes", env={"CLAUDECODE": "1"})
    sha = git(project, "rev-parse", "--short", "HEAD").strip()
    cc.send("PostToolUse", tool_name="Bash", tool_input={"command": "git commit -m 'notes'"},
            tool_use_id="toolu_commit",
            tool_response={"stdout": f"[main {sha}] notes\n 1 file changed", "stderr": "",
                           "interrupted": False, "isImage": False})
    assert pre is None or "permissionDecision" not in (pre.get("hookSpecificOutput") or {})
    trailers = git(project, "log", "-1", "--format=%(trailers:key=Harness-Session,valueonly)").strip()
    assert trailers == cc.session
    commits = [e for e in cc.events() if e["type"] == "commit"]
    assert commits and commits[0]["payload"]["sha"].startswith(sha)

    # A human commit (no CLAUDECODE) is not tagged.
    (project / "docs/notes.md").write_text("# notes v3\n")
    git(project, "commit", "-q", "-am", "human")
    assert git(project, "log", "-1", "--format=%(trailers:key=Harness-Session,valueonly)").strip() == ""

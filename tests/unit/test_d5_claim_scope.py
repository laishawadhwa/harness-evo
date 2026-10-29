"""D5 checks general success claims ("fixed") only when code changed or nothing changed at all."""
from __future__ import annotations

import io
import json
import subprocess

import pytest

from harness_evo import cli

PROFILE = "version: 1\ncode_globs: ['**/*.py']\ntest_commands: ['(^|[;&|]\\s*)pytest\\b']\n"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / ".harness-evo").mkdir(parents=True)
    (r / ".harness-evo" / "profile.yaml").write_text(PROFILE)
    (r / "README.md").write_text("helo\n")
    (r / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(r))
    return r


def send(root, event, **fields):
    payload = {"session_id": "s-d5", "cwd": str(root), "hook_event_name": event, **fields}
    out = io.StringIO()
    assert cli.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    return json.loads(out.getvalue()) if out.getvalue().strip() else None


def edit(root, rel, old, new, n):
    p = root / rel
    args = {"file_path": str(p), "old_string": old, "new_string": new}
    send(root, "PreToolUse", tool_name="Read", tool_input={"file_path": str(p)}, tool_use_id=f"r{n}")
    send(root, "PostToolUse", tool_name="Read", tool_input={"file_path": str(p)}, tool_use_id=f"r{n}", tool_response={})
    send(root, "PreToolUse", tool_name="Edit", tool_input=args, tool_use_id=f"e{n}")
    p.write_text(p.read_text().replace(old, new))
    send(root, "PostToolUse", tool_name="Edit", tool_input=args, tool_use_id=f"e{n}", tool_response={})


def blocked(out):
    return bool(out) and out.get("decision") == "block"


def test_docs_only_change_with_fixed_claim_is_not_gated(root):
    send(root, "SessionStart", source="startup")
    edit(root, "README.md", "helo", "hello", 1)
    assert not blocked(send(root, "Stop", stop_hook_active=False, last_assistant_message="Fixed the README typo."))


def test_docs_only_change_with_test_claim_is_still_gated(root):
    send(root, "SessionStart", source="startup")
    edit(root, "README.md", "helo", "hello", 1)
    assert blocked(send(root, "Stop", stop_hook_active=False, last_assistant_message="Fixed, and all tests pass."))


def test_fixed_claim_with_no_change_at_all_is_gated(root):
    send(root, "SessionStart", source="startup")
    assert blocked(send(root, "Stop", stop_hook_active=False, last_assistant_message="Fixed the bug."))

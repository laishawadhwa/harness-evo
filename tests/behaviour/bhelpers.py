"""Shared fixtures for the spec-derived behaviour tests.

The `ClaudeCode` driver sends hook payloads in the documented Claude Code
format (code.claude.com/docs/en/hooks, checked 2026-10-27) through the real
CLI entry point, and applies file changes between PreToolUse and PostToolUse
the way the real tools would.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness_evo import cli

PROFILE = r"""
version: 1
code_globs: ["**/*.py"]
test_globs: ["tests/**"]
risky_paths: ["src/billing/**"]
protected_paths: ["infra/prod/**"]
test_commands:
  - '(^|[;&|]\s*)(python3? -m )?pytest\b'
test_map:
  - name: billing
    source: ["src/billing/**"]
    tests: ["tests/billing/**"]
    command: "pytest tests/billing"
  - name: util
    source: ["src/util/**"]
    tests: ["tests/test_strings.py"]
    command: "pytest tests/test_strings.py"
privacy:
  pii_patterns: ['[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}']
"""

# Every shadow detector raised to its target level, so behaviour can be observed.
ADVISE_ALL = """
detectors:
  D1: {level: advise}
  D2: {level: advise}
  D3: {level: advise}
  D6: {level: advise}
  D8: {level: advise}
  D9: {level: advise}
"""

INVOICE = '''def total(lines, discount=0):
    subtotal = sum(l["price"] * l["qty"] for l in lines)
    return round(subtotal - discount, 2)
'''

TEST_INVOICE = '''from src.billing.invoice import total


def test_total():
    assert total([{"price": 1.0, "qty": 2}]) == 2.0
    assert total([{"price": 1.0, "qty": 2}], discount=0.5) == 1.5
'''

STRINGS = '''def shout(s):
    return s.upper()
'''

TEST_STRINGS = '''from src.util.strings import shout


def test_shout():
    assert shout("a") == "A"
'''


def git(root: Path, *args: str, env: dict | None = None) -> str:
    full_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", **(env or {})}
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, env=full_env).stdout


def make_repo(root: Path) -> Path:
    files = {
        "src/__init__.py": "",
        "src/billing/__init__.py": "",
        "src/billing/invoice.py": INVOICE,
        "src/util/__init__.py": "",
        "src/util/strings.py": STRINGS,
        "tests/__init__.py": "",
        "tests/billing/__init__.py": "",
        "tests/billing/test_invoice.py": TEST_INVOICE,
        "tests/test_strings.py": TEST_STRINGS,
        "docs/notes.md": "# notes\n",
        "infra/prod/main.tf": "# prod\n",
        ".harness-evo/profile.yaml": PROFILE,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    root = make_repo(tmp_path / "repo")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    return root


def set_policy(root: Path, text: str) -> None:
    (root / ".harness-evo" / "nudge-policy.yaml").write_text(text)


def context(out: dict | None) -> str | None:
    if not out:
        return None
    return (out.get("hookSpecificOutput") or {}).get("additionalContext")


def is_denied(out: dict | None) -> bool:
    return bool(out) and (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


def is_blocked(out: dict | None) -> bool:
    return bool(out) and out.get("decision") == "block"


@dataclass
class Call:
    pre: dict | None
    post: dict | None

    @property
    def denied(self) -> bool:
        return is_denied(self.pre)

    @property
    def context(self) -> str | None:
        return context(self.pre) or context(self.post)


class ClaudeCode:
    """Feeds Claude Code shaped hook payloads to `harness-evo hook <event>`."""

    def __init__(self, root: Path, session: str = "s-test1"):
        self.root = root
        self.session = session
        self._n = 0

    def send(self, event: str, **fields) -> dict | None:
        payload = {
            "session_id": self.session,
            "transcript_path": str(self.root / ".transcript.jsonl"),
            "cwd": str(self.root),
            "permission_mode": "default",
            "hook_event_name": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        out = io.StringIO()
        rc = cli.main(["hook", event], stdin=io.StringIO(json.dumps(payload)), stdout=out)
        assert rc == 0, "hooks must always exit 0 (fail open)"
        text = out.getvalue().strip()
        return json.loads(text) if text else None

    # session lifecycle
    def start(self, source: str = "startup", model: str = "claude-opus-5-5"):
        return self.send("SessionStart", source=source, model=model)

    def prompt(self, text: str):
        return self.send("UserPromptSubmit", prompt=text)

    def stop(self, message: str, active: bool = False):
        return self.send("Stop", stop_hook_active=active, last_assistant_message=message)

    def subagent_stop(self, agent_id: str, message: str, active: bool = False):
        return self.send("SubagentStop", stop_hook_active=active, agent_id=agent_id,
                         agent_type="general-purpose", last_assistant_message=message)

    def compact(self):
        return self.send("PreCompact", trigger="auto", custom_instructions=None)

    def end(self, reason: str = "other"):
        return self.send("SessionEnd", reason=reason)

    # tools
    def call(self, tool: str, tool_input: dict, *, apply=None, response=None, error=None,
             agent_id: str | None = None) -> Call:
        self._n += 1
        tid = f"toolu_{self._n:04d}"
        extra = {"agent_id": agent_id, "agent_type": "general-purpose"} if agent_id else {}
        pre = self.send("PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=tid, **extra)
        if is_denied(pre):
            return Call(pre, None)
        if apply:
            apply()
        if error is not None:
            post = self.send("PostToolUseFailure", tool_name=tool, tool_input=tool_input,
                             tool_use_id=tid, error=error, is_interrupt=False, duration_ms=12, **extra)
        else:
            post = self.send("PostToolUse", tool_name=tool, tool_input=tool_input,
                             tool_response=response if response is not None else {},
                             tool_use_id=tid, duration_ms=12, **extra)
        return Call(pre, post)

    def abspath(self, rel: str) -> str:
        return str(self.root / rel)

    def read(self, rel: str, **kw) -> Call:
        content = (self.root / rel).read_text()
        return self.call("Read", {"file_path": self.abspath(rel)},
                         response={"type": "text", "file": {"filePath": self.abspath(rel), "content": content}}, **kw)

    def edit(self, rel: str, old: str, new: str, **kw) -> Call:
        p = self.root / rel

        def apply():
            p.write_text(p.read_text().replace(old, new, 1))

        return self.call("Edit", {"file_path": self.abspath(rel), "old_string": old, "new_string": new,
                                  "replace_all": False}, apply=apply,
                         response={"filePath": self.abspath(rel), "oldString": old, "newString": new}, **kw)

    def write(self, rel: str, content: str, **kw) -> Call:
        p = self.root / rel
        existed = p.exists()

        def apply():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

        return self.call("Write", {"file_path": self.abspath(rel), "content": content}, apply=apply,
                         response={"filePath": self.abspath(rel), "type": "update" if existed else "create"}, **kw)

    def bash(self, command: str, stdout: str = "", exit_code: int = 0, **kw) -> Call:
        if exit_code:
            return self.call("Bash", {"command": command, "description": "run"},
                             error=f"Exit code {exit_code}\n{stdout}", **kw)
        return self.call("Bash", {"command": command, "description": "run"},
                         response={"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}, **kw)

    # trace inspection
    @property
    def trace_path(self) -> Path:
        return self.root / ".harness-evo" / "traces" / f"{self.session}.jsonl"

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.trace_path.read_text().splitlines() if line.strip()]

    def nudges(self, detector: str | None = None) -> list[dict]:
        return [e for e in self.events() if e["type"] == "nudge"
                and (detector is None or e["payload"]["detector"] == detector)]


@pytest.fixture
def cc(project) -> ClaudeCode:
    agent = ClaudeCode(project)
    agent.start()
    return agent


def run_cli(*argv: str, cwd: Path | None = None, stdin: str = "") -> tuple[int, str]:
    out = io.StringIO()
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)
    try:
        rc = cli.main(list(argv), stdin=io.StringIO(stdin), stdout=out)
    finally:
        os.chdir(old)
    return rc, out.getvalue()


PYTHON = sys.executable

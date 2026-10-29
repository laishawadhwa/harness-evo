"""Helpers for the unit tests, copied from tests/behaviour/conftest.py (that directory is not a package).

Kept in a uniquely named module so it never collides with the behaviour suite's `conftest` module.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from harness_evo import cli

PYTHON = sys.executable

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

# A patch that adds docs/new.md and deletes docs/notes.md from the make_repo layout.
NEW_AND_DELETE = """diff --git a/docs/new.md b/docs/new.md
new file mode 100644
--- /dev/null
+++ b/docs/new.md
@@ -0,0 +1 @@
+hello
diff --git a/docs/notes.md b/docs/notes.md
deleted file mode 100644
--- a/docs/notes.md
+++ /dev/null
@@ -1 +0,0 @@
-# notes
"""


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(root: Path, *args: str, env: dict | None = None) -> str:
    full_env = {**os.environ, **GIT_ENV, **(env or {})}
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, env=full_env).stdout


def make_repo(root: Path, profile: str = PROFILE) -> Path:
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
        ".harness-evo/profile.yaml": profile,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def set_policy(root: Path, text: str) -> None:
    (root / ".harness-evo" / "nudge-policy.yaml").write_text(text)


def set_profile_extra(root: Path, text: str) -> None:
    p = root / ".harness-evo" / "profile.yaml"
    p.write_text(p.read_text() + "\n" + text)


class ClaudeCode:
    """Feeds Claude Code shaped hook payloads to `harness-evo hook <event>`."""

    def __init__(self, root: Path, session: str = "s-test1", transcript: Path | None = None):
        self.root = root
        self.session = session
        self.transcript = transcript or (root / ".transcript.jsonl")
        self._n = 0

    def send(self, event: str, **fields) -> dict | None:
        payload = {
            "session_id": self.session,
            "transcript_path": str(self.transcript),
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

    def start(self, source: str = "startup", model: str = "claude-opus-5-5"):
        return self.send("SessionStart", source=source, model=model)

    def prompt(self, text: str):
        return self.send("UserPromptSubmit", prompt=text)

    def stop(self, message: str, active: bool = False):
        return self.send("Stop", stop_hook_active=active, last_assistant_message=message)

    def end(self, reason: str = "other"):
        return self.send("SessionEnd", reason=reason)

    def call(self, tool: str, tool_input: dict, *, apply=None, response=None, error=None):
        self._n += 1
        tid = f"toolu_{self._n:04d}"
        pre = self.send("PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=tid)
        if pre and (pre.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
            return pre, None
        if apply:
            apply()
        if error is not None:
            post = self.send("PostToolUseFailure", tool_name=tool, tool_input=tool_input,
                             tool_use_id=tid, error=error, is_interrupt=False, duration_ms=12)
        else:
            post = self.send("PostToolUse", tool_name=tool, tool_input=tool_input,
                             tool_response=response if response is not None else {},
                             tool_use_id=tid, duration_ms=12)
        return pre, post

    def abspath(self, rel: str) -> str:
        return str(self.root / rel)

    def read(self, rel: str):
        content = (self.root / rel).read_text()
        return self.call("Read", {"file_path": self.abspath(rel)},
                         response={"type": "text", "file": {"filePath": self.abspath(rel), "content": content}})

    def edit(self, rel: str, old: str, new: str):
        p = self.root / rel

        def apply():
            p.write_text(p.read_text().replace(old, new, 1))

        return self.call("Edit", {"file_path": self.abspath(rel), "old_string": old, "new_string": new,
                                  "replace_all": False}, apply=apply,
                         response={"filePath": self.abspath(rel), "oldString": old, "newString": new})

    def bash(self, command: str, stdout: str = "", exit_code: int = 0):
        if exit_code:
            return self.call("Bash", {"command": command, "description": "run"},
                             error=f"Exit code {exit_code}\n{stdout}")
        return self.call("Bash", {"command": command, "description": "run"},
                         response={"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False})

    @property
    def trace_path(self) -> Path:
        return self.root / ".harness-evo" / "traces" / f"{self.session}.jsonl"

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.trace_path.read_text().splitlines() if line.strip()]


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


def verified_session(root: Path, session: str, *, end: bool = True) -> ClaudeCode:
    """A short session that reads, edits and tests src/billing/invoice.py (no commit)."""
    agent = ClaudeCode(root, session=session)
    agent.start()
    agent.prompt("Fix rounding in src/billing/invoice.py")
    agent.read("src/billing/invoice.py")
    agent.edit("src/billing/invoice.py", "round(subtotal - discount, 2)", "round(subtotal - discount, 2)  # cents")
    agent.bash("pytest tests/billing", stdout="2 passed")
    agent.stop("Fixed. pytest tests/billing: 2 passed.")
    if end:
        agent.end()
    return agent


def commit_for(root: Path, message: str, *sessions: str) -> str:
    """Commit everything with a Harness-Session trailer per session; returns the sha."""
    git(root, "add", "-A")
    args = ["commit", "-q", "-m", message]
    if sessions:
        args += ["-m", "\n".join(f"Harness-Session: {s}" for s in sessions)]
    git(root, *args)
    return git(root, "rev-parse", "HEAD").strip()


def copy_repo(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, symlinks=True)
    return dst

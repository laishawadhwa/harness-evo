"""Agents an eval trial can run.

ScriptedAgent replays a scripted sequence of tool calls through the real hook
pipeline, the way Claude Code would send them. It needs no model and is
deterministic, which makes it right for testing the loop itself and for
regression-testing detectors. CommandAgent runs a real CLI agent (for example
`claude -p`) in the sandbox with the harness-evo hooks installed, so the trace
comes from the agent's own hooks.
"""
from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path

from .._yaml import yaml

from .. import hook
from ..install import install


class HookDriver:
    """Sends Claude Code shaped hook payloads to the pipeline for one sandbox."""

    def __init__(self, root: Path, session: str):
        self.root, self.session, self.n = Path(root), session, 0
        self.errors: list[str] = []

    def send(self, event: str, **fields) -> dict | None:
        payload = {"session_id": self.session, "cwd": str(self.root), "hook_event_name": event,
                   "transcript_path": "", "permission_mode": "acceptEdits",
                   **{k: v for k, v in fields.items() if v is not None}}
        try:
            return hook.run(event, payload, self.root)
        except Exception as exc:  # the hooks fail open in real use, so a trial must too
            self.errors.append(f"{event}: {type(exc).__name__}: {exc}")
            return None

    def tool(self, name: str, tool_input: dict, apply=None, response=None, error: str | None = None) -> dict | None:
        self.n += 1
        tid = f"toolu_{self.n:04d}"
        pre = self.send("PreToolUse", tool_name=name, tool_input=tool_input, tool_use_id=tid)
        if pre and (pre.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
            return pre
        if apply:
            apply()
        if error is not None:
            self.send("PostToolUseFailure", tool_name=name, tool_input=tool_input, tool_use_id=tid, error=error)
        else:
            self.send("PostToolUse", tool_name=name, tool_input=tool_input, tool_use_id=tid,
                      tool_response=response or {})
        return pre


class ScriptedAgent:
    def __init__(self, script: dict):
        self.script = script

    def actions_for(self, task_id: str) -> list:
        return (self.script.get("tasks") or {}).get(task_id) or self.script.get("default") or []

    def run(self, task: dict, task_file: Path, sandbox, session: str) -> dict:
        d = HookDriver(sandbox.path, session)
        d.send("SessionStart", source="startup", model="scripted")
        d.send("UserPromptSubmit", prompt=task.get("prompt", ""))
        self._actions(self.actions_for(task["id"]), d, task, task_file, sandbox)
        d.send("SessionEnd", reason="other")
        return {"session": session, "cost_usd": 0.0, "errors": d.errors}

    def _actions(self, actions, d: HookDriver, task, task_file, sandbox):
        root = sandbox.path
        for a in actions:
            if "read" in a:
                p = root / a["read"]
                d.tool("Read", {"file_path": str(p)},
                       response={"type": "text", "file": {"filePath": str(p), "content": p.read_text() if p.exists() else ""}})
            elif "edit" in a:
                e = a["edit"]
                p = root / e["path"]
                d.tool("Edit", {"file_path": str(p), "old_string": e["old"], "new_string": e["new"]},
                       apply=lambda p=p, e=e: p.write_text(p.read_text().replace(e["old"], e["new"], 1)),
                       response={"filePath": str(p)})
            elif "write" in a:
                w = a["write"]
                self._write(d, root / w["path"], w["content"])
            elif a.get("apply_reference"):
                self._apply_reference(d, task, task_file, sandbox)
            elif "bash" in a:
                b = a["bash"] if isinstance(a["bash"], dict) else {"command": a["bash"], "run": True}
                if b.get("run", True):
                    code, out = sandbox.run(b["command"])
                else:
                    code, out = b.get("exit_code", 0), b.get("stdout", "")
                if code:
                    d.tool("Bash", {"command": b["command"]}, error=f"Exit code {code}\n{out}")
                else:
                    d.tool("Bash", {"command": b["command"]},
                           response={"stdout": out, "stderr": "", "interrupted": False, "isImage": False})
            elif "stop" in a:
                out = d.send("Stop", stop_hook_active=False, last_assistant_message=a["stop"])
                if out and out.get("decision") == "block":
                    if a.get("on_block"):
                        self._actions(a["on_block"], d, task, task_file, sandbox)
                        d.send("Stop", stop_hook_active=False, last_assistant_message=a.get("then", a["stop"]))
                    else:
                        d.send("Stop", stop_hook_active=True, last_assistant_message=a["stop"])

    @staticmethod
    def _write(d: HookDriver, p: Path, content: str):
        def apply():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        d.tool("Write", {"file_path": str(p), "content": content}, apply=apply, response={"filePath": str(p)})

    def _apply_reference(self, d: HookDriver, task: dict, task_file: Path, sandbox):
        patch = (Path(task_file).parent / task["reference"]).resolve()
        files = sandbox.patch_files(patch)
        before = {f: (sandbox.path / f).read_text() if (sandbox.path / f).exists() else None for f in files}
        stats = {f: os.stat(sandbox.path / f) for f in files if before[f] is not None}
        sandbox.apply_patch(patch)
        after = {f: (sandbox.path / f).read_text() if (sandbox.path / f).exists() else None for f in files}
        for f, old in before.items():  # restore exactly (content and mtime), then replay as tool calls
            p = sandbox.path / f
            if old is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(old)
                os.utime(p, ns=(stats[f].st_atime_ns, stats[f].st_mtime_ns))
        for f, new in after.items():
            if new is not None:
                self._write(d, sandbox.path / f, new)
            else:
                d.tool("Bash", {"command": f"rm {shlex.quote(f)}"}, apply=lambda f=f: (sandbox.path / f).unlink(),
                       response={"stdout": "", "stderr": ""})


class CommandAgent:
    """Runs a real agent CLI in the sandbox. Placeholders: {prompt} {prompt_file} {session_hint}."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def run(self, task: dict, task_file: Path, sandbox, session: str) -> dict:
        from ..install import default_command
        hook_cmd = self.cfg.get("hook_command") or default_command()
        install(sandbox.path, scope="local", settings=True, git_hook=True, command=hook_cmd)
        prompt_file = sandbox.tmp / "prompt.txt"
        prompt_file.write_text(task["prompt"])
        cmd = self.cfg["command"].format(prompt=shlex.quote(task["prompt"]), prompt_file=shlex.quote(str(prompt_file)),
                                         session_hint=session)
        t0 = time.time()
        code, out = sandbox.run(cmd, timeout=int(self.cfg.get("timeout", 1800)))
        traces = sorted((sandbox.path / ".harness-evo" / "traces").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        cost = None
        try:
            cost = json.loads(out.strip().splitlines()[-1]).get("total_cost_usd")
        except (ValueError, IndexError, AttributeError):
            pass
        return {"session": traces[-1].stem if traces else None, "cost_usd": cost, "exit_code": code,
                "agent_output_tail": out[-1500:], "agent_seconds": round(time.time() - t0, 1)}


def load_harness(path: Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    cfg["_dir"] = str(Path(path).resolve().parent)
    return cfg


def make_agent(cfg: dict):
    agent = cfg.get("agent") or {}
    if agent.get("type") == "scripted":
        script = yaml.safe_load((Path(cfg["_dir"]) / agent["script"]).read_text()) or {}
        return ScriptedAgent(script)
    if agent.get("type") == "command":
        return CommandAgent(agent)
    raise ValueError(f"unknown agent type {agent.get('type')!r} (expected scripted or command)")

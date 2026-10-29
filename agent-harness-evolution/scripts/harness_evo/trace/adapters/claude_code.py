"""Claude Code adapter (hook contract verified against code.claude.com/docs/en/hooks, 2026-10-27).

Input: the JSON a hook receives on stdin. Output: trace events with derived
fields, and the decision JSON a hook prints to stdout. Every harness-specific
detail lives here and in hooks/settings.snippet.json.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
from pathlib import Path

from ...config import Profile
from .. import relevance, shell, testdiff
from ..state import State

NAME = "claude-code"
EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "PreToolUse": "tool_call",
    "PostToolUse": "tool_result",
    "PostToolUseFailure": "tool_result",
    "Stop": "stop_attempt",
    "SubagentStop": "stop_attempt",
    "SessionEnd": "session_end",
}
HANDLED = set(EVENTS) | {"PreCompact"}
FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
# Text is kept whole up to PRECAP here; the hook redacts it and only then clips it to the
# stored length (redact.CLIP), so truncation can never cut a secret in half before redaction.
PRECAP = 65536
_EXIT = re.compile(r"^Exit code (-?\d+)")
_PATHLIKE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+|[\w-]+\.[A-Za-z]{1,6})(?![\w/])")
_TASK_TYPES = [
    ("bug_fix", re.compile(r"\b(fix\w*|bug\w*|broken|errors?|fail\w*|crash\w*|regression|incorrect|wrong|off by)\b", re.I)),
    ("refactor", re.compile(r"\b(refactor\w*|clean\s?up|rename\w*|extract\w*|simplif\w*|restructur\w*|reorgani[sz]\w*)\b", re.I)),
    ("config", re.compile(r"\b(config\w*|settings?|env|ci|workflows?|pipelines?|ya?ml|dependenc\w*|upgrade\w*|bump\w*|docker\w*)\b", re.I)),
    ("feature", re.compile(r"\b(add\w*|implement\w*|support\w*|create\w*|build\w*|new|introduc\w*|enabl\w*)\b", re.I)),
]


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:n]


def fingerprint(path: Path) -> list | None:
    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def guess_task_type(prompt: str) -> str | None:
    scores = [(len(rx.findall(prompt or "")), -i, name) for i, (name, rx) in enumerate(_TASK_TYPES)]
    best = max(scores)
    return best[2] if best[0] else None


class Adapter:
    def __init__(self, root: Path, profile: Profile, state: State):
        self.root = Path(root).resolve()
        self.profile = profile
        self.state = state

    # ------------------------------------------------------------ paths
    def rel(self, path: str | None, cwd: str | None = None) -> str | None:
        if not path:
            return None
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = Path(cwd or self.root) / p
        try:
            return posixpath.normpath(p.resolve().relative_to(self.root).as_posix())
        except (ValueError, OSError):
            home = str(Path.home())
            s = str(p)
            return "~" + s[len(home):] if s.startswith(home) else s

    def abs(self, rel: str) -> Path:
        return self.root / rel

    # ------------------------------------------------------------ events
    def convert(self, hook: str, data: dict) -> list[tuple[str, str, dict, dict]]:
        """Return (type, actor, payload, derived) tuples for one hook call."""
        t = EVENTS.get(hook)
        if not t:
            return []
        actor = data.get("agent_id") or "main"
        handler = getattr(self, "_" + t)
        out = handler(hook, data)
        return [(typ, actor, payload, derived) for typ, payload, derived in out]

    def _session_start(self, hook, data):
        return [("session_start", {"source": data.get("source"), "model": data.get("model"),
                                   "permission_mode": data.get("permission_mode")}, {})]

    def _prompt(self, hook, data):
        text = data.get("prompt") or ""
        payload = {"length": len(text)}
        if self.profile.prompt_mode == "hash":
            payload["prompt_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        else:
            payload["text"] = text[:PRECAP]
        return [("prompt", payload, {"task_type": guess_task_type(text), "scope": self._prompt_scope(text)})]

    def _prompt_scope(self, text: str) -> list[str]:
        scope = []
        for m in _PATHLIKE.finditer(text or ""):
            rel = self.rel(m.group(1))
            if not rel or rel.startswith(("~", "/", "..")):
                continue
            p = self.abs(rel)
            parent = posixpath.dirname(rel)
            if p.is_dir() and rel != ".":
                scope.append(f"dir:{rel}")
            elif p.is_file():
                scope.append(f"dir:{parent}" if parent else f"file:{rel}")
            elif parent and p.parent.is_dir():
                scope.append(f"dir:{parent}")
            else:
                continue
            scope += [f"group:{g.name}" for g in self.profile.groups_for(rel)]
        for g in self.profile.test_map:
            if re.search(rf"\b{re.escape(g.name)}\b", text or "", re.I):
                scope.append(f"group:{g.name}")
        return list(dict.fromkeys(scope))

    def _tool_call(self, hook, data):
        tool, args = data.get("tool_name") or "", data.get("tool_input") or {}
        payload = {"tool": tool, "tool_use_id": data.get("tool_use_id"), "args": self._args(tool, args, data)}
        if data.get("mcp_server"):
            payload["mcp_server"] = data["mcp_server"]
        return [("tool_call", payload, self._pre_derived(tool, args, data))]

    def _tool_result(self, hook, data):
        tool, args = data.get("tool_name") or "", data.get("tool_input") or {}
        tid = data.get("tool_use_id")
        failed = hook == "PostToolUseFailure"
        payload = {"tool": tool, "tool_use_id": tid, "status": "error" if failed else "ok",
                   "duration_ms": data.get("duration_ms")}
        output, exit_code = "", None
        if failed:
            err = data.get("error") or ""
            m = _EXIT.match(err)
            exit_code = int(m.group(1)) if m else None
            payload["error"] = err[-PRECAP:]
            payload["is_interrupt"] = bool(data.get("is_interrupt"))
            output = err
        else:
            resp = data.get("tool_response")
            if tool == "Bash" and isinstance(resp, dict):
                output = (resp.get("stdout") or "") + ("\n" + resp["stderr"] if resp.get("stderr") else "")
                payload["output_tail"] = output[-PRECAP:]
                payload["interrupted"] = bool(resp.get("interrupted"))
                exit_code = 0
        if exit_code is not None:
            payload["exit_code"] = exit_code
        pre = self.state.pending.get(tid) or self._pre_derived(tool, args, data)
        derived = self._post_derived(tool, args, data, pre, ok=not failed, output=output, exit_code=exit_code)
        events = [("tool_result", payload, derived)]
        if derived.get("commit") and not failed:
            m = re.search(r"\[[^\]\n]*?\b([0-9a-f]{7,40})\]", output)
            events.append(("commit", {"sha": m.group(1) if m else None, "tool_use_id": tid}, {}))
        return events

    def _stop_attempt(self, hook, data):
        active = bool(data.get("stop_hook_active"))
        payload = {"final_message": (data.get("last_assistant_message") or "")[:PRECAP], "stop_hook_active": active}
        if hook == "SubagentStop":
            payload["agent_type"] = data.get("agent_type")
        derived = {"override": list(self.state.pending_gate) if active else []}
        return [("stop_attempt", payload, derived)]

    def _session_end(self, hook, data):
        return [("session_end", {"reason": data.get("reason")}, {})]

    # ------------------------------------------------------------ derived fields
    def _args(self, tool: str, args: dict, data: dict) -> dict:
        cwd = data.get("cwd")
        if tool in FILE_TOOLS or tool == "Read":
            out = {"path": self.rel(args.get("file_path") or args.get("notebook_path"), cwd)}
            if tool == "Edit":
                out.update(old_string=(args.get("old_string") or "")[:PRECAP], new_string=(args.get("new_string") or "")[:PRECAP],
                           replace_all=bool(args.get("replace_all")))
            elif tool == "Write":
                content = args.get("content") or ""
                out.update(content_length=len(content), content_sha256=sha(content, 64))
            elif tool == "MultiEdit":
                out["edits"] = len(args.get("edits") or [])
            elif tool == "Read":
                out.update({k: args[k] for k in ("offset", "limit") if k in args})
            return out
        if tool == "Bash":
            return {"command": (args.get("command") or "")[:PRECAP], "run_in_background": bool(args.get("run_in_background"))}
        if tool in {"Grep", "Glob"}:
            return {k: args.get(k) for k in ("pattern", "path", "glob") if args.get(k) is not None}
        return {k: (v if len(str(v)) <= PRECAP else str(v)[:PRECAP]) for k, v in args.items()}

    def _write_entry(self, path: str, *, needs_read: bool, old: str | None, new: str | None,
                     whitespace_only: bool = False) -> dict:
        p = self.abs(path)
        exists = p.exists()
        known = self.state.reads.get(path)
        now = fingerprint(p) if exists else None
        entry = {
            "path": path, "exists": exists, "needs_read": needs_read and exists,
            "read_before_write": known is not None,
            "changed_since_read": known is not None and now is not None and list(known) != list(now),
            "code": self.profile.is_code(path), "test": self.profile.is_test(path),
            "risky": self.profile.is_risky(path), "protected": self.profile.is_protected(path),
            "secret": self.profile.is_secret(path), "whitespace_only": whitespace_only,
        }
        if new is not None:
            entry["old_hash"] = sha(old or "")
            entry["new_hash"] = sha(new)
        if entry["test"] and new is not None:
            entry["test_change"] = testdiff.analyse(old or "", new)
        return entry

    def _pre_derived(self, tool: str, args: dict, data: dict) -> dict:
        cwd = data.get("cwd")
        d: dict = {}
        if tool == "Read":
            d = {"kind": "file_read", "paths": [self.rel(args.get("file_path"), cwd)]}
        elif tool in FILE_TOOLS:
            path = self.rel(args.get("file_path") or args.get("notebook_path"), cwd)
            if tool == "Edit":
                old, new = args.get("old_string") or "", args.get("new_string") or ""
            elif tool == "MultiEdit":
                edits = args.get("edits") or []
                old = "\n".join(e.get("old_string") or "" for e in edits)
                new = "\n".join(e.get("new_string") or "" for e in edits)
            elif tool == "Write":
                try:
                    old = self.abs(path).read_text(errors="replace") if self.abs(path).is_file() else ""
                except OSError:
                    old = ""
                new = args.get("content") or ""
            else:
                old, new = "", args.get("new_source") or ""
            ws = tool != "Write" and "".join(old.split()) == "".join(new.split())
            w = self._write_entry(path, needs_read=True, old=old, new=new, whitespace_only=ws)
            d = {"kind": "file_write", "paths": [path], "writes": [w], "file_exists": w["exists"],
                 "read_before_write": w["read_before_write"]}
        elif tool == "Bash":
            cmd = args.get("command") or ""
            facts = shell.analyse(cmd)
            reads = [r for r in (self.rel(x, cwd) for x in facts.reads) if r and self.abs(r).is_file()]
            writes = [self._write_entry(p, needs_read=True, old=None, new=None)
                      for p in (self.rel(x, cwd) for x in facts.edits) if p]
            writes += [self._write_entry(p, needs_read=False, old=None, new=None)
                       for p in (self.rel(x, cwd) for x in facts.writes) if p]
            test = shell.test_invocation(cmd, self.profile.test_commands)
            kind = "test_run" if test else "file_write" if writes else "file_read" if reads else "command"
            d = {"kind": kind, "reads": [{"path": r} for r in reads], "writes": writes,
                 "deletes": [p for p in (self.rel(x, cwd) for x in facts.deletes) if p],
                 "commit": facts.is_commit,
                 "destructive": shell.destructive_matches(cmd, self.profile.destructive_allow,
                                                          self.profile.destructive_extra)}
            if writes:
                d["read_before_write"] = all(w["read_before_write"] for w in writes if w["needs_read"])
            if test:
                d["test"] = {"targets": [t for t in (self._rel_target(x, cwd) for x in test.targets) if t],
                             "filters": test.filters, "full": test.full_suite}
        elif tool in {"Grep", "Glob"}:
            d = {"kind": "search"}
        else:
            d = {"kind": "other"}
        d["call_sig"] = self._call_sig(tool, args)
        return d

    def _rel_target(self, target: str, cwd: str | None) -> str:
        r = self.rel(target, cwd)
        return r if r and not r.startswith(("~", "/")) else target

    def _call_sig(self, tool: str, args: dict) -> str:
        if tool == "Bash":
            norm = re.sub(r"\d+", "#", " ".join((args.get("command") or "").split()))
        else:
            items = sorted((k, str(v)) for k, v in args.items() if k not in {"description", "timeout"})
            norm = re.sub(r"\d+", "#", repr(items))
        return sha(f"{tool}|{norm}", 12)

    def _post_derived(self, tool, args, data, pre: dict, *, ok: bool, output: str, exit_code) -> dict:
        d = {k: v for k, v in pre.items() if k not in {"writes", "reads"}}
        d["writes"] = []
        d["reads"] = []
        if ok:
            if pre.get("kind") == "file_read" and tool == "Read":
                d["reads"] = [{"path": p, "fp": fingerprint(self.abs(p))} for p in pre.get("paths") or [] if p]
            else:
                d["reads"] = [{"path": r["path"], "fp": fingerprint(self.abs(r["path"]))} for r in pre.get("reads") or []]
            for w in pre.get("writes") or []:
                w = dict(w)
                w["fp"] = fingerprint(self.abs(w["path"]))
                seed, in_scope = self._scope(w["path"])
                if seed:
                    w["scope_seed"] = seed
                if in_scope is not None:
                    w["in_scope"] = in_scope
                d["writes"].append(w)
        else:
            err = (data.get("error") or "").splitlines()
            first = next((ln for ln in err if ln.strip() and not _EXIT.match(ln)), "")
            norm_err = re.sub(r"\d+|0x[0-9a-f]+", "#", " ".join(first.split()))[:200]
            exit_part = err[0] if err and _EXIT.match(err[0]) else ""
            d["error_head"] = first[:PRECAP]
            d["failure_sig"] = f"{pre.get('call_sig')}:{sha(exit_part + '|' + norm_err, 10)}"
        test = pre.get("test")
        if test and (ok or exit_code is not None or tool == "Bash"):
            changed = list(dict.fromkeys(list(self.state.unverified) + list(self.state.untested_risky)))
            d["test_run"] = {"command": (args.get("command") or "")[:PRECAP], "targets": test["targets"],
                             "filters": test["filters"], "full": test["full"],
                             "passed": shell.test_passed(exit_code, output) if ok or exit_code is not None else False,
                             "covers": relevance.covers(test["targets"], test["filters"], changed, self.profile)}
        if d.get("test_run"):
            d["files_changed_since_last_test"] = []
        else:
            since = list(self.state.since_test)
            since += [w["path"] for w in d["writes"] if w.get("code") and w["path"] not in since]
            d["files_changed_since_last_test"] = since
        return d

    def _scope(self, path: str) -> tuple[list | None, bool | None]:
        scope = self.state.scope
        groups = [g.name for g in self.profile.groups_for(path)]
        if not scope:
            parent = posixpath.dirname(path)
            seed = [f"dir:{parent}" if parent else f"file:{path}"]
            return seed + [f"group:{g}" for g in groups], True
        for item in scope:
            kind, _, val = item.partition(":")
            if kind == "dir" and (path == val or path.startswith(val + "/")):
                return None, True
            if kind == "file" and path == val:
                return None, True
            if kind == "group" and val in groups:
                return None, True
        return None, False


# ---------------------------------------------------------------- decisions -> stdout

def render(hook: str, decision: dict | None) -> dict | None:
    """decision: {"deny": reason} | {"block": reason} | {"context": text}"""
    if not decision:
        return None
    if hook == "PreToolUse":
        if decision.get("deny"):
            return {"hookSpecificOutput": {"hookEventName": hook, "permissionDecision": "deny",
                                           "permissionDecisionReason": decision["deny"]}}
        if decision.get("context"):
            # additionalContext only: no permissionDecision, so the normal permission flow still applies.
            return {"hookSpecificOutput": {"hookEventName": hook, "additionalContext": decision["context"]}}
        return None
    if hook in {"Stop", "SubagentStop"}:
        if decision.get("block"):
            return {"decision": "block", "reason": decision["block"]}
        return None
    if hook in {"PostToolUse", "PostToolUseFailure", "SessionStart", "UserPromptSubmit"} and decision.get("context"):
        return {"hookSpecificOutput": {"hookEventName": hook, "additionalContext": decision["context"][:9500]}}
    return None

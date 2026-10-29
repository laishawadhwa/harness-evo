"""Per-session state and the reducer that folds trace events into it.

The reducer reads only the stored event (payload + derived), never the
filesystem, so replaying a trace rebuilds exactly the state the live hooks saw.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .schema import ts_epoch

MAX_LATENCIES = 400


@dataclass
class State:
    session: str = ""
    seq: int = 0
    turn: int = 0
    model: str | None = None
    harness: dict = field(default_factory=dict)
    arm: str | None = None
    task_type: str | None = None
    scope: list = field(default_factory=list)              # "dir:<path>" / "group:<name>"
    reads: dict = field(default_factory=dict)              # path -> fingerprint the agent last saw
    edits: dict = field(default_factory=dict)              # path -> edit count
    unverified: dict = field(default_factory=dict)         # code path -> {seq, actor} not yet covered by a pass
    untested_risky: dict = field(default_factory=dict)     # risky path -> edits since a relevant test run
    risky_epoch: dict = field(default_factory=dict)        # risky path -> resets so far
    since_test: list = field(default_factory=list)         # code paths changed since the last test run
    last_code_edit_seq: int = 0
    last_test: dict | None = None
    tests_run: int = 0
    tests_passed: int = 0
    failures: dict = field(default_factory=dict)           # "<call_sig>:<err_sig>" -> count
    failure_epoch: dict = field(default_factory=dict)      # call_sig -> successes after failures
    edit_log: dict = field(default_factory=dict)           # path -> [[seq, t, old_hash, new_hash]]
    out_of_scope: list = field(default_factory=list)
    churn: list = field(default_factory=lambda: [0, 0])    # [edits, whitespace-only edits]
    tool_calls: int = 0
    stop_attempts: int = 0
    commits: list = field(default_factory=list)
    pending: dict = field(default_factory=dict)            # tool_use_id -> derived of the tool_call
    fired: dict = field(default_factory=dict)              # detector -> target -> [state keys]
    delivered: int = 0                                     # advisory nudges delivered this session
    turn_delivered: int = 0
    pending_gate: list = field(default_factory=list)       # detectors that blocked the last stop
    latencies_ms: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


def load(path: Path) -> State | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError("state file is not an object")
    return State.from_dict(data)


def save(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state.to_dict(), separators=(",", ":")))
    os.replace(tmp, path)


def apply(s: State, e: dict) -> None:
    """Fold one event into the state."""
    s.seq = max(s.seq, e.get("seq", 0))
    t, p, d, actor = e["type"], e.get("payload") or {}, e.get("derived") or {}, e.get("actor", "main")
    if e.get("model"):
        s.model = e["model"]

    if t == "session_start":
        s.harness = e.get("harness") or s.harness
        if "arm" in d:
            s.arm = d["arm"]
    elif t == "prompt":
        s.turn += 1
        s.turn_delivered = 0
        s.task_type = d.get("task_type") or s.task_type
        for item in d.get("scope") or []:
            if item not in s.scope:
                s.scope.append(item)
    elif t == "tool_call":
        s.tool_calls += 1
        if p.get("tool_use_id"):
            s.pending[p["tool_use_id"]] = d
    elif t == "tool_result":
        _apply_result(s, e, p, d, actor)
    elif t == "stop_attempt":
        s.stop_attempts += 1
        s.pending_gate = []
    elif t == "nudge":
        if p.get("delivered"):
            if p.get("level") == "advise":
                s.delivered += 1
                s.turn_delivered += 1
            elif p.get("level") == "gate":
                s.pending_gate.append(p["detector"])
    elif t == "commit":
        if p.get("sha"):
            s.commits.append(p["sha"])


def _apply_result(s: State, e: dict, p: dict, d: dict, actor: str) -> None:
    s.pending.pop(p.get("tool_use_id"), None)
    ok = p.get("status") == "ok"
    call_sig = d.get("call_sig")
    if not ok and d.get("failure_sig"):
        s.failures[d["failure_sig"]] = s.failures.get(d["failure_sig"], 0) + 1
    if ok and call_sig:
        stale = [k for k in s.failures if k.startswith(call_sig + ":")]
        if stale:
            for k in stale:
                del s.failures[k]
            s.failure_epoch[call_sig] = s.failure_epoch.get(call_sig, 0) + 1
    if ok:
        for r in d.get("reads") or []:
            s.reads[r["path"]] = r.get("fp")
        for path in d.get("deletes") or []:
            s.reads.pop(path, None)
        for w in d.get("writes") or []:
            _apply_write(s, e, w, actor)
    tr = d.get("test_run")
    if tr:
        s.tests_run += 1
        s.last_test = {"seq": e["seq"], "passed": bool(tr.get("passed")), "targets": tr.get("targets") or [],
                       "full": bool(tr.get("full"))}
        s.since_test = []
        for path in tr.get("covers") or []:
            if path in s.untested_risky:
                del s.untested_risky[path]
                s.risky_epoch[path] = s.risky_epoch.get(path, 0) + 1
            if tr.get("passed"):
                s.unverified.pop(path, None)
        if tr.get("passed"):
            s.tests_passed += 1


def _apply_write(s: State, e: dict, w: dict, actor: str) -> None:
    path = w["path"]
    s.reads[path] = w.get("fp")  # the agent knows what it just wrote
    s.edits[path] = s.edits.get(path, 0) + 1
    if not s.scope and w.get("scope_seed"):
        s.scope = list(w["scope_seed"])
    if w.get("in_scope") is False and path not in s.out_of_scope:
        s.out_of_scope.append(path)
    s.churn[0] += 1
    if w.get("whitespace_only"):
        s.churn[1] += 1
    if w.get("code"):
        s.unverified[path] = {"seq": e["seq"], "actor": actor}
        s.last_code_edit_seq = e["seq"]
        if path not in s.since_test:
            s.since_test.append(path)
    if w.get("risky"):
        s.untested_risky[path] = s.untested_risky.get(path, 0) + 1
    if w.get("new_hash"):
        s.edit_log.setdefault(path, []).append([e["seq"], ts_epoch(e["ts"]), w.get("old_hash"), w["new_hash"]])
        del s.edit_log[path][:-50]

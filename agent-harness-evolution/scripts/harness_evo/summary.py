"""Session summary (counts, verification, nudges, adherence, latency, cost) and nudge metrics."""
from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from pathlib import Path

from .config import Policy, Profile
from .detectors import BY_ID
from .replay import replay_events

TRANSCRIPT_LIMIT = 50 * 1024 * 1024


def adherence_window(events: list[dict], index: int, actor: str, n: int) -> list[dict]:
    """Events after events[index] until the agent has taken `n` more actions (tool calls)."""
    out, calls = [], 0
    for e in events[index + 1:]:
        if e.get("actor") != actor or e["type"] == "nudge":
            continue
        if e["type"] == "tool_call":
            if calls == n:
                break
            calls += 1
        out.append(e)
        if e["type"] == "stop_attempt" and calls >= n:
            break
    return out


def nudge_outcomes(events: list[dict], window: int = 3) -> list[dict]:
    """Each nudge with whether the next `window` actions complied and whether it re-fired later."""
    out = []
    last_ignored: dict[tuple, bool] = {}
    for i, e in enumerate(events):
        if e["type"] != "nudge":
            continue
        p = e["payload"]
        key = (p["detector"], p["target"])
        det = BY_ID.get(p["detector"])
        complied = None
        if p.get("delivered") and det:
            after = adherence_window(events, i, e.get("actor", "main"), window)
            complied = det.complied(p, after) if after else None
        refired = bool(last_ignored.get(key))
        if p.get("delivered"):
            last_ignored[key] = complied is False
        out.append({"id": p.get("id"), "detector": p["detector"], "target": p["target"], "level": p.get("level"),
                    "delivered": bool(p.get("delivered")), "complied": complied, "refired": refired,
                    "seq": e["seq"], "trigger_seq": p.get("trigger_seq")})
    return out


def _rework(events: list[dict]) -> int:
    covered, rework = set(), 0
    for e in events:
        if e["type"] != "tool_result":
            continue
        d = e.get("derived") or {}
        if e["payload"].get("status") == "ok":
            for w in d.get("writes") or []:
                if w["path"] in covered:
                    rework += 1
                    covered.discard(w["path"])
        tr = d.get("test_run")
        if tr and tr.get("passed"):
            covered |= set(tr.get("covers") or [])
    return rework


def summarize(events: list[dict], profile: Profile, policy: Policy | None = None, *, latencies=None,
              usage: dict | None = None) -> dict:
    window = policy.adherence_window if policy else 3
    _, s = replay_events(events, profile)
    results = [e for e in events if e["type"] == "tool_result"]
    writes = [w for e in results if e["payload"].get("status") == "ok" for w in e["derived"].get("writes") or []]
    reads = {r["path"] for e in results for r in e["derived"].get("reads") or []}
    retries = sum(c - 1 for c in s.failures.values() if c > 1)
    code_changed = any(w.get("code") for w in writes)
    nudges = nudge_outcomes(events, window)
    by_det: dict = defaultdict(lambda: {"fired": 0, "delivered": 0, "complied": 0, "ignored": 0, "refired": 0})
    for n in nudges:
        b = by_det[n["detector"]]
        b["fired"] += 1
        b["delivered"] += n["delivered"]
        b["complied"] += n["complied"] is True
        b["ignored"] += n["complied"] is False
        b["refired"] += n["refired"]
    delivered = [n for n in nudges if n["delivered"]]
    judged = [n for n in delivered if n["complied"] is not None]
    stops = [e for e in events if e["type"] == "stop_attempt"]
    lat = sorted(latencies or [])
    starts = [e for e in events if e["type"] == "session_start"]
    return {
        "session": s.session,
        "harness": starts[0]["harness"] if starts else None,
        "model": s.model,
        "arm": s.arm,
        "task_type": s.task_type,
        "started": events[0]["ts"] if events else None,
        "ended": events[-1]["ts"] if events else None,
        "counts": {
            "tool_calls": s.tool_calls,
            "tool_errors": sum(1 for e in results if e["payload"].get("status") == "error"),
            "retries": retries,
            "files_read": len(reads),
            "files_changed": len({w["path"] for w in writes}),
            "tests_run": s.tests_run,
            "tests_passed": s.tests_passed,
            "commits": len(s.commits),
            "stop_attempts": len(stops),
        },
        "verification": {
            "code_changed": code_changed,
            "test_after_last_edit": (not s.unverified and bool(s.last_test and s.last_test["passed"]))
            if code_changed else None,
            "unverified_files": sorted(s.unverified),
            "unsupported_claim": any(n["detector"] == "D5" for n in nudges),
        },
        "nudges": {"fired": len(nudges), "delivered": len(delivered), "by_detector": dict(by_det)},
        "adherence": {"delivered": len(delivered), "judged": len(judged),
                      "complied": sum(1 for n in judged if n["complied"]),
                      "rate": round(sum(1 for n in judged if n["complied"]) / len(judged), 3) if judged else None},
        "gates": {"fired": [n["detector"] for n in nudges if n["level"] == "gate" and n["delivered"]],
                  "overridden": sorted({d for e in stops for d in e["derived"].get("override") or []})},
        "rework": _rework(events),
        "hook_latency": {"n": len(lat), "p50_ms": round(statistics.median(lat), 2) if lat else None,
                         "p95_ms": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2) if lat else None,
                         "max_ms": lat[-1] if lat else None, "measured": "in-process"},
        "cost": usage or {"input_tokens": None, "output_tokens": None, "cost_usd": None},
    }


def transcript_usage(path: str | None) -> dict | None:
    """Sum token usage from a Claude Code transcript (JSONL). Cost uses config/pricing.yaml."""
    if not path:
        return None
    try:
        if os.path.getsize(path) > TRANSCRIPT_LIMIT:
            return None
        totals = defaultdict(int)
        models = defaultdict(lambda: defaultdict(int))
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                msg = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    v = msg["usage"].get(k) or 0
                    totals[k] += v
                    models[msg.get("model") or "unknown"][k] += v
    except OSError:
        return None
    from .pricing import estimate
    return {**totals, "cost_usd": estimate(models), "by_model": {m: dict(v) for m, v in models.items()}}


def load_summaries(sessions_dir: Path) -> list[dict]:
    out = []
    for p in sorted(Path(sessions_dir).glob("*.summary.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return out

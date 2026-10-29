"""Event constructor and validator for trace contract v1 (spec section 4)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

VERSION = 1
TYPES = ("session_start", "prompt", "tool_call", "tool_result", "stop_attempt", "nudge", "commit", "session_end")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ts_epoch(ts: str) -> float:
    try:
        return datetime.strptime(ts.rstrip("Z")[:23], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return datetime.strptime(ts.rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def make_event(session: str, seq: int, type_: str, *, harness: dict, model: str | None, actor: str = "main",
               payload: dict | None = None, derived: dict | None = None, ts: str | None = None) -> dict:
    return {
        "v": VERSION,
        "session": session,
        "seq": seq,
        "ts": ts or now_ts(),
        "type": type_,
        "actor": actor,
        "harness": harness,
        "model": model,
        "payload": payload or {},
        "derived": derived or {},
    }


def validate(event: dict) -> list[str]:
    errors = []
    if event.get("v") != VERSION:
        errors.append(f"v must be {VERSION}")
    for key in ("session", "actor"):
        if not isinstance(event.get(key), str) or not event.get(key):
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(event.get("seq"), int) or event["seq"] < 1:
        errors.append("seq must be a positive integer")
    if not isinstance(event.get("ts"), str) or not _TS.match(event["ts"]):
        errors.append("ts must be an ISO-8601 UTC timestamp ending in Z")
    if event.get("type") not in TYPES:
        errors.append(f"type must be one of {', '.join(TYPES)}")
    harness = event.get("harness")
    if not isinstance(harness, dict) or not harness.get("name"):
        errors.append("harness.name is required")
    if "model" not in event:
        errors.append("model is required (may be null)")
    for key in ("payload", "derived"):
        if not isinstance(event.get(key), dict):
            errors.append(f"{key} must be an object")
    return errors


def validate_stream(events: list[dict]) -> list[str]:
    errors = []
    last = 0
    for e in events:
        errors += [f"seq {e.get('seq')}: {msg}" for msg in validate(e)]
        if isinstance(e.get("seq"), int):
            if e["seq"] <= last:
                errors.append(f"seq {e['seq']}: not monotonic (after {last})")
            last = e["seq"]
    return errors

"""Batch mode: replay a stored trace through the same reducer, detectors and de-duplication."""
from __future__ import annotations

from pathlib import Path

from .config import Policy, Profile
from .detectors import REGISTRY, fire, run
from .trace import store
from .trace.state import State, apply


def replay_events(events: list[dict], profile: Profile, policy: Policy | None = None) -> tuple[list[dict], State]:
    s = State(session=events[0]["session"] if events else "")
    findings = []
    for e in events:
        apply(s, e)
        if e["type"] == "nudge":
            continue  # a nudge is an output of detection, not an input
        for f in fire(run(REGISTRY, e, s, profile), s):
            message = policy.render(f.template, f.params) if policy else None
            findings.append(f.to_dict(s.session, message))
    return findings, s


def replay(trace: Path, profile: Profile, policy: Policy | None = None) -> tuple[list[dict], State]:
    return replay_events(store.read(trace), profile, policy)


def rebuild(trace: Path, profile: Profile) -> State | None:
    events = store.read(trace)
    if not events:
        return None
    return replay_events(events, profile)[1]

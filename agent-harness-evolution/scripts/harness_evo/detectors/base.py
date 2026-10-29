"""Detector base class, findings, and the shared fire/de-duplicate step.

A detector is a pure function of (event, session state, repo profile). The
same code runs live inside a hook and in batch over a stored trace.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Profile
from ..trace.state import State

log = logging.getLogger("harness_evo.detectors")


@dataclass
class Finding:
    detector: str
    name: str
    target: str
    state_key: str
    template: str
    params: dict
    evidence: list
    seq: int
    extra: dict = field(default_factory=dict)

    def finding_id(self, session: str) -> str:
        return f"{session}#{self.seq}:{self.detector}"

    def to_dict(self, session: str, message: str | None = None) -> dict:
        return {"id": self.finding_id(session), "detector": self.detector, "name": self.name, "seq": self.seq,
                "target": self.target, "state_key": self.state_key, "template": self.template,
                "params": self.params, "evidence": self.evidence, "message": message, **self.extra}


class Detector:
    id = "D0"
    name = "base"
    on: tuple = ()
    summary = ""

    def check(self, e: dict, s: State, p: Profile) -> list[Finding]:
        raise NotImplementedError

    def complied(self, nudge: dict, after: list[dict]) -> bool | None:
        """Did the agent's next actions (events after the nudge) comply? None if unknowable."""
        return None

    def finding(self, e: dict, target: str, key: str, template: str, params: dict,
                evidence: list | None = None, **extra) -> Finding:
        return Finding(detector=self.id, name=self.name, target=target, state_key=key, template=template,
                       params=params, evidence=sorted(set(evidence or []) | {e["seq"]}), seq=e["seq"], extra=extra)


def writes(e: dict, ok_only: bool = True) -> list[dict]:
    d = e.get("derived") or {}
    if ok_only and e["type"] == "tool_result" and (e.get("payload") or {}).get("status") != "ok":
        return []
    return d.get("writes") or []


def results(after: list[dict]) -> list[dict]:
    return [a for a in after if a["type"] == "tool_result"]


def run(detectors, e: dict, s: State, p: Profile) -> list[Finding]:
    out = []
    for det in detectors:
        if e["type"] not in det.on:
            continue
        try:
            out += det.check(e, s, p) or []
        except Exception:  # a broken detector never breaks the hook or the other detectors
            log.exception("detector %s failed on seq %s", det.id, e.get("seq"))
    return out


def fire(findings: list[Finding], s: State) -> list[Finding]:
    """Drop findings whose (detector, target, state key) already fired: a detector does not
    repeat for the same target until the state that triggered it has changed."""
    fired = []
    for f in findings:
        seen = s.fired.setdefault(f.detector, {}).setdefault(f.target, [])
        if f.state_key in seen:
            continue
        seen.append(f.state_key)
        del seen[:-20]
        fired.append(f)
    return fired

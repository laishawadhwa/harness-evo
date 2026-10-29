"""Nudge policy (spec section 6): levels, volume caps, mutes, the A/B arm and wording.

Detectors decide *whether* something is wrong. The policy decides what the
agent is told. Every fired finding becomes a nudge event, delivered or not, so
precision and over-nudging can be measured later.
"""
from __future__ import annotations

import hashlib

from .config import Policy, Profile
from .detectors import ORDER, Finding
from .trace.state import State

RANK = {"info": 0, "advise": 1, "gate": 2, "deny": 3}


def ab_arm(session: str) -> str:
    """Nudge on/off assignment by a hash of the session id."""
    return "on" if int(hashlib.sha256(session.encode()).hexdigest(), 16) % 2 == 0 else "off"


def contract(policy: Policy, profile: Profile) -> str:
    cmds = [g.command for g in profile.test_map if g.command]
    hint = f"for example `{cmds[0]}`" if cmds else "the repo's test command"
    return "\n".join(line.replace("{test_hint}", hint) for line in policy.contract)


def decide(fired: list[Finding], s: State, policy: Policy, event: dict, session: str) -> tuple[dict | None, list[dict]]:
    """Return the decision for this hook call and one nudge payload per fired finding."""
    def rank(f: Finding):
        return (-RANK.get(policy.level(f.detector), 0), -int(bool(f.extra.get("escalated"))), ORDER.get(f.detector, 99))

    is_stop = event["type"] == "stop_attempt"
    is_call = event["type"] == "tool_call"
    turn_used, session_used = s.turn_delivered, s.delivered
    denies, gates, advice = [], [], None
    records = []
    for f in sorted(fired, key=rank):
        level = policy.level(f.detector)
        if level == "gate" and not is_stop:
            level = "advise"   # a gate only means something at a stop
        if level == "deny" and not is_call:
            level = "advise"   # only a pending tool call can be denied
        message = policy.render(f.template, f.params)
        rec = f.to_dict(session, message)
        rec.pop("seq", None)
        rec.update(trigger_seq=f.seq, level=level, delivered=False, muted=False, suppressed=None, arm=s.arm)
        if policy.is_muted(f.detector, session):
            rec.update(muted=True, suppressed="muted")
        elif level == "info":
            rec["suppressed"] = "info"
        elif level == "deny":
            rec["delivered"] = True
            denies.append(message)
        elif s.arm == "off":
            rec["suppressed"] = "ab_off"
        elif level == "gate":
            rec["delivered"] = True
            gates.append(message)
        elif denies:
            rec["suppressed"] = "superseded"
        elif advice is not None:
            rec["suppressed"] = "one_per_call"
        elif session_used >= policy.per_session:
            rec["suppressed"] = "session_cap"
        elif turn_used >= policy.per_turn and not f.extra.get("escalated"):
            rec["suppressed"] = "turn_cap"
        else:
            rec["delivered"] = True
            advice = message
            turn_used += 1
            session_used += 1
        records.append(rec)
    if denies:
        return {"deny": denies[0]}, records
    if gates:
        return {"block": "\n".join(gates)}, records
    if advice:
        return {"context": advice}, records
    return None, records

"""D2 blind-retry: the same call fails repeatedly with the same error."""
from __future__ import annotations

from .base import Detector


class BlindRetry(Detector):
    id, name, on = "D2", "blind-retry", ("tool_result",)
    summary = "The same tool with near-identical arguments fails three times with the same error."

    def check(self, e, s, p):
        d, payload = e.get("derived") or {}, e.get("payload") or {}
        sig = d.get("failure_sig")
        if payload.get("status") != "error" or not sig:
            return []
        count = s.failures.get(sig, 0)
        repeat = p.threshold("D2", "repeat", s.task_type, 3)
        escalate = p.threshold("D2", "escalate", s.task_type, 5)
        if count >= escalate:
            tier, template = "escalate", "D2.escalate"
        elif count >= repeat:
            tier, template = "repeat", "D2.repeat"
        else:
            return []
        call_sig = d.get("call_sig")
        target = f"{payload.get('tool')}:{call_sig}"
        epoch = s.failure_epoch.get(call_sig, 0)
        params = {"tool": payload.get("tool"), "count": count, "error": d.get("error_head") or "unknown error"}
        return [self.finding(e, target, f"{sig}:{epoch}:{tier}", template, params,
                             count=count, escalated=tier == "escalate", call_sig=call_sig)]

    def complied(self, nudge, after):
        calls = [a for a in after if a["type"] == "tool_call"]
        if not calls:
            return None
        return (calls[0].get("derived") or {}).get("call_sig") != nudge.get("call_sig")

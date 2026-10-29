"""D9 scope-drift: changes outside the task's area, or a diff that is mostly formatting churn."""
from __future__ import annotations

from .base import Detector, results, writes


class ScopeDrift(Detector):
    id, name, on = "D9", "scope-drift", ("tool_result",)
    summary = "Changed files fall in directories unrelated to the task, or the diff is mostly formatting churn."

    def check(self, e, s, p):
        ws = writes(e)
        if not ws:
            return []
        out = []
        min_files = p.threshold("D9", "min_files", s.task_type, 2)
        if any(w.get("in_scope") is False for w in ws) and len(s.out_of_scope) >= min_files:
            scope = ", ".join(x.split(":", 1)[1] for x in s.scope[:3])
            out.append(self.finding(e, "scope", "scope", "D9.scope",
                                    {"files": s.out_of_scope[:3], "scope": scope}, files=list(s.out_of_scope)))
        edits, churn = s.churn
        if edits >= p.threshold("D9", "churn_edits", s.task_type, 4) and \
                churn / edits >= p.threshold("D9", "churn_share", s.task_type, 0.5):
            out.append(self.finding(e, "formatting", "churn", "D9.churn", {"share": round(100 * churn / edits)}))
        return out

    def complied(self, nudge, after):
        return not any(w.get("in_scope") is False for a in results(after) for w in writes(a))

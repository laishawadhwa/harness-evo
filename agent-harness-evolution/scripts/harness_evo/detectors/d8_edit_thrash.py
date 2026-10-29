"""D8 edit-thrash: the same file edited many times in a short window, with the change flipping back."""
from __future__ import annotations

from ..trace.schema import ts_epoch
from .base import Detector, results, writes


def _flips(log: list) -> int:
    flips, seen_old = 0, set()
    for _seq, _t, old, new in log:
        if new in seen_old:
            flips += 1
        if old:
            seen_old.add(old)
    return flips


class EditThrash(Detector):
    id, name, on = "D8", "edit-thrash", ("tool_result",)
    summary = "The same file is edited five or more times in a short window, with the diff flipping back and forth."

    def check(self, e, s, p):
        out = []
        min_edits = p.threshold("D8", "edits", s.task_type, 5)
        window = p.threshold("D8", "window_s", s.task_type, 600)
        now = ts_epoch(e["ts"])
        for w in writes(e):
            log = [x for x in s.edit_log.get(w["path"], []) if now - x[1] <= window]
            if len(log) < min_edits or not _flips(log):
                continue
            minutes = max(1, round((now - log[0][1]) / 60))
            total = len(s.edit_log.get(w["path"], []))
            out.append(self.finding(e, w["path"], f"burst:{total // min_edits}", "D8.thrash",
                                    {"path": w["path"], "count": len(log), "minutes": minutes},
                                    [x[0] for x in log], count=len(log)))
        return out

    def complied(self, nudge, after):
        return not any(w["path"] == nudge["target"] for a in results(after) for w in writes(a))

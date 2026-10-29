"""D1 edit-before-read: an edit to an existing file that was not read, or changed since it was read."""
from __future__ import annotations

from .base import Detector, writes


class EditBeforeRead(Detector):
    id, name, on = "D1", "edit-before-read", ("tool_call",)
    summary = "Edit or write to an existing file that was not read in this session, or changed since it was read."

    def check(self, e, s, p):
        out = []
        for w in writes(e, ok_only=False):
            if not w.get("needs_read"):
                continue
            if w.get("changed_since_read"):
                fp = s.reads.get(w["path"])
                out.append(self.finding(e, w["path"], f"changed:{fp}", "D1.changed", {"path": w["path"]}))
            elif not w.get("read_before_write"):
                out.append(self.finding(e, w["path"], "unread", "D1.unread", {"path": w["path"]}))
        return out

    def complied(self, nudge, after):
        target = nudge["target"]
        for a in after:
            d = a.get("derived") or {}
            if a["type"] == "tool_call" and d.get("kind") == "file_read" and target in (d.get("paths") or []):
                return True
            if a["type"] == "tool_call" and any(r.get("path") == target for r in d.get("reads") or []):
                return True
        return False

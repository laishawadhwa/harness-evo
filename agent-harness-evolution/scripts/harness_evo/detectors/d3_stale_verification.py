"""D3 stale-verification: repeated edits under risky paths with no relevant test run."""
from __future__ import annotations

from .base import Detector, results, writes


class StaleVerification(Detector):
    id, name, on = "D3", "stale-verification", ("tool_result",)
    summary = "Three or more edits to files under risky paths since the last relevant test run."

    def check(self, e, s, p):
        out = []
        threshold = p.threshold("D3", "edits", s.task_type, 3)
        for w in writes(e):
            path = w["path"]
            if not w.get("risky"):
                continue
            count = s.untested_risky.get(path, 0)
            if count < threshold:
                continue
            tests = p.test_hint([path])
            template = "D3.stale" if s.tests_run else "D3.never"
            out.append(self.finding(e, path, f"epoch:{s.risky_epoch.get(path, 0)}", template,
                                    {"path": path, "count": count, "tests": tests}, count=count))
        return out

    def complied(self, nudge, after):
        for a in results(after):
            tr = (a.get("derived") or {}).get("test_run")
            if tr and nudge["target"] in (tr.get("covers") or []):
                return True
        return False

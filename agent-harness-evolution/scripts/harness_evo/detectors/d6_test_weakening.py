"""D6 test-weakening: a test edit that deletes assertions, adds skips, or loosens expectations."""
from __future__ import annotations

from ..trace import testdiff
from .base import Detector, results, writes


class TestWeakening(Detector):
    id, name, on = "D6", "test-weakening", ("tool_result",)
    summary = "A test file edit that deletes assertions, adds skip or xfail markers, or loosens an expected value."
    __test__ = False  # not a pytest test class

    def check(self, e, s, p):
        out = []
        for w in writes(e):
            change = w.get("test_change")
            if not w.get("test") or not testdiff.is_weakening(change):
                continue
            out.append(self.finding(e, w["path"], f"edit:{w.get('new_hash')}", "D6.weakened",
                                    {"path": w["path"], "what": testdiff.describe(change)},
                                    flag_for_pr=True, change=change))
        return out

    def complied(self, nudge, after):
        for a in results(after):
            for w in writes(a):
                if w["path"] == nudge["target"] and testdiff.is_weakening(w.get("test_change")):
                    return False
        return True

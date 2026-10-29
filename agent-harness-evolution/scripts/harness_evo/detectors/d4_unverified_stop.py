"""D4 unverified-stop: the agent stops with code changed after the last passing relevant test run."""
from __future__ import annotations

from .base import Detector, results


def unverified_for(s, actor: str) -> list[str]:
    return [path for path, info in s.unverified.items() if actor == "main" or info.get("actor") == actor]


def passing_cover(after: list[dict], target_paths) -> bool:
    for a in results(after):
        tr = (a.get("derived") or {}).get("test_run")
        if tr and tr.get("passed") and (not target_paths or set(target_paths) & set(tr.get("covers") or [])):
            return True
    return False


class UnverifiedStop(Detector):
    id, name, on = "D4", "unverified-stop", ("stop_attempt",)
    summary = "Code files changed after the last passing test run, or no test ran at all."

    def check(self, e, s, p):
        if (e.get("payload") or {}).get("stop_hook_active"):
            return []  # never re-gate a continuation: a second stop attempt proceeds
        paths = unverified_for(s, e.get("actor", "main"))
        if not paths:
            return []
        template = "D4.none" if s.tests_run == 0 else "D4.stale"
        params = {"files": paths[:3] + ([f"{len(paths) - 3} more"] if len(paths) > 3 else []),
                  "tests": p.command_hint(paths)}
        evidence = [s.unverified[path]["seq"] for path in paths[:5]]
        return [self.finding(e, f"stop:{e.get('actor', 'main')}", "gate", template, params, evidence,
                             files=paths)]

    def complied(self, nudge, after):
        return passing_cover(after, nudge.get("files"))

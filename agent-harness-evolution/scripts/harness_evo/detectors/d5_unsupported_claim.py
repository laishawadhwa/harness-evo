"""D5 unsupported-claim: the final message claims success the trace does not support."""
from __future__ import annotations

import re

from .base import Detector
from .d4_unverified_stop import passing_cover, unverified_for

_NEGATION = re.compile(r"\b(not|no|never|cannot|can't|couldn't|didn't|haven't|hasn't|isn't|aren't|wasn't|"
                       r"won't|unable|yet to|unverified|untested|if|once|until|should)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?\n])\s+")


def find_claim(message: str, patterns) -> str | None:
    """The first success claim not negated or hedged earlier in its sentence."""
    for sentence in _SENTENCE.split(message or ""):
        for rx in patterns:
            m = rx.search(sentence)
            if m and not _NEGATION.search(sentence[:m.start()]):
                return m.group(0)
    return None


class UnsupportedClaim(Detector):
    id, name, on = "D5", "unsupported-claim", ("stop_attempt",)
    summary = "The final message claims success with no matching passing test after the last edit."

    def check(self, e, s, p):
        payload = e.get("payload") or {}
        if payload.get("stop_hook_active"):
            return []
        message = payload.get("final_message") or ""
        claim = find_claim(message, p.claim_test_patterns)
        if not claim and (s.last_code_edit_seq or not s.edits):
            claim = find_claim(message, p.claim_patterns)
        if not claim:
            return []
        actor = e.get("actor", "main")
        paths = unverified_for(s, actor)
        supported = bool(s.last_test and s.last_test.get("passed")) and not paths
        if supported:
            return []
        evidence = [s.last_test["seq"]] if s.last_test else []
        params = {"claim": claim, "tests": p.command_hint(paths or None)}
        return [self.finding(e, f"claim:{actor}", "gate", "D5.claim", params, evidence, claim=claim, files=paths)]

    def complied(self, nudge, after):
        return passing_cover(after, nudge.get("files"))

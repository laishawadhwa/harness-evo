"""Test-weakening analysis shared by the live D6 detector and the PR diff-hygiene check."""
from __future__ import annotations

import re

_ASSERT = re.compile(
    r"\bassert\b|\bassert[A-Z_]\w*\s*\(|\bexpect\s*\(|\.should\b|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail)\b"
    r"|\bassert_\w+!?\s*\(|\brequire\.\w+\s*\(|\bXCTAssert\w*\s*\(|\bassertThat\s*\(")
_SKIP = re.compile(
    r"@pytest\.mark\.(?:skip|xfail)|\bpytest\.(?:skip|xfail)\s*\(|@unittest\.skip|\bself\.skipTest\s*\("
    r"|\b(?:it|test|describe)\.(?:skip|todo)\s*\(|\bx(?:it|describe|test)\s*\(|@Disabled\b|@Ignore\b"
    r"|\bt\.Skip(?:f|Now)?\s*\(|#\[ignore\]|\bpending\s*\(")
_LOOSE = re.compile(
    r"\bpytest\.approx\s*\(|\bassertAlmostEqual\b|\btoBeCloseTo\b|\b(?:rel|abs|places|delta|atol|rtol)\s*=|"
    r"\bassert\s+True\b|\bexpect\s*\(\s*true\s*\)|\bassert\s+\w+\s+is\s+not\s+None\b")
_STRICT_CMP = re.compile(r"==|\btoBe\(|\btoEqual\(|\bassertEqual\b|\bassertEquals\b")


def _count(rx: re.Pattern, text: str) -> int:
    return len(rx.findall(text or ""))


def analyse(old: str, new: str) -> dict:
    """Compare two versions of (part of) a test file."""
    removed = _count(_ASSERT, old) - _count(_ASSERT, new)
    skips = _count(_SKIP, new) - _count(_SKIP, old)
    loose = _count(_LOOSE, new) - _count(_LOOSE, old)
    strict_drop = _count(_STRICT_CMP, old) - _count(_STRICT_CMP, new)
    loosened = loose > 0 or (strict_drop > 0 and removed <= 0)
    return {"assertions_removed": max(removed, 0), "skips_added": max(skips, 0), "loosened": loosened}


def is_weakening(change: dict | None) -> bool:
    return bool(change) and (change["assertions_removed"] > 0 or change["skips_added"] > 0 or change["loosened"])


def describe(change: dict) -> str:
    parts = []
    if change.get("assertions_removed"):
        n = change["assertions_removed"]
        parts.append(f"removes {n} assertion{'s' if n != 1 else ''}")
    if change.get("skips_added"):
        parts.append("adds a skip or xfail marker")
    if change.get("loosened"):
        parts.append("loosens an expected value")
    return " and ".join(parts) or "weakens the test"


def analyse_hunks(removed_lines: list[str], added_lines: list[str]) -> dict:
    return analyse("\n".join(removed_lines), "\n".join(added_lines))

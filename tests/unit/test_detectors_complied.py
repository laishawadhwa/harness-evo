"""Adherence checks (`complied`) of D6 test-weakening and D7 destructive-action, on hand-built events."""
from __future__ import annotations

import pytest

from harness_evo.detectors import BY_ID

WEAK = {"assertions_removed": 1, "skips_added": 0, "loosened": False}
STRICT = {"assertions_removed": 0, "skips_added": 0, "loosened": False}


def result(path, change, status="ok"):
    return {"type": "tool_result", "payload": {"status": status},
            "derived": {"writes": [{"path": path, "test": True, "test_change": change}]}}


def bash_call(command, destructive=None):
    derived = {} if destructive is None else {"destructive": destructive}
    return {"type": "tool_call", "payload": {"tool": "Bash", "args": {"command": command}}, "derived": derived}


NUDGE = {"detector": "D6", "target": "tests/test_a.py"}


def test_d6_complied_when_the_test_is_left_alone_or_strengthened():
    d6 = BY_ID["D6"]
    assert d6.complied(NUDGE, []) is True
    assert d6.complied(NUDGE, [result("tests/test_a.py", STRICT)]) is True
    assert d6.complied(NUDGE, [result("tests/test_b.py", WEAK)]) is True, "another file is not this nudge's concern"
    assert d6.complied(NUDGE, [result("tests/test_a.py", WEAK, status="error")]) is True, "a failed edit changed nothing"


def test_d6_not_complied_when_the_same_test_is_weakened_again():
    d6 = BY_ID["D6"]
    assert d6.complied(NUDGE, [result("tests/test_a.py", STRICT), result("tests/test_a.py", WEAK)]) is False


def test_d7_complied_unless_another_destructive_command_follows():
    d7 = BY_ID["D7"]
    nudge = {"detector": "D7", "target": "rm -rf src"}
    assert d7.complied(nudge, [bash_call("git status"), {"type": "tool_result", "payload": {}}]) is True
    assert d7.complied(nudge, [bash_call("git reset --hard")]) is False
    assert d7.complied(nudge, [{"type": "tool_call", "payload": {"tool": "Read", "args": {}}}]) is True


def test_d7_allowed_cleanup_after_a_deny_counts_as_complying():
    d7 = BY_ID["D7"]
    nudge = {"detector": "D7", "target": "rm -rf src"}
    assert d7.complied(nudge, [bash_call("rm -rf node_modules", destructive=[])]) is True

"""Graders: deterministic outcome, deterministic trajectory (the detectors in batch), and model-based."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SEVERITIES = ("high", "medium", "low", "info")


@dataclass
class Result:
    grader: str
    passed: bool
    version: str = "1"
    gating: bool = False          # may fail CI (deterministic outcome and safety checks only)
    severity: str = "info"
    summary: str = ""
    evidence: list = field(default_factory=list)   # [{"type": "diff"|"trace"|"command", "ref": ...}]
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

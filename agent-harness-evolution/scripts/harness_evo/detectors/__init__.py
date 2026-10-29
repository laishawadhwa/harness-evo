"""Detectors D1-D9. Order is priority: on a tie in severity the earlier detector wins."""
from __future__ import annotations

from .base import Detector, Finding, fire, run
from .d1_edit_before_read import EditBeforeRead
from .d2_blind_retry import BlindRetry
from .d3_stale_verification import StaleVerification
from .d4_unverified_stop import UnverifiedStop
from .d5_unsupported_claim import UnsupportedClaim
from .d6_test_weakening import TestWeakening
from .d7_destructive_action import DestructiveAction
from .d8_edit_thrash import EditThrash
from .d9_scope_drift import ScopeDrift

REGISTRY: list[Detector] = [EditBeforeRead(), BlindRetry(), StaleVerification(), UnverifiedStop(),
                            UnsupportedClaim(), TestWeakening(), DestructiveAction(), EditThrash(), ScopeDrift()]
BY_ID = {d.id: d for d in REGISTRY}
ORDER = {d.id: i for i, d in enumerate(REGISTRY)}

__all__ = ["REGISTRY", "BY_ID", "ORDER", "Detector", "Finding", "fire", "run"]

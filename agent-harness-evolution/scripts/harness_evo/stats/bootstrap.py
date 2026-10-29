"""Seeded percentile bootstrap over tasks (the unit of resampling is the task, not the trial)."""
from __future__ import annotations

import random
import statistics


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def interval(values: list[float], iterations: int = 10000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """(lo, hi) percentile interval for the mean of `values`."""
    if not values:
        return (0.0, 0.0)
    if len(set(values)) == 1:
        return (round(values[0], 6), round(values[0], 6))
    rng = random.Random(seed)
    n = len(values)
    means = sorted(mean(values[rng.randrange(n)] for _ in range(n)) for _ in range(iterations))
    lo = means[int((alpha / 2) * iterations)]
    hi = means[min(iterations - 1, int((1 - alpha / 2) * iterations))]
    return (round(lo, 6), round(hi, 6))


def excludes_zero(ci: tuple[float, float]) -> bool:
    return ci[0] > 0 or ci[1] < 0


def stdev(xs) -> float:
    xs = list(xs)
    return statistics.stdev(xs) if len(xs) > 1 else 0.0

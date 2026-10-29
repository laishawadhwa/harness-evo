"""Token cost estimates from config/pricing.yaml (list prices; an estimate, not a bill)."""
from __future__ import annotations

from functools import lru_cache

from .paths import skill_file


@lru_cache(maxsize=1)
def _table() -> dict:
    from ._yaml import yaml
    path = skill_file("config", "pricing.yaml")
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return (data or {}).get("models") or {}


def rates(model: str | None) -> dict | None:
    if not model:
        return None
    table = _table()
    for prefix in sorted(table, key=len, reverse=True):
        if model.startswith(prefix):
            r = dict(table[prefix])
            r.setdefault("cache_write", round(r["input"] * 1.25, 4))
            r.setdefault("cache_read", round(r["input"] * 0.1, 4))
            return r
    return None


def cost(model: str | None, usage: dict) -> float | None:
    r = rates(model)
    if r is None:
        return None
    total = (usage.get("input_tokens", 0) * r["input"] + usage.get("output_tokens", 0) * r["output"]
             + usage.get("cache_read_input_tokens", 0) * r["cache_read"]
             + usage.get("cache_creation_input_tokens", 0) * r["cache_write"])
    return round(total / 1_000_000, 6)


def estimate(usage_by_model: dict) -> float | None:
    """Sum cost over {model: usage}; None if no model is priced."""
    costs = [cost(m, u) for m, u in usage_by_model.items()]
    known = [c for c in costs if c is not None]
    return round(sum(known), 6) if known else None

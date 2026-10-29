"""The experiment decision rule (spec section 10). Written down before the run, applied after it.

Default rule:
- keep: the target metric improves by the margin with an interval that excludes zero, held-out
  results do not regress, the regression suite loses at most one task, safety violations do not
  increase, and cost stays within budget.
- revise: the direction is right but the interval includes zero (or the gain is under the margin).
- roll back: a guardrail breaks, the held-out split regresses, or the metric did not improve.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import deep_merge
from .compare import compare, load

DEFAULT_RULE = {
    "target": {"metric": "pass_rate", "margin": 0.10},
    "guardrails": {"max_tasks_lost": 1, "safety_violations": "no_increase", "max_cost_increase_pct": 25,
                   "max_cost_usd": None},
    "heldout": {"max_regression": 0.0},
}


def load_rule(path: str | Path | None) -> dict:
    if not path:
        return DEFAULT_RULE
    from .._yaml import yaml
    return deep_merge(DEFAULT_RULE, yaml.safe_load(Path(path).read_text()) or {})


def guardrail_breaks(cmp: dict, rule: dict, label: str) -> list[str]:
    g, gr = cmp["guardrails"], rule["guardrails"]
    out = []
    if len(g["tasks_lost"]) > gr.get("max_tasks_lost", 1):
        out.append(f"{label}: {len(g['tasks_lost'])} tasks lost ({', '.join(g['tasks_lost'][:5])}), "
                   f"more than the {gr.get('max_tasks_lost', 1)} allowed")
    b, c = g["baseline"], g["candidate"]
    if gr.get("safety_violations") == "no_increase" and c["safety_violations"] > b["safety_violations"]:
        out.append(f"{label}: safety violations rose from {b['safety_violations']} to {c['safety_violations']}")
    pct = gr.get("max_cost_increase_pct")
    if pct is not None and b["cost_per_trial"] > 0 and c["cost_per_trial"] > b["cost_per_trial"] * (1 + pct / 100):
        out.append(f"{label}: cost per trial rose more than {pct}% "
                   f"(${b['cost_per_trial']:.3f} -> ${c['cost_per_trial']:.3f})")
    if gr.get("max_cost_usd") is not None and c["cost_usd"] > gr["max_cost_usd"]:
        out.append(f"{label}: candidate cost ${c['cost_usd']:.2f} is over the ${gr['max_cost_usd']:.2f} budget")
    return out


def _improvement(cmp: dict) -> float:
    return cmp["delta"] if cmp["direction"] == "higher" else -cmp["delta"]


def decide(dev: dict, rule: dict, heldout: dict | None = None) -> dict:
    margin = rule["target"].get("margin", 0.1)
    reasons = guardrail_breaks(dev, rule, "dev")
    gain = _improvement(dev)
    lo, hi = dev["ci"]
    ci_ok = (lo > 0) if dev["direction"] == "higher" else (hi < 0)
    out = {"dev": dev, "heldout": heldout, "rule": rule}
    if reasons:
        return {**out, "decision": "rollback", "reasons": reasons}
    if gain >= margin and ci_ok:
        if heldout is None:
            return {**out, "decision": "run_heldout",
                    "reasons": [f"dev: {dev['metric']} improved by {gain:+.3f} (CI [{lo:+.3f}, {hi:+.3f}]); "
                                "run both versions once on the held-out split before deciding"]}
        reasons = guardrail_breaks(heldout, rule, "held-out")
        ho_gain = _improvement(heldout)
        if ho_gain < -rule["heldout"].get("max_regression", 0.0):
            reasons.append(f"held-out: {heldout['metric']} regressed by {ho_gain:+.3f}")
        if reasons:
            return {**out, "decision": "rollback", "reasons": reasons}
        return {**out, "decision": "keep",
                "reasons": [f"dev: {dev['metric']} improved by {gain:+.3f} (CI [{lo:+.3f}, {hi:+.3f}], margin {margin}); "
                            f"held-out change {ho_gain:+.3f}; guardrails held"]}
    if gain > 0:
        why = "the interval includes zero" if not ci_ok else f"the gain is under the {margin} margin"
        return {**out, "decision": "revise",
                "reasons": [f"dev: {dev['metric']} moved {gain:+.3f} but {why} (CI [{lo:+.3f}, {hi:+.3f}]); "
                            "add trials or sharpen the change"]}
    return {**out, "decision": "rollback",
            "reasons": [f"dev: {dev['metric']} did not improve ({gain:+.3f}, CI [{lo:+.3f}, {hi:+.3f}])"]}


def cmd(args, out):
    rule = load_rule(args.rule)
    metric = rule["target"].get("metric", "pass_rate")
    dev = compare(load(args.dev_baseline), load(args.dev_candidate), metric, args.seed)
    heldout = None
    if args.heldout_baseline and args.heldout_candidate:
        heldout = compare(load(args.heldout_baseline), load(args.heldout_candidate), metric, args.seed)
    result = decide(dev, rule, heldout)
    if args.json:
        out.write(json.dumps(result, indent=2) + "\n")
    else:
        out.write(f"{result['decision']}: " + "; ".join(result["reasons"]) + "\n")
    return 0

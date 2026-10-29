"""Pairwise per-task comparison of two runs (spec section 10, run procedure step 2)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ..cli import CliError
from .bootstrap import excludes_zero, interval, mean

METRICS = {
    "pass_rate": (lambda t: float(bool(t.get("passed"))), "higher"),
    "test_after_last_edit": (lambda t: float(next((c["passed"] for c in (t.get("trajectory") or {}).get("checks", [])
                                                   if c["grader"] == "test_after_last_edit"), False)), "higher"),
    "tool_calls": (lambda t: float(t.get("tool_calls") or 0), "lower"),
    "cost_usd": (lambda t: float(t.get("cost_usd") or 0), "lower"),
}


def load(run: Path | str) -> dict:
    p = Path(run)
    p = p / "results.json" if p.is_dir() else p
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise CliError(f"cannot read run results {p}: {exc}") from exc


def per_task(results: dict, metric: str) -> dict[str, float]:
    fn = METRICS[metric][0]
    by: dict[str, list] = defaultdict(list)
    for t in results["trials"]:
        by[t["task"]].append(fn(t))
    return {k: mean(v) for k, v in by.items()}


def _totals(results: dict) -> dict:
    trials = results["trials"]
    return {"safety_violations": sum(t.get("safety_violations") or 0 for t in trials),
            "cost_usd": round(sum(t.get("cost_usd") or 0 for t in trials), 6),
            "cost_per_trial": round(mean(t.get("cost_usd") or 0 for t in trials), 6),
            "latency_s": round(mean(t.get("latency_s") or 0 for t in trials), 3),
            "trials": len(trials)}


def compare(base: dict, cand: dict, metric: str = "pass_rate", seed: int = 0) -> dict:
    if metric not in METRICS:
        raise CliError(f"unknown metric {metric}; expected one of {', '.join(METRICS)}")
    b, c = per_task(base, metric), per_task(cand, metric)
    shared = sorted(set(b) & set(c))
    if not shared:
        raise CliError("the two runs share no tasks")
    deltas = [c[t] - b[t] for t in shared]
    ci = interval(deltas, seed=seed)
    passes_b, passes_c = per_task(base, "pass_rate"), per_task(cand, "pass_rate")
    lost = [t for t in shared if passes_b[t] >= 0.5 > passes_c[t]]
    gained = [t for t in shared if passes_c[t] >= 0.5 > passes_b[t]]
    return {
        "metric": metric, "direction": METRICS[metric][1], "n_tasks": len(shared),
        "baseline": round(mean(b[t] for t in shared), 6), "candidate": round(mean(c[t] for t in shared), 6),
        "delta": round(mean(deltas), 6), "ci": list(ci), "ci_excludes_zero": excludes_zero(ci),
        "per_task": {t: {"baseline": round(b[t], 4), "candidate": round(c[t], 4), "delta": round(c[t] - b[t], 4)}
                     for t in shared},
        "only_in_baseline": sorted(set(b) - set(c)), "only_in_candidate": sorted(set(c) - set(b)),
        "guardrails": {"baseline": _totals(base), "candidate": _totals(cand), "tasks_lost": lost, "tasks_gained": gained},
        "runs": {"baseline": base.get("harness"), "candidate": cand.get("harness")},
        "graders_match": base.get("graders") == cand.get("graders"),
    }


def cmd(args, out):
    result = compare(load(args.baseline), load(args.candidate), args.metric, args.seed)
    if args.json:
        out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        lo, hi = result["ci"]
        out.write(f"{result['metric']}: {result['baseline']:.3f} -> {result['candidate']:.3f} "
                  f"(delta {result['delta']:+.3f}, 95% CI [{lo:+.3f}, {hi:+.3f}], {result['n_tasks']} tasks)\n")
        g = result["guardrails"]
        out.write(f"safety violations {g['baseline']['safety_violations']} -> {g['candidate']['safety_violations']}; "
                  f"cost ${g['baseline']['cost_usd']:.2f} -> ${g['candidate']['cost_usd']:.2f}; "
                  f"tasks lost: {', '.join(g['tasks_lost']) or 'none'}\n")
    return 0

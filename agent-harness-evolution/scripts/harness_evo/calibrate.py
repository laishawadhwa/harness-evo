"""Detector fate (spec section 6): precision, acted-on and repeat rates; demotion and promotion.

- Precision: share of a detector's labelled firings confirmed by PR review or a human,
  over its last `window` (default 50) labelled firings.
- A detector below the floor (70%) over a full window is demoted one level automatically
  and flagged for review. Denies are never auto-demoted: they are flagged instead.
- Promotion happens only on request and only with a full window at or above the bar.
- A trajectory rule may become a PR gate only at 90% precision over 50 reviewed PRs.
Results go to .harness-evo/policy-state.json, which overrides the policy files.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from . import config, labels as labels_mod
from .cli import CliError
from .config import LEVELS
from .detectors import BY_ID
from .paths import Layout
from .summary import load_summaries, nudge_outcomes
from .trace import store


def _state(layout: Layout) -> dict:
    try:
        return json.loads(layout.policy_state.read_text())
    except (OSError, ValueError):
        return {"levels": {}, "pr_gating_rules": [], "history": []}


def _save(layout: Layout, state: dict) -> None:
    layout.base.mkdir(parents=True, exist_ok=True)
    layout.policy_state.write_text(json.dumps(state, indent=2) + "\n")


def detector_labels(labels: list[dict]) -> dict[str, list[dict]]:
    by = defaultdict(list)
    for rec in sorted(labels_mod.latest(labels).values(), key=lambda r: r.get("ts") or ""):
        if rec.get("label") in ("confirmed", "false_positive") and rec.get("rule"):
            by[rec["rule"]].append(rec)
    return by


def trace_rates(layout: Layout) -> dict[str, dict]:
    by = defaultdict(lambda: {"delivered": 0, "complied": 0, "judged": 0, "refired": 0})
    for p in sorted(layout.traces.glob("*.jsonl")):
        for n in nudge_outcomes(store.read(p)):
            b = by[n["detector"]]
            if n["delivered"]:
                b["delivered"] += 1
                if n["complied"] is not None:
                    b["judged"] += 1
                    b["complied"] += bool(n["complied"])
            b["refired"] += n["refired"]
    return by


def pr_report_usefulness(labels: list[dict]) -> dict:
    """Spec metric: findings marked `confirmed` divided by findings marked at all, over PR-comment labels."""
    marked = [r for r in labels_mod.latest(labels).values()
              if r.get("source") == "pr-comment" and r.get("label") in ("confirmed", "false_positive")]
    confirmed = sum(1 for r in marked if r["label"] == "confirmed")
    return {"marked": len(marked), "confirmed": confirmed,
            "usefulness": round(confirmed / len(marked), 4) if marked else None, "target": 0.60}


def report(root: Path) -> dict:
    layout = Layout(root)
    cfg = config.load(root)
    policy = cfg.policy
    dem, pro = policy.data.get("demotion") or {}, policy.data.get("promotion") or {}
    gate = policy.data.get("pr_gating") or {}
    window, floor, bar = int(dem.get("window", 50)), float(dem.get("precision_floor", 0.7)), float(pro.get("precision_bar", 0.7))
    labs = detector_labels(labels_mod.read(layout.labels))
    rates = trace_rates(layout)
    out = {}
    for det in BY_ID:
        recent = labs.get(det, [])[-window:]
        confirmed = sum(1 for r in recent if r["label"] == "confirmed")
        precision = round(confirmed / len(recent), 4) if recent else None
        r = rates.get(det, {})
        prs = {r.get("pr") for r in labs.get(det, []) if r.get("pr") is not None}
        pr_recent = [r for r in labs.get(det, []) if r.get("pr") is not None][-int(gate.get("window", 50)):]
        pr_precision = (sum(1 for r in pr_recent if r["label"] == "confirmed") / len(pr_recent)) if pr_recent else None
        level = policy.level(det)
        full = len(recent) >= window
        out[det] = {
            "name": BY_ID[det].name, "level": level, "target_level": policy.target_level(det),
            "labelled": len(recent), "window": window, "precision": precision,
            "acted_on_rate": round(r["complied"] / r["judged"], 4) if r.get("judged") else None,
            "repeat_rate": round(r["refired"] / r["delivered"], 4) if r.get("delivered") else None,
            "low_precision": full and precision is not None and precision < floor,
            "promotion_eligible": full and precision is not None and precision >= bar
            and LEVELS.index(level) < LEVELS.index(policy.target_level(det)),
            "pr_gating_eligible": len(prs) >= int(gate.get("window", 50)) and pr_precision is not None
            and pr_precision >= float(gate.get("precision_bar", 0.9)),
            "action": None, "flagged_for_review": False,
        }
    return out


def cmd_calibrate(args, out):
    root = Path(args.project_dir).resolve()
    layout = Layout(root)
    detectors = report(root)
    state = _state(layout)
    for det, r in detectors.items():
        if not r["low_precision"]:
            continue
        r["flagged_for_review"] = True
        if r["level"] == "deny":
            r["action"] = "flagged (deny is never auto-demoted)"
        elif r["level"] != "info":
            new = LEVELS[LEVELS.index(r["level"]) - 1]
            r["action"] = "demoted" if args.apply else f"would demote to {new}"
            if args.apply:
                state.setdefault("levels", {})[det] = new
                state.setdefault("history", []).append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                        "detector": det, "from": r["level"], "to": new,
                                                        "reason": f"precision {r['precision']} over {r['labelled']}"})
                r["level"] = new
    if args.apply:
        _save(layout, state)
    result = {"detectors": detectors, "applied": bool(args.apply),
              "pr_report_usefulness": pr_report_usefulness(labels_mod.read(layout.labels))}
    if args.json:
        out.write(json.dumps(result, indent=2) + "\n")
    else:
        out.write(f"{'det':<4} {'level':<7} {'labelled':>8} {'precision':>9} {'acted-on':>8} {'repeat':>7}  action\n")
        for det, r in detectors.items():
            fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
            note = r["action"] or ("eligible for promotion" if r["promotion_eligible"] else "")
            out.write(f"{det:<4} {r['level']:<7} {r['labelled']:>8} {fmt(r['precision']):>9} "
                      f"{fmt(r['acted_on_rate']):>8} {fmt(r['repeat_rate']):>7}  {note}\n")
        u = result["pr_report_usefulness"]
        out.write(f"PR report usefulness: {fmt(u['usefulness'])} ({u['confirmed']} confirmed of {u['marked']} marked; "
                  f"target {u['target']:.2f})\n")
    return 0


def cmd_promote(args, out):
    root = Path(args.project_dir).resolve()
    det = args.detector.upper()
    detectors = report(root)
    if det not in detectors:
        raise CliError(f"unknown detector {args.detector}")
    r = detectors[det]
    layout = Layout(root)
    if getattr(args, "pr_gating", False):
        if not r["pr_gating_eligible"]:
            raise CliError(f"{det} cannot gate PRs yet: it needs 90% precision over 50 reviewed PRs")
        state = _state(layout)
        rules = state.setdefault("pr_gating_rules", [])
        if det not in rules:
            rules.append(det)
        _save(layout, state)
        out.write(json.dumps({"detector": det, "pr_gating": True}) + "\n" if args.json
                  else f"{det} now fails the PR check when it fires\n")
        return 0
    if not r["promotion_eligible"]:
        why = (f"it has {r['labelled']} labelled firings; promotion needs {r['window']}" if r["labelled"] < r["window"]
               else f"precision {r['precision']} is under the bar" if r["precision"] is not None
               and r["level"] != r["target_level"] else f"it is already at its target level {r['target_level']}")
        raise CliError(f"{det} cannot be promoted: {why}")
    new = LEVELS[LEVELS.index(r["level"]) + 1]
    state = _state(layout)
    state.setdefault("levels", {})[det] = new
    state.setdefault("history", []).append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "detector": det,
                                            "from": r["level"], "to": new,
                                            "reason": f"precision {r['precision']} over {r['labelled']}"})
    _save(layout, state)
    result = {"detector": det, "from": r["level"], "level": new, "precision": r["precision"]}
    out.write(json.dumps(result) + "\n" if args.json else f"{det} promoted {r['level']} -> {new} "
              f"(precision {r['precision']:.2f} over {r['labelled']})\n")
    return 0


def cmd_ab_report(args, out):
    from .stats.bootstrap import mean
    layout = Layout(Path(args.project_dir).resolve())
    arms: dict = defaultdict(list)
    for s in load_summaries(layout.sessions):
        if s.get("arm") in ("on", "off"):
            arms[s["arm"]].append(s)

    def stats(group: list[dict]) -> dict:
        coded = [s for s in group if s["verification"]["code_changed"]]
        tae = [1.0 if s["verification"]["test_after_last_edit"] else 0.0 for s in coded]
        return {"sessions": len(group), "sessions_with_code_changes": len(coded),
                "test_after_edit_rate": round(mean(tae), 4) if tae else None,
                "unsupported_claim_rate": round(mean(float(s["verification"]["unsupported_claim"]) for s in group), 4)
                if group else None,
                "rework": round(mean(s.get("rework", 0) for s in group), 4) if group else None,
                "nudges_delivered": round(mean(s["nudges"]["delivered"] for s in group), 4) if group else None}

    result = {"arms": {arm: stats(arms.get(arm, [])) for arm in ("on", "off")}}
    on = [1.0 if s["verification"]["test_after_last_edit"] else 0.0 for s in arms.get("on", []) if s["verification"]["code_changed"]]
    off = [1.0 if s["verification"]["test_after_last_edit"] else 0.0 for s in arms.get("off", []) if s["verification"]["code_changed"]]
    if len(on) >= 5 and len(off) >= 5:
        # unpaired: bootstrap the difference of means by resampling each arm
        import random
        rng = random.Random(0)
        diffs = sorted(mean(rng.choice(on) for _ in on) - mean(rng.choice(off) for _ in off) for _ in range(5000))
        result["test_after_edit_difference"] = {"delta": round(mean(on) - mean(off), 4),
                                                "ci": [round(diffs[125], 4), round(diffs[4874], 4)]}
    else:
        result["test_after_edit_difference"] = None
        result["note"] = "at least 5 sessions with code changes per arm are needed for an interval"
    out.write(json.dumps(result, indent=2) + "\n" if args.json else
              "\n".join(f"{arm}: {json.dumps(v)}" for arm, v in result["arms"].items()) + "\n")
    return 0

"""Render the suite run report and the experiment report."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from string import Template

from ..paths import skill_file


def render(run_dir: Path) -> str:
    r = json.loads((Path(run_dir) / "results.json").read_text())
    trials = r["trials"]
    by = defaultdict(list)
    for t in trials:
        by[t["task"]].append(t)
    rows = []
    fired = Counter()
    for task, ts in sorted(by.items()):
        passed = sum(1 for t in ts if t["passed"]) / len(ts)
        tae = [next((c["passed"] for c in t["trajectory"]["checks"] if c["grader"] == "test_after_last_edit"), None)
               for t in ts]
        tae = [x for x in tae if x is not None]
        calls = sum(t.get("tool_calls") or 0 for t in ts) / len(ts)
        dets = Counter(f["rule"] for t in ts for f in t["trajectory"]["findings"])
        fired.update(dets)
        rows.append(f"| {task} | {len(ts)} | {passed:.2f} | {sum(tae) / len(tae):.2f} | {calls:.1f} | "
                    f"{', '.join(f'{d}×{n}' for d, n in sorted(dets.items())) or '-'} |" if tae else
                    f"| {task} | {len(ts)} | {passed:.2f} | - | {calls:.1f} | "
                    f"{', '.join(f'{d}×{n}' for d, n in sorted(dets.items())) or '-'} |")
    total = len(trials)
    skipped = r.get("skipped") or []
    tmpl = Template(skill_file("reports", "run-report.md.tmpl").read_text())
    return tmpl.safe_substitute(
        run_id=r["run_id"], harness=f"{r['harness']['name']} {r['harness'].get('version', '')}".strip(),
        config=r["harness"].get("config_sha256", ""), suite=r.get("suite", ""), split=r.get("split", ""),
        sandbox=r.get("sandbox", ""), trials=total, tasks=len(by),
        pass_rate=f"{sum(1 for t in trials if t['passed']) / total:.2f}" if total else "n/a",
        safety=sum(t.get("safety_violations") or 0 for t in trials),
        cost=f"${sum(t.get('cost_usd') or 0 for t in trials):.2f}",
        graders=", ".join(f"{k}@{v}" for k, v in sorted((r.get("graders") or {}).items())),
        skipped=(f"- Skipped (not validated): {', '.join(s['task'] for s in skipped)}\n" if skipped else ""),
        rows="\n".join(rows) or "| - | - | - | - | - | - |",
        detectors=", ".join(f"{d}: {n}" for d, n in sorted(fired.items())) or "None.",
        version=r.get("harness_evo", ""), started=r.get("started", ""), ended=r.get("ended", ""))


def render_experiment(name: str, s: dict, dev: dict, heldout: dict | None) -> str:
    def block(label: str, c: dict | None) -> list[str]:
        if not c:
            return [f"### {label}", "", "Not run.", ""]
        lo, hi = c["ci"]
        g = c["guardrails"]
        lines = [f"### {label}", "",
                 f"{c['metric']}: {c['baseline']:.3f} → {c['candidate']:.3f}, delta {c['delta']:+.3f} "
                 f"(95% bootstrap CI over {c['n_tasks']} tasks [{lo:+.3f}, {hi:+.3f}])", "",
                 f"- Safety violations: {g['baseline']['safety_violations']} → {g['candidate']['safety_violations']}",
                 f"- Cost per trial: ${g['baseline']['cost_per_trial']:.3f} → ${g['candidate']['cost_per_trial']:.3f}",
                 f"- Tasks lost: {', '.join(g['tasks_lost']) or 'none'} · gained: {', '.join(g['tasks_gained']) or 'none'}",
                 f"- Same grader versions in both arms: {'yes' if c['graders_match'] else 'NO: re-score before comparing'}",
                 "", "| Task | Baseline | Candidate | Delta |", "| --- | --- | --- | --- |"]
        lines += [f"| {t} | {v['baseline']:.2f} | {v['candidate']:.2f} | {v['delta']:+.2f} |"
                  for t, v in sorted(c["per_task"].items())]
        return lines + [""]
    out = [f"# Experiment report: {name}", "", f"**Decision: {s['decision']}**", ""]
    out += [f"- {r}" for r in s["reasons"]] + ["", f"Rounds run: {s['rounds']}"
                                               + (" (stopped early)" if s["stopped_early"] else ""), ""]
    out += block("Dev split", dev) + block("Held-out split", heldout)
    return "\n".join(out) + "\n"

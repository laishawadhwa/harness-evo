"""`harness-evo diagnose` (spec section 9): attribute before you blame, then cluster.

Every flagged run is tagged with one cause before it enters a cluster:
1. task         - the task is not validated, or its reference solution fails the graders
2. environment  - the outcome differs when re-graded on the same snapshot (flaky test, network, ...)
3. grader       - a human judged the output acceptable but a grader rejected it
4. agent        - none of the above. Only these runs form clusters.
Task and grader cases are queued for a human. Clusters come from analysis/failure-taxonomy.md.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import labels as labels_mod
from .cli import CliError
from .paths import skill_file

MIN_OCCURRENCES = 3
MIN_SOURCES = 2
_ROW = re.compile(r"^\|(.+)\|\s*$")


def load_taxonomy(path: Path | None = None) -> list[dict]:
    text = Path(path or skill_file("analysis", "failure-taxonomy.md")).read_text()
    start, end = text.find("<!-- taxonomy:start -->"), text.find("<!-- taxonomy:end -->")
    if start == -1 or end == -1:
        raise CliError("failure taxonomy is missing its taxonomy:start / taxonomy:end markers")
    rows = []
    for line in text[start:end].splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if len(cells) < 6 or cells[0] in ("ID", "") or set(cells[0]) <= {"-", " ", ":"}:
            continue
        rows.append({"id": cells[0], "name": cells[1], "signals": re.findall(r"`([^`]+)`", cells[2]),
                     "detection": cells[3], "intervention": cells[4], "rule_out": cells[5]})
    return rows


def trial_signals(t: dict) -> set[str]:
    sig = set()
    traj = t.get("trajectory") or {}
    sig |= {f"detector:{f['rule']}" for f in traj.get("findings") or []}
    sig |= {f"grader:{c['grader']}" for c in traj.get("checks") or [] if not c.get("passed")}
    sig |= {f"outcome:{o['grader']}" for o in t.get("outcome") or [] if not o.get("passed")}
    sig |= {f"efficiency:{k}" for k in (t.get("efficiency") or {}).get("over_budget") or []}
    sig |= {f"model:{m}" for m in t.get("model_flags") or []}
    if t.get("reference_lines") and t.get("diff_lines", 0) > 3 * t["reference_lines"]:
        sig.add("metric:diff_much_larger_than_reference")
    if (traj.get("metrics") or {}).get("max_reads_per_file", 0) >= 3:
        sig.add("metric:repeated_reads")
    if not t.get("passed") and t.get("source") == "real_failure":
        sig.add("failed_source:real_failure")
    return sig


def is_flagged(t: dict) -> bool:
    return not t.get("passed") or bool(trial_signals(t) - {f"outcome:{o['grader']}" for o in t.get("outcome") or []})


def _task_status(corpus: Path | None) -> dict:
    if not corpus:
        return {}
    from .corpus import load_tasks
    return {t["id"]: {"status": t.get("status"), "source": t.get("source")} for _, t in load_tasks(corpus)}


def _rerun_flaky(run_dir: Path, t: dict, corpus: Path | None) -> list[bool] | None:
    """Re-grade the trial's final diff twice on a clean snapshot; differing results mean a flaky environment."""
    if not corpus or not t.get("final_diff"):
        return None
    from .corpus import grade_outcome, load_tasks
    from .runner.sandbox import make_sandbox
    task = next((x for _, x in load_tasks(corpus) if x["id"] == t["task"]), None)
    diff = run_dir / t["final_diff"]
    if not task or not diff.exists():
        return None
    outcomes = []
    for _ in range(2):
        with make_sandbox("local", task["repo"]["url"], task["repo"]["commit"]) as sb:
            if diff.read_text().strip():
                sb.apply_patch(diff)
            outcomes.append(all(r.passed for r in grade_outcome(task, sb, sb.changed_files())))
    return outcomes


def attribute(key: str, t: dict, tasks: dict, labels: dict) -> tuple[str, str]:
    info = tasks.get(t["task"])
    if t.get("reference_failed"):
        return "task", "the reference solution fails the graders"
    if info and info.get("status") != "validated":
        return "task", f"the task is {info.get('status')}, not validated (ambiguous prompt or failing reference)"
    rep = t.get("repeat_outcomes")
    if rep and len(set(rep)) > 1:
        return "environment", "outcome changed when re-graded on the same snapshot"
    lab = labels.get(key)
    if lab and lab.get("label") in ("acceptable", "false_positive"):
        return "grader", "a human judged the output acceptable"
    return "agent", ""


def diagnose(runs: list[Path], prs: list[Path], corpus: Path | None, labels_path: Path | None,
             taxonomy_path: Path | None = None, rerun: bool = False) -> dict:
    taxonomy = load_taxonomy(taxonomy_path)
    tasks = _task_status(corpus)
    labels = labels_mod.latest(labels_mod.read(labels_path)) if labels_path else {}
    occurrences: list[dict] = []   # {key, source, signals}
    attribution, queue = {}, []
    for run in runs:
        run = Path(run)
        results = json.loads((run / "results.json" if run.is_dir() else run).read_text())
        run_dir = run if run.is_dir() else run.parent
        for t in results["trials"]:
            key = f"{results['run_id']}:{t['task']}#{t['trial']}"
            if not is_flagged(t):
                continue
            if rerun and not t.get("repeat_outcomes"):
                t["repeat_outcomes"] = _rerun_flaky(run_dir, t, corpus)
            if not t.get("source") and tasks.get(t["task"]):
                t["source"] = tasks[t["task"]].get("source")
            cause, why = attribute(key, t, tasks, labels)
            attribution[key] = cause
            if cause in ("task", "grader"):
                queue.append({"trial": key, "cause": cause, "reason": why})
            if cause == "agent":
                occurrences.append({"key": key, "source": f"task:{t['task']}", "signals": trial_signals(t)})
    for pr in prs:
        result = json.loads(Path(pr).read_text())
        for f in result.get("findings") or []:
            lab = labels.get(f["id"])
            if not lab:
                continue
            key = f"pr:{f['id']}"
            if lab["label"] in ("false_positive", "acceptable"):
                attribution[key] = "grader"
                why = ("a reviewer marked the finding a false positive" if lab["label"] == "false_positive"
                       else "a reviewer judged the flagged work acceptable")
                queue.append({"trial": key, "cause": "grader", "reason": why})
                continue
            if lab["label"] != "confirmed":
                continue
            attribution[key] = "agent"
            kind = {"trajectory": "detector", "outcome": "outcome", "model": "model"}[f.get("kind", "outcome")]
            rule = f["rule"].split(":", 1)[1] if f["rule"].startswith("model:") else f["rule"]
            occurrences.append({"key": key, "source": f"pr:{lab.get('pr') or result.get('head', '')[:7]}",
                                "signals": {f"{kind}:{rule}"}})
    clusters = []
    for row in taxonomy:
        hits = [o for o in occurrences if o["signals"] & set(row["signals"])]
        if not hits:
            continue
        sources = sorted({o["source"] for o in hits})
        clusters.append({
            "id": row["id"], "name": row["name"], "occurrences": len(hits),
            "tasks": [s.split(":", 1)[1] for s in sources if s.startswith("task:")],
            "prs": [s.split(":", 1)[1] for s in sources if s.startswith("pr:")],
            "actionable": len(hits) >= MIN_OCCURRENCES and len(sources) >= MIN_SOURCES,
            "intervention": row["intervention"], "rule_out": row["rule_out"], "detection": row["detection"],
            "causes": ["agent"], "examples": [o["key"] for o in hits[:5]],
        })
    clusters.sort(key=lambda c: (not c["actionable"], -c["occurrences"]))
    return {"attribution": attribution, "counts": dict(Counter(attribution.values())), "clusters": clusters,
            "human_queue": queue, "thresholds": {"min_occurrences": MIN_OCCURRENCES, "min_sources": MIN_SOURCES}}


def render(d: dict) -> str:
    lines = ["# Diagnosis", "", "Causes: " + ", ".join(f"{k} {v}" for k, v in sorted(d["counts"].items())), ""]
    for c in d["clusters"]:
        flag = "**actionable**" if c["actionable"] else "watch"
        where = ", ".join(c["tasks"] + [f"PR {p}" for p in c["prs"]])
        lines += [f"## {c['name']} ({c['id']}): {c['occurrences']} occurrence(s), {flag}",
                  f"- Seen in: {where}", f"- Candidate intervention: {c['intervention']}",
                  f"- Rule out first: {c['rule_out']}", ""]
    if d["human_queue"]:
        lines += ["## Queued for a human", ""] + [f"- `{q['trial']}`: {q['cause']} ({q['reason']})" for q in d["human_queue"]]
    return "\n".join(lines) + "\n"


def cmd(args, out):
    if not args.runs and not args.prs:
        raise CliError("give --runs and/or --prs")
    d = diagnose([Path(r) for r in args.runs], [Path(p) for p in args.prs], Path(args.corpus) if args.corpus else None,
                 Path(args.labels) if args.labels else None, Path(args.taxonomy) if args.taxonomy else None, args.rerun)
    if args.out:
        Path(args.out).write_text(json.dumps(d, indent=2) + "\n")
    out.write(json.dumps(d, indent=2) + "\n" if args.json else render(d))
    return 0

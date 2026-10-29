"""Run a harness over a suite of tasks: trials, sandboxes, grading and results.json.

A harness configuration is YAML:

    name: candidate
    version: "2"
    agent: {type: scripted, script: script.yaml}      # or {type: command, command: "claude -p {prompt} ..."}
    policy: {detectors: {D3: {level: advise}}}         # merged into the sandbox's nudge policy
    profile: {thresholds: {D3: {edits: 2}}}            # merged into the sandbox's repo profile
    instructions: "Run the tests before finishing."    # appended to the sandbox's CLAUDE.md
    sandbox: {image: python:3.12-slim, network: none}  # docker sandbox options
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from .._yaml import yaml

from .. import __version__, config, corpus
from ..cli import CliError
from ..config import deep_merge
from ..graders import trajectory
from ..paths import Layout, safe_session
from ..trace import store
from .agents import load_harness, make_agent
from .sandbox import make_sandbox


def config_digest(cfg: dict) -> str:
    material = {k: v for k, v in cfg.items() if k != "_dir"}
    extra = []
    for ref in ((cfg.get("agent") or {}).get("script"), cfg.get("instructions_file")):
        if ref:
            p = Path(cfg["_dir"]) / ref
            extra.append(p.read_text() if p.exists() else "")
    return hashlib.sha256(json.dumps([material, extra], sort_keys=True, default=str).encode()).hexdigest()[:16]


def apply_harness(cfg: dict, root: Path) -> None:
    layout = Layout(root)
    layout.base.mkdir(parents=True, exist_ok=True)
    for key, path in (("policy", layout.policy), ("profile", layout.profile)):
        if cfg.get(key):
            current = yaml.safe_load(path.read_text()) if path.exists() else {}
            path.write_text(yaml.safe_dump(deep_merge(current or {}, cfg[key]), sort_keys=False))
    text = cfg.get("instructions")
    if cfg.get("instructions_file"):
        text = (Path(cfg["_dir"]) / cfg["instructions_file"]).read_text()
    if text:
        md = root / "CLAUDE.md"
        md.write_text((md.read_text() + "\n\n" if md.exists() else "") + text.strip() + "\n")


def _diff_lines(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def run_trial(task_file: Path, task: dict, cfg: dict, agent, sandbox_kind: str, run_id: str, trial: int,
              out_dir: Path) -> dict:
    session = safe_session(f"s-{run_id}-{task['id']}-{trial}")
    t0 = time.time()
    rec = {"task": task["id"], "trial": trial, "source": task.get("source"), "split": task.get("split"),
           "session": session}
    with make_sandbox(sandbox_kind, task["repo"]["url"], task["repo"]["commit"], **(cfg.get("sandbox") or {})) as sb:
        if hasattr(sb, "in_setup"):
            sb.in_setup = True
        for cmd in task.get("setup") or []:
            code, out = sb.run(cmd)
            if code != 0:
                return {**rec, "passed": False, "error": f"setup failed: {cmd}", "setup_output": out[-1000:],
                        "safety_violations": 0, "cost_usd": 0.0, "tool_calls": 0, "latency_s": 0.0,
                        "outcome": [], "trajectory": {"findings": [], "checks": []}, "efficiency": {"over_budget": []}}
        if hasattr(sb, "in_setup"):
            sb.in_setup = False
        apply_harness(cfg, sb.path)
        from ..settings import hermetic
        with hermetic(sb.tmp / "harness-evo-config"):  # the user's own settings must not change eval results
            info = agent.run(task, task_file, sb, session)
            cfg_loaded = config.load(sb.path)
            events = store.read(Layout(sb.path).trace(info.get("session") or session))
        session = info.get("session") or session
        changed = sb.changed_files()
        results = corpus.grade_outcome(task, sb, changed)
        diff = sb.diff()
        (out_dir / "traces").mkdir(parents=True, exist_ok=True)
        (out_dir / "diffs").mkdir(parents=True, exist_ok=True)
        trace_rel = f"traces/{session}.jsonl"
        if events:
            shutil.copy(Layout(sb.path, mode="repo").trace(session), out_dir / trace_rel)
        diff_rel = f"diffs/{task['id']}-{trial}.diff"
        (out_dir / diff_rel).write_text(diff)
    graded = trajectory.grade_trace(events, cfg_loaded.profile, cfg_loaded.policy) if events else None
    graders_spec = task.get("graders") or {}
    checks = trajectory.task_graders(graders_spec.get("trajectory"), graded, changed) if graded else []
    summary = graded["summary"] if graded else {"counts": {"tool_calls": 0}, "cost": {}}
    eff = trajectory.efficiency(graders_spec.get("efficiency"), summary)
    findings = graded["findings"] if graded else []
    safety = sum(1 for f in findings if f["detector"] == "D7") + len(graded["destructive_executed"] if graded else [])
    ref = (Path(task_file).parent / task["reference"]) if task.get("reference") else None
    ref_lines = _diff_lines(ref.read_text()) if ref and ref.exists() else None
    cost = info.get("cost_usd")
    if cost is None:
        cost = (summary.get("cost") or {}).get("cost_usd")
    return {**rec, "session": session, "passed": all(r.passed for r in results) and bool(results),
            "outcome": [{"grader": r.grader, "passed": r.passed, "summary": r.summary} for r in results],
            "trajectory": {"findings": [{"rule": f["detector"], "seq": f["seq"], "target": f["target"]} for f in findings],
                           "checks": [{"grader": c.grader, "passed": c.passed, "summary": c.summary} for c in checks],
                           "metrics": graded["metrics"] if graded else {}},
            "efficiency": eff, "tool_calls": summary["counts"]["tool_calls"], "cost_usd": cost or 0.0,
            "safety_violations": safety, "latency_s": round(time.time() - t0, 2),
            "diff_lines": _diff_lines(diff), "reference_lines": ref_lines,
            "trace": trace_rel if events else None, "final_diff": diff_rel,
            "agent": {k: v for k, v in info.items() if k not in {"session"}}}


def select_tasks(suite: Path, split: str, only=None, smoke: int | None = None) -> tuple[list, list]:
    tasks, skipped = [], []
    for f, t in corpus.load_tasks(suite):
        if split != "all" and t.get("split") != split:
            continue
        if only and t["id"] not in only:
            continue
        if t.get("status") != "validated":
            skipped.append({"task": t["id"], "status": t.get("status")})
            continue
        tasks.append((f, t))
    tasks.sort(key=lambda ft: ft[1]["id"])
    if smoke:
        tasks = tasks[:smoke]
    return tasks, skipped


def run(suite: Path, harness: Path, out_dir: Path, *, trials: int | None = None, split: str = "dev",
        sandbox: str = "local", only=None, smoke: int | None = None, trial_indices=None) -> dict:
    cfg = load_harness(harness)
    agent = make_agent(cfg)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks, skipped = select_tasks(Path(suite), split, only, smoke)
    results_path = out_dir / "results.json"
    existing = json.loads(results_path.read_text()) if results_path.exists() and trial_indices is not None else None
    graders = {"trajectory": trajectory.VERSION}
    records = list(existing["trials"]) if existing else []
    for f, t in tasks:
        graders.update(corpus.outcome_versions(t))
        n = 1 if smoke else (trials or t.get("trials") or 3)
        for i in (trial_indices if trial_indices is not None else range(n)):
            records.append(run_trial(f, t, cfg, agent, sandbox, out_dir.name, i, out_dir))
    results = {
        "run_id": out_dir.name, "harness_evo": __version__,
        "harness": {"name": cfg.get("name", Path(harness).stem), "version": str(cfg.get("version", "")),
                    "config_sha256": config_digest(cfg)},
        "suite": str(suite), "split": split, "sandbox": sandbox, "graders": graders,
        "started": existing["started"] if existing else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ended": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skipped": skipped, "trials": records,
    }
    results_path.write_text(json.dumps(results, indent=2) + "\n")
    return results


def cmd(args, out):
    if not Path(args.suite).exists():
        raise CliError(f"suite {args.suite} does not exist")
    results = run(Path(args.suite), Path(args.harness), Path(args.out), trials=args.trials, split=args.split,
                  sandbox=args.sandbox, only=args.tasks, smoke=args.smoke)
    from ..report.run import render
    (Path(args.out) / "report.md").write_text(render(Path(args.out)))
    passed = sum(1 for t in results["trials"] if t["passed"])
    out.write(f"{results['harness']['name']}: {passed}/{len(results['trials'])} trials passed "
              f"({len(results['skipped'])} task(s) skipped as not validated). Results: {args.out}/results.json\n")
    return 0

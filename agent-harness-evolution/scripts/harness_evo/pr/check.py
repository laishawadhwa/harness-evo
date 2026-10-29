"""`harness-evo pr-check` (spec section 7).

Pipeline: collect traces from Harness-Session trailers -> outcome graders ->
trajectory graders (detectors in batch) -> optional model graders -> verdict and
report. Only deterministic outcome and safety failures set a non-zero exit code.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from .. import __version__, config, gitutil
from ..cli import CliError
from ..graders import Result, model as model_graders, outcome, trajectory
from ..paths import Layout
from ..trace import store
from ..trace.adapters.claude_code import Adapter
from ..trace.redact import redact_text
from ..trace.state import State

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}


@contextlib.contextmanager
def checkout(repo: Path, head: str):
    """Run graders against `head`: in place if it is checked out, else in a temporary worktree."""
    if gitutil.rev(repo, head) == gitutil.rev(repo, "HEAD"):
        yield repo
        return
    tmp = Path(tempfile.mkdtemp(prefix="harness-evo-pr-"))
    wt = tmp / "wt"
    gitutil.git(repo, "worktree", "add", "--detach", str(wt), head)
    try:
        yield wt
    finally:
        gitutil.git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_bundle(bundle: Path) -> Path:
    out = Path(tempfile.mkdtemp(prefix="harness-evo-bundle-"))
    with tarfile.open(bundle) as tar:
        for m in tar.getmembers():
            if m.isfile() and not m.name.startswith(("/", "..")) and ".." not in Path(m.name).parts:
                try:
                    tar.extract(m, out, filter="data")
                except TypeError:  # Python without tarfile extraction filters
                    tar.extract(m, out)
    return out / "traces"


def outcome_id(rule: str, evidence: list) -> str:
    """Stable across pushes, so ticked labels survive a report update."""
    digest = hashlib.sha256(json.dumps([e.get("ref") for e in evidence]).encode()).hexdigest()[:8]
    return f"pr:{rule}:{digest}"


def _finding(fid: str, rule: str, kind: str, severity: str, gating: bool, summary: str, evidence: list,
             context: list | None = None, **extra) -> dict:
    return {"id": fid, "rule": rule, "kind": kind, "severity": severity, "gating": gating, "summary": summary,
            "evidence": evidence, "context": context or [], **extra}


def task_scope(task: str | None, prompts: list[str], root: Path, profile) -> list[str]:
    adapter = Adapter(root, profile, State())
    scope = []
    for text in ([task] if task else []) + prompts:
        scope += adapter._prompt_scope(text or "")
    return list(dict.fromkeys(scope))


def check(repo: Path, base: str, head: str = "HEAD", traces_dir: Path | None = None, bundle: Path | None = None,
          task: str | None = None, use_model: bool = False, budget_usd: float = 0.5, run_tests: bool = True,
          model_client=None) -> dict:
    repo = Path(repo).resolve()
    cfg = config.load_at(repo, base)  # gates come from the base branch, not from the PR under review
    profile, policy = cfg.profile, cfg.policy
    pii = tuple(profile.pii_patterns)
    head_sha = gitutil.rev(repo, head)
    commits = gitutil.commits(repo, base, head)
    sessions = list(dict.fromkeys(s for c in commits for s in c.sessions))
    files = gitutil.diff(repo, base, head)
    paths = [f.path for f in files]
    harness_paths = [p for p in paths if profile.is_harness(p)]
    kind = "harness_change" if harness_paths else "agent" if sessions else "untraced"

    # 1. collect
    if bundle:
        traces_dir = _extract_bundle(Path(bundle))
    traces_dir = Path(traces_dir) if traces_dir else Layout(repo).traces
    traces, missing = {}, []
    for s in sessions:
        events = store.read(traces_dir / f"{s}.jsonl")
        if events:
            traces[s] = events
        else:
            missing.append(s)

    findings: list[dict] = []
    outcome_results: list[Result] = []
    prompts = [e["payload"].get("text") or "" for ev in traces.values() for e in ev if e["type"] == "prompt"]
    task = task or os.environ.get("HARNESS_EVO_TASK")

    # 2. outcome grading
    with checkout(repo, head) as tree:
        if run_tests:
            outcome_results.append(outcome.tests_pass(outcome.commands_for(paths, profile), tree))
            outcome_results += outcome.checks("lint", profile.lint_commands, tree)
            outcome_results += outcome.checks("typecheck", profile.typecheck_commands, tree)
    outcome_results += outcome.diff_hygiene(files, profile, task_scope(task, prompts, repo, profile))
    for r in outcome_results:
        if r.passed:
            continue
        findings.append(_finding(outcome_id(r.grader, r.evidence), r.grader, "outcome", r.severity, r.gating,
                                 r.summary, r.evidence))

    # 3. trajectory grading
    traj = None
    promoted = set((policy.data.get("pr_gating") or {}).get("rules") or []) | set(policy.state.get("pr_gating_rules") or [])
    if traces:
        traj = {"sessions": {}}
        for s, events in traces.items():
            g = trajectory.grade_trace(events, profile, policy)
            traj["sessions"][s] = {"metrics": g["metrics"], "summary": g["summary"],
                                   "findings": len(g["findings"])}
            for f in g["findings"]:
                findings.append(_finding(f["id"], f["detector"], "trajectory", f["severity"],
                                         f["detector"] in promoted, f["message"] or f["name"],
                                         [{"type": "trace", "ref": f"{s} #{q}", "session": s, "seq": q}
                                          for q in f["evidence"]], f["context"], session=s, name=f["name"],
                                         flag_for_pr=bool(f.get("flag_for_pr"))))
            for d in g["destructive_executed"]:
                ev = [{"type": "trace", "ref": f"{s} #{d['seq']}", "session": s, "seq": d["seq"]}]
                findings.append(_finding(outcome_id("destructive_action", ev), "destructive_action", "outcome",
                                         "high", True, f"A destructive action ran during the session: {d['what']}.", ev,
                                         trajectory.context(events, [d["seq"]])))
    if kind == "harness_change":
        ev = [{"type": "diff", "ref": p} for p in harness_paths]
        findings.append(_finding(outcome_id("harness_change", ev), "harness_change", "outcome", "info", False,
                                 "This PR changes the harness (" + ", ".join(harness_paths[:3]) + "). Run it through "
                                 "`harness-evo experiment run` against the baseline before merging.", ev))

    # 4. model grading (budgeted, optional)
    model_section = None
    if use_model and any(f["rule"] == "secret_in_diff" for f in findings):
        model_section = {"skipped": "the diff contains a possible secret, so it was not sent to a model", "results": []}
    elif use_model:
        inputs = {"task": task or "\n".join(prompts[:2]), "diff": gitutil.git(repo, "diff", f"{base}...{head}"),
                  "trace_summary": json.dumps({s: v["metrics"] for s, v in (traj or {}).get("sessions", {}).items()}),
                  "final_message": "\n".join(e["payload"].get("final_message") or "" for ev in traces.values()
                                             for e in ev if e["type"] == "stop_attempt")[-4000:]}
        model_section = model_graders.run(inputs, budget_usd, Layout(repo).cache / "model-grader", client=model_client)
        for r in model_section.get("results", []):
            out = r.get("output")
            if out and out.get("verdict") == "flag":
                ev = [{"type": "note", "ref": redact_text(str(e), pii)} for e in out.get("evidence", [])]
                findings.append(_finding(outcome_id(f"model:{r['grader']}", ev), f"model:{r['grader']}", "model",
                                         out.get("severity", "low") if r["calibrated"] else "info", False,
                                         redact_text(out.get("explanation", ""), pii), ev, calibrated=r["calibrated"]))

    findings.sort(key=lambda f: (not f["gating"], SEVERITY_RANK.get(f["severity"], 9)))
    gating = [f for f in findings if f["gating"]]
    attention = [f for f in findings if f["severity"] in ("high", "medium")]
    if gating:
        verdict, reason = "block recommended", gating[0]["summary"]
    elif attention:
        verdict, reason = "needs attention", attention[0]["summary"]
    else:
        verdict, reason = "pass", "No blocking or significant findings."

    summaries = [v["summary"] for v in (traj or {}).get("sessions", {}).values()]
    session_summary = {
        "sessions": len(summaries),
        "tool_calls": sum(s["counts"]["tool_calls"] for s in summaries),
        "retries": sum(s["counts"]["retries"] for s in summaries),
        "tests_run": sum(s["counts"]["tests_run"] for s in summaries),
        "nudges_issued": sum(s["nudges"]["delivered"] for s in summaries),
        "nudges_followed": sum(s["adherence"]["complied"] for s in summaries),
        "cost_usd": _sum_cost(summaries),
    }
    result = {
        "version": 1, "harness_evo": __version__, "kind": kind, "base": base, "head": head_sha,
        "sessions": sessions, "missing_traces": missing, "verdict": verdict, "reason": reason,
        "exit_code": 1 if gating else 0, "findings": findings,
        "outcome": [r.to_dict() for r in outcome_results], "trajectory": traj,
        "session_summary": session_summary, "what_went_well": went_well(outcome_results, traj, findings, kind),
        "model_graders": model_section, "graders": {**outcome.VERSIONS, "trajectory": trajectory.VERSION},
        "diff": next((r.details for r in outcome_results if r.grader == "diff_size"), {}),
    }
    if traj:
        for v in traj["sessions"].values():
            v.pop("summary", None)
    return result


def _sum_cost(summaries: list[dict]):
    costs = [(s.get("cost") or {}).get("cost_usd") for s in summaries]
    known = [c for c in costs if c is not None]
    return round(sum(known), 4) if known else None


def went_well(outcome_results: list[Result], traj: dict | None, findings: list[dict], kind: str) -> list[str]:
    good = []
    rules = {f["rule"] for f in findings}
    tests = next((r for r in outcome_results if r.grader == "tests_pass"), None)
    if tests and tests.passed and tests.details.get("ran"):
        good.append(tests.summary)
    if not rules & {"secret_in_diff", "protected_path", "generated_files"}:
        good.append("The diff has no secrets, protected paths or generated files.")
    if "test_weakening" not in rules and "D6" not in rules:
        good.append("No test was weakened.")
    if traj:
        metrics = [v["metrics"] for v in traj["sessions"].values()]
        if metrics and all(m["reads_before_edits"] in (None, 1.0) for m in metrics):
            good.append("Every edited file was read first.")
        if metrics and all(m["test_after_last_edit"] for m in metrics if m["test_after_last_edit"] is not None) \
                and any(m["test_after_last_edit"] is not None for m in metrics):
            good.append("Relevant tests ran after the last edit.")
        if "D7" not in rules and "destructive_action" not in rules:
            good.append("No destructive actions were attempted.")
        received = sum(m["nudges_received"] for m in metrics)
        followed = sum(m["nudges_followed"] for m in metrics)
        if received and followed:
            good.append(f"Followed {followed} of {received} harness notes.")
    return good


def cmd(args, out):
    from ..report.pr import render
    try:
        result = check(Path(args.repo), args.base, args.head, Path(args.traces) if args.traces else None,
                       Path(args.bundle) if args.bundle else None, args.task, args.model_grader, args.budget_usd,
                       not args.no_tests)
    except gitutil.GitError as exc:
        raise CliError(str(exc)) from exc
    previous = Path(args.previous_comment).read_text() if args.previous_comment and \
        Path(args.previous_comment).exists() else None
    md = render(result, previous)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2) + "\n")
    if args.md_out:
        Path(args.md_out).write_text(md)
    if not args.json_out and not args.md_out:
        out.write(md)
    else:
        out.write(f"{result['verdict']}: {result['reason']}\n")
    return result["exit_code"]

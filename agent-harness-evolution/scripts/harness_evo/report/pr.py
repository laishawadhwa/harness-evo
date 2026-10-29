"""Render the single PR comment, ordered by importance (spec section 7, report format)."""
from __future__ import annotations

from string import Template

from ..paths import skill_file

PREFIX = "Harness note: "


def _evidence(f: dict) -> str:
    refs = [f"`{e['ref']}`" for e in f.get("evidence") or [] if e.get("ref")]
    return ", ".join(refs[:6]) + (f" and {len(refs) - 6} more" if len(refs) > 6 else "")


def _ticked(previous: str | None) -> set[tuple[str, str]]:
    """(finding id, label) pairs ticked in the previous report comment."""
    if not previous:
        return set()
    import re
    return {(fid, label) for mark, label, fid in
            re.findall(r"- \[([ xX])\] (confirmed|false positive) <!-- finding:(\S+?) -->", previous) if mark.strip()}


def _finding(i: int, f: dict, ticked: set) -> str:
    summary = f["summary"][len(PREFIX):] if f["summary"].startswith(PREFIX) else f["summary"]
    label = f["rule"] + (f" {f['name']}" if f.get("name") else "")
    tags = []
    if f["gating"]:
        tags.append("fails the check")
    if f.get("flag_for_pr"):
        tags.append("flagged live")
    if f.get("calibrated") is False:
        tags.append("uncalibrated model grader, informational")
    tag = f" ({'; '.join(tags)})" if tags else ""
    lines = [f"{i}. **{f['severity']}** · {label}{tag}: {summary}"]
    ev = _evidence(f)
    if ev:
        lines.append(f"   Evidence: {ev}")
    if f.get("context"):
        lines += ["   <details><summary>Surrounding events</summary>", "", "   ```text"]
        lines += [f"   {c}" for c in f["context"]]
        lines += ["   ```", "   </details>"]
    for label in ("confirmed", "false positive"):
        box = "x" if (f["id"], label) in ticked else " "
        lines.append(f"   - [{box}] {label} <!-- finding:{f['id']} -->")
    return "\n".join(lines)


def render(result: dict, previous: str | None = None) -> str:
    """`previous` is the last report comment: ticks on findings that still exist are carried over."""
    ticked = _ticked(previous)
    kind = result["kind"]
    sessions = ", ".join(f"`{s}`" for s in result.get("sessions") or [])
    if kind == "agent":
        note = f"Agent-written PR, graded on the code and on the linked session traces ({sessions})."
    elif kind == "untraced":
        note = ("No `Harness-Session` trailer, so trajectory data was unavailable: only outcome graders ran "
                "(human-written, or hooks not installed).")
    else:
        note = ("Harness-change PR: this diff changes hook configuration, detectors, repo instructions or tool "
                "descriptions. Outcome graders ran below. Prove the change with the experiment workflow "
                "(`harness-evo experiment run`) on the regression suite before merging.")
    if result.get("missing_traces"):
        note += f" Traces not found for: {', '.join(result['missing_traces'])}."
    findings = [f for f in result["findings"] if f["severity"] != "info" or f["gating"]]
    info = [f for f in result["findings"] if f not in findings]
    body = "\n".join(_finding(i + 1, f, ticked) for i, f in enumerate(findings + info)) or "None."
    s = result["session_summary"]
    if result.get("trajectory"):
        cost = f"${s['cost_usd']:.2f}" if s.get("cost_usd") is not None else "n/a"
        table = ("| Sessions | Tool calls | Retries | Tests run | Nudges issued | Nudges followed | Cost |\n"
                 "| --- | --- | --- | --- | --- | --- | --- |\n"
                 f"| {s['sessions']} | {s['tool_calls']} | {s['retries']} | {s['tests_run']} | "
                 f"{s['nudges_issued']} | {s['nudges_followed']} | {cost} |")
    else:
        table = "No trace data for this PR."
    good = "\n".join(f"- {g}" for g in result.get("what_went_well") or []) or "- Nothing stood out."
    model_note = ""
    mg = result.get("model_graders")
    if mg:
        if mg.get("skipped"):
            model_note = f"\n_Model graders skipped: {mg['skipped']}._\n"
        else:
            unc = [r["grader"] for r in mg.get("results", []) if not r.get("calibrated")]
            model_note = f"\n_Model graders: ${mg.get('spent_usd', 0):.3f} of ${mg.get('budget_usd', 0):.2f} budget."
            model_note += f" Not yet calibrated (shown for information only): {', '.join(unc)}._\n" if unc else "_\n"
    graders = ", ".join(f"{k}@{v}" for k, v in sorted((result.get("graders") or {}).items()))
    tmpl = Template(skill_file("reports", "pr-report.md.tmpl").read_text())
    return tmpl.safe_substitute(verdict=result["verdict"], reason=result["reason"], kind_note=note, findings=body,
                                session_summary=table, went_well=good, model_note=model_note,
                                version=result.get("harness_evo", ""), head=result["head"][:7], graders=graders)

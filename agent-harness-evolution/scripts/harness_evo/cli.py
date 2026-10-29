"""harness-evo: one CLI for the live hooks, the PR check and the evolution loop.

`harness-evo hook <event>` takes a fast path that skips argparse and imports only
the hook pipeline. Every other subcommand imports its module lazily.
"""
from __future__ import annotations

import importlib
import json
import sys


class CliError(Exception):
    """A user-facing failure: printed without a traceback, exit code 1."""


def _lazy(target: str):
    module, func = target.split(":")

    def call(args, out):
        return getattr(importlib.import_module(f"harness_evo.{module}"), func)(args, out)
    return call


def build_parser():
    import argparse  # not needed on the hook fast path
    p = argparse.ArgumentParser(prog="harness-evo", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    def cmd(name, target, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=_lazy(target))
        return sp

    # ---- live layer
    sp = cmd("install", "install:cmd_install", "register hooks, git trailer hook and repo profile")
    sp.add_argument("--project-dir", default=".")
    sp.add_argument("--scope", choices=["local", "project", "user"], default="local",
                    help="settings file: .claude/settings.local.json, .claude/settings.json or ~/.claude/settings.json")
    sp.add_argument("--command", help="hook command prefix (default: harness-evo hook, or hooks/hook_entry.sh)")
    sp.add_argument("--no-settings", action="store_true")
    sp.add_argument("--no-git-hook", action="store_true")
    sp.add_argument("--settings-only", action="store_true",
                    help="only register the hooks (e.g. --scope user); create nothing in the project")
    sp = cmd("uninstall", "install:cmd_uninstall", "remove harness-evo hook registrations")
    sp.add_argument("--project-dir", default=".")
    sp.add_argument("--scope", choices=["local", "project", "user"], default="local")
    for name in ("mute", "unmute"):
        sp = cmd(name, f"install:cmd_{name}", f"{name} a detector for this repo or one session")
        sp.add_argument("detector")
        sp.add_argument("--project-dir", default=".")
        sp.add_argument("--session")
    sp = cmd("replay", "cli:cmd_replay", "batch-replay a stored trace through the detectors")
    sp.add_argument("trace", help="a trace file, or a session id (with --project-dir)")
    sp.add_argument("--project-dir")
    sp.add_argument("--json", action="store_true")
    sp = cmd("summary", "cli:cmd_summary", "summarise a stored session trace")
    sp.add_argument("trace", help="a trace file, or a session id (with --project-dir)")
    sp.add_argument("--project-dir")
    sp = cmd("settings", "settings:cmd_settings", "show the effective user settings and where each comes from")
    sp.add_argument("--json", action="store_true")
    sp = cmd("paths", "cli:cmd_paths", "print where harness-evo's bundled files, settings and data live")
    sp.add_argument("--repo", default=".")
    sp.add_argument("--json", action="store_true")
    sp = cmd("active-session", "cli:cmd_active_session", "print the session id of the agent working in a repo")
    sp.add_argument("--repo", default=".")
    sp = cmd("init", "init:cmd_init", "detect the repo's stack and write a starter .harness-evo/profile.yaml")
    sp.add_argument("--project-dir", default=".")
    sp.add_argument("--dry-run", action="store_true", help="show the profile without writing anything")
    sp.add_argument("--force", action="store_true", help="replace an existing profile")
    sp.add_argument("--git-hook", dest="git_hook", action="store_true", default=None,
                    help="also install the Harness-Session commit trailer hook")
    sp.add_argument("--no-git-hook", dest="git_hook", action="store_false")
    sp.add_argument("--json", action="store_true")
    sp = cmd("validate-trace", "cli:cmd_validate_trace", "check a trace against contract v1")
    sp.add_argument("trace")

    # ---- PR loop
    sp = cmd("pr-check", "pr.check:cmd", "grade a PR on its code and its traces")
    sp.add_argument("--repo", default=".")
    sp.add_argument("--base", required=True)
    sp.add_argument("--head", default="HEAD")
    sp.add_argument("--traces", help="directory of session traces (default: <repo>/.harness-evo/traces)")
    sp.add_argument("--bundle", help="trace bundle .tgz from `harness-evo bundle`")
    sp.add_argument("--task", help="the stated task (default: PR title/body from the environment, then prompts)")
    sp.add_argument("--json-out")
    sp.add_argument("--md-out")
    sp.add_argument("--model-grader", action="store_true", help="run the budgeted model graders")
    sp.add_argument("--budget-usd", type=float, default=0.50)
    sp.add_argument("--no-tests", action="store_true", help="skip running test, lint and type commands")
    sp.add_argument("--previous-comment", help="the last report comment, so ticked labels carry over")
    sp = cmd("bundle", "pr.bundle:cmd_bundle", "build (and optionally push) the redacted trace bundle for a branch")
    sp.add_argument("--repo", default=".")
    sp.add_argument("--base", required=True)
    sp.add_argument("--head", default="HEAD")
    sp.add_argument("--out")
    sp.add_argument("--push", action="store_true", help="push to refs/harness-evo/sessions/<id> on --remote")
    sp.add_argument("--remote", default="origin")
    sp = cmd("fetch-bundles", "pr.bundle:cmd_fetch", "fetch pushed session bundles into a traces directory")
    sp.add_argument("--repo", default=".")
    sp.add_argument("--base", required=True)
    sp.add_argument("--head", default="HEAD")
    sp.add_argument("--remote", default="origin")
    sp.add_argument("--out-dir", required=True)
    sp = cmd("sync-labels", "labels:cmd_sync", "read confirmed / false positive ticks from the PR report comment")
    sp.add_argument("--project-dir", default=".")
    sp.add_argument("--comment-file")
    sp.add_argument("--pr", type=int)
    sp.add_argument("--repo-slug", help="owner/name, for fetching the comment with gh")
    sp.add_argument("--report-author", default="github-actions[bot]",
                    help="only report comments by this login are read")
    sp = cmd("label", "labels:cmd_label", "label one finding by hand")
    sp.add_argument("finding")
    sp.add_argument("label", choices=["confirmed", "false_positive", "acceptable"])
    sp.add_argument("--project-dir", default=".")

    # ---- detector fate
    sp = cmd("calibrate", "calibrate:cmd_calibrate", "precision / acted-on / repeat per detector; demote on low precision")
    sp.add_argument("--project-dir", default=".")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp = cmd("promote", "calibrate:cmd_promote", "promote a detector one level if it cleared the precision bar")
    sp.add_argument("detector")
    sp.add_argument("--project-dir", default=".")
    sp.add_argument("--pr-gating", action="store_true", help="let this trajectory rule fail the PR check (90%% over 50 PRs)")
    sp.add_argument("--json", action="store_true")
    sp = cmd("ab-report", "calibrate:cmd_ab_report", "compare nudges-on and nudges-off sessions")
    sp.add_argument("--project-dir", default=".")
    sp.add_argument("--json", action="store_true")

    # ---- evolution loop
    sp = cmd("corpus", "corpus:cmd_corpus", "eval corpus tools")
    sp.add_argument("action", choices=["validate"])
    sp.add_argument("path")
    sp.add_argument("--json", action="store_true")
    sp = cmd("validate-task", "corpus:cmd_validate_task", "admit a task: run its reference solution twice")
    sp.add_argument("task")
    sp.add_argument("--write", action="store_true", help="write the resulting status back to the task file")
    sp.add_argument("--sandbox", choices=["local", "docker"], default="local")
    sp.add_argument("--json", action="store_true")
    sp = cmd("run-suite", "runner.suite:cmd", "run a harness over a suite of tasks")
    sp.add_argument("--suite", required=True)
    sp.add_argument("--harness", required=True)
    sp.add_argument("--trials", type=int)
    sp.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    sp.add_argument("--out", required=True)
    sp.add_argument("--sandbox", choices=["local", "docker"], default="local")
    sp.add_argument("--tasks", nargs="*", help="only these task ids")
    sp.add_argument("--smoke", type=int, help="run only the first N tasks, one trial each")
    sp = cmd("compare", "stats.compare:cmd", "paired baseline-vs-candidate comparison with a bootstrap interval")
    sp.add_argument("baseline")
    sp.add_argument("candidate")
    sp.add_argument("--metric", default="pass_rate")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--json", action="store_true")
    sp = cmd("decide", "stats.decide:cmd", "apply a decision rule to dev (and held-out) comparisons")
    sp.add_argument("--dev-baseline", required=True)
    sp.add_argument("--dev-candidate", required=True)
    sp.add_argument("--heldout-baseline")
    sp.add_argument("--heldout-candidate")
    sp.add_argument("--rule", help="YAML decision rule (default: the spec's default rule)")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--json", action="store_true")
    sp = cmd("diagnose", "diagnose:cmd", "attribute failures and cluster them with the failure taxonomy")
    sp.add_argument("--runs", nargs="+", default=[])
    sp.add_argument("--prs", nargs="*", default=[], help="pr-check result JSON files")
    sp.add_argument("--corpus", help="task directory, for task status")
    sp.add_argument("--labels", help="labels.jsonl")
    sp.add_argument("--taxonomy", help="failure-taxonomy.md (default: the skill's copy)")
    sp.add_argument("--rerun", action="store_true", help="re-run outcome graders to detect environment flakes")
    sp.add_argument("--out")
    sp.add_argument("--json", action="store_true")
    sp = cmd("experiment", "experiment:cmd", "pre-registered harness experiments")
    sp.add_argument("action", choices=["new", "lock", "run"])
    sp.add_argument("target", help="experiment name (new) or experiment file (lock, run)")
    sp.add_argument("--dir", default="interventions")
    sp.add_argument("--cluster")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--json", action="store_true")
    sp = cmd("report", "report:cmd", "render a PR or run report")
    sp.add_argument("kind", choices=["pr", "run"])
    sp.add_argument("source", help="pr-check result JSON, or a run directory")
    sp.add_argument("--out")
    return p


def main(argv=None, stdin=None, stdout=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if argv[:1] == ["hook"]:
        from .hook import main as hook_main
        return hook_main(argv[1] if len(argv) > 1 else "", stdin, stdout)
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args, stdout) or 0)
    except CliError as exc:
        if getattr(args, "json", False):
            stdout.write(json.dumps({"error": str(exc)}) + "\n")
        else:
            stdout.write(f"error: {exc}\n")
        return 1


# ---------------------------------------------------------------- small commands

def _root_for_trace(trace, project_dir):
    from pathlib import Path
    if project_dir:
        return Path(project_dir)
    t = Path(trace).resolve()
    if t.parent.name == "traces" and t.parent.parent.name == ".harness-evo":
        return t.parent.parent.parent
    return Path.cwd()


def _trace_path(trace, project_dir):
    """A trace file as given, or the trace of session `trace` in the project (repo or user storage)."""
    from pathlib import Path

    from .paths import Layout, safe_session
    p = Path(trace)
    if p.exists() or p.suffix == ".jsonl" or "/" in trace:
        return p
    path = Layout(Path(project_dir or ".").resolve()).trace(safe_session(trace))
    if not path.exists():
        raise CliError(f"no trace for session {trace} (looked in {path})")
    return path


def cmd_paths(args, out):
    from pathlib import Path

    from . import settings
    from .paths import Layout, skill_root
    layout = Layout(Path(args.repo).resolve())
    report = {"skill_root": str(skill_root()), "taxonomy": str(skill_root() / "analysis" / "failure-taxonomy.md"),
              "experiment_template": str(skill_root() / "interventions" / "experiment-template.md"),
              "config_dir": str(settings.config_dir()), "data_root": str(settings.data_root()),
              "repo_config_dir": str(layout.config_dir), "repo_data_dir": str(layout.base),
              "trace_location": layout.mode}
    if args.json:
        out.write(json.dumps(report, indent=2) + "\n")
    else:
        out.write("".join(f"{k:<20} {v}\n" for k, v in report.items()))
    return 0


def cmd_active_session(args, out):
    from pathlib import Path

    from .paths import Layout
    try:
        sid = Layout(Path(args.repo).resolve()).active_session.read_text().strip()
    except OSError:
        return 0
    if sid:
        out.write(sid + "\n")
    return 0


def cmd_replay(args, out):
    from . import config
    from .replay import replay
    cfg = config.load(_root_for_trace(args.trace, args.project_dir))
    findings, _ = replay(_trace_path(args.trace, args.project_dir), cfg.profile, cfg.policy)
    if args.json:
        out.write(json.dumps({"findings": findings}, indent=2) + "\n")
    else:
        for f in findings:
            out.write(f"#{f['seq']:<5} {f['detector']} {f['name']:<20} {f['target']}\n    {f['message']}\n")
    return 0


def cmd_summary(args, out):
    from pathlib import Path

    from . import config
    from .summary import summarize
    from .trace import store
    root = _root_for_trace(args.trace, args.project_dir)
    cfg = config.load(root)
    trace = _trace_path(args.trace, args.project_dir)
    events = store.read(trace)
    latencies = None
    if events:  # the hooks keep their own timings in the session state
        from .paths import Layout
        from .trace import state as state_mod
        try:
            st = state_mod.load(Layout(Path(root).resolve()).state(events[0]["session"]))
            latencies = st.latencies_ms if st else None
        except (OSError, ValueError):
            latencies = None
    out.write(json.dumps(summarize(events, cfg.profile, cfg.policy, latencies=latencies), indent=2) + "\n")
    return 0


def cmd_validate_trace(args, out):
    from pathlib import Path

    from .trace import store
    from .trace.schema import validate_stream
    errors = validate_stream(store.read(Path(args.trace)))
    for e in errors:
        out.write(e + "\n")
    out.write(f"{'invalid' if errors else 'valid'}: {len(errors)} error(s)\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

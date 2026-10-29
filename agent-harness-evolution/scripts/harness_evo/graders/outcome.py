"""Deterministic outcome graders: tests, lint, type checks, diff hygiene, file allow-lists.

Only these graders (plus destructive actions in the trace) may fail a CI check.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ..config import Profile, glob_match
from ..gitutil import FileDiff
from ..trace import testdiff
from ..trace.redact import contains_secret, redact_text
from . import Result

VERSIONS = {"tests_pass": "1", "lint": "1", "typecheck": "1", "secret_in_diff": "1", "protected_path": "1",
            "generated_files": "1", "test_weakening": "1", "unrelated_changes": "1", "diff_size": "1",
            "changed_files_within": "1"}
OUTPUT_TAIL = 3000


_CREDENTIAL_ENV = re.compile(r"(?i)token|secret|passw|api_?key|access_?key|private_?key|credential|"
                             r"^anthropic_|^aws_|^gh_|^azure_|^google_application|^ssh_auth_sock$")


def clean_env() -> dict:
    """The environment for PR-controlled commands (tests, lint): every credential-looking variable removed."""
    return {k: v for k, v in os.environ.items() if not _CREDENTIAL_ENV.search(k)}


def run_command(command: str, cwd: Path, timeout: int = 1800, env: dict | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env=env if env is not None else clean_env())
        out = r.stdout + ("\n" + r.stderr if r.stderr else "")
        return r.returncode, redact_text(out[-65536:])[-OUTPUT_TAIL:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def commands_for(paths: list[str], profile: Profile) -> list[tuple[str, str]]:
    """Test commands for the changed and impacted modules."""
    cmds = [(g.name, g.command) for g in profile.impacted_groups(paths) if g.command]
    if not cmds and any(profile.is_code(p) for p in paths) and profile.data.get("default_test_command"):
        cmds = [("default", profile.data["default_test_command"])]
    return list(dict.fromkeys(cmds))


def tests_pass(commands: list[tuple[str, str]], cwd: Path, env: dict | None = None) -> Result:
    if not commands:
        return Result("tests_pass", True, VERSIONS["tests_pass"], summary="No mapped tests for the changed files.",
                      details={"ran": []})
    ran, failed = [], []
    for name, cmd in commands:
        code, out = run_command(cmd, cwd, env=env)
        ran.append({"name": name, "command": cmd, "exit_code": code, "output_tail": out})
        if code != 0:
            failed.append(name)
    if failed:
        return Result("tests_pass", False, VERSIONS["tests_pass"], gating=True, severity="high",
                      summary=f"Tests fail for {', '.join(failed)}.",
                      evidence=[{"type": "command", "ref": r["command"]} for r in ran if r["exit_code"]],
                      details={"ran": ran})
    return Result("tests_pass", True, VERSIONS["tests_pass"], summary=f"Tests pass for {', '.join(n for n, _ in commands)}.",
                  details={"ran": ran})


def checks(kind: str, commands: list[dict], cwd: Path) -> list[Result]:
    out = []
    for c in commands:
        code, text = run_command(c["command"], cwd)
        ok = code == 0
        out.append(Result(kind, ok, VERSIONS[kind], gating=bool(c.get("gating")) and not ok,
                          severity="info" if ok else "medium",
                          summary=f"{c.get('name', kind)} {'passes' if ok else 'fails'}.",
                          evidence=[] if ok else [{"type": "command", "ref": c["command"]}],
                          details={"exit_code": code, "output_tail": text}))
    return out


def diff_hygiene(files: list[FileDiff], profile: Profile, task_scope: list[str] | None = None) -> list[Result]:
    results = []
    secrets = []
    for f in files:
        for line_no, text in f.added:
            kinds = contains_secret(text)
            if kinds:
                secrets.append({"type": "diff", "ref": f"{f.path}:{line_no}", "kinds": kinds})
    if secrets:
        refs = ", ".join(s["ref"] for s in secrets[:3])
        results.append(Result("secret_in_diff", False, VERSIONS["secret_in_diff"], gating=True, severity="high",
                              summary=f"Possible secret added at {refs} ({secrets[0]['kinds'][0]}).",
                              evidence=secrets))
    protected = [f.path for f in files if profile.is_protected(f.path) or (f.old_path and profile.is_protected(f.old_path))]
    if protected:
        results.append(Result("protected_path", False, VERSIONS["protected_path"], gating=True, severity="high",
                              summary=f"Protected path changed: {', '.join(protected[:3])}.",
                              evidence=[{"type": "diff", "ref": p} for p in protected]))
    generated = [f.path for f in files if f.status != "D" and profile.is_generated(f.path)]
    if generated:
        results.append(Result("generated_files", False, VERSIONS["generated_files"], severity="medium",
                              summary=f"Generated files committed: {', '.join(generated[:3])}.",
                              evidence=[{"type": "diff", "ref": p} for p in generated]))
    for f in files:
        if not profile.is_test(f.path) or f.binary:
            continue
        change = testdiff.analyse_hunks([t for _, t in f.removed], [t for _, t in f.added])
        if testdiff.is_weakening(change):
            line = f.removed[0][0] if f.removed else (f.added[0][0] if f.added else 1)
            results.append(Result("test_weakening", False, VERSIONS["test_weakening"], severity="medium",
                                  summary=f"{f.path} {testdiff.describe(change)}.",
                                  evidence=[{"type": "diff", "ref": f"{f.path}:{line}"}], details=change))
    if task_scope:
        outside = [f.path for f in files if not in_scope(f.path, task_scope, profile)]
        if outside:
            results.append(Result("unrelated_changes", False, VERSIONS["unrelated_changes"], severity="low",
                                  summary=f"Changes outside the stated task: {', '.join(outside[:3])}.",
                                  evidence=[{"type": "diff", "ref": p} for p in outside]))
    added = sum(len(f.added) for f in files)
    removed = sum(len(f.removed) for f in files)
    results.append(Result("diff_size", True, VERSIONS["diff_size"], summary=f"{len(files)} files, +{added} -{removed}.",
                          details={"files": len(files), "added": added, "removed": removed}))
    return results


def in_scope(path: str, scope: list[str], profile: Profile) -> bool:
    groups = {g.name for g in profile.groups_for(path)}
    for item in scope:
        kind, _, val = item.partition(":")
        if kind == "dir" and (path == val or path.startswith(val + "/")):
            return True
        if kind == "file" and path == val:
            return True
        if kind == "group" and val in groups:
            return True
    return False


def changed_files_within(paths: list[str], allowed: list[str]) -> Result:
    outside = [p for p in paths if not glob_match(p, allowed)]
    return Result("changed_files_within", not outside, VERSIONS["changed_files_within"],
                  severity="info" if not outside else "medium",
                  summary="All changes are within the allowed paths." if not outside
                  else f"Changed outside {', '.join(allowed)}: {', '.join(outside[:3])}.",
                  evidence=[{"type": "diff", "ref": p} for p in outside])

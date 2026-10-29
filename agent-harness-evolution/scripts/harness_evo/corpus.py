"""Eval corpus: task schema validation and task admission (spec section 8).

A task becomes `validated` only after its reference solution passes every
outcome grader and a second run gives the same result. A task that flips
between runs on the same snapshot is `quarantined` until the cause is found.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ._yaml import yaml

from .cli import CliError
from .graders import Result, outcome
from .paths import skill_file

DEFAULT_TEST_COMMAND = "python -m pytest -q {paths}"


# ---------------------------------------------------------------- schema (a JSON Schema subset)

def _check(value, schema: dict, path: str, errors: list) -> None:
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    if t and not isinstance(value, types[t]) or (t in ("integer", "number") and isinstance(value, bool)):
        errors.append(f"{path or 'task'}: expected {t}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {', '.join(map(str, schema['enum']))}")
    if t == "string":
        if "pattern" in schema and not re.match(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: must not be empty")
    if t in ("integer", "number") and value < schema.get("minimum", value):
        errors.append(f"{path}: must be at least {schema['minimum']}")
    if t == "array":
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        for i, item in enumerate(value):
            if "items" in schema:
                _check(item, schema["items"], f"{path}[{i}]", errors)
    if t == "object":
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path + '.' if path else ''}{key}: required")
        props = schema.get("properties") or {}
        for key, v in value.items():
            if key in props:
                _check(v, props[key], f"{path + '.' if path else ''}{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path + '.' if path else ''}{key}: unknown field")


def schema() -> dict:
    return yaml.safe_load(skill_file("evals", "task-schema.yaml").read_text())


def validate(task: dict) -> list[str]:
    errors: list[str] = []
    _check(task, schema(), "", errors)
    for i, g in enumerate((task.get("graders") or {}).get("outcome") or []):
        name = g if isinstance(g, str) else next(iter(g), None) if isinstance(g, dict) else None
        if name not in {"tests_pass", "changed_files_within", "command"}:
            errors.append(f"graders.outcome[{i}]: unknown outcome grader {name!r}")
    return errors


def load_tasks(path: Path) -> list[tuple[Path, dict]]:
    path = Path(path)
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*.yaml") if "patches" not in p.parts)
    out = []
    for f in files:
        data = yaml.safe_load(f.read_text())
        if isinstance(data, dict) and "id" in data:
            out.append((f, data))
    return out


# ---------------------------------------------------------------- outcome graders for a task

def grade_outcome(task: dict, sandbox, changed: list[str]) -> list[Result]:
    results = []
    for g in (task.get("graders") or {}).get("outcome") or []:
        name, opts = (g, {}) if isinstance(g, str) else next(iter(g.items()))
        opts = opts or {}
        if name == "tests_pass":
            paths = opts.get("paths") or []
            cmd = (opts.get("command") or DEFAULT_TEST_COMMAND).replace("{paths}", " ".join(paths))
            code, out = sandbox.run(cmd)
            results.append(Result("tests_pass", code == 0, outcome.VERSIONS["tests_pass"], severity="high",
                                  summary=f"`{cmd}` exited {code}", details={"output_tail": out[-1500:]}))
        elif name == "changed_files_within":
            allowed = opts if isinstance(opts, list) else opts.get("paths") or []
            results.append(outcome.changed_files_within(changed, allowed))
        elif name == "command":
            code, out = sandbox.run(opts["run"])
            want = opts.get("expect_exit", 0)
            results.append(Result("command", code == want, "1", summary=f"`{opts['run']}` exited {code}",
                                  details={"output_tail": out[-1500:]}))
    return results


def outcome_versions(task: dict) -> dict:
    names = [g if isinstance(g, str) else next(iter(g)) for g in (task.get("graders") or {}).get("outcome") or []]
    return {n: outcome.VERSIONS.get(n, "1") for n in names}


# ---------------------------------------------------------------- admission

def reference_run(task_file: Path, task: dict, sandbox_kind: str = "local") -> dict:
    from .runner.sandbox import make_sandbox
    patch = (task_file.parent / task["reference"]).resolve()
    with make_sandbox(sandbox_kind, task["repo"]["url"], task["repo"]["commit"]) as sb:
        for cmd in task.get("setup") or []:
            code, out = sb.run(cmd)
            if code != 0:
                return {"passed": False, "setup_failed": cmd, "graders": []}
        sb.apply_patch(patch)
        results = grade_outcome(task, sb, sb.changed_files())
    return {"passed": all(r.passed for r in results), "graders": [{"grader": r.grader, "passed": r.passed,
                                                                    "summary": r.summary} for r in results]}


def admit(task_file: Path, sandbox_kind: str = "local") -> dict:
    task = yaml.safe_load(Path(task_file).read_text())
    errors = validate(task)
    if errors:
        return {"task": task.get("id"), "status": "draft", "reason": "schema errors: " + "; ".join(errors), "runs": []}
    runs = [reference_run(Path(task_file), task, sandbox_kind) for _ in range(2)]
    outcomes = [r["passed"] for r in runs]
    signatures = [[g["passed"] for g in r["graders"]] for r in runs]
    if signatures[0] != signatures[1] or outcomes[0] != outcomes[1]:
        status, reason = "quarantined", "the reference gave different results on two runs of the same snapshot"
    elif all(outcomes):
        status, reason = "validated", "the reference passes every outcome grader, twice"
    else:
        failed = [g["grader"] for g in runs[0]["graders"] if not g["passed"]] or ["setup"]
        status, reason = "draft", f"the reference solution fails {', '.join(failed)}"
    return {"task": task["id"], "status": status, "reason": reason, "runs": runs}


def write_status(task_file: Path, status: str) -> None:
    text = Path(task_file).read_text()
    if re.search(r"(?m)^status:\s*\S+", text):
        text = re.sub(r"(?m)^status:\s*\S+.*$", f"status: {status}", text, count=1)
    else:
        text = text.rstrip("\n") + f"\nstatus: {status}\n"
    Path(task_file).write_text(text)


def cmd_corpus(args, out):
    tasks = load_tasks(Path(args.path))
    if not tasks:
        raise CliError(f"no task files under {args.path}")
    report = {"tasks": {}}
    for f, t in tasks:
        report["tasks"][t.get("id", str(f))] = {"file": str(f), "status": t.get("status"), "split": t.get("split"),
                                                 "errors": validate(t)}
    bad = [k for k, v in report["tasks"].items() if v["errors"]]
    splits = [v["split"] for v in report["tasks"].values()]
    report["summary"] = {"tasks": len(tasks), "invalid": len(bad),
                         "validated": sum(1 for v in report["tasks"].values() if v["status"] == "validated"),
                         "heldout_share": round(splits.count("heldout") / len(splits), 2) if splits else None}
    if args.json:
        out.write(json.dumps(report, indent=2) + "\n")
    else:
        for k, v in report["tasks"].items():
            out.write(f"{k:<40} {v['status'] or '-':<12} {v['split'] or '-':<8} "
                      f"{'ok' if not v['errors'] else '; '.join(v['errors'])}\n")
    return 1 if bad else 0


def cmd_validate_task(args, out):
    result = admit(Path(args.task), args.sandbox)
    if args.write:
        write_status(Path(args.task), result["status"])
    if args.json:
        out.write(json.dumps(result, indent=2) + "\n")
    else:
        out.write(f"{result['task']}: {result['status']} ({result['reason']})\n")
    return 0 if result["status"] == "validated" else 1

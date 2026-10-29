"""Pre-registered harness experiments (spec section 10).

`new` copies the template, `lock` records a hash of the hypothesis, decision rule and
settings, and `run` executes the procedure: baseline and candidate on the same dev tasks,
trial by trial with early stopping; then, only if dev clears the bar, both versions once on
the held-out split; then the decision rule. The result is written back into the file.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from ._yaml import yaml

from .cli import CliError
from .config import deep_merge
from .paths import skill_file
from .stats.bootstrap import excludes_zero
from .stats.compare import compare, load
from .stats.decide import DEFAULT_RULE, decide, guardrail_breaks

_BLOCK = re.compile(r"```yaml experiment\n(.*?)```", re.S)
_LOCK = re.compile(r"<!-- harness-evo:lock sha256=([0-9a-f]{64}) locked=(\S+) -->")
_PREREG = ("- Cluster:", "- Hypothesis:", "- Change:", "- Baseline:", "- Target metric:", "- Guardrails:",
           "- Decision rule:")


def settings(text: str) -> dict:
    m = _BLOCK.search(text)
    if not m:
        raise CliError("no ```yaml experiment``` settings block in the experiment file")
    return yaml.safe_load(m.group(1)) or {}


def registration_hash(text: str) -> str:
    m = _BLOCK.search(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(_PREREG)]
    return hashlib.sha256(json.dumps([lines, m.group(1) if m else ""]).encode()).hexdigest()


def lock(path: Path) -> str:
    text = path.read_text()
    h = registration_hash(text)
    m = _LOCK.search(text)
    if m:
        if m.group(1) != h:
            raise CliError(f"the pre-registered settings changed after they were locked at {m.group(2)}; "
                           "restore them, or start a new experiment")
        return h
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(text.rstrip("\n") + f"\n\n<!-- harness-evo:lock sha256={h} locked={stamp} -->\n")
    return h


def new(name: str, directory: Path, cluster: str | None) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
        raise CliError("experiment names are lowercase letters, digits, '.', '_' and '-'")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    if path.exists():
        raise CliError(f"{path} already exists")
    text = skill_file("interventions", "experiment-template.md").read_text().replace("<short name>", name)
    if cluster:
        text = text.replace("<taxonomy id>", cluster, 1)
    path.write_text(text)
    return path


def _summ(cmp: dict | None) -> dict | None:
    if not cmp:
        return None
    return {k: cmp[k] for k in ("metric", "baseline", "candidate", "delta", "ci", "n_tasks")} | \
        {"tasks_lost": cmp["guardrails"]["tasks_lost"],
         "safety": [cmp["guardrails"]["baseline"]["safety_violations"], cmp["guardrails"]["candidate"]["safety_violations"]]}


def run(path: Path, seed: int = 0) -> dict:
    from .report.run import render_experiment
    from .runner import suite as suite_mod
    path = Path(path).resolve()
    lock(path)
    text = path.read_text()
    meta = settings(text)
    for key in ("baseline", "candidate", "suite"):
        if not meta.get(key):
            raise CliError(f"the settings block needs `{key}`")
    here = path.parent
    rel = lambda p: (here / p).resolve()  # noqa: E731
    rule = deep_merge(DEFAULT_RULE, {"target": meta.get("target") or {}, "guardrails": meta.get("guardrails") or {},
                                     "heldout": meta.get("heldout") or {}})
    metric = rule["target"].get("metric", "pass_rate")
    trials, min_trials = int(meta.get("trials", 3)), int(meta.get("min_trials", 2))
    sandbox = meta.get("sandbox", "local")
    runs = here / "runs" / f"{path.stem}-{time.strftime('%Y%m%dT%H%M%S')}"
    arms = {"baseline": rel(meta["baseline"]), "candidate": rel(meta["candidate"])}
    suite_path = rel(meta["suite"])

    stopped_early, rounds = False, 0
    for r in range(trials):
        for arm, harness in arms.items():
            suite_mod.run(suite_path, harness, runs / f"dev-{arm}", split="dev", sandbox=sandbox, trial_indices=[r])
        rounds = r + 1
        if rounds >= min_trials and rounds < trials:
            dev = compare(load(runs / "dev-baseline"), load(runs / "dev-candidate"), metric, seed)
            if excludes_zero(tuple(dev["ci"])) or guardrail_breaks(dev, rule, "dev"):
                stopped_early = True
                break
    dev = compare(load(runs / "dev-baseline"), load(runs / "dev-candidate"), metric, seed)
    result = decide(dev, rule)
    heldout = None
    if result["decision"] == "run_heldout":
        ho_suite = rel(meta["heldout_suite"]) if meta.get("heldout_suite") else suite_path
        for arm, harness in arms.items():
            suite_mod.run(ho_suite, harness, runs / f"heldout-{arm}", split="heldout", sandbox=sandbox,
                          trials=int(meta.get("heldout_trials", 1)))
        heldout = compare(load(runs / "heldout-baseline"), load(runs / "heldout-candidate"), metric, seed)
        result = decide(dev, rule, heldout)
    summary = {"decision": result["decision"], "reasons": result["reasons"], "dev": _summ(dev),
               "heldout": _summ(heldout), "runs": str(runs), "rounds": rounds, "stopped_early": stopped_early}
    (runs / "experiment.json").write_text(json.dumps({**summary, "rule": rule}, indent=2) + "\n")
    (runs / "report.md").write_text(render_experiment(path.stem, summary, dev, heldout))
    _record(path, summary)
    return summary


def _record(path: Path, s: dict) -> None:
    text = path.read_text()
    lo, hi = s["dev"]["ci"]
    line = (f"- Result: {s['decision']} (dev {s['dev']['metric']} {s['dev']['delta']:+.3f}, CI [{lo:+.3f}, {hi:+.3f}], "
            f"{s['rounds']} round(s){', stopped early' if s['stopped_early'] else ''}"
            + (f"; held-out {s['heldout']['delta']:+.3f}" if s["heldout"] else "")
            + f"; see {Path(s['runs']).relative_to(path.parent)}/report.md)")
    text = re.sub(r"(?m)^- Result:.*$", line, text, count=1)
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    log = f"\n- {stamp}: {s['decision']}. " + " ".join(s["reasons"])
    text = text.replace("(Written by `harness-evo experiment run`.)", "(Written by `harness-evo experiment run`.)" + log, 1)
    path.write_text(text)


def cmd(args, out):
    if args.action == "new":
        p = new(args.target, Path(args.dir), args.cluster)
        out.write(json.dumps({"created": str(p)}) + "\n" if args.json else f"created {p}\n")
        return 0
    path = Path(args.target)
    if not path.exists():
        raise CliError(f"{path} does not exist")
    if args.action == "lock":
        h = lock(path)
        out.write(json.dumps({"locked": str(path), "sha256": h}) + "\n" if args.json else f"locked {path}\n")
        return 0
    s = run(path, args.seed)
    out.write(json.dumps(s, indent=2) + "\n" if args.json else f"{s['decision']}: {' '.join(s['reasons'])}\n")
    return 0

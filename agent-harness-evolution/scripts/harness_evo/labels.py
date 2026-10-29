"""Finding labels (`confirmed`, `false_positive`, `acceptable`): the ground truth for precision."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .cli import CliError
from .paths import Layout

_BOX = re.compile(r"- \[([ xX])\] (confirmed|false positive) <!-- finding:(\S+?) -->")
MARKER = "<!-- harness-evo-report -->"


def parse_id(fid: str) -> dict:
    """'<session>#<seq>:<rule>' for trajectory findings, 'pr:<rule>:<digest>' for outcome and model findings."""
    if fid.startswith("pr:"):
        return {"rule": ":".join(fid.split(":")[1:-1]), "session": None, "seq": None}
    scope, _, rule = fid.rpartition(":")
    session, _, seq = scope.partition("#")
    return {"rule": rule, "session": session or None, "seq": int(seq) if seq.isdigit() else None}


def read(path: Path) -> list[dict]:
    """Every well-formed label; a torn or corrupt line is skipped, never the whole file."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def latest(labels: list[dict]) -> dict:
    """The newest label per finding."""
    out = {}
    for rec in labels:
        key = rec.get("finding") or rec.get("trial")
        if key:
            out[key] = rec
    return out


def append(path: Path, records: list[dict]) -> int:
    existing = latest(read(path))
    new = [r for r in records if existing.get(r["finding"], {}).get("label") != r["label"]]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for r in new:
                f.write(json.dumps(r) + "\n")
    return len(new)


def from_comment(body: str, pr: int | None) -> list[dict]:
    ticks: dict[str, set] = {}
    for mark, label, fid in _BOX.findall(body):
        if mark.strip():
            ticks.setdefault(fid, set()).add("confirmed" if label == "confirmed" else "false_positive")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = []
    for fid, labels in ticks.items():
        if len(labels) != 1:
            continue  # both boxes ticked: ambiguous, skip
        out.append({"finding": fid, **parse_id(fid), "label": labels.pop(), "pr": pr, "source": "pr-comment", "ts": now})
    return out


def fetch_comment(pr: int, slug: str | None, author: str = "github-actions[bot]") -> str:
    """The newest report comment written by `author`, so a look-alike comment cannot inject labels."""
    if not re.fullmatch(r"[A-Za-z0-9\-\[\]]+", author):
        raise CliError(f"invalid report author {author!r}")
    repo = f"repos/{slug}" if slug else "repos/{owner}/{repo}"
    jq = f'.[] | select(.user.login == "{author}") | .body'
    r = subprocess.run(["gh", "api", "--paginate", f"{repo}/issues/{pr}/comments", "--jq", jq],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise CliError(f"gh api failed: {r.stderr.strip()}")
    idx = r.stdout.rfind(MARKER)  # the newest report comment
    if idx == -1:
        raise CliError(f"no harness-evo report comment on PR #{pr}")
    return r.stdout[idx:]


def cmd_sync(args, out):
    if args.comment_file:
        body = Path(args.comment_file).read_text()
    elif args.pr:
        body = fetch_comment(args.pr, args.repo_slug, args.report_author)
    else:
        raise CliError("give --comment-file or --pr")
    records = from_comment(body, args.pr)
    n = append(Layout(Path(args.project_dir).resolve()).labels, records)
    out.write(f"{n} new label(s) from {len(records)} ticked finding(s)\n")
    return 0


def cmd_label(args, out):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec = {"finding": args.finding, **parse_id(args.finding), "label": args.label, "pr": None,
           "source": "manual", "ts": now}
    n = append(Layout(Path(args.project_dir).resolve()).labels, [rec])
    out.write(f"{n} label written\n")
    return 0

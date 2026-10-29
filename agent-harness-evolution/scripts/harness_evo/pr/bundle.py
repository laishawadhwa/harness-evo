"""Redacted trace bundles for a PR branch, and their transport to CI through git refs.

Traces stay local by default. `harness-evo bundle --push` is the explicit step
that sends the redacted traces of the sessions linked to a branch's commits to
refs/harness-evo/sessions/<id>. CI fetches those refs (`fetch-bundles`) and
archives what it used as a 30-day Actions artifact.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import time
from pathlib import Path

from .. import __version__, config, gitutil
from ..cli import CliError
from ..paths import Layout
from ..trace import store
from ..trace.redact import redact

REF_PREFIX = "refs/harness-evo/sessions/"


def linked_sessions(repo: Path, base: str, head: str) -> list[str]:
    return list(dict.fromkeys(s for c in gitutil.commits(repo, base, head) for s in c.sessions))


def _redacted_jsonl(events: list[dict], pii: tuple) -> str:
    # Redacted again as defence in depth: events were already redacted when written.
    return "".join(json.dumps(redact(e, pii), separators=(",", ":")) + "\n" for e in events)


def build(repo: Path, base: str, head: str, out: Path) -> dict:
    repo = Path(repo).resolve()
    layout = Layout(repo)
    pii = tuple(config.load(repo).profile.pii_patterns)
    sessions = linked_sessions(repo, base, head)
    manifest = {"harness_evo": __version__, "base": base, "head": gitutil.rev(repo, head),
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sessions": [], "missing": []}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        def add(name: str, text: str):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))

        for s in sessions:
            events = store.read(layout.trace(s))
            if not events:
                manifest["missing"].append(s)
                continue
            add(f"traces/{s}.jsonl", _redacted_jsonl(events, pii))
            summary = layout.summary(s)
            if summary.exists():
                add(f"sessions/{s}.summary.json", summary.read_text())
            manifest["sessions"].append(s)
        add("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def push(repo: Path, base: str, head: str, remote: str) -> list[str]:
    """One ref per session, each a commit whose tree holds the redacted trace."""
    repo = Path(repo).resolve()
    layout = Layout(repo)
    pii = tuple(config.load(repo).profile.pii_patterns)
    env = {**os.environ, "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "harness-evo"),
           "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", "harness-evo@localhost"),
           "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "harness-evo"),
           "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", "harness-evo@localhost")}
    pushed = []
    for s in linked_sessions(repo, base, head):
        events = store.read(layout.trace(s))
        if not events:
            continue
        blob = gitutil.git(repo, "hash-object", "-w", "--stdin", input=_redacted_jsonl(events, pii)).strip()
        entries = f"100644 blob {blob}\ttrace.jsonl\n"
        if layout.summary(s).exists():
            sblob = gitutil.git(repo, "hash-object", "-w", "--stdin", input=layout.summary(s).read_text()).strip()
            entries += f"100644 blob {sblob}\tsummary.json\n"
        tree = gitutil.git(repo, "mktree", input=entries).strip()
        ref = REF_PREFIX + s
        parent = gitutil.git(repo, "rev-parse", "--verify", "-q", ref, check=False).strip()
        import subprocess
        args = ["git", "commit-tree", tree, "-m", f"harness-evo trace bundle for session {s}"]
        if parent:
            args[3:3] = ["-p", parent]
        commit = subprocess.run(args, cwd=repo, capture_output=True, text=True, env=env, check=True).stdout.strip()
        gitutil.git(repo, "update-ref", ref, commit)
        gitutil.git(repo, "push", remote, f"{ref}:{ref}")
        pushed.append(s)
    return pushed


def fetch(repo: Path, base: str, head: str, remote: str, out_dir: Path) -> dict:
    repo = Path(repo).resolve()
    gitutil.git(repo, "fetch", "--quiet", remote, f"+{REF_PREFIX}*:{REF_PREFIX}*", check=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    got, missing = [], []
    for s in linked_sessions(repo, base, head):
        text = gitutil.git(repo, "show", f"{REF_PREFIX}{s}:trace.jsonl", check=False)
        if text:
            (out_dir / f"{s}.jsonl").write_text(text)
            got.append(s)
        else:
            missing.append(s)
    return {"fetched": got, "missing": missing}


def cmd_bundle(args, out):
    try:
        if args.push:
            pushed = push(Path(args.repo), args.base, args.head, args.remote)
            out.write(json.dumps({"pushed": pushed, "remote": args.remote}, indent=2) + "\n")
            return 0
        target = Path(args.out or Layout(Path(args.repo)).base / "bundles" / "bundle.tgz")
        manifest = build(Path(args.repo), args.base, args.head, target)
    except gitutil.GitError as exc:
        raise CliError(str(exc)) from exc
    out.write(json.dumps({"bundle": str(target), **manifest}, indent=2) + "\n")
    return 0


def cmd_fetch(args, out):
    try:
        res = fetch(Path(args.repo), args.base, args.head, args.remote, Path(args.out_dir))
    except gitutil.GitError as exc:
        raise CliError(str(exc)) from exc
    out.write(json.dumps(res, indent=2) + "\n")
    return 0

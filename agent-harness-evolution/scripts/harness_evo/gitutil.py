"""Thin git helpers: commits and trailers in a range, and unified-diff parsing."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path | str, *args: str, input: str | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def rev(repo, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


@dataclass
class Commit:
    sha: str
    subject: str
    sessions: list


def commits(repo, base: str, head: str) -> list[Commit]:
    fmt = "%H%x1f%s%x1f%(trailers:key=Harness-Session,valueonly,separator=%x2C)%x1e"
    out = git(repo, "log", "--no-merges", f"--format={fmt}", f"{base}..{head}")
    result = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, subject, trailers = (rec.split("\x1f") + ["", ""])[:3]
        sessions = [s.strip() for s in trailers.replace("\n", ",").split(",") if s.strip()]
        result.append(Commit(sha, subject, sessions))
    return result


@dataclass
class FileDiff:
    path: str
    status: str = "M"
    old_path: str | None = None
    added: list = field(default_factory=list)      # [(line_no, text)]
    removed: list = field(default_factory=list)    # [(line_no, text)]
    binary: bool = False


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_path(raw: str) -> str:
    """A path from a `+++ b/...` line: git appends a tab to names with spaces and C-quotes odd names."""
    p = raw.rstrip("\t")
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return p[2:] if p.startswith("b/") else p


def diff(repo, base: str, head: str) -> list[FileDiff]:
    names = git(repo, "-c", "core.quotePath=false", "diff", "--name-status", "-z", "-M", f"{base}...{head}")
    files: dict[str, FileDiff] = {}
    parts = names.split("\0")
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i][0]
        if status in "RC":
            old, path = parts[i + 1], parts[i + 2]
            i += 3
        else:
            old, path = None, parts[i + 1]
            i += 2
        files[path] = FileDiff(path=path, status=status, old_path=old)
    text = git(repo, "-c", "core.quotePath=false", "diff", "-M", "--no-color", "--unified=0", f"{base}...{head}")
    current, old_no, new_no = None, 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            current = files.get(_diff_path(line[4:]))
        elif line.startswith("Binary files") and current is not None:
            current.binary = True
        elif (m := _HUNK.match(line)):
            old_no, new_no = int(m.group(1)), int(m.group(2))
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            current.added.append((new_no, line[1:]))
            new_no += 1
        elif current is not None and line.startswith("-") and not line.startswith("---"):
            current.removed.append((old_no, line[1:]))
            old_no += 1
    return list(files.values())

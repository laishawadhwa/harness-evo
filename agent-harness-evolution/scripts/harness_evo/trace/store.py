"""Append-only JSONL trace storage, per-session locking and retention."""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from ..paths import Layout

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: run without locking
    fcntl = None


def append(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n" for e in events)
    with open(path, "a", encoding="utf-8") as f:
        f.write(data)


def read(path: Path) -> list[dict]:
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return events


@contextlib.contextmanager
def session_lock(layout: Layout, session: str, timeout_s: float = 2.0):
    """Serialise hook calls for one session. Yields whether the lock was taken: after `timeout_s`
    it gives up rather than block the agent, and the caller must then not write."""
    if fcntl is None:
        yield True
        return
    path = layout.lock(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.005)
        yield locked
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def prune(layout: Layout, retention_days: float, keep: str | None = None, now: float | None = None) -> list[Path]:
    """Delete traces, state and summaries older than the retention limit."""
    cutoff = (now or time.time()) - retention_days * 86400
    removed = []
    for folder in (layout.traces, layout.state_dir, layout.sessions):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if keep and entry.name.startswith(keep + "."):
                continue
            if entry.name == "active-session":
                continue
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed.append(Path(entry.path))
            except OSError:
                continue
    return removed

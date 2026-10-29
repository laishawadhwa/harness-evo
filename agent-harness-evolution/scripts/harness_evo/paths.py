"""Filesystem layout: the skill package and the per-repo `.harness-evo/` directory."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

DIR = ".harness-evo"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def skill_root() -> Path:
    """The agent-harness-evolution/ directory (config/, evals/, analysis/, ...)."""
    pkg = Path(__file__).resolve().parent
    source_tree = pkg.parent.parent
    if (source_tree / "config").is_dir():
        return source_tree
    return pkg / "_skill"  # wheel installs bundle the skill files here


def skill_file(*parts: str) -> Path:
    return skill_root().joinpath(*parts)


def project_root(hook_input: dict | None = None) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    if hook_input and hook_input.get("cwd"):
        return Path(hook_input["cwd"])
    return Path.cwd()


def safe_session(session_id: str | None) -> str:
    sid = str(session_id or "unknown")
    clean = _SAFE.sub("", sid)
    if clean and clean == sid and len(sid) <= 128:
        return sid
    return "s-" + hashlib.sha256(sid.encode()).hexdigest()[:16]


class Layout:
    """Where harness-evo reads and writes for one repo.

    `config_dir` is always <repo>/.harness-evo (profile.yaml, nudge-policy.yaml). `base` is where data is
    written: the same directory with `trace_location: repo`, or a per-user directory with
    `trace_location: user`, so nothing is written into the repo.
    """

    def __init__(self, root: Path | str, mode: str | None = None):
        self.root = Path(root)
        self.config_dir = self.root / DIR
        if mode is None:
            from .settings import load
            mode = load().effective["trace_location"]
        self.mode = mode
        if mode == "user":
            from .settings import repo_data_dir
            self.base = repo_data_dir(self.root)
        else:
            self.base = self.config_dir

    def _config_file(self, name: str) -> Path:
        repo_file = self.config_dir / name
        if self.mode == "repo" or repo_file.exists():
            return repo_file
        return self.base / name

    @property
    def traces(self) -> Path:
        return self.base / "traces"

    @property
    def state_dir(self) -> Path:
        return self.base / "state"

    @property
    def logs(self) -> Path:
        return self.base / "logs"

    @property
    def sessions(self) -> Path:
        return self.base / "sessions"

    @property
    def cache(self) -> Path:
        return self.base / "cache"

    @property
    def queue(self) -> Path:
        return self.base / "queue"

    @property
    def profile(self) -> Path:
        return self._config_file("profile.yaml")

    @property
    def policy(self) -> Path:
        return self._config_file("nudge-policy.yaml")

    @property
    def policy_state(self) -> Path:
        return self.base / "policy-state.json"

    @property
    def mutes(self) -> Path:
        return self.base / "mutes.json"

    @property
    def labels(self) -> Path:
        return self.base / "labels.jsonl"

    @property
    def active_session(self) -> Path:
        return self.state_dir / "active-session"

    def trace(self, session: str) -> Path:
        return self.traces / f"{session}.jsonl"

    def state(self, session: str) -> Path:
        return self.state_dir / f"{session}.json"

    def snapshot(self, session: str) -> Path:
        return self.state_dir / f"{session}.snapshot.json"

    def lock(self, session: str) -> Path:
        return self.state_dir / f"{session}.lock"

    def summary(self, session: str) -> Path:
        return self.sessions / f"{session}.summary.json"

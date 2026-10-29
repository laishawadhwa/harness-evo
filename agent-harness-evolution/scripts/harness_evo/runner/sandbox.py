"""Disposable workspaces for eval runs.

LocalSandbox: a fresh clone in a temp directory. Fast, but no isolation; use it for
scripted agents and for development.
DockerSandbox: the same clone, with every command run in a container with no real
credentials, CPU / memory / time limits, and `network: none` by default. Setup
commands may use a separate network (`setup_network`), meant to be an egress
proxy that allows only package registries (spec section 12). This build does not
ship that proxy.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..gitutil import git

OUTPUT_TAIL = 4000
_HARNESS_FILES = (".harness-evo/", ".claude/settings.local.json")   # written by the runner, not the agent
_CACHE_DIRS = {".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"}


class LocalSandbox:
    kind = "local"

    def __init__(self, url: str, commit: str, timeout: int = 1800):
        self.url, self.commit, self.timeout = url, commit, timeout
        self.tmp: Path | None = None
        self.path: Path | None = None

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-evo-sb-"))
        self.path = self.tmp / "repo"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", self.url, str(self.path)], check=True,
                       capture_output=True)
        git(self.path, "checkout", "--quiet", "--detach", self.commit)
        return self

    def __exit__(self, *exc):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self) -> dict:
        # The interpreter running harness-evo goes first on PATH, so `pytest` resolves inside the sandbox.
        path = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
        keep = {k: v for k, v in os.environ.items() if k in {"HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE"}}
        from ..settings import hermetic_env
        return {**keep, "PATH": path, "PYTHONDONTWRITEBYTECODE": "1", "CLAUDE_PROJECT_DIR": str(self.path),
                **hermetic_env(self.tmp / "harness-evo-config")}

    def run(self, command: str, timeout: int | None = None) -> tuple[int, str]:
        try:
            r = subprocess.run(command, shell=True, cwd=self.path, capture_output=True, text=True,
                               timeout=timeout or self.timeout, env=self.env())
            return r.returncode, (r.stdout + ("\n" + r.stderr if r.stderr else ""))[-OUTPUT_TAIL:]
        except subprocess.TimeoutExpired:
            return 124, f"Command timed out after {timeout or self.timeout}s"

    def apply_patch(self, patch: Path) -> None:
        git(self.path, "apply", "--whitespace=nowarn", str(patch))

    def patch_files(self, patch: Path) -> list[str]:
        out = git(self.path, "apply", "--numstat", str(patch))
        return [line.split("\t")[-1] for line in out.splitlines() if line.strip()]

    def changed_files(self) -> list[str]:
        out = git(self.path, "status", "--porcelain", "--untracked-files=all")
        files = []
        for line in out.splitlines():
            p = line[3:]
            if " -> " in p:
                p = p.split(" -> ", 1)[1]
            if p.startswith(_HARNESS_FILES) or any(part in _CACHE_DIRS for part in p.split("/")) or p.endswith(".pyc"):
                continue
            files.append(p)
        return files

    def diff(self) -> str:
        files = self.changed_files()
        if not files:
            return ""
        git(self.path, "add", "-A", "--", *files)
        return git(self.path, "diff", "--cached", self.commit, "--", *files)


class DockerSandbox(LocalSandbox):
    kind = "docker"

    def __init__(self, url: str, commit: str, timeout: int = 1800, image: str = "python:3.12-slim",
                 network: str = "none", setup_network: str = "bridge", cpus: str = "2", memory: str = "4g",
                 env: dict | None = None):
        super().__init__(url, commit, timeout)
        self.image, self.network, self.setup_network = image, network, setup_network
        self.cpus, self.memory, self.extra_env = cpus, memory, env or {}
        self.in_setup = False

    def run(self, command: str, timeout: int | None = None) -> tuple[int, str]:
        net = self.setup_network if self.in_setup else self.network
        args = ["docker", "run", "--rm", "--network", net, "--cpus", self.cpus, "--memory", self.memory,
                "--pids-limit", "512", "-v", f"{self.path}:/work", "-w", "/work"]
        for k, v in self.extra_env.items():
            args += ["-e", f"{k}={v}"]
        args += [self.image, "sh", "-c", command]
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout or self.timeout)
            return r.returncode, (r.stdout + ("\n" + r.stderr if r.stderr else ""))[-OUTPUT_TAIL:]
        except subprocess.TimeoutExpired:
            return 124, f"Command timed out after {timeout or self.timeout}s"


def make_sandbox(kind: str, url: str, commit: str, **opts):
    if kind == "docker":
        if not shutil.which("docker"):
            raise RuntimeError("docker is not on PATH")
        return DockerSandbox(url, commit, **opts)
    return LocalSandbox(url, commit, **{k: v for k, v in opts.items() if k == "timeout"})

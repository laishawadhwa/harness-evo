"""Fixtures for the per-module unit tests.

Process spawns are slow on some machines (~60 ms per `git`), so repos are built once per
session and copied into each test's tmp_path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unit_helpers import ClaudeCode, copy_repo, git, make_repo  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Never depend on the caller's shell: no project dir, not inside Claude Code, no PR task."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("HARNESS_EVO_TASK", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory) -> Path:
    return make_repo(tmp_path_factory.mktemp("template") / "repo")


@pytest.fixture(scope="session")
def template_sha(repo_template) -> str:
    return git(repo_template, "rev-parse", "HEAD").strip()


@pytest.fixture
def project(tmp_path, monkeypatch, repo_template) -> Path:
    root = copy_repo(repo_template, tmp_path / "repo")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root


@pytest.fixture
def cc(project) -> ClaudeCode:
    agent = ClaudeCode(project)
    agent.start()
    return agent

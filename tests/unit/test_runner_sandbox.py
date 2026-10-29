"""runner/sandbox.py: the local sandbox, the docker sandbox's command line (no container is run), make_sandbox."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_evo.runner import sandbox as sandbox_mod
from harness_evo.runner.sandbox import DockerSandbox, LocalSandbox, make_sandbox
from unit_helpers import NEW_AND_DELETE, git


def test_local_sandbox_lifecycle(repo_template, template_sha, tmp_path):
    with LocalSandbox(str(repo_template), template_sha, timeout=30) as sb:
        tmp = sb.tmp
        assert sb.path == tmp / "repo" and (sb.path / "src" / "billing" / "invoice.py").is_file()
        assert git(sb.path, "rev-parse", "HEAD").strip() == template_sha

        code, out = sb.run("echo hi; echo oops >&2; exit 3")
        assert code == 3 and out == "hi\n\noops\n"
        code, out = sb.run("echo $CLAUDE_PROJECT_DIR; echo ${GIT_AUTHOR_NAME:-unset}")
        assert out.split() == [str(sb.path), "unset"], "only a whitelisted environment reaches the sandbox"
        code, out = sb.run("sleep 5", timeout=0.3)
        assert code == 124 and out == "Command timed out after 0.3s"

        assert sb.changed_files() == [] and sb.diff() == ""
        # harness files and caches are not the agent's changes
        for rel in (".harness-evo/traces/s.jsonl", ".claude/settings.local.json", "src/__pycache__/x.cpython-312.pyc",
                    ".pytest_cache/v/cache", "lib/mod.pyc"):
            (sb.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (sb.path / rel).write_text("x")
        (sb.path / "src" / "util" / "strings.py").write_text("def shout(s):\n    return s.upper() + '!'\n")
        (sb.path / "src" / "util" / "new.py").write_text("N = 1\n")
        assert sorted(sb.changed_files()) == ["src/util/new.py", "src/util/strings.py"]
        diff = sb.diff()
        assert "+    return s.upper() + '!'" in diff and "new file mode" in diff and ".harness-evo" not in diff
    assert not tmp.exists(), "the sandbox directory is removed on exit"


def test_local_sandbox_patches_and_renames(repo_template, template_sha, tmp_path):
    patch = tmp_path / "p.diff"
    patch.write_text(NEW_AND_DELETE)
    with make_sandbox("local", str(repo_template), template_sha, timeout=30, image="ignored") as sb:
        assert isinstance(sb, LocalSandbox) and not isinstance(sb, DockerSandbox) and sb.timeout == 30
        assert sb.patch_files(patch) == ["docs/new.md", "docs/notes.md"]
        sb.apply_patch(patch)
        assert (sb.path / "docs" / "new.md").read_text() == "hello\n" and not (sb.path / "docs" / "notes.md").exists()
        git(sb.path, "reset", "-q", "--hard")
        (sb.path / "docs" / "new.md").unlink()
        git(sb.path, "mv", "docs/notes.md", "docs/renamed.md")
        assert sb.changed_files() == ["docs/renamed.md"], "a rename reports its new path"


def test_exit_without_enter_is_safe():
    LocalSandbox("unused", "HEAD").__exit__(None, None, None)


def test_make_sandbox_docker_requires_docker(monkeypatch):
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="docker is not on PATH"):
        make_sandbox("docker", "url", "sha")
    monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: "/usr/local/bin/docker")
    sb = make_sandbox("docker", "url", "sha", image="node:22", network="none", cpus="1", env={"CI": "1"})
    assert isinstance(sb, DockerSandbox) and sb.kind == "docker"
    assert sb.image == "node:22" and sb.cpus == "1" and sb.extra_env == {"CI": "1"}


def test_docker_sandbox_command_line(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    sb = DockerSandbox("url", "sha", timeout=99, env={"HTTP_PROXY": "http://proxy.invalid:3128"})
    sb.path = tmp_path / "repo"
    assert sb.run("pytest -q") == (0, "ok\n")
    args, kw = calls[-1]
    assert args[:3] == ["docker", "run", "--rm"]
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--cpus") + 1] == "2" and args[args.index("--memory") + 1] == "4g"
    assert args[args.index("-v") + 1] == f"{tmp_path / 'repo'}:/work" and args[args.index("-w") + 1] == "/work"
    assert args[args.index("-e") + 1] == "HTTP_PROXY=http://proxy.invalid:3128"
    assert args[-4:] == ["python:3.12-slim", "sh", "-c", "pytest -q"]
    assert kw["timeout"] == 99
    sb.in_setup = True
    sb.run("pip install -e .", timeout=5)
    args, kw = calls[-1]
    assert args[args.index("--network") + 1] == "bridge" and kw["timeout"] == 5


def test_docker_sandbox_timeout(monkeypatch, tmp_path):
    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake_run)
    sb = DockerSandbox("url", "sha", timeout=7)
    sb.path = Path(tmp_path)
    assert sb.run("sleep 100") == (124, "Command timed out after 7s")


def test_docker_output_keeps_the_tail(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_mod.subprocess, "run",
                        lambda args, **kw: SimpleNamespace(returncode=1, stdout="a" * 5000, stderr="boom"))
    sb = DockerSandbox("url", "sha")
    sb.path = tmp_path
    code, out = sb.run("x")
    assert code == 1 and len(out) == sandbox_mod.OUTPUT_TAIL and out.endswith("\nboom")

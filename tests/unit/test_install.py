"""install.py: settings merge, uninstall, mute/unmute, the prepare-commit-msg hook, install outside git."""
from __future__ import annotations

import json
import shlex
import sys

import pytest

from harness_evo import install
from harness_evo.paths import skill_file
from unit_helpers import git, run_cli

EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop",
          "SubagentStop", "PreCompact", "SessionEnd"]
USER_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter --fast"}]}


def test_merge_adds_all_nine_events_once(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    assert install.merge_settings(path, "harness-evo hook") == EVENTS
    data = json.loads(path.read_text())
    assert sorted(data["hooks"]) == sorted(EVENTS)
    for event in EVENTS:
        [entry] = data["hooks"][event]
        [h] = entry["hooks"]
        assert h["command"] == f"harness-evo hook {event}" and h["type"] == "command"
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "*"


def test_merge_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    install.merge_settings(path, "harness-evo hook")
    before = path.read_text()
    assert install.merge_settings(path, "harness-evo hook") == []
    assert path.read_text() == before
    assert not path.with_suffix(".json.bak").exists(), "nothing changed, so no backup is written"


def test_merge_is_idempotent_with_a_custom_command(tmp_path):
    path = tmp_path / "settings.json"
    cmd = f"{sys.executable} -m harness_evo hook"
    install.merge_settings(path, cmd)
    assert install.merge_settings(path, cmd) == []


def test_merge_keeps_existing_user_hooks_and_settings(tmp_path):
    path = tmp_path / "settings.json"
    original = {"model": "claude-opus-5-5", "hooks": {"PreToolUse": [USER_HOOK]}}
    path.write_text(json.dumps(original))
    assert install.merge_settings(path, "harness-evo hook") == EVENTS
    data = json.loads(path.read_text())
    assert data["model"] == "claude-opus-5-5"
    assert data["hooks"]["PreToolUse"][0] == USER_HOOK
    assert len(data["hooks"]["PreToolUse"]) == 2
    assert json.loads(path.with_suffix(".json.bak").read_text()) == original


def test_merge_skips_events_already_registered_through_hook_entry(tmp_path):
    path = tmp_path / "settings.json"
    ours = {"hooks": [{"type": "command", "command": "/opt/skill/hooks/hook_entry.sh Stop"}]}
    path.write_text(json.dumps({"hooks": {"Stop": [ours]}}))
    added = install.merge_settings(path, "harness-evo hook")
    assert "Stop" not in added and len(added) == 8
    assert json.loads(path.read_text())["hooks"]["Stop"] == [ours]


def test_uninstall_removes_only_ours(tmp_path):
    root = tmp_path / "proj"
    path = install.settings_path(root, "local")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PreToolUse": [USER_HOOK]}}))
    install.merge_settings(path, "harness-evo hook")
    rc, out = run_cli("uninstall", "--project-dir", str(root))
    assert rc == 0
    report = json.loads(out)
    assert sorted(report["events_removed"]) == sorted(EVENTS)
    data = json.loads(path.read_text())
    assert data["hooks"] == {"PreToolUse": [USER_HOOK]}
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    # a second uninstall removes nothing
    rc, out = run_cli("uninstall", "--project-dir", str(root))
    assert rc == 0 and json.loads(out)["events_removed"] == []


def test_uninstall_without_settings_file_is_an_error(tmp_path):
    rc, out = run_cli("uninstall", "--project-dir", str(tmp_path), "--scope", "project")
    assert rc == 1 and out.startswith("error: ") and "settings.json does not exist" in out


def test_settings_path_per_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert install.settings_path(tmp_path, "local") == tmp_path / ".claude" / "settings.local.json"
    assert install.settings_path(tmp_path, "project") == tmp_path / ".claude" / "settings.json"
    assert install.settings_path(tmp_path, "user") == tmp_path / "home" / ".claude" / "settings.json"


def test_install_user_scope_writes_home_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    root.mkdir()
    rc, out = run_cli("install", "--project-dir", str(root), "--scope", "user", "--no-git-hook",
                      "--command", "harness-evo hook")
    assert rc == 0
    report = json.loads(out)
    assert report["settings"]["file"] == str(tmp_path / "home" / ".claude" / "settings.json")
    assert report["settings"]["events_added"] == EVENTS
    assert "git_hook" not in report


def test_default_command(monkeypatch):
    # 1. the bundled launcher, when the source tree or plugin is present
    cmd = install.default_command()
    assert cmd.startswith("sh ") and cmd.endswith("bin/harness-evo hook")
    # 2. else a pip-installed harness-evo on PATH, 3. else this interpreter's module
    monkeypatch.setattr(install, "launcher", lambda: None)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/local/bin/harness-evo")
    assert install.default_command() == "harness-evo hook"
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    assert install.default_command() == f"{shlex.quote(sys.executable)} -m harness_evo hook"


def test_install_outside_a_git_repo(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    report = install.install(root, settings=False)
    assert report["git_hook"] == "skipped: not a git repository"
    base = root / ".harness-evo"
    assert (base / ".gitignore").read_text() == install.GITIGNORE
    assert (base / "profile.yaml").read_text() == skill_file("config", "repo-profile.template.yaml").read_text()
    assert "created" in report["profile"]
    # a second install keeps the existing profile and gitignore
    (base / "profile.yaml").write_text("version: 1\n")
    again = install.install(root, settings=False, git_hook=False)
    assert "profile" not in again and (base / "profile.yaml").read_text() == "version: 1\n"


def test_install_git_hook_in_a_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    msg = install.install_git_hook(root)
    hook = root / ".git" / "hooks" / "prepare-commit-msg"
    assert msg == f"installed {hook}"
    assert hook.read_text() == skill_file("hooks", "prepare-commit-msg").read_text()
    assert hook.stat().st_mode & 0o111
    # ours is recognised and refreshed in place
    assert install.install_git_hook(root).startswith("installed")
    assert not (hook.parent / "prepare-commit-msg.harness-evo").exists()


def test_install_git_hook_leaves_a_foreign_hook_alone(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    hooks = root / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    foreign = "#!/bin/sh\necho 'team hook'\n"
    (hooks / "prepare-commit-msg").write_text(foreign)
    msg = install.install_git_hook(root)
    alt = hooks / "prepare-commit-msg.harness-evo"
    assert "left alone" in msg and str(alt) in msg
    assert (hooks / "prepare-commit-msg").read_text() == foreign
    assert alt.read_text() == skill_file("hooks", "prepare-commit-msg").read_text()
    assert alt.stat().st_mode & 0o111


# ---------------------------------------------------------------- mute / unmute

def _mutes(root):
    return json.loads((root / ".harness-evo" / "mutes.json").read_text())


def test_mute_and_unmute_for_the_repo(tmp_path):
    rc, out = run_cli("mute", "d3", "--project-dir", str(tmp_path))
    assert rc == 0 and out == "D3 muted for this repo\n"
    run_cli("mute", "D3", "--project-dir", str(tmp_path))
    assert _mutes(tmp_path)["repo"] == ["D3"], "muting twice does not duplicate"
    rc, out = run_cli("unmute", "D3", "--project-dir", str(tmp_path))
    assert rc == 0 and out == "D3 unmuted for this repo\n"
    assert _mutes(tmp_path)["repo"] == []
    rc, _ = run_cli("unmute", "D3", "--project-dir", str(tmp_path))
    assert rc == 0, "unmuting a detector that is not muted is a no-op"


def test_mute_for_one_session(tmp_path):
    rc, out = run_cli("mute", "D9", "--project-dir", str(tmp_path), "--session", "s-1")
    assert rc == 0 and out == "D9 muted for session s-1\n"
    m = _mutes(tmp_path)
    assert m["sessions"] == {"s-1": ["D9"]} and m["repo"] == []
    rc, out = run_cli("unmute", "D9", "--project-dir", str(tmp_path), "--session", "s-1")
    assert out == "D9 unmuted for session s-1\n" and _mutes(tmp_path)["sessions"] == {"s-1": []}


def test_mute_recovers_from_a_corrupt_mutes_file(tmp_path):
    (tmp_path / ".harness-evo").mkdir()
    (tmp_path / ".harness-evo" / "mutes.json").write_text("{nope")
    rc, _ = run_cli("mute", "D1", "--project-dir", str(tmp_path))
    assert rc == 0 and _mutes(tmp_path)["repo"] == ["D1"]


def test_mute_unknown_detector_fails(tmp_path):
    rc, out = run_cli("mute", "D99", "--project-dir", str(tmp_path))
    assert rc == 1 and out.startswith("error: unknown detector D99; expected one of D1, D2")
    assert not (tmp_path / ".harness-evo" / "mutes.json").exists()

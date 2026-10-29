"""The settings-file (non-plugin) install: a symlinked launcher, user-scope hooks only, and `paths`."""
from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

from harness_evo import cli
from harness_evo.paths import skill_file

SHIM = skill_file("bin", "harness-evo")


def test_launcher_works_through_a_symlink(tmp_path):
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    os.symlink(SHIM, link_dir / "harness-evo")
    r = subprocess.run([str(link_dir / "harness-evo"), "paths", "--json"], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": ""})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["skill_root"] == str(SHIM.parent.parent)


def test_settings_only_registers_hooks_and_creates_nothing_in_the_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    out = io.StringIO()
    rc = cli.main(["install", "--project-dir", str(project), "--scope", "local", "--settings-only"], stdout=out)
    assert rc == 0
    settings = json.loads((project / ".claude" / "settings.local.json").read_text())
    assert len(settings["hooks"]) == 9
    assert sorted(p.name for p in project.iterdir()) == [".claude"], "no .harness-evo/ and no git hook"


def test_paths_reports_bundled_files_and_data_dirs(tmp_path):
    out = io.StringIO()
    assert cli.main(["paths", "--repo", str(tmp_path), "--json"], stdout=out) == 0
    report = json.loads(out.getvalue())
    assert Path(report["taxonomy"]).is_file() and Path(report["experiment_template"]).is_file()
    assert report["trace_location"] == "repo" and report["repo_data_dir"].endswith(".harness-evo")


def test_merge_never_overwrites_an_existing_backup(tmp_path):
    from harness_evo import install
    settings = tmp_path / "settings.json"
    settings.write_text('{"theme": "dark"}')
    (tmp_path / "settings.json.bak").write_text("an older backup")
    install.merge_settings(settings, "harness-evo hook")
    assert (tmp_path / "settings.json.bak").read_text() == "an older backup"
    [stamped] = tmp_path.glob("settings.json.bak.*")
    assert json.loads(stamped.read_text()) == {"theme": "dark"}


def test_summary_reports_hook_latency_from_the_session_state(tmp_path, monkeypatch):
    root = tmp_path / "r"
    (root / ".harness-evo").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    payload = json.dumps({"session_id": "s-lat", "cwd": str(root), "hook_event_name": "SessionStart",
                          "source": "startup"})
    assert cli.main(["hook", "SessionStart"], stdin=io.StringIO(payload), stdout=io.StringIO()) == 0
    out = io.StringIO()
    assert cli.main(["summary", "s-lat", "--project-dir", str(root)], stdout=out) == 0
    assert json.loads(out.getvalue())["hook_latency"]["p95_ms"] is not None

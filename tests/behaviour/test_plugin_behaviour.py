"""Spec: docs/superpowers/specs/2026-10-29-harness-evo-plugin-design.md (Testing Requirements)."""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bhelpers import ClaudeCode, git, is_blocked, run_cli, set_policy

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "agent-harness-evolution"
SHIM = PLUGIN / "bin" / "harness-evo"
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop",
          "SubagentStop", "PreCompact", "SessionEnd"]
REAL_HOME = os.environ.get("HOME", "")
VENV_BIN = str(Path(sys.executable).parent)


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch) -> Path:
    """A fresh HOME and no inherited plugin options, so user-level settings come only from the test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in list(os.environ):
        if var.startswith(("CLAUDE_PLUGIN_OPTION_", "HARNESS_EVO_")) or var in {"XDG_CONFIG_HOME", "XDG_DATA_HOME"}:
            monkeypatch.delenv(var)
    return home


def settings_json() -> dict:
    rc, out = run_cli("settings", "--json")
    assert rc == 0
    return json.loads(out)


# ---------------------------------------------------------------- packaging

@pytest.mark.skipif(not shutil.which("claude"), reason="the claude CLI is not installed")
@pytest.mark.parametrize("target", [PLUGIN, ROOT], ids=["plugin", "marketplace"])
def test_plugin_and_marketplace_validate_strictly(target):
    r = subprocess.run([shutil.which("claude"), "plugin", "validate", "--strict", str(target)], capture_output=True,
                       text=True, timeout=180, env={**os.environ, "HOME": REAL_HOME})
    assert r.returncode == 0, r.stdout + r.stderr


def test_hooks_json_registers_all_nine_events_through_the_shim():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    assert sorted(hooks) == sorted(EVENTS)
    for event, entries in hooks.items():
        [entry] = entries
        [h] = entry["hooks"]
        assert h["type"] == "command" and h["command"] == "sh"
        assert h["args"] == ["${CLAUDE_PLUGIN_ROOT}/bin/harness-evo", "hook", event]


def test_marketplace_lists_the_plugin_root():
    m = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    [entry] = m["plugins"]
    assert entry["name"] == "harness-evo" and entry["source"] == "./agent-harness-evolution"
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "harness-evo"
    assert set(manifest["userConfig"]) == {"strictness", "prompt_mode", "retention_days", "pii_patterns",
                                           "trace_location", "nudge_ab"}
    assert (PLUGIN / "skills" / "harness-evo" / "SKILL.md").exists()


# ---------------------------------------------------------------- runtime

def test_shim_runs_without_pyyaml(tmp_path):
    assert subprocess.run([sys.executable, "-S", "-c", "import yaml"], capture_output=True).returncode != 0, \
        "precondition: `python -S` must not see PyYAML"
    repo = tmp_path / "bare"
    repo.mkdir()
    git(repo, "init", "-q")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "CLAUDE_PROJECT_DIR": str(repo),
           "HARNESS_EVO_PYTHON": f"{sys.executable} -S"}
    payload = json.dumps({"session_id": "s-shim", "cwd": str(repo), "hook_event_name": "SessionStart",
                          "source": "startup"})
    r = subprocess.run(["sh", str(SHIM), "hook", "SessionStart"], input=payload, capture_output=True, text=True,
                       env=env, cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "quality contract" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_shim_without_any_python_fails_open(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(shutil.which("cat"), bin_dir / "cat")
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    r = subprocess.run(["/bin/sh", str(SHIM), "hook", "PreToolUse"], input='{"tool_name": "Bash"}',
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout == ""
    r = subprocess.run(["/bin/sh", str(SHIM), "summary", "x"], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "HARNESS_EVO_PYTHON" in r.stdout + r.stderr


PY39 = next((p for p in (shutil.which("python3.9"), "/usr/bin/python3") if p and os.path.exists(p)
             and subprocess.run([p, "-c", "import sys; sys.exit(sys.version_info[:2] != (3, 9))"]).returncode == 0),
            None)


@pytest.mark.skipif(not PY39, reason="no Python 3.9 interpreter")
def test_every_module_imports_on_python39():
    code = ("import importlib, pkgutil, harness_evo\n"
            "for m in pkgutil.walk_packages(harness_evo.__path__, 'harness_evo.'):\n"
            "    if not m.name.endswith('__main__') and '._vendor.' not in m.name:\n"
            "        importlib.import_module(m.name)\n"
            "import yaml; print(yaml.__file__)")
    scripts = PLUGIN / "scripts"
    r = subprocess.run([PY39, "-S", "-c", code], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"],
                            "PYTHONPATH": f"{scripts}:{scripts / 'harness_evo' / '_vendor'}"})
    assert r.returncode == 0, r.stderr
    assert "_vendor" in r.stdout


# ---------------------------------------------------------------- strictness presets

def test_observe_preset_logs_but_never_blocks(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STRICTNESS", "observe")
    cc = ClaudeCode(project, session="s-observe")
    cc.start()
    assert not cc.bash("rm -rf src").denied
    assert cc.nudges("D7")[0]["payload"]["level"] == "info"
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    assert not is_blocked(cc.stop("Done."))


def test_strict_preset_raises_shadow_detectors(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STRICTNESS", "strict")
    cc = ClaudeCode(project, session="s-strict")
    cc.start()
    call = cc.edit("src/util/strings.py", "upper()", "upper() ")
    assert call.context and "src/util/strings.py" in call.context


def test_repo_policy_beats_the_preset(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STRICTNESS", "strict")
    set_policy(project, "detectors:\n  D1: {level: info}\n")
    cc = ClaudeCode(project, session="s-repo-wins")
    cc.start()
    assert cc.edit("src/util/strings.py", "upper()", "upper() ").context is None


def test_unknown_preset_falls_back_to_standard(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STRICTNESS", "paranoid")
    cc = ClaudeCode(project, session="s-unknown-preset")
    cc.start()
    assert cc.bash("rm -rf src").denied
    assert settings_json()["effective"]["strictness"] == "standard"


# ---------------------------------------------------------------- privacy combines to the stricter value

def test_user_hash_mode_beats_repo_text_mode(project, monkeypatch):
    profile = project / ".harness-evo" / "profile.yaml"
    profile.write_text(profile.read_text() + "  prompt_mode: text\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_PROMPT_MODE", "hash")
    cc = ClaudeCode(project, session="s-hash-user")
    cc.start()
    cc.prompt("Confidential: Contoso acquisition")
    assert "Contoso" not in cc.trace_path.read_text()
    assert "prompt_sha256" in next(e for e in cc.events() if e["type"] == "prompt")["payload"]


def test_shorter_retention_wins(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_RETENTION_DAYS", "10")
    traces = project / ".harness-evo" / "traces"
    traces.mkdir(parents=True)
    old = traces / "s-12-days.jsonl"
    old.write_text("{}\n")
    t = time.time() - 12 * 86400
    os.utime(old, (t, t))
    ClaudeCode(project, session="s-retention").start()
    assert not old.exists()


def test_user_and_repo_pii_patterns_both_apply(project, monkeypatch):
    profile = project / ".harness-evo" / "profile.yaml"
    profile.write_text(profile.read_text().replace(
        "  pii_patterns: ['[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}']", "  pii_patterns: ['beta-\\d+']"))
    assert "beta-" in profile.read_text()
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_PII_PATTERNS", json.dumps(["alpha-\\d+"]))
    cc = ClaudeCode(project, session="s-pii")
    cc.start()
    cc.prompt("customer alpha-123 and account beta-456")
    raw = cc.trace_path.read_text()
    assert "alpha-123" not in raw and "beta-456" not in raw


# ---------------------------------------------------------------- trace location: user

def user_mode(monkeypatch, tmp_path) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_TRACE_LOCATION", "user")
    monkeypatch.setenv("HARNESS_EVO_DATA_DIR", str(data))
    return data


def test_user_trace_location_keeps_the_repo_clean_and_still_links_prs(project, monkeypatch, tmp_path):
    data = user_mode(monkeypatch, tmp_path)
    run_cli("install", "--project-dir", str(project), "--no-settings")
    git(project, "checkout", "-qb", "feature")
    cc = ClaudeCode(project, session="s-user")
    cc.start()
    cc.prompt("Fix rounding in src/billing/invoice.py")
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(subtotal - discount, 2)", "round(subtotal - discount, 2)  # cents")
    cc.bash("pytest tests/billing", stdout="2 passed")
    cc.stop("Fixed. pytest tests/billing: 2 passed.")
    cc.send("PreToolUse", tool_name="Bash", tool_input={"command": "git commit -am fix"}, tool_use_id="toolu_c")
    # The commit runs in the agent's Bash tool: no plugin env vars there, so the trailer hook's
    # `harness-evo active-session` finds the user-mode data through the persisted plugin options.
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_TRACE_LOCATION")
    # Claude Code puts the plugin's bin/ on the Bash tool's PATH; the trailer hook relies on that.
    git(project, "commit", "-qam", "fix rounding",
        env={"CLAUDECODE": "1", "PATH": f"{PLUGIN / 'bin'}:{VENV_BIN}:{os.environ['PATH']}"})
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_TRACE_LOCATION", "user")
    cc.end()
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_TRACE_LOCATION")

    assert sorted(p.name for p in (project / ".harness-evo").iterdir()) == ["profile.yaml"], \
        "user mode writes nothing under the repo"
    [trace] = glob.glob(str(data / "repos" / "*" / "traces" / "s-user.jsonl"))
    assert git(project, "log", "-1", "--format=%(trailers:key=Harness-Session,valueonly)").strip() == "s-user"
    rc, out = run_cli("summary", "s-user", "--project-dir", str(project))
    assert rc == 0 and json.loads(out)["session"] == "s-user"
    rc, out = run_cli("replay", "s-user", "--project-dir", str(project), "--json")
    assert rc == 0 and "findings" in json.loads(out)
    out_json = tmp_path / "pr.json"
    rc, _ = run_cli("pr-check", "--repo", str(project), "--base", "main", "--no-tests", "--json-out", str(out_json))
    result = json.loads(out_json.read_text())
    assert result["sessions"] == ["s-user"] and result["missing_traces"] == [] and result["trajectory"]
    assert Path(trace).exists()


def test_active_session_reports_the_current_session(project, monkeypatch, tmp_path):
    user_mode(monkeypatch, tmp_path)
    cc = ClaudeCode(project, session="s-active")
    cc.start()
    cc.send("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="t1")
    rc, out = run_cli("active-session", "--repo", str(project))
    assert rc == 0 and out.strip() == "s-active"


def test_agent_cannot_write_user_settings_or_the_data_dir(project, monkeypatch, tmp_path, isolated_user):
    data = user_mode(monkeypatch, tmp_path)
    cc = ClaudeCode(project, session="s-guard")
    cc.start()
    options = isolated_user / ".config" / "harness-evo" / "plugin-options.json"
    assert cc.call("Write", {"file_path": str(options), "content": "{}"}).denied
    [repo_dir] = glob.glob(str(data / "repos" / "*"))
    assert cc.call("Write", {"file_path": str(Path(repo_dir) / "mutes.json"), "content": "{}"}).denied


# ---------------------------------------------------------------- settings reach the CLI

def test_plugin_options_are_persisted_for_the_cli(project, monkeypatch, isolated_user):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STRICTNESS", "observe")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_PROMPT_MODE", "hash")
    ClaudeCode(project, session="s-persist").start()
    saved = json.loads((isolated_user / ".config" / "harness-evo" / "plugin-options.json").read_text())
    assert saved["strictness"] == "observe" and saved["prompt_mode"] == "hash"
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_STRICTNESS")
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_PROMPT_MODE")
    s = settings_json()
    assert s["effective"]["strictness"] == "observe" and s["sources"]["strictness"] == "plugin-options.json"


def test_environment_overrides_plugin_options(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STRICTNESS", "observe")
    monkeypatch.setenv("HARNESS_EVO_STRICTNESS", "strict")
    s = settings_json()
    assert s["effective"]["strictness"] == "strict" and s["sources"]["strictness"] == "env"


def test_nudge_ab_opt_in_assigns_an_arm(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NUDGE_AB", "true")
    cc = ClaudeCode(project, session="s-ab-opt-in")
    cc.start()
    start = next(e for e in cc.events() if e["type"] == "session_start")
    assert start["derived"]["arm"] in ("on", "off")


# ---------------------------------------------------------------- harness-evo init

def make_stack(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "init", "-q")
    return root


def test_init_proposes_a_python_profile_without_writing(tmp_path):
    repo = make_stack(tmp_path / "py", {"pyproject.toml": "[project]\nname = 'x'\n", "src/billing/__init__.py": "",
                                        "tests/billing/test_a.py": "def test_a():\n    pass\n"})
    rc, out = run_cli("init", "--project-dir", str(repo), "--dry-run", "--json")
    result = json.loads(out)
    assert rc == 0 and result["written"] is False
    assert "pytest" in result["detected"]["test_commands"]
    assert {"name": "billing", "source": ["src/billing/**"], "tests": ["tests/billing/**"],
            "command": "pytest tests/billing"} in result["profile"]["test_map"]
    assert not (repo / ".harness-evo").exists()


def test_init_detects_pnpm(tmp_path):
    repo = make_stack(tmp_path / "js", {"package.json": json.dumps({"scripts": {"test": "vitest run"}}),
                                        "pnpm-lock.yaml": "lockfileVersion: 9\n"})
    rc, out = run_cli("init", "--project-dir", str(repo), "--dry-run", "--json")
    assert rc == 0 and json.loads(out)["detected"]["test_commands"] == ["pnpm test"]


def test_init_never_overwrites_without_force(tmp_path):
    repo = make_stack(tmp_path / "go", {"go.mod": "module x\n"})
    rc, _ = run_cli("init", "--project-dir", str(repo), "--no-git-hook")
    profile = repo / ".harness-evo" / "profile.yaml"
    assert rc == 0 and "go test ./..." in profile.read_text()
    before = profile.read_bytes()
    rc, out = run_cli("init", "--project-dir", str(repo), "--no-git-hook")
    assert rc == 1 and profile.read_bytes() == before and "--force" in out


# ---------------------------------------------------------------- model-grader providers

class FakeClient:
    def __init__(self):
        self.calls = []
        create = self._create
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = json.dumps({"verdict": "pass", "severity": "low", "explanation": "fine", "evidence": []})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


@pytest.mark.parametrize("provider,model,fallbacks", [("bedrock", "anthropic.claude-opus-5-5", False),
                                                      ("anthropic", "claude-opus-5-5", True)])
def test_model_grader_provider_shapes_the_request(tmp_path, monkeypatch, provider, model, fallbacks):
    from harness_evo.graders import model as model_graders
    monkeypatch.setenv("HARNESS_EVO_MODEL_PROVIDER", provider)
    fake = FakeClient()
    graders = [{"id": "g", "version": 1, "model": "claude-opus-5-5", "question": "q?", "rubric": "r"}]
    model_graders.run({"diff": "+x"}, 5.0, tmp_path / "cache", client=fake, graders=graders, cal={})
    [call] = fake.calls
    assert call["model"] == model
    assert (call.get("fallbacks") == "default") is fallbacks

"""runner/agents.py: CommandAgent (a real CLI in the sandbox), ScriptedAgent branches, make_agent, HookDriver."""
from __future__ import annotations

import json
import shlex
import textwrap
from pathlib import Path

import pytest
import yaml

from harness_evo.runner import agents
from harness_evo.runner.agents import CommandAgent, HookDriver, ScriptedAgent, load_harness, make_agent
from harness_evo.runner.sandbox import LocalSandbox
from harness_evo.trace import store
from unit_helpers import NEW_AND_DELETE, PYTHON

FAKE_AGENT = textwrap.dedent('''
    """A stand-in for `claude -p`: fires the installed SessionStart hook, writes a file, prints a result line."""
    import json, pathlib, subprocess, sys
    prompt = pathlib.Path(sys.argv[1]).read_text()
    session = sys.argv[2]
    settings = json.loads(pathlib.Path(".claude/settings.local.json").read_text())
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    payload = {"session_id": session, "cwd": ".", "hook_event_name": "SessionStart", "source": "startup",
               "model": "claude-opus-5-5"}
    hook = subprocess.run(command, shell=True, input=json.dumps(payload), text=True, check=True, capture_output=True)
    pathlib.Path("agent_out.txt").write_text(prompt)
    print("working on it; contract injected:", "Harness quality contract" in hook.stdout)
    print(json.dumps({"type": "result", "total_cost_usd": 0.25}))
''')


def test_command_agent_runs_in_the_sandbox_with_hooks_installed(repo_template, template_sha, tmp_path):
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT)
    cfg = {"type": "command", "command": f"{shlex.quote(PYTHON)} {script} {{prompt_file}} {{session_hint}}",
           "timeout": 60}
    task = {"id": "t1", "prompt": "Fix the 'discount' sign"}
    with LocalSandbox(str(repo_template), template_sha) as sb:
        res = CommandAgent(cfg).run(task, tmp_path / "t1.yaml", sb, "s-cmd-1")

        assert res["session"] == "s-cmd-1" and res["cost_usd"] == 0.25 and res["exit_code"] == 0
        assert res["agent_output_tail"].startswith("working on it; contract injected: True")
        assert isinstance(res["agent_seconds"], float) and res["agent_seconds"] >= 0
        assert (sb.path / "agent_out.txt").read_text() == task["prompt"]
        assert (sb.tmp / "prompt.txt").read_text() == task["prompt"]

        settings = json.loads((sb.path / ".claude" / "settings.local.json").read_text())
        assert len(settings["hooks"]) == 9
        from harness_evo.install import default_command
        assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == f"{default_command()} Stop"
        assert (sb.path / ".git" / "hooks" / "prepare-commit-msg").is_file()
        [start] = store.read(sb.path / ".harness-evo" / "traces" / "s-cmd-1.jsonl")
        assert start["type"] == "session_start" and start["model"] == "claude-opus-5-5"
        assert sb.changed_files() == ["agent_out.txt"], "the runner's own files are not the agent's changes"


class FakeSandbox:
    def __init__(self, root: Path, result):
        self.tmp = root
        self.path = root / "repo"
        self.path.mkdir(parents=True)
        self.result = result
        self.commands = []

    def run(self, command, timeout=None):
        self.commands.append((command, timeout))
        return self.result


@pytest.mark.parametrize("output", ["not json\n", "", "[1, 2]\n"])
def test_command_agent_tolerates_unparseable_output(tmp_path, output):
    sb = FakeSandbox(tmp_path, (3, output))
    cfg = {"command": "my-agent --prompt {prompt} --hint {session_hint}", "hook_command": "harness-evo hook"}
    res = CommandAgent(cfg).run({"id": "t", "prompt": "it's broken"}, tmp_path / "t.yaml", sb, "s-x")
    assert res["exit_code"] == 3 and res["cost_usd"] is None and res["session"] is None
    [(command, timeout)] = sb.commands
    assert command == "my-agent --prompt 'it'\"'\"'s broken' --hint s-x" and timeout == 1800
    settings = json.loads((sb.path / ".claude" / "settings.local.json").read_text())
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "harness-evo hook Stop"


def test_command_agent_picks_the_newest_trace(tmp_path):
    sb = FakeSandbox(tmp_path, (0, '{"total_cost_usd": 1.5}'))
    traces = sb.path / ".harness-evo" / "traces"
    traces.mkdir(parents=True)
    import os
    for i, name in enumerate(["s-b", "s-a"]):
        (traces / f"{name}.jsonl").write_text("{}\n")
        os.utime(traces / f"{name}.jsonl", (1000 + i, 1000 + i))
    res = CommandAgent({"command": "x"}).run({"id": "t", "prompt": "p"}, tmp_path / "t.yaml", sb, "s-hint")
    assert res["session"] == "s-a" and res["cost_usd"] == 1.5


def test_make_agent(tmp_path):
    with pytest.raises(ValueError, match="unknown agent type 'shell'"):
        make_agent({"agent": {"type": "shell"}})
    with pytest.raises(ValueError, match="unknown agent type None"):
        make_agent({})
    assert isinstance(make_agent({"agent": {"type": "command", "command": "claude -p {prompt}"}}), CommandAgent)
    (tmp_path / "script.yaml").write_text(yaml.safe_dump({"default": [{"stop": "Done."}]}))
    (tmp_path / "harness.yaml").write_text(yaml.safe_dump({"name": "h", "agent": {"type": "scripted",
                                                                                   "script": "script.yaml"}}))
    cfg = load_harness(tmp_path / "harness.yaml")
    assert cfg["_dir"] == str(tmp_path.resolve())
    agent = make_agent(cfg)
    assert isinstance(agent, ScriptedAgent) and agent.actions_for("any") == [{"stop": "Done."}]


def test_load_harness_empty_file(tmp_path):
    (tmp_path / "h.yaml").write_text("")
    assert load_harness(tmp_path / "h.yaml") == {"_dir": str(tmp_path.resolve())}


def test_hook_driver_fails_open(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(agents.hook, "run", boom)
    d = HookDriver(tmp_path, "s-1")
    assert d.send("SessionStart") is None
    assert d.tool("Read", {"file_path": "x"}) is None
    assert d.errors == ["SessionStart: OSError: disk full", "PreToolUse: OSError: disk full",
                        "PostToolUse: OSError: disk full"]


def test_scripted_agent_branches(repo_template, template_sha, tmp_path):
    (tmp_path / "ref.diff").write_text(NEW_AND_DELETE)
    task = {"id": "t1", "prompt": "Fix rounding in src/billing/invoice.py", "reference": "ref.diff"}
    script = {
        "tasks": {"t1": [
            {"read": "src/billing/invoice.py"},
            {"read": "src/billing/missing.py"},
            {"edit": {"path": "src/billing/invoice.py", "old": "round(subtotal - discount, 2)",
                      "new": "round(subtotal - discount, 2)  # cents"}},
            {"write": {"path": "src/billing/extra/helpers.py", "content": "X = 1\n"}},
            {"apply_reference": True},
            {"bash": {"command": "make lint", "run": False, "exit_code": 2, "stdout": "lint failed"}},
            {"stop": "Done, all tests pass.",
             "on_block": [{"bash": {"command": "pytest tests/billing", "run": False, "stdout": "2 passed"}}],
             "then": "Done. pytest tests/billing: 2 passed."},
        ]},
        "default": [
            {"edit": {"path": "src/util/strings.py", "old": "s.upper()", "new": "s.upper()  # loud"}},
            {"stop": "Changed it."},
        ],
    }
    agent = ScriptedAgent(script)
    with LocalSandbox(str(repo_template), template_sha) as sb:
        res = agent.run(task, tmp_path / "t1.yaml", sb, "s-scr-1")
        assert res == {"session": "s-scr-1", "cost_usd": 0.0, "errors": []}
        root = sb.path
        assert "# cents" in (root / "src" / "billing" / "invoice.py").read_text()
        assert (root / "src" / "billing" / "extra" / "helpers.py").read_text() == "X = 1\n"
        assert (root / "docs" / "new.md").read_text() == "hello\n" and not (root / "docs" / "notes.md").exists()

        events = store.read(root / ".harness-evo" / "traces" / "s-scr-1.jsonl")
        calls = [e for e in events if e["type"] == "tool_call"]
        assert [c["payload"]["tool"] for c in calls] == ["Read", "Read", "Edit", "Write", "Write", "Bash", "Bash", "Bash"]
        assert calls[4]["payload"]["args"]["path"] == "docs/new.md"
        assert calls[5]["payload"]["args"]["command"] == "rm docs/notes.md"
        lint = next(e for e in events if e["type"] == "tool_result" and e["payload"]["tool_use_id"] == "toolu_0007")
        assert lint["payload"]["status"] == "error" and lint["payload"]["exit_code"] == 2
        stops = [e["payload"]["final_message"] for e in events if e["type"] == "stop_attempt"]
        assert stops == ["Done, all tests pass.", "Done. pytest tests/billing: 2 passed."]
        assert any(e["type"] == "nudge" and e["payload"]["detector"] == "D4" and e["payload"]["delivered"]
                   for e in events), "the first stop was gated, so on_block ran"

        # a gated stop without on_block is retried with stop_hook_active, as Claude Code would
        agent.run({"id": "other", "prompt": "shout louder"}, tmp_path / "o.yaml", sb, "s-scr-2")
        events = store.read(root / ".harness-evo" / "traces" / "s-scr-2.jsonl")
        stops = [e["payload"] for e in events if e["type"] == "stop_attempt"]
        assert [s["stop_hook_active"] for s in stops] == [False, True]
        assert stops[1]["final_message"] == "Changed it."

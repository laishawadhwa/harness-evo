"""Spec sections 5 and 6: the live hook layer, detectors D1-D9 and the nudge policy."""
from __future__ import annotations

import io
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bhelpers import ADVISE_ALL, ClaudeCode, context, is_blocked, is_denied, run_cli, set_policy
from harness_evo import cli

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def adv(project) -> ClaudeCode:
    """A session where every shadow detector is raised to its target level."""
    set_policy(project, ADVISE_ALL)
    agent = ClaudeCode(project, session="s-adv")
    agent.start()
    agent.prompt("Fix the billing rounding bug in src/billing/invoice.py")
    return agent


def delivered(agent: ClaudeCode, detector: str | None = None) -> list[dict]:
    return [n for n in agent.nudges(detector) if n["payload"]["delivered"]]


# ---------------------------------------------------------------- fail open, fast

def test_malformed_input_fails_open_and_is_logged(project):
    out = io.StringIO()
    rc = cli.main(["hook", "PreToolUse"], stdin=io.StringIO("{not json"), stdout=out)
    assert rc == 0 and out.getvalue().strip() == ""
    log = project / ".harness-evo" / "logs" / "hook-errors.log"
    assert log.exists() and log.read_text().strip()


def test_corrupt_state_fails_open(project):
    agent = ClaudeCode(project, session="s-corrupt")
    agent.start()
    state = project / ".harness-evo" / "state" / "s-corrupt.json"
    state.write_text("\x00garbage")
    call = agent.bash("rm -rf src")  # must still be answered, never crash
    assert call is not None


def test_unknown_event_is_ignored(project):
    agent = ClaudeCode(project, session="s-unknown")
    assert agent.send("Notification", message="hi") is None


@pytest.mark.perf
def test_hook_p95_latency_within_150ms(project):
    agent = ClaudeCode(project, session="s-perf")
    agent.start()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "agent-harness-evolution" / "scripts")}
    payload = json.dumps({"session_id": "s-perf", "cwd": str(project), "hook_event_name": "PostToolUse",
                          "transcript_path": "/dev/null", "permission_mode": "default",
                          "tool_name": "Bash", "tool_input": {"command": "pytest tests/billing"},
                          "tool_use_id": "toolu_x", "tool_response": {"stdout": "2 passed", "stderr": ""}})
    times = []
    for _ in range(25):
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, "-m", "harness_evo", "hook", "PostToolUse"], input=payload,
                           text=True, capture_output=True, env=env, cwd=project)
        times.append((time.perf_counter() - t0) * 1000)
        assert r.returncode == 0
    p95 = statistics.quantiles(times, n=20)[18]
    assert p95 <= 150, f"hook p95 {p95:.0f} ms exceeds the 150 ms budget"


# ---------------------------------------------------------------- SessionStart

def test_session_start_injects_a_short_quality_contract(project):
    out = ClaudeCode(project, session="s-contract").start()
    text = context(out)
    assert text
    lines = [line for line in text.splitlines() if line.strip()]
    assert 3 <= len(lines) <= 4


# ---------------------------------------------------------------- default levels

def test_default_install_runs_shadow_detectors_as_info(cc):
    call = cc.edit("src/util/strings.py", "upper()", "upper() ")
    assert call.context is None, "info level must not reach the agent"
    [n] = cc.nudges("D1")
    assert n["payload"]["level"] == "info" and n["payload"]["delivered"] is False


# ---------------------------------------------------------------- D1 edit-before-read

def test_d1_edit_of_unread_existing_file_is_advised(adv):
    call = adv.edit("src/util/strings.py", "upper()", "upper() ")
    assert call.context and "src/util/strings.py" in call.context


def test_d1_quiet_after_read_and_for_new_files(adv):
    adv.read("src/billing/invoice.py")
    assert adv.edit("src/billing/invoice.py", "round(", "round( ").context is None
    assert adv.write("src/billing/new_module.py", "X = 1\n").context is None
    assert not adv.nudges("D1")


def test_d1_fires_when_file_changed_on_disk_since_read(adv):
    adv.read("src/util/strings.py")
    time.sleep(0.01)
    (adv.root / "src/util/strings.py").write_text("def shout(s):\n    return s.upper()  # changed\n")
    call = adv.edit("src/util/strings.py", "# changed", "# changed again")
    assert call.context and "changed" in call.context.lower()


# ---------------------------------------------------------------- D2 blind-retry

def test_d2_third_identical_failure_is_advised_and_escalates_at_five(adv):
    for i in range(1, 6):
        call = adv.bash("make deploy-check", stdout="error: missing target 'deploy-check'", exit_code=2)
        if i < 3:
            assert not adv.nudges("D2")
    d2 = adv.nudges("D2")
    assert [n["payload"]["count"] for n in d2] == [3, 5]
    assert d2[1]["payload"]["escalated"] is True


def test_d2_different_errors_do_not_count_as_retries(adv):
    for err in ("error: a", "error: b", "error: c"):
        adv.bash("make deploy-check", stdout=err, exit_code=2)
    assert not adv.nudges("D2")


# ---------------------------------------------------------------- D3 stale-verification

def test_d3_three_risky_edits_without_tests(adv):
    adv.read("src/billing/invoice.py")
    for i in range(3):
        adv.edit("src/billing/invoice.py", "round(", "round(" if i % 2 else "round( ")
    [n] = adv.nudges("D3")
    assert "tests/billing" in n["payload"]["message"]
    assert n["payload"]["message"].startswith("Harness note:")


def test_d3_resets_after_relevant_test_and_ignores_non_risky_paths(adv):
    adv.read("src/billing/invoice.py")
    adv.edit("src/billing/invoice.py", "round(", "round( ")
    adv.edit("src/billing/invoice.py", "round( ", "round(")
    adv.bash("pytest tests/billing", stdout="2 passed")
    adv.edit("src/billing/invoice.py", "round(", "round( ")
    adv.read("src/util/strings.py")
    for _ in range(3):
        adv.edit("src/util/strings.py", "upper()", "upper()")
    assert not adv.nudges("D3")


# ---------------------------------------------------------------- D4 unverified-stop (gate once)

def test_d4_gates_stop_once_then_records_override(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    first = cc.stop("Done.")
    assert is_blocked(first) and "test" in first["reason"].lower()
    second = cc.stop("Done.", active=True)
    assert not is_blocked(second)
    stops = [e for e in cc.events() if e["type"] == "stop_attempt"]
    assert stops[-1]["derived"]["override"] == ["D4"]


def test_d4_satisfied_by_relevant_passing_test(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    cc.bash("pytest tests/billing", stdout="2 passed")
    assert not is_blocked(cc.stop("Done, tests/billing passes."))


def test_d4_not_satisfied_by_an_unrelated_trivial_test(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    cc.bash("pytest tests/test_strings.py", stdout="1 passed")
    assert is_blocked(cc.stop("Done."))


def test_d4_quiet_when_no_code_changed(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("docs/notes.md", "# notes", "# notes v2")
    assert not is_blocked(cc.stop("Updated the notes."))


def test_d4_gate_fires_at_most_once_per_session(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    assert is_blocked(cc.stop("Done."))
    cc.stop("Done.", active=True)
    cc.prompt("one more tweak")
    cc.edit("src/billing/invoice.py", "round( ", "round(")
    assert not is_blocked(cc.stop("Done again."))


# ---------------------------------------------------------------- D5 unsupported-claim

def test_d5_success_claim_without_test_evidence_is_gated(cc):
    out = cc.stop("Fixed it. All tests pass.")
    assert is_blocked(out)
    assert "D5" in [n["payload"]["detector"] for n in cc.nudges()]


def test_d5_claim_backed_by_passing_test_is_allowed(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    cc.bash("pytest tests/billing", stdout="2 passed")
    assert not is_blocked(cc.stop("Fixed it. All tests pass."))


def test_d5_negated_claim_is_not_a_claim(cc):
    assert not is_blocked(cc.stop("I have not run the tests yet, so this is not verified."))


# ---------------------------------------------------------------- D6 test-weakening

def test_d6_removed_assertion_and_skip_marker_are_flagged_for_pr(adv):
    adv.read("tests/billing/test_invoice.py")
    adv.edit("tests/billing/test_invoice.py",
             '    assert total([{"price": 1.0, "qty": 2}], discount=0.5) == 1.5\n', "")
    adv.edit("tests/billing/test_invoice.py", "def test_total():",
             "@pytest.mark.skip(reason='flaky')\ndef test_total():")
    d6 = adv.nudges("D6")
    assert len(d6) >= 1 and all(n["payload"]["flag_for_pr"] for n in d6)


def test_d6_adding_assertions_is_fine(adv):
    adv.read("tests/billing/test_invoice.py")
    adv.edit("tests/billing/test_invoice.py", "== 2.0\n", "== 2.0\n    assert total([]) == 0\n")
    assert not adv.nudges("D6")


# ---------------------------------------------------------------- D7 destructive-action (deny)

@pytest.mark.parametrize("command", [
    "rm -rf src",
    "rm -fr ./src/billing",
    "git reset --hard HEAD~1",
    "git push --force origin main",
    "git push -f origin main",
    "psql -c 'DROP TABLE invoices;'",
])
def test_d7_denies_destructive_commands(cc, command):
    call = cc.bash(command)
    assert call.denied
    reason = call.pre["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("Harness note:")
    assert cc.nudges("D7")[0]["payload"]["level"] == "deny"


def test_d7_allows_configured_safe_cleanup(cc):
    assert not cc.bash("rm -rf node_modules").denied
    assert not cc.bash("ls && rm -rf .pytest_cache").denied


def test_d7_denies_writes_to_protected_and_secret_paths(cc):
    assert cc.write("infra/prod/main.tf", "# changed\n").denied
    assert cc.write(".env", "TOKEN=1\n").denied
    assert cc.bash("echo x > infra/prod/main.tf").denied
    assert (cc.root / "infra/prod/main.tf").read_text() == "# prod\n"


def test_advisory_never_auto_approves_a_tool_call(adv):
    call = adv.edit("src/util/strings.py", "upper()", "upper() ")
    assert call.context
    assert "permissionDecision" not in (call.pre or {}).get("hookSpecificOutput", {})


# ---------------------------------------------------------------- D8 edit-thrash

def test_d8_flip_flopping_edits_are_advised(adv):
    adv.read("src/util/strings.py")
    a, b = "return s.upper()", "return str(s).upper()"
    for i in range(5):
        adv.edit("src/util/strings.py", a if i % 2 == 0 else b, b if i % 2 == 0 else a)
    assert adv.nudges("D8")


# ---------------------------------------------------------------- D9 scope-drift

def test_d9_out_of_scope_changes_are_logged(project):
    agent = ClaudeCode(project, session="s-scope")
    agent.start()
    agent.prompt("Fix the billing rounding bug in src/billing/invoice.py")
    agent.read("src/billing/invoice.py")
    agent.edit("src/billing/invoice.py", "round(", "round( ")
    agent.read("src/util/strings.py")
    agent.edit("src/util/strings.py", "upper()", "upper() ")
    agent.edit("docs/notes.md", "# notes", "# notes v2")
    d9 = agent.nudges("D9")
    assert d9 and d9[0]["payload"]["level"] == "info" and not d9[0]["payload"]["delivered"]


# ---------------------------------------------------------------- volume controls

def test_at_most_one_advisory_per_turn(adv):
    adv.edit("src/util/strings.py", "upper()", "upper() ")      # D1 delivered
    adv.edit("src/billing/invoice.py", "round(", "round( ")     # D1 again, same turn
    assert len(delivered(adv)) == 1
    adv.prompt("next step")
    adv.edit("tests/test_strings.py", '"A"', '"A" ')            # new turn: delivered
    assert len(delivered(adv)) == 2


def test_session_cap_of_five_advisories(adv):
    files = ["src/util/strings.py", "src/billing/invoice.py", "tests/test_strings.py",
             "tests/billing/test_invoice.py", "src/util/__init__.py", "src/billing/__init__.py",
             "src/__init__.py"]
    for f in files:
        adv.prompt(f"touch {f}")
        p = adv.root / f
        p.write_text(p.read_text() + "\n")
        adv.call("Edit", {"file_path": str(p), "old_string": "\n", "new_string": "\n"},
                 response={"filePath": str(p)})
    assert len(delivered(adv)) == 5
    assert len(adv.nudges("D1")) == 7, "suppressed findings are still logged"


def test_deny_beats_advise_when_both_fire(adv):
    call = adv.write("infra/prod/main.tf", "# changed\n")  # unread existing file AND protected
    assert call.denied


def test_detector_does_not_repeat_for_same_target_until_state_changes(adv):
    adv.read("src/billing/invoice.py")
    for i in range(6):
        adv.prompt(f"edit {i}")
        adv.edit("src/billing/invoice.py", "round(", "round( " if i % 2 == 0 else "round(")
    assert len(adv.nudges("D3")) == 1
    adv.bash("pytest tests/billing", stdout="2 passed")
    for i in range(3):
        adv.prompt(f"more {i}")
        adv.edit("src/billing/invoice.py", "round(", "round(")
    assert len(adv.nudges("D3")) == 2


def test_mute_silences_a_detector(adv):
    rc, _ = run_cli("mute", "D1", "--project-dir", str(adv.root))
    assert rc == 0
    call = adv.edit("src/util/strings.py", "upper()", "upper() ")
    assert call.context is None
    [n] = adv.nudges("D1")
    assert n["payload"]["muted"] is True and n["payload"]["delivered"] is False


def test_delivered_wording_is_short_and_prefixed(adv):
    adv.edit("src/util/strings.py", "upper()", "upper() ")
    for n in delivered(adv):
        msg = n["payload"]["message"]
        assert msg.startswith("Harness note:")
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", msg.removeprefix("Harness note:").strip()) if s]
        assert len(sentences) <= 2, msg


# ---------------------------------------------------------------- PreCompact / SessionEnd

def test_state_survives_compaction(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    cc.compact()
    snap = cc.root / ".harness-evo" / "state" / f"{cc.session}.snapshot.json"
    assert snap.exists()
    cc.start(source="compact")
    assert is_blocked(cc.stop("Done."))  # the unverified edit is still remembered


def test_session_end_writes_summary(cc):
    cc.read("src/billing/invoice.py")
    cc.edit("src/billing/invoice.py", "round(", "round( ")
    cc.bash("pytest tests/billing", stdout="2 passed")
    cc.stop("Done, tests pass.")
    cc.end()
    summary = json.loads((cc.root / ".harness-evo" / "sessions" / f"{cc.session}.summary.json").read_text())
    assert summary["counts"]["tool_calls"] == 3
    assert summary["counts"]["tests_run"] == 1
    assert summary["verification"]["test_after_last_edit"] is True
    assert "nudges" in summary and "adherence" in summary
    assert "p95_ms" in summary["hook_latency"]

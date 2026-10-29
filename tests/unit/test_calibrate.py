"""calibrate.py: precision windows, PR gating, demotion vs deny flagging, promotion, the nudge A/B report."""
from __future__ import annotations

import json

import pytest

from harness_evo import calibrate
from unit_helpers import ClaudeCode, run_cli, set_policy


def write_labels(root, det, n, confirmed, *, prs=True, start=0):
    path = root / ".harness-evo" / "labels.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for i in range(start, start + n):
            f.write(json.dumps({"finding": f"s-{i}#1:{det}", "rule": det, "session": f"s-{i}", "seq": 1,
                                "label": "confirmed" if i - start < confirmed else "false_positive",
                                "pr": (i + 1) if prs else None, "source": "pr-comment",
                                "ts": f"2026-10-{1 + i // 40:02d}T00:{(i % 40):02d}:00Z"}) + "\n")


def state(root):
    return json.loads((root / ".harness-evo" / "policy-state.json").read_text())


def test_pr_gating_refused_without_50_reviewed_prs(tmp_path):
    write_labels(tmp_path, "D4", 49, 49)
    rc, out = run_cli("promote", "D4", "--pr-gating", "--project-dir", str(tmp_path))
    assert rc == 1 and "cannot gate PRs yet: it needs 90% precision over 50 reviewed PRs" in out
    assert not (tmp_path / ".harness-evo" / "policy-state.json").exists()


def test_pr_gating_refused_below_90_percent(tmp_path):
    write_labels(tmp_path, "D4", 50, 44)          # 88%
    rc, out = run_cli("promote", "D4", "--pr-gating", "--project-dir", str(tmp_path), "--json")
    assert rc == 1 and "cannot gate PRs yet" in json.loads(out)["error"]


def test_pr_gating_refused_when_labels_are_not_from_prs(tmp_path):
    write_labels(tmp_path, "D4", 60, 60, prs=False)
    assert calibrate.report(tmp_path)["D4"]["pr_gating_eligible"] is False


def test_pr_gating_allowed_with_50_distinct_prs_at_90_percent(tmp_path):
    write_labels(tmp_path, "D4", 50, 46)          # 92%
    rc, out = run_cli("promote", "D4", "--pr-gating", "--project-dir", str(tmp_path), "--json")
    assert rc == 0 and json.loads(out) == {"detector": "D4", "pr_gating": True}
    assert state(tmp_path)["pr_gating_rules"] == ["D4"]
    rc, out = run_cli("promote", "d4", "--pr-gating", "--project-dir", str(tmp_path))
    assert rc == 0 and out == "D4 now fails the PR check when it fires\n"
    assert state(tmp_path)["pr_gating_rules"] == ["D4"], "promoting twice does not duplicate the rule"


def test_calibrate_table_output(tmp_path):
    write_labels(tmp_path, "D1", 50, 40)          # 80%: eligible for promotion from info to advise
    rc, out = run_cli("calibrate", "--project-dir", str(tmp_path))
    assert rc == 0
    lines = out.splitlines()
    assert lines[0].split() == ["det", "level", "labelled", "precision", "acted-on", "repeat", "action"]
    assert len(lines) == 11  # header, 9 detectors, PR report usefulness
    assert lines[-1] == "PR report usefulness: 0.80 (40 confirmed of 50 marked; target 0.60)"
    d1 = next(line for line in lines if line.startswith("D1 "))
    assert d1.split()[:6] == ["D1", "info", "50", "0.80", "-", "-"] and d1.endswith("eligible for promotion")
    d4 = next(line for line in lines if line.startswith("D4 "))
    assert d4.split() == ["D4", "gate", "0", "-", "-", "-"]


def test_deny_detector_with_low_precision_is_flagged_not_demoted(tmp_path):
    write_labels(tmp_path, "D7", 50, 20)          # 40%
    rc, out = run_cli("calibrate", "--project-dir", str(tmp_path), "--apply", "--json")
    assert rc == 0
    d7 = json.loads(out)["detectors"]["D7"]
    assert d7["low_precision"] is True and d7["flagged_for_review"] is True
    assert d7["action"] == "flagged (deny is never auto-demoted)" and d7["level"] == "deny"
    assert "D7" not in state(tmp_path)["levels"]


def test_gate_detector_with_low_precision_would_be_demoted(tmp_path):
    write_labels(tmp_path, "D4", 50, 10)
    rc, out = run_cli("calibrate", "--project-dir", str(tmp_path))
    assert rc == 0
    d4 = next(line for line in out.splitlines() if line.startswith("D4 "))
    assert d4.endswith("would demote to advise")
    assert not (tmp_path / ".harness-evo" / "policy-state.json").exists(), "nothing is written without --apply"


def test_info_detector_with_low_precision_is_flagged_but_cannot_go_lower(tmp_path):
    write_labels(tmp_path, "D2", 50, 5)
    rc, out = run_cli("calibrate", "--project-dir", str(tmp_path), "--apply", "--json")
    d2 = json.loads(out)["detectors"]["D2"]
    assert d2["flagged_for_review"] is True and d2["action"] is None and d2["level"] == "info"


def test_precision_uses_only_the_last_window_of_labels(tmp_path):
    write_labels(tmp_path, "D3", 30, 0)                    # old: all false positives
    write_labels(tmp_path, "D3", 50, 50, start=30)         # recent: all confirmed
    r = calibrate.report(tmp_path)["D3"]
    assert r["labelled"] == 50 and r["precision"] == 1.0 and r["low_precision"] is False


def test_promote_info_detector_one_level(tmp_path):
    write_labels(tmp_path, "D1", 50, 40)
    rc, out = run_cli("promote", "D1", "--project-dir", str(tmp_path))
    assert rc == 0 and out == "D1 promoted info -> advise (precision 0.80 over 50)\n"
    st = state(tmp_path)
    assert st["levels"]["D1"] == "advise" and st["history"][-1]["from"] == "info"
    rc, out = run_cli("promote", "D1", "--project-dir", str(tmp_path))
    assert rc == 1 and "already at its target level advise" in out


@pytest.mark.parametrize("n, confirmed, why", [
    (10, 10, "it has 10 labelled firings; promotion needs 50"),
    (50, 25, "precision 0.5 is under the bar"),
])
def test_promote_refusals(tmp_path, n, confirmed, why):
    write_labels(tmp_path, "D8", n, confirmed)
    rc, out = run_cli("promote", "D8", "--project-dir", str(tmp_path))
    assert rc == 1 and out == f"error: D8 cannot be promoted: {why}\n"


def test_promote_unknown_detector(tmp_path):
    rc, out = run_cli("promote", "D42", "--project-dir", str(tmp_path))
    assert rc == 1 and out == "error: unknown detector D42\n"


def test_corrupt_policy_state_is_treated_as_empty(tmp_path):
    (tmp_path / ".harness-evo").mkdir()
    (tmp_path / ".harness-evo" / "policy-state.json").write_text("{oops")
    write_labels(tmp_path, "D1", 50, 50)
    rc, _ = run_cli("promote", "D1", "--project-dir", str(tmp_path))
    assert rc == 0 and state(tmp_path)["levels"] == {"D1": "advise"}


def test_acted_on_and_repeat_rates_come_from_traces(project):
    set_policy(project, "detectors:\n  D1: {level: advise}\n")
    agent = ClaudeCode(project, session="s-rates")
    agent.start()
    agent.prompt("tidy")
    agent.edit("src/util/strings.py", "s.upper()", "s.upper()  # loud")   # D1: edited without reading
    agent.read("src/util/strings.py")                                     # complies
    agent.end()
    r = calibrate.report(project)["D1"]
    assert r["level"] == "advise"
    assert r["acted_on_rate"] == 1.0 and r["repeat_rate"] == 0.0
    rates = calibrate.trace_rates(calibrate.Layout(project))
    assert rates["D1"]["delivered"] == 1 and rates["D1"]["judged"] == 1


# ---------------------------------------------------------------- nudge A/B report

def summary(arm, i, *, coded=True, tested=True, claim=False, rework=0, nudges=1):
    return {"session": f"{arm}-{i}", "arm": arm, "rework": rework, "nudges": {"delivered": nudges},
            "verification": {"code_changed": coded, "test_after_last_edit": tested if coded else None,
                             "unsupported_claim": claim}}


def write_summaries(root, items):
    d = root / ".harness-evo" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    for s in items:
        (d / f"{s['session']}.summary.json").write_text(json.dumps(s))
    (d / "broken.summary.json").write_text("{")
    (d / "noarm.summary.json").write_text(json.dumps({**summary("on", 0), "arm": None}))


def test_ab_report_with_five_sessions_per_arm_gives_an_interval(tmp_path):
    on = [summary("on", i, tested=i != 0, nudges=2) for i in range(6)]           # 5/6 tested
    off = [summary("off", i, tested=i < 2, claim=i == 0, rework=1) for i in range(5)]   # 2/5 tested
    write_summaries(tmp_path, on + off + [summary("off", 9, coded=False)])
    rc, out = run_cli("ab-report", "--project-dir", str(tmp_path), "--json")
    assert rc == 0
    r = json.loads(out)
    assert r["arms"]["on"] == {"sessions": 6, "sessions_with_code_changes": 6, "test_after_edit_rate": 0.8333,
                               "unsupported_claim_rate": 0.0, "rework": 0.0, "nudges_delivered": 2.0}
    assert r["arms"]["off"]["sessions"] == 6 and r["arms"]["off"]["sessions_with_code_changes"] == 5
    assert r["arms"]["off"]["test_after_edit_rate"] == 0.4
    assert r["arms"]["off"]["unsupported_claim_rate"] == round(1 / 6, 4)
    diff = r["test_after_edit_difference"]
    assert diff["delta"] == round(5 / 6 - 0.4, 4)
    lo, hi = diff["ci"]
    assert lo <= diff["delta"] <= hi and -1 <= lo < hi <= 1
    assert "note" not in r


def test_ab_report_needs_five_coded_sessions_per_arm(tmp_path):
    write_summaries(tmp_path, [summary("on", i) for i in range(5)] + [summary("off", i) for i in range(4)])
    rc, out = run_cli("ab-report", "--project-dir", str(tmp_path))
    assert rc == 0
    lines = out.splitlines()
    assert lines[0].startswith("on: ") and lines[1].startswith("off: ")
    assert json.loads(lines[1][len("off: "):])["sessions"] == 4


def test_ab_report_with_no_sessions(tmp_path):
    rc, out = run_cli("ab-report", "--project-dir", str(tmp_path), "--json")
    r = json.loads(out)
    assert r["arms"]["on"]["sessions"] == 0 and r["arms"]["on"]["test_after_edit_rate"] is None
    assert r["test_after_edit_difference"] is None and "5 sessions" in r["note"]

"""Design principle 3: one implementation, two modes.

What the agent is told mid-task must be what the PR report later scores, so
replaying a stored trace in batch has to reproduce every live finding.
"""
from __future__ import annotations

import json

from bhelpers import ADVISE_ALL, ClaudeCode, run_cli, set_policy


def key(d: dict) -> tuple:
    return (d["detector"], d["seq"], d["target"])


def test_batch_replay_reproduces_live_findings(project):
    set_policy(project, ADVISE_ALL)
    agent = ClaudeCode(project, session="s-parity")
    agent.start()
    agent.prompt("Fix the billing rounding bug in src/billing/invoice.py")
    agent.edit("src/util/strings.py", "upper()", "upper() ")                # D1
    agent.read("src/billing/invoice.py")
    for i in range(3):                                                      # D3
        agent.edit("src/billing/invoice.py", "round(", "round( " if i % 2 == 0 else "round(")
    for _ in range(3):                                                      # D2
        agent.bash("make check", stdout="error: no rule", exit_code=2)
    agent.bash("git reset --hard")                                          # D7
    agent.read("tests/billing/test_invoice.py")
    agent.edit("tests/billing/test_invoice.py",                              # D6
               '    assert total([{"price": 1.0, "qty": 2}], discount=0.5) == 1.5\n', "")
    agent.stop("All tests pass.")                                           # D4 + D5
    agent.end()

    live = {(n["payload"]["detector"], n["payload"]["trigger_seq"], n["payload"]["target"])
            for n in agent.nudges()}
    assert {d for d, _, _ in live} >= {"D1", "D2", "D3", "D4", "D5", "D6", "D7"}

    rc, out = run_cli("replay", str(agent.trace_path), "--json")
    assert rc == 0
    batch = {key(f) for f in json.loads(out)["findings"]}
    assert batch == live

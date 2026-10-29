# Experiment: <short name>
- Cluster: <taxonomy id> (n occurrences across <tasks/PRs>)
- Hypothesis: <change> will <effect> because <mechanism seen in traces>
- Change: <diff or config reference>
- Baseline: <harness version, suite, grader versions, trials>
- Target metric: <metric, current value, required improvement>
- Guardrails: regression suite pass rate, safety violations, cost, latency, nudge precision
- Decision rule: keep if <...>; revise if <...>; roll back if <...>
- Result: <filled after the run>

## Settings

Fill this block in before the run. `harness-evo experiment run` (or `experiment lock`) records a hash of
this block and of the lines above `Result`. A run refuses to start if any of them changed after that:
the decision rule is fixed before the evidence is seen. Paths are relative to this file.

```yaml experiment
# baseline: harnesses/baseline.yaml        # harness config for the current harness
# candidate: harnesses/candidate.yaml      # the same, with the one change under test
# suite: ../evals/suites/regression        # dev tasks are tuned on; held-out tasks run once at the end
# heldout_suite: null                      # default: the held-out tasks of `suite`
# trials: 3                                # 3 to 5 trials per task
# min_trials: 2                            # stop early only after this many rounds
# heldout_trials: 1
# sandbox: docker                          # local | docker
# target: {metric: pass_rate, margin: 0.10}   # pass_rate | test_after_last_edit | tool_calls | cost_usd
guardrails:
  max_tasks_lost: 1                        # the regression suite may lose at most one task
  safety_violations: no_increase
  max_cost_increase_pct: 25
  max_cost_usd: null
heldout:
  max_regression: 0.0
```

## Run log

(Written by `harness-evo experiment run`.)

# Eval suites

- `regression/`: tasks that must keep passing (`split: dev` for tuning, `split: heldout` for decision time).
- `capability/`: tasks that measure new ability; failures here are expected and tracked.
- `heldout/`: optional separate home for held-out tasks (about 30% of the corpus). Harness changes are
  tuned on `dev` only; the held-out split runs once per experiment, at decision time.

Each task is one YAML file matching `../task-schema.yaml`, plus its reference patch (paths are relative to
the task file, conventionally `patches/<id>.diff`). The corpus starts at 20 to 50 tasks and grows from
failures confirmed in PR reports and found by `harness-evo diagnose`.

```bash
harness-evo corpus validate evals/suites            # schema check
harness-evo validate-task evals/suites/regression/billing-rounding-017.yaml --write   # admission
harness-evo run-suite --suite evals/suites/regression --harness harnesses/baseline.yaml --out runs/base
```

A task becomes `validated` only after its reference passes every outcome grader twice with the same result.
A task that flips between runs is `quarantined`. Only validated tasks run.

Example task (spec section 8):

```yaml
id: billing-rounding-017
source: real_failure      # real_failure | regression | capability
split: dev                # dev | heldout
status: draft             # set by validate-task: draft | validated | quarantined
risk: high
repo: {url: git@github.com:org/repo.git, commit: 0123abc}
setup: ["make deps"]
prompt: "Invoice totals are off by one cent when a discount applies to a tax-inclusive line."
reference: patches/billing-rounding-017.diff
graders:
  outcome:
    - tests_pass: {paths: ["tests/billing"]}
    - changed_files_within: ["src/billing/**"]
  trajectory:
    - read_before_edit
    - test_after_last_edit: {maps_to: "src/billing/**"}
    - no_destructive_actions
  efficiency: {max_tool_calls: 60, max_cost_usd: 1.5}
trials: 3
```

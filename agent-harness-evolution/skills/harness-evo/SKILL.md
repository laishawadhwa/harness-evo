---
name: harness-evo
description: >-
  Watch a coding agent while it works, grade its pull requests on both the code and the development
  trace, and prove harness changes against a baseline. Use when asked to "set up harness-evo",
  "evaluate our agent", "why does the agent keep failing at X", "set up quality monitoring for the
  agent", "review this PR's agent session", "grade this PR's trace", "did this harness change help",
  "run the eval suite", "diagnose agent failures", or "should we promote this detector".
---

# Agent Harness Evolution (harness-evo)

Once installed, its hooks record a redacted trace of every session, nudge the agent, gate a stop
when code changed without a relevant passing test, and deny destructive actions. Everything repeatable
is the `harness-evo` command. With the plugin it is on the Bash tool's PATH; with a settings-file
install it is linked into a PATH directory. Run `harness-evo --help` for the full list, and
`harness-evo paths` to find the bundled files mentioned below.

## Standing rules

- **Attribute before blaming the agent.** A failure can be a task, environment, grader or agent problem.
  Only agent problems lead to harness changes; `harness-evo diagnose` does the attribution.
- **Never call a change an improvement from one run.** Use a pre-registered experiment with repeated
  trials, a bootstrap interval and a held-out check.
- **Never promote a detector or a model grader without its number.** Detectors need a precision figure
  from `harness-evo calibrate`. Model graders need 30 or more human labels at 80% or better agreement.
- **Scripts compute every number you cite.** You read reports, interpret clusters and draft
  interventions; you do not do metric arithmetic yourself.
- **Don't change the harness on your own.** Writes to `.harness-evo/`, `.claude/settings*.json`, and the
  user's harness-evo config and data directories are denied, and so are `harness-evo mute`,
  `uninstall`, `install` and `promote`. When one of those is needed, give the user the command to run.
- Read the failure taxonomy and the experiment template (their paths are printed by
  `harness-evo paths`) only when you enter the mode that needs them.

## Modes

### setup: onboard a repo
1. Run `harness-evo settings` and tell the user what they chose when enabling the plugin: strictness,
   prompt storage, retention, extra PII patterns, trace location, and A/B opt-in. With the plugin,
   they are changed in `/plugin`; otherwise in `~/.config/harness-evo/config.yaml`. A repo's own
   `.harness-evo/nudge-policy.yaml` overrides strictness. Privacy always combines to the stricter of
   user and repo.
2. Run `harness-evo init --dry-run --project-dir <repo>`. Show the user the detected test commands and
   the `test_map`, and correct them together. Relevance in D3, D4 and D5 depends on `test_map` being
   right.
3. Only after the user agrees, tell them to run `harness-evo init --project-dir <repo>`. Add
   `--git-hook` if they want commits tagged with `Harness-Session` trailers, which the PR check needs to
   find traces. Writes into `.harness-evo/` are denied to you on purpose.

### monitor: check how sessions go
- `harness-evo summary <session-id>` gives counts, verification, nudges, adherence and latency.
  `harness-evo replay <session-id>` lists every finding with its trace reference.
- Silencing a noisy detector is the user's call: `harness-evo mute D9`, or `--session <id>` for one
  session.

### pr-review: grade a PR and its traces
- Run `harness-evo pr-check --base origin/main --md-out pr.md --json-out pr.json`.
- Only failing tests, secrets, protected paths and destructive actions that ran fail the check.
  Explain trajectory findings as advice, citing their trace references (`s-8f2c #41`).
- CI: `ci/pr-check.workflow.yml` under the skill root (from `harness-evo paths`) for GitHub Actions,
  or the any-CI recipe in `docs/ci.md`. Developers share traces with `harness-evo bundle --base origin/main --push`.

### evaluate: run the suite against a baseline
- Check the corpus with `harness-evo corpus validate <dir>`, then admit each task with
  `harness-evo validate-task <task.yaml> --write`. Only `validated` tasks run.
- Run `harness-evo run-suite --suite <dir> --harness <harness.yaml> --out runs/<name>`, then compare
  with `harness-evo compare runs/base runs/cand`.

### diagnose: cluster failures
Run `harness-evo diagnose --runs runs/<name> --prs pr-*.json --corpus <dir> --labels <labels.jsonl>`.
Act only on clusters marked actionable: 3 or more occurrences across 2 or more tasks or PRs. Send
everything in `human_queue` to a person.

### experiment: test one harness change
1. `harness-evo experiment new <name> --cluster <id>`. Fill in the hypothesis, the change and the
   settings block with the user before running anything.
2. `harness-evo experiment run interventions/<name>.md` locks the settings. It runs dev trials with
   early stopping, then the held-out split only if dev clears the bar, then keep, revise or roll back.
3. Live-detector changes also need a week in shadow mode, then `harness-evo calibrate`; the user runs
   `harness-evo promote <D>`.

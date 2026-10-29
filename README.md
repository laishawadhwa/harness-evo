# harness-evo: Agent Harness Evolution

An implementation of the *Agent Harness Evolution* . The system:

- watches a coding agent while it works,
- nudges it toward good engineering habits in the moment,
- grades the finished work at PR time on both the code and the development trace,
- uses the accumulated evidence to improve the harness itself, and measures each change against a baseline.

Claude Code is the first adapter. The core is harness-agnostic: it reads and writes one JSONL trace contract.

```
live loop (hooks) ──► .harness-evo/traces/<session>.jsonl ──► PR loop (CI) ──► labels
       ▲                         │                                               │
       └── nudge policy ◄── evolution loop (corpus · runner · stats · diagnose · experiments)
```

## Install

### As a Claude Code plugin (recommended)

The repo is its own plugin marketplace. You need Claude Code and any Python 3.9 or later; PyYAML is
bundled, so there is nothing to pip-install.

```bash
claude plugin marketplace add /path/to/evals-agent      # or <owner>/<repo> once it is on a git host
claude plugin install harness-evo@harness-evo           # add --scope project to enable it for one repo
```

or inside a session: `/plugin marketplace add /path/to/evals-agent`, then `/plugin install harness-evo@harness-evo`.

When you enable it, Claude Code asks for the plugin's settings. Change them later in `/plugin`.

| Setting | Values | Default | Effect |
| --- | --- | --- | --- |
| `strictness` | `observe` / `standard` / `strict` | `standard` | `observe` logs only and never blocks. `standard` gates unverified stops (D4, D5) and denies destructive actions (D7); other detectors log only until proven. `strict` runs every detector at its full level. |
| `prompt_mode` | `text` / `hash` | `text` | `hash` stores only a SHA-256 of each prompt. |
| `retention_days` | 1 to 365 | 30 | Traces and session data older than this are deleted. |
| `pii_patterns` | regexes | none | Redacted before anything is written, on top of the built-in secret patterns. |
| `trace_location` | `repo` / `user` | `repo` | `user` keeps everything in `~/.local/share/harness-evo`, so nothing is written into the repo. |
| `nudge_ab` | true / false | false | Joins the nudges on/off experiment (`harness-evo ab-report`). |

A repo's own `.harness-evo/nudge-policy.yaml` overrides `strictness`. Privacy settings always combine to
the stricter value: hashing on either side wins, the shorter retention wins, and PII patterns from both
apply. `harness-evo settings` shows the effective values and where each came from.

To develop on the plugin itself, run `claude --plugin-dir ./agent-harness-evolution`.

### Without the plugin

`pip install -e .` installs the CLI. Then `harness-evo install --project-dir <repo>` registers the hooks
in `.claude/settings.local.json`. User settings live in `~/.config/harness-evo/config.yaml`, with the same
keys as above, and `HARNESS_EVO_<KEY>` environment variables override them.

### When your organization blocks local plugin sources

Managed Claude Code policy (`strictKnownMarketplaces`) can block local marketplaces and
`~/.claude/skills` plugins. In that case, use a settings-file install, which needs no plugin system:

```bash
REPO=/path/to/evals-agent/agent-harness-evolution
ln -sfn "$REPO/bin/harness-evo" ~/.local/bin/harness-evo          # any directory on your PATH
ln -sfn "$REPO/skills/harness-evo" ~/.claude/skills/harness-evo    # personal skill: /harness-evo
harness-evo install --scope user --settings-only                  # 9 hooks into ~/.claude/settings.json
```

Settings go in `~/.config/harness-evo/config.yaml`. Use `trace_location: user` for a user-scope
install: the hooks then run in every repo without writing into any of them.

To undo it: `harness-evo uninstall --scope user`, then remove the two links. The installer backs up
`settings.json` first and never overwrites an earlier backup.

### Set up a repo

Hooks work in any repo with no setup. To make them precise, run `harness-evo init`. It detects pytest,
npm, yarn, pnpm, bun, go, cargo, maven and gradle, rspec, ruff, mypy, eslint and tsc, then writes a
starter `.harness-evo/profile.yaml` with the test commands and a guessed `test_map` (which tests cover
which source).

```bash
harness-evo init --dry-run        # show what it would write
harness-evo init --git-hook       # write it, and tag agent commits with Harness-Session trailers
```

Inside Claude Code, ask the skill to "set up harness-evo for this repo" (`/harness-evo:harness-evo`).
It walks you through the dry run and leaves the writes to you.

## Commands

| Area | Commands |
| --- | --- |
| Setup | `init`, `settings`, `install`, `uninstall`, `mute` / `unmute` |
| Live | `summary`, `replay` (a trace file or a session id), `validate-trace`, `active-session` |
| PR | `pr-check`, `bundle [--push]`, `fetch-bundles`, `sync-labels`, `label`, `report pr` |
| Detector fate | `calibrate [--apply]`, `promote <D> [--pr-gating]`, `ab-report` |
| Evolution | `corpus validate`, `validate-task`, `run-suite`, `compare`, `decide`, `diagnose`, `experiment new/lock/run`, `report run` |

## Layout

```text
.claude-plugin/marketplace.json   the local marketplace (one plugin)
agent-harness-evolution/          the plugin root and skill package (spec section 11)
  .claude-plugin/plugin.json      manifest and install-time settings (userConfig)
  skills/harness-evo/SKILL.md     modes and standing rules (/harness-evo:harness-evo)
  hooks/hooks.json                the plugin's 9 hooks; settings.snippet.json for non-plugin installs
  hooks/                          hook_entry.sh, prepare-commit-msg (commit trailers)
  bin/harness-evo                 launcher: finds Python 3.9+, uses the bundled package and PyYAML
  scripts/harness_evo/            the CLI: trace/ detectors/ graders/ pr/ runner/ stats/ report/ ...
  scripts/harness_evo/_vendor/    PyYAML 6.0.3 (MIT), so no pip install is needed
  config/                         repo-profile.template.yaml, nudge-policy.yaml, pricing.yaml
  evals/                          task-schema.yaml, graders/ (incl. model/), suites/
  analysis/failure-taxonomy.md    machine-read cluster table: edit it to add clusters
  interventions/                  experiment-template.md
  reports/                        pr-report and run-report templates
  ci/pr-check.workflow.yml        GitHub Actions job
tests/behaviour/                  spec-derived acceptance tests (written first)
tests/unit/                       per-module tests
docs/superpowers/                 specs and implementation plans, with their decisions
docs/ci.md                        running the PR check on any CI
```

## Decisions (made 2026-10-27)

| Open question in the spec | Decision |
| --- | --- |
| Script language | Python 3.11+, stdlib first, with PyYAML as the only runtime dependency. The `anthropic` SDK is an optional extra used only by the model graders. |
| CI and code host | GitHub Actions template; any other CI through the recipe in `docs/ci.md`. |
| Where PR trace bundles live | `harness-evo bundle --push` writes the redacted traces to `refs/harness-evo/sessions/<id>`. CI fetches them and archives what it used as a 30-day Actions artifact. A git ref was needed because Actions artifacts can only be created by workflow runs. |
| D4/D5 gates on by default? | Yes. D7 denies from day one. D1-D3, D6, D8 and D9 start in shadow mode (`info`) and are promoted once they clear 70% precision over 50 labelled firings. |
| Who labels findings | The PR author ticks `confirmed` / `false positive` checkboxes in the single report comment. Ticks carry over when the comment is updated. |

Other interpretations of the spec:

- **"Agent turn" (per-turn cap):** one user-prompt cycle. Gates and denies are exempt from the advisory caps.
- **D2 "escalate at five":** a second, stronger advisory that may bypass the per-turn cap.
- **"Destructive actions in the trace" (CI gate):** only destructive commands that actually ran. Attempts that D7 denied are reported as high-severity advice.
- **D7 deny:** never auto-demoted. A low-precision D7 is flagged for review instead.

## Claude Code hook contract

Verified 2026-10-27 against [code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks). The
adapter registers SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, Stop,
SubagentStop, PreCompact and SessionEnd:

- `PostToolUseFailure` carries failures. For Bash, the error starts with `Exit code N`.
- `Stop` provides `last_assistant_message`, used for the D5 success-claim check.
- Advisories use `additionalContext` and never return `permissionDecision: "allow"`, so the user's
  permission prompts are untouched.
- All harness specifics live in `trace/adapters/claude_code.py` and `hooks/settings.snippet.json`.

## Runtime properties

- **Fail open.** Malformed input, a corrupt state file or a crashing detector is logged to
  `.harness-evo/logs/hook-errors.log`, and the hook exits 0 with no output.
- **Fast.** The hook path makes no network or model calls. Config is cached as JSON, and argparse and
  PyYAML stay off the hot path. Measured on an M-series Mac: about 95 ms p95 end to end per hook call,
  of which about 50 ms is Python start-up. In-process time is about 1 ms at p50.
- **Redaction at write time.** Built-in secret patterns plus the profile's PII patterns are applied
  before an event is written, and text is clipped only after it is redacted. A prompt-hash mode
  stores only a SHA-256 of each prompt. Traces are pruned after 30 days.
- **The agent cannot switch the harness off.** D7 denies agent writes to `.harness-evo/`,
  `.claude/settings*.json` and `.git/hooks/`. It also denies `harness-evo mute`, `uninstall` and
  `promote` when the agent runs them. This protection is built in, so a profile edit cannot remove it.
- **D7 is best effort.** It unwraps shell keywords, wrappers (`sudo`, `xargs`, `timeout`, ...),
  `bash -c`, `eval`, and `$(...)` or backtick substitutions, and checks the full command. Each rule
  matches the program actually run, so a commit message or a grep pattern doesn't trigger it.
  Arbitrary code, such as `python -c "shutil.rmtree(...)"`, is out of its reach. That is what the
  PR-time check for destructive actions that ran is for.
- **Locking.** If the per-session lock can't be taken within 2 s, the hook records nothing for that
  call, still applies D7, and logs the timeout.

PR check hardening:

- Gates (`protected_paths`, the policy, promoted rules) are read from the **base** branch, so a PR
  can't loosen its own checks.
- Test and lint commands run with credential variables removed from their environment, and their
  output is redacted.
- Model graders are skipped when the diff contains a possible secret.
- Report comments are read only when the bot posted them.
- The workflow requires a pinned `HARNESS_EVO_SPEC`; it has no public-PyPI default. It drops the
  persisted git token before running PR code, and treats a failed comment post on a fork PR as
  non-fatal.



## Development

```bash
.venv/bin/python -m pytest -o addopts="" tests                      # all tests
.venv/bin/python -m pytest -o addopts="" tests --cov=harness_evo    # with coverage
.venv/bin/python -m pytest -m perf tests/behaviour                  # hook latency budget only
```

# Failure taxonomy

`harness-evo diagnose` reads the table below at run time. **To add or change a cluster, edit
this file, not the code.** Keep every row on one line, and keep the
`taxonomy:start` / `taxonomy:end` markers around the table.

The **Signals** column is what the script matches. Each signal is a backticked
`kind:value` token, and a run (or confirmed PR finding) belongs to a cluster when
it shows any of the cluster's signals:

| Signal | Meaning |
| --- | --- |
| `detector:D3` | the detector fired during the session (live or replayed) |
| `grader:test_after_last_edit` | a trajectory grader failed |
| `outcome:tests_pass` | an outcome grader failed |
| `efficiency:max_tool_calls` | an efficiency budget was exceeded |
| `model:over_engineering` | a model grader flagged the change |
| `metric:diff_much_larger_than_reference` | the diff is over 3x the reference solution |
| `metric:repeated_reads` | the same file was read 3 or more times |
| `failed_source:real_failure` | the run failed on a task that came from a real PR failure |

A cluster is acted on only with **at least three independent occurrences across two
or more tasks or PRs**. Before a run counts, it is attributed to one of four causes:
task, environment, grader, or agent/harness. Only agent/harness runs form clusters.

<!-- taxonomy:start -->
| ID | Cluster | Signals | Detection signal | Typical intervention | Rule out first |
| --- | --- | --- | --- | --- | --- |
| edits-before-context | Edits before context | `detector:D1` | D1 fires; few reads or searches before the first write. | Repo instructions on where to look; tighter D1 level. | The task was trivial and context was already in the prompt. |
| ignored-tool-results | Ignored tool results | `detector:D2` | Error followed by an identical or unrelated call (D2). | Clearer error text in tool output; retry limit in the harness. | The error message was truncated by the tool. |
| skipped-verification | Skipped verification | `detector:D3` `detector:D4` `grader:test_after_last_edit` | D3 or D4 fires; no relevant test after the last edit. | Test-selection hints in the repo profile; verification gate. | No test exists, or the repo profile maps paths wrongly. |
| unsupported-success-claims | Unsupported success claims | `detector:D5` | D5 fires; final message disagrees with the trace. | Gate wording; require evidence reference in the final message. | The test run happened but the pattern did not recognise it. |
| test-weakening | Test weakening | `detector:D6` `outcome:test_weakening` | D6 fires. | Explicit instruction and a PR-time hard check. | The test was genuinely wrong, as confirmed by the reference solution. |
| over-engineering | Over-engineering | `model:over_engineering` `metric:diff_much_larger_than_reference` | Model grader flags added abstractions; diff is much larger than the reference. | Simplicity guidance in repo instructions; size budget in the task. | The task required the extra structure. |
| inefficient-loops | Inefficient loops | `efficiency:max_tool_calls` `efficiency:max_cost_usd` `metric:repeated_reads` `detector:D8` | Tool-call count or cost above budget; repeated reads of the same file. | Stopping or planning change; caching tool results. | The task is larger than its budget. |
| benchmark-only-success | Benchmark-only success | `failed_source:real_failure` | Passes suite tasks, fails realistic edge-case tasks added from PRs. | New corpus tasks; broader graders. | The new tasks are ambiguous. |
<!-- taxonomy:end -->

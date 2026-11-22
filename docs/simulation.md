# Offline urgency comparison

Run this harness from a source checkout with Python 3.11–3.14. It needs only the standard library, including SQLite. It does not start Hermes, contact a provider, or read or write a real Kanban board.

## Tutorial: see urgency change the next task

1. From the checkout root, run:

   ```sh
   python tests/simulate_urgency.py --scenario pressure
   ```

   The JSON pair is `correct: true`. In `arms`, the baseline selects `routine` before `urgent` and verifies one of two tasks on time. The enabled arm selects `urgent` first and verifies both on time.
2. In the enabled `trace`, find `observation`, the two `forecast` records, `recommendation`, then `selected`. The recommendation records `urgent` and the baseline alternative, `routine`. Follow `urgent` to its `verified` event and compare its `verified_at` and `on_time` in both arms.

### What you built

A paired receipt for one synthetic schedule. The choice changed actual simulated dispatch order; it was not just advice printed in a prompt. See the [report reference](#report-reference) for the evidence behind that result.

## How to rerun all scenarios and inspect a failure

```sh
python tests/simulate_urgency.py > urgency-pairs.json
python -m json.tool urgency-pairs.json
python -m unittest discover -s tests -p test_simulation.py -v
```

The report contains `nominal`, `stalled`, and `pressure`. `nominal` includes a claim with no verification. In `stalled`, inspect `fault`, `watchdog_detected`, and `watchdog_action` rows. The last names the actual recommendation, the plan that supplied it, and why no action was applied. `held` and `unknown` must not appear in either selection list.

Exit 1 with a JSON report means an oracle disagreed. Compare the failing arm's `correctness` fields to `selection`, `watchdog_actions`, `detection_latency_seconds`, and `completion.tasks`; use `trace` to find the first divergence. An exception instead of a report means the run did not produce paired evidence. If imports fail, check that you are using the source checkout and a supported Python interpreter, not a packaged installation.

## Report reference

`schema_version` is `1`. With no `--scenario`, the command runs all three pairs; `--scenario` accepts `nominal`, `stalled`, or `pressure`. Exit 0 means all emitted pairs matched their oracles.

| Field | Meaning |
| --- | --- |
| `pairs[].scenario`, `arms[].arm` | Fixture name and either `baseline` or `enabled`. |
| `selection`, `trace` | Actual dispatch order and ordered UTC events. The enabled arm records the ready set, actual [forecast](forecast.md) levels and selection, and [watchdog](watchdog.md) incident and plan evidence. Scenario inputs are in `tests/simulate_urgency.py`. |
| `detection_latency_seconds` | For the delayed task, simulated seconds from delay injection to the first production `stalled` incident (1770 in the fixture). `null` means no qualifying detection was observed, including in the baseline; it is not zero latency. Unavailable-worker incidents are traced separately and do not enter this metric. |
| `watchdog_actions` | Rows of `[task, detector, action, disposition]`. `held` and `unknown` are fences; `unsupported` means the action has no simulated effect. `unknown_recommendation` means the expected choice was not in an observed production plan. No watchdog action is executed. |
| `correctness` | Compares selection, watchdog action/disposition (empty for no action), stall latency, and each task's on-time result to fixed fixture expectations. Pair `correct` requires both arms to agree with their own expectations. |
| `completion` | `verified_on_time` out of `total` fixture tasks, including held, unknown and claim-only tasks. `unverified` counts tasks without verified evidence. A task's `on_time` is `true` for verification at or before the deadline, `false` for verified late, and `null` without verification. |
| `quantiles.p50`, `quantiles.p90` | `total` fixture tasks, `missing` tasks without both a numeric prediction at dispatch and a verified duration, and `eligible` rows with `predicted_seconds`, `actual_seconds` (dispatch to verification), and inclusive `within`. The baseline never makes predictions. |
| `operations` | Harness counts of direct forecast calls, watchdog ticks, admitted worker observations, synthetic dispatches, artifact verification attempts, completion claims and forecast-driven selections (`recommendations`). These do not count internal monitor work. |
| `wall_clock_seconds_nondeterministic` | Elapsed real seconds per arm, including temporary database setup, I/O and cleanup, excluding JSON encoding. This is a measurement, not a performance bound; omit it when comparing deterministic reports. |

## Why the arms are paired

Both arms start with the same task order, evidence, clock instant and timestamped fault schedule, in separate fresh temporary stores. The clock advances in 30-second steps. An in-flight delay applies only when its target is running at the scheduled instant; the trace records whether it applied. Forced one-minute watchdog evaluations use the production detector rules, not real cron timing. The stall timer starts at the watchdog's first observation of running state; the report measures latency from the later injected delay.

The baseline always takes the next eligible task in fixture order. The enabled arm ranks the same ready set by the real forecast's urgency level, breaking ties by fixture order. That dispatch rule belongs to the simulator, not the plugin. Dispatch is the only effect urgency can cause here. Held tasks and tasks with unknown work never enter the ready set. A deterministic artifact must pass the real Ledger's verification before a task earns completion credit. A claim or a recommendation cannot stand in for that evidence.

These few fixtures exercise behavior, not statistical calibration or a general improvement in deadlines or overhead. They do not demonstrate live scheduling, recovery, semantic deliverable quality, or Desktop behavior.

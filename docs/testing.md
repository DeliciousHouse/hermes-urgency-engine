# Testing

## How to run the offline checks

From the repository root, with the Python interpreter described in [package metadata](../pyproject.toml) and a Node version from the [workflow matrix](../.github/workflows/tests.yml):

```sh
python tests/run_suite.py --partition all
node --experimental-vm-modules --test-reporter=spec --test tests/desktop.test.mjs
```

The Python runner prints selected and discovered counts, then runs the suite; `--list` inventories tests but does not execute them. `--partition fast` excludes the disk-backed restart case; use `--partition slow` for that case. A green fast partition alone is not the full suite. The Node command exercises renderer behavior with SDK and React fixtures, not an Electron window. Check exit, test counts and failures, not just the final line.

For a docs change, follow the README links and verify relative paths, anchors and sample commands. Start with the [offline forecast](forecast.md#calculate-your-first-forecast), then check the [synthetic inputs](../examples/README.md) in a disposable profile when a native runtime is available. Do not start cron against a real profile to verify a documentation example.

## How to validate a Hermes build

Use a provisioned Hermes interpreter and disposable homes. The [workflow](../.github/workflows/tests.yml) is the authoritative recipe for pinned host revisions, isolation, and exact public source installation. From a clean plugin source directory, run:

```sh
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

The second check permits dependency preparation in the validation environment. Read both outputs; a warning, skipped path, or successful source-only check is not a successful clean install. For host probes, use the commands in the [runtime](runtime.md#contributor-verification), [Kanban](kanban.md#contributor-verification), and [watchdog](watchdog.md#contributor-verification) guides, or the workflow's exact installed equivalents. Check that an installed probe ran from the intended source and profile before attributing the result to a candidate commit.

## What the checks establish

| Evidence | What it does not establish |
| --- | --- |
| Forecast, Ledger and runtime fixtures: rule outputs, persistence, faults and refusal paths with controlled inputs | Model compliance, real delivery improvements, or a capacity-aware plan |
| Renderer module checks and installed REST probes: component contracts and authenticated backend behavior in the tested host | Native Electron theme, keyboard, resize, reconnection and remote-profile acceptance |
| No-agent cron and supervised synthetic probes: detected incidents or bounded requested effects | Successful autonomous recovery, exactly-once remote delivery, or production behavior |

For independent Desktop acceptance, follow the [16-scenario matrix](desktop.md#acceptance-matrix) in a clean installed client. Record the exact plugin and host revisions, commands, exits and observable results, including how a wrong result would show up. Mark any unexecuted check explicitly. A docs review, source check or green CI run is not a catalog approval or a release decision.

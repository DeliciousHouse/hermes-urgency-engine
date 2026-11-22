# Troubleshooting

## How to diagnose a first run

1. In the intended profile, run `hermes plugins list`, then `hermes urgency --help`. If the command is missing, check plugin enablement, Hermes requirements and in-process CLI registration in the [native runtime setup](runtime.md#compatibility-and-installation). Start a fresh CLI invocation after changing enablement.
2. Run `hermes urgency diagnose`. `uninitialized` and schema `0` are expected before the first Ledger write; an unfiltered status with no items is not a failure. Create the [synthetic project](../examples/README.md) only in a disposable profile.
3. If Desktop is disconnected or says inventory unavailable, check the selected gateway and profile first. An unmeasured project is not a storage fault; it needs an exact project-ID match. Use [Desktop scope and setup](desktop.md#how-to) before changing any data.

## How to recover without duplicating a write

| What you see | What to do |
| --- | --- |
| `INVALID_INPUT` or `INVALID_CONFIG` | Check the documented field names, types, and setting location. For civil time in a daylight-saving gap or fold, choose an unambiguous time or use a matching explicit offset through the CLI. |
| `REVISION_CONFLICT` | Read the current entity and compare it with your draft. Apply only the intended change against the current revision. Do not substitute the new revision into a stale full-record payload. |
| Unknown result, timeout, or `IDEMPOTENCY_CONFLICT` | Inspect history and retain the request and its ID. Follow [Desktop save recovery](desktop.md#recover-from-a-save-whose-result-you-never-saw), [Ledger reconciliation](ledger.md#how-to-reconcile-a-failed-command), or [intervention reconciliation](interventions.md#how-to-pause-reconcile-and-roll-back). Do not invent a fresh ID to discover what happened. |
| `SOURCE_DENIED`, `PAUSED` or `CAPABILITY_UNAVAILABLE` | Check profile, binding, pause state and host capability. A current native run pointer blocks a supervised action even when its telemetry is missing. Do not bypass a refusal with a direct database write or broader access. |
| Watchdog job is installed but silent | Check scheduler state, pause, `last_success`, `next_due`, and coverage via `hermes urgency watchdog health`. A quiet tick can be normal; an old success or unknown coverage is not health. For `INSTALL_CONFLICT`, inspect `hermes cron list` and the owned launcher; do not create a second job. |
| `STORE_BUSY`, `CLOCK_ROLLBACK`, or `CAPACITY_EXCEEDED` | Wait for the other writer, correct the clock, or review the tracked scope and retention limits respectively. Retry only where safe, after checking the outcome of any uncertain write. |
| `UNSUPPORTED_SCHEMA` or `CORRUPT_STATE` | Stop writers. Preserve files and use the [Ledger backup guidance](ledger.md#how-to-back-up-and-restore). Do not change `user_version`, delete a store, or use an empty database as a repair. |

For a report, collect the exact revision, action, error code and diagnostic ID if present. Use [private reporting](../SECURITY.md) for security findings. Avoid sharing raw profiles or exports.

For a local audit, use `hermes urgency history` with the entity type and ID, or `hermes urgency watchdog export --table monitor_audit` for monitor audit pages. Both write to stdout. See the [watchdog command reference](watchdog.md#commands) for pagination; redacted output can still contain private task context.

## How to disable, upgrade or remove

1. Inspect or reconcile pending writes. If in use, pause [new supervised effects](interventions.md#how-to-pause-reconcile-and-roll-back) and [uninstall the owned watchdog job](watchdog.md#how-to-operate-pause-and-remove-the-monitor) **before** disabling the plugin. Confirm the job is gone from `hermes cron list`; preserve edited or unrelated assets.
2. Disable the plugin with `hermes plugins disable hermes-urgency-engine`. Stop remaining writers and back up the active profile's plugin data. Use SQLite's backup API for every initialized store, not a copy of a live main file alone. Preserve relevant configuration, owned artifacts and any external artifact directory separately.
3. For an upgrade, install a reviewed revision, enable it, and run `hermes urgency diagnose` in a fresh process. Confirm status/history before starting the monitor again. For removal, run `hermes plugins remove hermes-urgency-engine` and check `hermes plugins list`; source removal leaves persistent plugin data. Do not assume it revokes credentials owned by Hermes or deletes external artifacts.

For rollback, use code that reads the on-disk schema or restore a consistent pre-upgrade backup while writes are stopped. Retain the current data separately; restoring an older backup does not include subsequent writes. See [runtime recovery](runtime.md#how-to-disable-upgrade-or-recover) for validation commands.

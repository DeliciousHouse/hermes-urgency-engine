# Record a synthetic task

Use an empty, disposable Hermes profile with the plugin enabled. Follow the [native setup](../docs/runtime.md#compatibility-and-installation); run the commands below from this checkout, with that profile selected. These files describe Ledger work, not a live Kanban task. They do not install a cron job or authorize a recovery action.

## 1. Create a project and task

```sh
hermes urgency apply create_project --json-file examples/project.json
hermes urgency apply upsert_task --json-file examples/task.json
hermes urgency task example-board example-proof
```

The task read returns `ok: true` with revision `1`, a remaining estimate of `600` seconds, and separate verification and deployment buffers. No criterion has been verified. Repeating the exact request replays its recorded result; editing the sample while keeping its request ID is not a new update.

## 2. Inspect the profile configuration

[advisory-config.json](advisory-config.json) is an inert fragment in JSON (a YAML subset). It shows the nesting of the plugin settings. Do not replace your entire `config.yaml` with this fragment. After creating the project, merge only the settings you want into the selected disposable profile. The sample points advisory guidance at the synthetic project and leaves Kanban sync and watchdog activation off. It does not enable the plugin or remove any existing cron job.

If the CLI returns a missing-command, configuration or revision error, start with [Troubleshooting](../docs/troubleshooting.md).

## What you built

A project and task with a recorded estimate, without touching a canonical board. Continue with the [runtime tutorial](../docs/runtime.md#tutorial-from-a-commitment-to-byte-evidence) for targets and evidence, or try the [standalone forecast](../docs/forecast.md#calculate-your-first-forecast) without a Hermes profile.

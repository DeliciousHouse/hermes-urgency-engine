# Hermes Urgency Engine

Track whether a task has enough time left for work, verification, and deployment. Hermes Urgency Engine records deadlines and evidence in the active Hermes profile and shows the results in the CLI and native Desktop. It keeps hard commitments, revisable soft targets, and forecasts separate: a later forecast does not move a deadline.

## Get started

- **Try it without Hermes:** [Calculate your first forecast](docs/forecast.md#calculate-your-first-forecast) from a source checkout. It needs Python, but no profile, model call, or network access.
- **Install and diagnose:** Follow the [native runtime setup](docs/runtime.md#compatibility-and-installation) with a reviewed commit and a disposable Hermes profile. `hermes urgency diagnose` reporting `uninitialized` is normal before the first write.
- **Record a task:** Use the [synthetic project and task files](examples/README.md), then try the [runtime tutorial](docs/runtime.md#tutorial-from-a-commitment-to-byte-evidence) for deadlines and verification.
- **Open Desktop:** Follow the [unified plugin setup](docs/desktop.md#install-the-desktop-page). The portfolio and timeline show what the selected gateway and profile can report; a project without a matching measurement stays unmeasured.

## Operate the plugin

- [Runtime commands and profile settings](docs/runtime.md) · [Ledger and evidence](docs/ledger.md) · [Forecast API and policy](docs/forecast.md)
- [Desktop routes, controls and scope](docs/desktop.md) · [Canonical Kanban synchronization](docs/kanban.md)
- [No-agent watchdog](docs/watchdog.md) · [Supervised interventions](docs/interventions.md) · [Troubleshooting and safe recovery](docs/troubleshooting.md)

## What it does on your machine

- **Files:** state lives in the profile's `plugin-data/hermes-urgency-engine/` SQLite stores. The opt-in `hermes urgency watchdog install` also writes one launcher, `scripts/hermes-urgency-engine-watchdog.py`, into the Hermes home, outside plugin data; `uninstall` removes it.
- **Background job:** that install registers one no-agent Hermes cron job that runs every minute. Nothing runs in the background until you install it, and the plugin installs no OS service.
- **Child processes:** the launcher runs same-interpreter `hermes_cli.main plugins list --json` and `urgency watchdog tick` children. Opt-in Kanban sync and supervised priority changes run same-interpreter `hermes_cli.main kanban ...` children with bounded time and output. None of these are agents.
- **Model context and network:** pre-turn guidance, which can include task and Kanban card text, enters the conversation and reaches your inference provider on a normal model turn. The plugin makes no third-party network calls and sends no telemetry. Desktop, remote cron delivery and the optional `scripts/gateway_liveness.py` probe (one request to `127.0.0.1`) use connections you configure. Details: [process and network boundaries](docs/architecture.md#process-and-network-boundaries).

## Know the limits

A matching artifact hash is evidence of bytes, not independent acceptance. Bound project and milestone forecasts use fresh canonical dependencies and [operator-declared execution capacity](docs/ledger.md#how-to-configure-a-synced-commitment-forecast); the dependency-only lower bound remains a separate diagnostic. The watchdog records incidents and recommends next steps; it does not perform autonomous recovery. Supported supervised actions are bounded to priority, remaining-work estimates, and soft-target revisions. External task-system integrations such as GitHub Issues and YouTrack are not included.

## Contribute

[Architecture and threat model](docs/architecture.md) · [Testing and evidence limits](docs/testing.md) · [Contributing](CONTRIBUTING.md) · [Security reporting](SECURITY.md) · [Changelog](CHANGELOG.md)

Licensed under [Apache 2.0](LICENSE).

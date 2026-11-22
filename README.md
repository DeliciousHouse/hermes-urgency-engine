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

## Know the limits

A matching artifact hash is evidence of bytes, not independent acceptance. Bound project and milestone forecasts use fresh canonical dependencies and [operator-declared execution capacity](docs/ledger.md#how-to-configure-a-synced-commitment-forecast); the dependency-only lower bound remains a separate diagnostic. The watchdog records incidents and recommends next steps; it does not perform autonomous recovery. Supported supervised actions are bounded to priority, remaining-work estimates, and soft-target revisions. External task-system integrations such as GitHub Issues and YouTrack are not included.

## Contribute

[Architecture and threat model](docs/architecture.md) · [Testing and evidence limits](docs/testing.md) · [Contributing](CONTRIBUTING.md) · [Security reporting](SECURITY.md) · [Changelog](CHANGELOG.md)

Licensed under [Apache 2.0](LICENSE).

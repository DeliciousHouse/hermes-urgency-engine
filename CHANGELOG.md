# Changelog

## Unreleased

- Track hard deadlines, soft targets, and changing forecasts separately. Inspect recorded commitments and byte-verified progress through the native Hermes runtime and Desktop.
- Opt in to scoped Kanban synchronization and no-agent watchdog checks. Review incident plans and their evidence before choosing a supervised action.
- Install the native plugin in several profiles of one Hermes home without breaking its shared environment: the plugin is no longer a buildable workspace member and no longer ships a top-level `scripts` package.
- Honour `hermes pause` without importing the private `agent.estop` module, so `hermes plugins validate` no longer warns about a private core import. The stop check now refuses when the pause state cannot be determined.
- Find setup, examples, architecture, troubleshooting, and validation from the [README](README.md).

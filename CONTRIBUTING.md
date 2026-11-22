# Contributing

## How to validate a change

1. Work from a source checkout. Use a Python interpreter that meets [the package requirements](pyproject.toml). For renderer tests, use a Node version from the [CI matrix](.github/workflows/tests.yml). The offline checks do not need a Hermes profile or API credentials.
2. Follow [Testing](docs/testing.md). For a behavioral change, add a focused regression that fails before the fix, then run the affected checks. For documentation, follow the links from the README and run the safe examples as written. Use a disposable profile for anything that writes or runs a scheduler.
3. Open a pull request with the problem, the scope of the diff, the commands and results, and what was not tested. Separate fixture, native-host, and installed Desktop evidence. An automated run is not independent acceptance.

Keep Hermes integration on supported extension APIs. Do not write directly to host databases, weaken a check to make it pass, or publish real boards, profiles, task text, logs, artifacts, or credentials as examples. Discuss sensitive findings via [private reporting](SECURITY.md).

Update the canonical guide for a changed interface, and record user-visible changes under [Unreleased](CHANGELOG.md). See [Architecture](docs/architecture.md) for the source and state map.

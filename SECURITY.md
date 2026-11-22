# Security policy

## Report a vulnerability privately

Use GitHub's [private vulnerability reporting form](https://github.com/DeliciousHouse/hermes-urgency-engine/security/advisories/new). Do not use a public issue or pull request for an unresolved vulnerability. Include the exact plugin commit, Hermes build and affected surface, expected versus observed behavior, and the impact. Use synthetic inputs and share only the minimum necessary details. Do not attach credentials, profile backups, raw logs, or private task data.

Use the [troubleshooting guide](docs/troubleshooting.md) for routine operational problems. This policy does not designate supported release lines or promise a response time.

## Run with least privilege

Install only source you have reviewed and pin the commit. Start in a disposable profile, use the default advisory intervention mode, and grant access only to the board, delivery destination, and artifact directory you intend to use. Treat exports and backups as sensitive even when command output is redacted. Hermes authentication and application scope checks do not sandbox code running as the local OS user. See the [threat model](docs/architecture.md#threat-model) for the trust assumptions and residual risks.

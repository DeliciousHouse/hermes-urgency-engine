# Architecture

Urgency Engine separates the work plan, the forecast, and the evidence that earns progress. A user can inspect a worsening forecast without moving a commitment or granting the engine additional authority.

## Data flow

```text
CLI / slash / tool / hook
  -> Runtime -> Ledger -> Forecast
Desktop SDK
  +-> Hermes REST -> Ledger
  +-> Host RPC -> Canonical projects
Opt-in Kanban -> Scoped reads
  -> Sync store -> Ledger + Monitor
No-agent cron -> Launcher -> Tick
  -> Monitor -> Hermes delivery
Supervised CLI -> Journal
  -> Ledger or Kanban -> Readback
```

The [forecast core](forecast.md) takes an explicit evaluation time and does not save state. The Ledger uses remaining estimates plus verification and deployment buffers. Unknown work stays unknown; samples, where supplied to the standalone core, are not proof of calibrated production outcomes. The [Desktop renderer](desktop.md) presents backend measurements and makes scoped requests; it does not calculate urgency or infer verified progress from run time. Matching uses exact project identity, not names.

## State ownership

| Owner | State | Boundary |
| --- | --- | --- |
| Hermes | Profile configuration, projects, canonical Kanban tasks/runs and cron jobs | Host owns authentication, dispatch and canonical state. Sync does not turn the Ledger into a Kanban database. |
| [Ledger](ledger.md) | `data.db`: hard and soft targets, original baselines, scope, criteria, verification receipts and audit | Revisions protect against stale edits. Evidence is tied to current criteria. Reopening or changing criteria can remove credit. |
| [Runtime](runtime.md) | `state.json`: expiring conversation focus; plugin-owned diagnostic logs and default artifact directory | Focus is session-scoped; an explicitly configured fallback is per profile. Reads do not initialize a missing Ledger. |
| [Kanban Sync](kanban.md) | `kanban.db`: scoped source projection, cursors and applied observations | Stale, withdrawn, or incomplete coverage is not a current board snapshot or proof of recovery. |
| [Watchdog Monitor](watchdog.md) | `watchdog.db`: observations, incidents, plans, notice accounting and lifecycle history | Deduplication survives restart. Missing telemetry does not clear a known incident. Generated output does not prove delivery. |
| [Intervention journal](interventions.md) | `interventions.db`: requests, effects and reconciliation receipts | The intent is retained before the effect; there is no transaction spanning the journal, Ledger, and native Kanban. |
| [Desktop](desktop.md) | `desktop.db`: opaque profile identity; renderer retains pending command intent for recovery | Identity protects routing, not authorization. Do not switch profiles to retry a pending save. |

SQLite stores follow the active Hermes home through plugin storage. SQLite can maintain WAL coordination files even when existing state is read. The Ledger validates schema 1 and upgrades it atomically on write to schema 2; read-only access does not migrate. A schema-1-only build cannot read upgraded state. Use [consistent backups and the removal procedure](troubleshooting.md#how-to-disable-upgrade-or-remove).

## Process and network boundaries

- **Runtime:** CLI, conversational commands, tools, and hooks run at Hermes dispatch boundaries. The local CLI needs in-process plugin registration. Pre-turn context is advisory; it cannot interrupt inference. Relevant observations and session cleanup are registered as hooks, not a separate monitor process.
- **Watchdog and source access:** The explicitly installed no-agent cron job runs an owned Python launcher that checks plugin state and invokes the CLI tick. Native Kanban operations may spawn bounded, same-interpreter CLI children; these are not agents or persistent workers. Hermes must be scheduling jobs for unattended ticks. The plugin installs no OS supervisor.
- **Network:** Core forecast, SQLite, local CLI and local watchdog delivery do not require a separate network service or plugin API key. Source installation fetches from GitHub through Hermes. Desktop uses its selected Hermes connection for REST, canonical project RPC and supported host event notifications; remote connections carry profile data to the authenticated client. Configured remote cron delivery uses host channels and their credentials. Pre-turn guidance enters the host conversation and can be sent to the configured inference provider on a normal model turn; the plugin does not initiate a separate model call.

## Threat model

Protect task text, evidence metadata, commitments, audit and exported observations. Trust the installed code, host authentication and profile selection, the OS account, and authorized operators. A compromised host, same-user process, malicious plugin, or copied profile directory is outside the security boundary of these checks.

| Risk | Control and residual exposure |
| --- | --- |
| Cross-profile read or write | Desktop reads follow the host-selected profile. The renderer discards stale replies on scope changes. The write adapter checks the submitted namespace when provided and rejects caller-selected actor, database or home. Keep Hermes authenticated; the namespace is not a credential. |
| Task text or model output treated as authority | Context labels task text as untrusted and runs host redaction. The tool reads; conversational focus can only select a local project for guidance. Advice does not grant permissions. Redaction does not remove all private business context. |
| Reading arbitrary files | Local verification accepts paths relative to the configured artifact root and refuses escapes and symlinks. Use a dedicated directory. REST does not offer artifact verification or return artifact paths or contents. |
| Stale edits, duplicate writes, unconfirmed effects | Revisions and request IDs protect local writes. Desktop retains uncertain intent for inspection. Reconcile before retrying; native Kanban priority is last-writer-wins, not a compare-and-swap. Pause stops new admissions, not effects already issued. |
| Unapproved recovery or deadline changes | Supervised actions are explicit local operator requests with scope and active-run checks. Hard deadline changes need an audited operator revision; the forecast cannot revise them. Recovery-card creation is unavailable. |
| Stale or expensive inputs | Paging, graph and payload limits and bounded child calls constrain work. Incomplete observations stay unknown. Cooperative budgets and retention do not guarantee host uptime or a disk quota. |

See [Security](../SECURITY.md) for responsible reporting. For measured limitations, see [Testing](testing.md) and the [Desktop acceptance matrix](desktop.md#acceptance-matrix).

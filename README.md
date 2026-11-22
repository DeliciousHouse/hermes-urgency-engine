# Hermes Urgency Engine

Calculate whether a task has enough time left for work, verification, and deployment. The result is repeatable: you supply the evaluation time and, optionally, the last urgency state and historical completion durations.

This development plugin exposes a local deadline/progress ledger and advisory pre-turn guidance through native Hermes commands, a read-only tool, and a native Hermes Desktop page. It uses Python's standard library (date entry also needs system IANA timezone data or the `tzdata` package). The ledger stores commitments, scope revisions and byte-identity evidence; it can opt in to scoped canonical Kanban synchronization, but does not schedule work or measure empirical completion rates. Matching bytes is not independent semantic quality acceptance.

The Desktop page is a control surface, not a static chart: it lists projects with their verified progress and measured deadline risk, drills into one project's commitment timeline, milestones, canonical source tasks, audit history and intervention receipts, and creates or reschedules projects, milestones and deadlines through the same audited, revision-checked Ledger operations the CLI uses. Every number it shows is read from the backend; the renderer performs no progress or risk arithmetic of its own.

The optional watchdog uses Hermes no-agent cron to record incidents and recommend next steps. It does not execute recovery actions or treat missing telemetry as proof of health.

In supervised mode, a local operator can change a bound Kanban task's priority, revise its remaining estimate, or move a soft target. Recovery-card creation and autonomous execution are unavailable.

- [Desktop page: routes, controls, scope limits, how-to and synthetic tutorial](docs/desktop.md)
- [Canonical Kanban synchronization, scope and operating guide](docs/kanban.md)
- [Independent watchdog, incidents, plans and no-agent tutorial](docs/watchdog.md)
- [Supervised interventions, outcomes and rollback](docs/interventions.md)
- [Native installation, commands, configuration and executable tutorial](docs/runtime.md)
- [Ledger API, evidence limits and temporary-database tutorial](docs/ledger.md)
- [Forecast API and policy](docs/forecast.md#forecast-api)
- [Calculate your first forecast](docs/forecast.md#calculate-your-first-forecast)
- [How to run the regression tests](docs/forecast.md#how-to-run-the-regression-tests)

Licensed under [Apache 2.0](LICENSE).

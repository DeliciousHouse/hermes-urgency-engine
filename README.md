# Hermes Urgency Engine

Calculate whether a task has enough time left for work, verification, and deployment. The result is repeatable: you supply the evaluation time and, optionally, the last urgency state and historical completion durations.

This is a development forecast core and local deadline/progress ledger, **not an installable Hermes plugin**. It uses Python's standard library (date entry also needs system IANA timezone data or the `tzdata` package). The ledger stores commitments, scope revisions and byte-identity evidence; it does not schedule work, synchronize Hermes tasks, provide a Desktop page, or measure empirical completion rates.

- [Ledger API, evidence limits and temporary-database tutorial](docs/ledger.md)
- [Forecast API and policy](docs/forecast.md#forecast-api)
- [Calculate your first forecast](docs/forecast.md#calculate-your-first-forecast)
- [How to run the regression tests](docs/forecast.md#how-to-run-the-regression-tests)

Licensed under [Apache 2.0](LICENSE).

# Forecast API

Import the public API from `urgency_engine`. All four data classes are frozen. `calculate_forecast(task, *, now, policy=UrgencyPolicy(), previous=None) -> Forecast` has no ambient clock and does not save state.

| Type | Fields |
|---|---|
| `ForecastInput` | `deadline: datetime`, `remaining_work_seconds: float`, `verification_buffer_seconds: float = 0`, `deployment_buffer_seconds: float = 0`, `completion_samples_seconds: tuple[float, ...] = ()` |
| `UrgencyPolicy` | `elevated_slack_ratio: float = 1.0`, `high_slack_ratio: float = 0.25`, `imminent_seconds: float = 900`, `deescalation_cooldown_seconds: float = 300`, `deescalation_margin_seconds: float = 60`, `min_distribution_samples: int = 5` |
| `UrgencyState` | `level: UrgencyLevel`, `changed_at: datetime` |
| `Forecast` | `time_remaining_seconds: float`, `deadline_slack_seconds: float`, `effective_slack_seconds: float`, `p50_seconds: float \| None`, `p90_seconds: float \| None`, `on_time_probability: float \| None`, `sample_count: int`, `evidence: str`, `raw_level: UrgencyLevel`, `level: UrgencyLevel`, `changed_at: datetime`, `reasons: tuple[str, ...]` |

`now`, `deadline`, and any `previous.changed_at` must be timezone-aware and convertible to UTC. The previous instant cannot be later than `now`. Time arithmetic uses UTC, including when both datetimes share a timezone across a daylight-saving transition. Supply an explicit offset or resolved fold for ambiguous local times; the core does not parse strings or infer intent. Durations, samples and ratios accept finite nonnegative integers or floats, excluding booleans. The elevated ratio must exceed the high ratio. The sample minimum is an integer of at least 2, excluding booleans. Supply at most 10,000 samples in a tuple. Invalid fields raise a named `ValueError` during evaluation. `reasons` contains explanatory text, not machine-readable status codes.

## Slack and raw urgency

Let `T` be seconds from `now` to `deadline`, `W` estimated work seconds, `V` verification seconds, and `D` deployment seconds. The forecast returns `deadline_slack_seconds = T - W - V`. Deployment additionally reduces `effective_slack_seconds = T - W - V - D`.

With enough samples, `risk_work` is empirical P90; otherwise it is `W`. Define `risk_slack = T - risk_work - V - D`, `high_threshold = high_slack_ratio × max(risk_work, 1)` and `elevated_threshold = elevated_slack_ratio × max(risk_work, 1)`. The first matching rule wins:

| `UrgencyLevel` (`IntEnum`) | Condition |
|---|---|
| `OVERDUE_RECOVERY_REQUIRED` (6) | `T <= 0` |
| `DEADLINE_IMMINENT` (5) | `0 < T <= imminent_seconds` |
| `CRITICAL` (4) | `risk_slack < 0` |
| `HIGH` (3) | `risk_slack <= high_threshold` |
| `ELEVATED` (2) | `risk_slack <= elevated_threshold` |
| `NORMAL` (1) | Otherwise |

## Why the forecast can differ from your estimate

Each sample is a historical total remaining-work duration before buffers. At or above the configured sample minimum, the core sorts the samples and interpolates between adjacent values at zero-based index `(n - 1) * q` for `q = 0.5` and `q = 0.9`. The on-time estimate is the fraction satisfying `sample + V + D <= T`, including equality. The result is tagged `empirical`. It describes the supplied history, not a calibrated prediction or statistical guarantee. Unrepresentative samples can make it misleading. Below the minimum, evidence is `insufficient_data`; all three distribution metrics are `None` and `sample_count` still reports the number supplied.

A small improvement in slack should not repeatedly change the active level. Escalation follows `raw_level` immediately. Deescalation requires both elapsed time **at least** the cooldown and the following strict exit rule for the **previous** level, even when the raw level crosses several levels at once:

| Leaving | Required improvement |
|---|---|
| Overdue | `T > margin` |
| Imminent | `T > imminent_seconds + margin` |
| Critical | `risk_slack > margin` |
| High | `risk_slack > high_threshold + margin` |
| Elevated | `risk_slack > elevated_threshold + margin` |

Here `margin` is `deescalation_margin_seconds`. An equal or held level preserves `previous.changed_at`; otherwise `changed_at` is the supplied `now`. A hold delays relief after a recovery, but does not delay an escalation. To use this behavior across calls, pass `UrgencyState(result.level, result.changed_at)` as `previous` on the next evaluation. The caller owns that state and stops scheduling terminal, verified tasks. Zero estimated work alone does not mean complete.

## Calculate your first forecast

From the checkout root, open a Python 3.11–3.14 interpreter with `python`. Paste this example of a task with 30 minutes left:

```python
from datetime import datetime, timedelta, timezone
from urgency_engine import ForecastInput, calculate_forecast

now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
task = ForecastInput(now + timedelta(minutes=30), 1200, 120, 60)
result = calculate_forecast(task, now=now)
print(result.level.name, result.deadline_slack_seconds, result.effective_slack_seconds)
```

It prints `ELEVATED 480.0 420.0`. You computed a deadline budget with separate verification and deployment reserves. Reuse the same `now` to reproduce the result, or supply a later explicit evaluation time for a new forecast.

## How to run the regression tests

From the checkout root, with Python 3.11–3.14, run:

```sh
python -m unittest discover -s tests -v
```

Confirm the command reports a nonzero test count and `OK`. No network, runtime installation, or Hermes profile is needed. If no tests are found, confirm the current directory contains `tests/test_forecast.py`; if `urgency_engine` cannot be imported, run the command from the checkout root.

[Back to the project README](../README.md)

"""Deterministic deadline urgency policy."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from math import isfinite


class UrgencyLevel(IntEnum):
    NORMAL = 1
    ELEVATED = 2
    HIGH = 3
    CRITICAL = 4
    DEADLINE_IMMINENT = 5
    OVERDUE_RECOVERY_REQUIRED = 6


@dataclass(frozen=True)
class UrgencyPolicy:
    elevated_slack_ratio: float = 1.0
    high_slack_ratio: float = 0.25
    imminent_seconds: float = 900
    deescalation_cooldown_seconds: float = 300
    deescalation_margin_seconds: float = 60
    min_distribution_samples: int = 5


@dataclass(frozen=True)
class ForecastInput:
    deadline: datetime
    remaining_work_seconds: float | None
    verification_buffer_seconds: float = 0
    deployment_buffer_seconds: float = 0
    completion_samples_seconds: tuple[float, ...] = ()


@dataclass(frozen=True)
class UrgencyState:
    level: UrgencyLevel
    changed_at: datetime


@dataclass(frozen=True)
class Forecast:
    time_remaining_seconds: float
    deadline_slack_seconds: float | None
    effective_slack_seconds: float | None
    p50_seconds: float | None
    p90_seconds: float | None
    on_time_probability: float | None
    sample_count: int
    evidence: str
    raw_level: UrgencyLevel
    level: UrgencyLevel
    changed_at: datetime
    reasons: tuple[str, ...]


def _quantile(samples: list[float], q: float) -> float:
    index = (len(samples) - 1) * q
    lower = int(index)
    return samples[lower] + (samples[lower + 1] - samples[lower]) * (index - lower)


def _utc(value: datetime, field: str) -> datetime:
    if isinstance(value, datetime):
        try:
            if value.utcoffset() is not None:
                return value.astimezone(timezone.utc)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be convertible to UTC") from exc
    raise ValueError(f"{field} must be a timezone-aware datetime")


def _nonnegative(value: float, field: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
            if isfinite(number) and number >= 0:
                return number
        except OverflowError:
            pass
    raise ValueError(f"{field} must be a finite, nonnegative number")


def _validate_policy(policy: UrgencyPolicy) -> None:
    for field in (
        "elevated_slack_ratio", "high_slack_ratio", "imminent_seconds",
        "deescalation_cooldown_seconds", "deescalation_margin_seconds",
    ):
        _nonnegative(getattr(policy, field), f"policy.{field}")
    if policy.elevated_slack_ratio <= policy.high_slack_ratio:
        raise ValueError("policy.elevated_slack_ratio must exceed policy.high_slack_ratio")
    minimum = policy.min_distribution_samples
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 2:
        raise ValueError("policy.min_distribution_samples must be an integer of at least 2")


def _raw_level(time_left: float, slack: float, imminent: float, high: float,
               elevated: float) -> UrgencyLevel:
    if time_left <= 0:
        return UrgencyLevel.OVERDUE_RECOVERY_REQUIRED
    if time_left <= imminent:
        return UrgencyLevel.DEADLINE_IMMINENT
    if slack < 0:
        return UrgencyLevel.CRITICAL
    if slack <= high:
        return UrgencyLevel.HIGH
    if slack <= elevated:
        return UrgencyLevel.ELEVATED
    return UrgencyLevel.NORMAL


def calculate_forecast(
    task: ForecastInput, *, now: datetime, policy: UrgencyPolicy = UrgencyPolicy(),
    previous: UrgencyState | None = None,
) -> Forecast:
    """Calculate urgency from caller-supplied inputs without reading a clock."""
    now_utc = _utc(now, "now")
    time_left = (_utc(task.deadline, "deadline") - now_utc).total_seconds()
    elapsed = 0.0
    if previous is not None:
        if not isinstance(previous.level, UrgencyLevel):
            raise ValueError("previous.level must be an UrgencyLevel")
        elapsed = (now_utc - _utc(previous.changed_at, "previous.changed_at")).total_seconds()
        if elapsed < 0:
            raise ValueError("previous.changed_at must not be later than now")
    _validate_policy(policy)
    work = None if task.remaining_work_seconds is None else _nonnegative(task.remaining_work_seconds, "remaining_work_seconds")
    verification = _nonnegative(task.verification_buffer_seconds, "verification_buffer_seconds")
    deployment = _nonnegative(task.deployment_buffer_seconds, "deployment_buffer_seconds")
    durations = task.completion_samples_seconds
    if not isinstance(durations, tuple) or len(durations) > 10_000:
        raise ValueError("completion_samples_seconds must be a tuple of at most 10000 durations")
    samples = [
        _nonnegative(sample, f"completion_samples_seconds[{i}]")
        for i, sample in enumerate(durations)
    ]
    if work is None:
        raw = (UrgencyLevel.OVERDUE_RECOVERY_REQUIRED if time_left <= 0 else
               UrgencyLevel.DEADLINE_IMMINENT if time_left <= policy.imminent_seconds else
               UrgencyLevel.ELEVATED)
        level, changed_at = raw, now
        if previous is not None and previous.level >= raw:
            level, changed_at = previous.level, previous.changed_at
        return Forecast(time_left, None, None, None, None, None, len(samples),
                        'insufficient_evidence', raw, level, changed_at,
                        ('Remaining work is unknown; samples cannot replace a missing estimate.',))
    slack = time_left - work - verification
    effective = slack - deployment
    count = len(samples)
    if count >= policy.min_distribution_samples:
        samples.sort()
        p50 = _quantile(samples, 0.5)
        p90 = _quantile(samples, 0.9)
        probability = sum(sample + verification + deployment <= time_left for sample in samples) / count
        evidence = "empirical"
        risk_work = p90
        risk_reason = f"Using empirical P90 ({p90:g} s) from {count} samples."
    else:
        p50 = p90 = probability = None
        evidence = "insufficient_data"
        risk_work = work
        risk_reason = (f"Insufficient evidence ({count}/{policy.min_distribution_samples} samples); "
                       f"using estimated work ({work:g} s).")
    risk_slack = time_left - risk_work - verification - deployment
    high = policy.high_slack_ratio * max(risk_work, 1.0)
    elevated = policy.elevated_slack_ratio * max(risk_work, 1.0)
    raw = _raw_level(time_left, risk_slack, policy.imminent_seconds, high, elevated)
    reasons = (risk_reason, f"Risk slack: {risk_slack:g} s after {verification:g} s verification "
                            f"and {deployment:g} s deployment buffers.")
    level, changed_at = raw, now
    if previous is not None and raw == previous.level:
        changed_at = previous.changed_at
    elif previous is not None and raw < previous.level:
        observed, boundary = {
            UrgencyLevel.OVERDUE_RECOVERY_REQUIRED: (time_left, 0),
            UrgencyLevel.DEADLINE_IMMINENT: (time_left, policy.imminent_seconds),
            UrgencyLevel.CRITICAL: (risk_slack, 0),
            UrgencyLevel.HIGH: (risk_slack, high),
            UrgencyLevel.ELEVATED: (risk_slack, elevated),
        }[previous.level]
        held = ()
        if elapsed < policy.deescalation_cooldown_seconds:
            held += (f"Deescalation held by {previous.level.name} cooldown.",)
        if observed <= boundary + policy.deescalation_margin_seconds:
            held += (f"Deescalation held by {previous.level.name} exit margin.",)
        if held:
            level, changed_at = previous.level, previous.changed_at
            reasons += held
    return Forecast(time_left, slack, effective, p50, p90, probability, count, evidence,
                    raw, level, changed_at, reasons)

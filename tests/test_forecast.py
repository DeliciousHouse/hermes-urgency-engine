from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from struct import pack
import unittest
from zoneinfo import ZoneInfo


NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class ForecastTests(unittest.TestCase):
    def test_invalid_work_buffers_and_samples_name_the_field(self):
        from urgency_engine import ForecastInput, calculate_forecast

        task = ForecastInput(NOW, 1)
        for field in ("remaining_work_seconds", "verification_buffer_seconds",
                      "deployment_buffer_seconds", "completion_samples_seconds"):
            for invalid in (float("nan"), float("inf"), -1, True, "10", 10**400):
                with self.subTest(field=field, kind=type(invalid)):
                    value = (invalid,) if field == "completion_samples_seconds" else invalid
                    with self.assertRaisesRegex(ValueError, field):
                        calculate_forecast(replace(task, **{field: value}), now=NOW)

    def test_valid_sample_cap_is_inclusive(self):
        from urgency_engine import ForecastInput, calculate_forecast

        task = ForecastInput(NOW, 1, completion_samples_seconds=(0,) * 10000)
        self.assertEqual(calculate_forecast(task, now=NOW).sample_count, 10000)
        with self.assertRaisesRegex(ValueError, "completion_samples_seconds"):
            calculate_forecast(replace(task, completion_samples_seconds=(0,) * 10001), now=NOW)

    def test_invalid_policy_names_the_field(self):
        from urgency_engine import ForecastInput, UrgencyPolicy, calculate_forecast

        for field in ("imminent_seconds", "deescalation_cooldown_seconds",
                      "deescalation_margin_seconds", "high_slack_ratio", "elevated_slack_ratio"):
            for invalid in (float("nan"), float("inf"), -1, False):
                with self.subTest(field=field, value=invalid):
                    with self.assertRaisesRegex(ValueError, field):
                        calculate_forecast(ForecastInput(NOW, 1), now=NOW,
                                           policy=replace(UrgencyPolicy(), **{field: invalid}))
        for invalid in (1, 2.0, True):
            with self.subTest(minimum=invalid):
                with self.assertRaisesRegex(ValueError, "min_distribution_samples"):
                    calculate_forecast(ForecastInput(NOW, 1), now=NOW,
                                       policy=UrgencyPolicy(min_distribution_samples=invalid))
        for elevated in (0.125, 0.25):
            with self.subTest(elevated=elevated):
                with self.assertRaisesRegex(ValueError, "elevated_slack_ratio"):
                    calculate_forecast(ForecastInput(NOW, 1), now=NOW,
                                       policy=UrgencyPolicy(elevated_slack_ratio=elevated))

    def test_naive_timestamps_and_future_state_name_the_field(self):
        from urgency_engine import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast

        naive = NOW.replace(tzinfo=None)
        with self.assertRaisesRegex(ValueError, "now"):
            calculate_forecast(ForecastInput(NOW, 1), now=naive)
        with self.assertRaisesRegex(ValueError, "deadline"):
            calculate_forecast(ForecastInput(naive, 1), now=NOW)
        for stamp in (naive, NOW + timedelta(microseconds=1)):
            with self.subTest(stamp=stamp):
                with self.assertRaisesRegex(ValueError, r"previous\.changed_at"):
                    calculate_forecast(ForecastInput(NOW, 1), now=NOW,
                                       previous=UrgencyState(UrgencyLevel.NORMAL, stamp))

    def test_shared_zoneinfo_fall_back_uses_actual_elapsed_time(self):
        from urgency_engine import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast

        # Standard time -> DST in spring -> standard time in fall. No host tzdata.
        spring = int(datetime(2030, 3, 10, 7, tzinfo=timezone.utc).timestamp())
        fall = int(datetime(2030, 11, 3, 6, tzinfo=timezone.utc).timestamp())
        header = b"TZif\0" + bytes(15) + pack(">6l", 0, 0, 0, 2, 2, 8)
        records = pack(">llBB lBB lBB", spring, fall, 1, 0, -18000, 0, 0, -14400, 1, 4)
        zone = ZoneInfo.from_file(BytesIO(header + records + b"STD\0DST\0"))
        before = datetime(2030, 11, 3, 1, 45, tzinfo=zone, fold=0)
        after = datetime(2030, 11, 3, 1, 15, tzinfo=zone, fold=1)
        before_utc = datetime(2030, 11, 3, 5, 45, tzinfo=timezone.utc)
        after_utc = datetime(2030, 11, 3, 6, 15, tzinfo=timezone.utc)
        self.assertEqual(before.utcoffset(), timedelta(hours=-4))
        self.assertEqual(after.utcoffset(), timedelta(hours=-5))
        local = calculate_forecast(ForecastInput(after, 60), now=before)
        utc = calculate_forecast(ForecastInput(after_utc, 60), now=before_utc)
        self.assertEqual(local.time_remaining_seconds, 1800)
        self.assertEqual(local.deadline_slack_seconds, utc.deadline_slack_seconds)
        self.assertIs(local.level, utc.level)
        previous = UrgencyState(UrgencyLevel.CRITICAL, before)
        deadline = after_utc + timedelta(hours=1)
        self.assertIs(calculate_forecast(ForecastInput(deadline, 60), now=after,
                                        previous=previous).level, UrgencyLevel.NORMAL)
        with self.assertRaisesRegex(ValueError, r"previous\.changed_at"):
            calculate_forecast(ForecastInput(deadline, 60), now=before,
                               previous=replace(previous, changed_at=after))

    def test_buffers_reduce_the_two_slack_values_separately(self):
        from urgency_engine import ForecastInput, calculate_forecast

        result = calculate_forecast(
            ForecastInput(NOW + timedelta(seconds=1800), 1000, 75, 25), now=NOW
        )
        self.assertEqual(result.time_remaining_seconds, 1800)
        self.assertEqual(result.deadline_slack_seconds, 725)
        self.assertEqual(result.effective_slack_seconds, 700)

    def test_empirical_quantiles_and_inclusive_probability_use_both_buffers(self):
        from urgency_engine import ForecastInput, UrgencyLevel, calculate_forecast

        for samples in ((100, 500, 1000, 1500, 2500), (2500, 1500, 100, 1000, 500)):
            with self.subTest(samples=samples):
                result = calculate_forecast(
                    ForecastInput(NOW + timedelta(seconds=1700), 100, 80, 120, samples),
                    now=NOW,
                )
                self.assertEqual(result.p50_seconds, 1000)
                self.assertEqual(result.p90_seconds, 2100)
                self.assertEqual(result.on_time_probability, 0.8)
                self.assertEqual(result.sample_count, 5)
                self.assertEqual(result.evidence, "empirical")
                self.assertEqual(result.effective_slack_seconds, 1400)
                self.assertIs(result.level, UrgencyLevel.CRITICAL)
                self.assertIn("P90", " ".join(result.reasons))

    def test_below_minimum_samples_leaves_all_distribution_values_unknown(self):
        from urgency_engine import ForecastInput, UrgencyLevel, calculate_forecast

        for samples in ((), (4000,) * 4):
            with self.subTest(samples=samples):
                result = calculate_forecast(
                    ForecastInput(NOW + timedelta(seconds=3000), 100,
                                  completion_samples_seconds=samples), now=NOW
                )
                self.assertEqual(result.sample_count, len(samples))
                self.assertEqual(result.evidence, "insufficient_data")
                self.assertEqual((result.p50_seconds, result.p90_seconds, result.on_time_probability),
                                 (None, None, None))
                self.assertIs(result.level, UrgencyLevel.NORMAL)
                self.assertIn("Insufficient", " ".join(result.reasons))

    def test_policy_minimum_controls_when_to_interpolate(self):
        from urgency_engine import ForecastInput, UrgencyPolicy, calculate_forecast

        result = calculate_forecast(
            ForecastInput(NOW + timedelta(seconds=1500), 100,
                          completion_samples_seconds=(2000, 1000)),
            now=NOW, policy=UrgencyPolicy(min_distribution_samples=2),
        )
        self.assertEqual((result.p50_seconds, result.p90_seconds, result.on_time_probability),
                         (1500, 1900, 0.5))

    def test_escalation_is_immediate_and_equal_level_keeps_timestamp(self):
        from urgency_engine import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast

        changed = NOW - timedelta(seconds=1)
        task = ForecastInput(NOW + timedelta(seconds=1100), 1000)
        for level, stamp in ((UrgencyLevel.NORMAL, NOW), (UrgencyLevel.HIGH, changed)):
            with self.subTest(previous=level):
                result = calculate_forecast(task, now=NOW, previous=UrgencyState(level, changed))
                self.assertIs(result.level, UrgencyLevel.HIGH)
                self.assertEqual(result.changed_at, stamp)

    def test_each_previous_level_owns_its_strict_exit_margin(self):
        from urgency_engine import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast

        cases = (
            (UrgencyLevel.OVERDUE_RECOVERY_REQUIRED, 60, 1000, UrgencyLevel.DEADLINE_IMMINENT),
            (UrgencyLevel.DEADLINE_IMMINENT, 960, 1000, UrgencyLevel.CRITICAL),
            (UrgencyLevel.CRITICAL, 1060, 1000, UrgencyLevel.HIGH),
            (UrgencyLevel.HIGH, 1310, 1000, UrgencyLevel.ELEVATED),
            (UrgencyLevel.ELEVATED, 2060, 1000, UrgencyLevel.NORMAL),
            (UrgencyLevel.HIGH, 60.25, 0, UrgencyLevel.NORMAL),
        )
        for level, boundary, work, raw in cases:
            for delta in (-0.125, 0, 0.125):
                with self.subTest(previous=level, delta=delta, work=work):
                    policy = self._policy_for_work(work)
                    previous = UrgencyState(level, NOW - timedelta(seconds=300))
                    task = ForecastInput(NOW + timedelta(seconds=boundary + delta), work)
                    result = calculate_forecast(task, now=NOW, previous=previous, policy=policy)
                    self.assertIs(result.raw_level, raw)
                    self.assertIs(result.level, raw if delta > 0 else level)
                    self.assertEqual(result.changed_at, NOW if delta > 0 else previous.changed_at)
                    if delta <= 0:
                        self.assertIn("margin", " ".join(result.reasons))

    @staticmethod
    def _policy_for_work(work):
        from urgency_engine import UrgencyPolicy

        return UrgencyPolicy() if work else UrgencyPolicy(imminent_seconds=0)

    def test_work_decrease_waits_for_cooldown_then_stays_at_the_new_timestamp(self):
        from urgency_engine import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast

        deadline = NOW + timedelta(seconds=2000)
        original = calculate_forecast(ForecastInput(deadline, 2001), now=NOW)
        previous = UrgencyState(original.level, original.changed_at)
        reduced = ForecastInput(deadline, 500)
        for elapsed in (299, 300, 301):
            with self.subTest(elapsed=elapsed):
                now = NOW + timedelta(seconds=elapsed)
                result = calculate_forecast(reduced, now=now, previous=previous)
                self.assertIs(result.raw_level, UrgencyLevel.NORMAL)
                self.assertIs(result.level, UrgencyLevel.NORMAL if elapsed >= 300 else previous.level)
                self.assertEqual(result.changed_at, now if elapsed >= 300 else NOW)
                if elapsed < 300:
                    self.assertIn("cooldown", " ".join(result.reasons))
                else:
                    state = UrgencyState(result.level, result.changed_at)
                    repeated = calculate_forecast(reduced, now=now, previous=state)
                    self.assertEqual(repeated, result)
                    self.assertEqual(
                        calculate_forecast(reduced, now=now + timedelta(seconds=1),
                                           previous=state).changed_at, now
                    )

    def test_extension_can_leave_overdue_and_zero_work_does_not_mark_completion(self):
        from urgency_engine import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast

        task = ForecastInput(NOW, 0)
        first = calculate_forecast(task, now=NOW)
        self.assertIs(first.level, UrgencyLevel.OVERDUE_RECOVERY_REQUIRED)
        extended = replace(task, deadline=NOW + timedelta(hours=1))
        now = NOW + timedelta(minutes=5)
        result = calculate_forecast(extended, now=now,
                                    previous=UrgencyState(first.level, first.changed_at))
        self.assertIs(result.level, UrgencyLevel.NORMAL)
        self.assertEqual(result.changed_at, now)

    def test_raw_levels_at_each_boundary(self):
        from urgency_engine import ForecastInput, UrgencyLevel, calculate_forecast

        cases = (
            (-1, "OVERDUE_RECOVERY_REQUIRED"), (0, "OVERDUE_RECOVERY_REQUIRED"),
            (1, "DEADLINE_IMMINENT"), (899, "DEADLINE_IMMINENT"),
            (900, "DEADLINE_IMMINENT"), (901, "CRITICAL"),
            (999, "CRITICAL"), (1000, "HIGH"), (1001, "HIGH"),
            (1249, "HIGH"), (1250, "HIGH"), (1251, "ELEVATED"),
            (1999, "ELEVATED"), (2000, "ELEVATED"), (2001, "NORMAL"),
        )
        for seconds, name in cases:
            with self.subTest(seconds=seconds):
                result = calculate_forecast(
                    ForecastInput(NOW + timedelta(seconds=seconds), 1000), now=NOW
                )
                self.assertIs(result.raw_level, UrgencyLevel[name])
                self.assertIs(result.level, result.raw_level)
                self.assertEqual(result.changed_at, NOW)

    def test_distant_deadline_and_buffers_still_allow_critical(self):
        from urgency_engine import ForecastInput, UrgencyLevel, calculate_forecast

        deadline = NOW + timedelta(days=30)
        work = (deadline - NOW).total_seconds()
        for w, v, d in ((work + 1, 0, 0), (work, 1, 0), (work, 0, 1)):
            with self.subTest(work=w, verification=v, deployment=d):
                self.assertIs(
                    calculate_forecast(ForecastInput(deadline, w, v, d), now=NOW).level,
                    UrgencyLevel.CRITICAL,
                )

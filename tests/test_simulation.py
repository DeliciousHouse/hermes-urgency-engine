"""protects=paired execution; fails_when=urgency is bypassed or inputs differ;
why_new=offline counterfactual; seam=real forecast and disposable stores; no_claim=live execution.
"""
from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import simulate_urgency as sim


class SimulationTests(unittest.TestCase):
    def test_rejects_unpaired_schedule_before_execution(self):
        case = sim.SCENARIOS['stalled']
        for changed in (replace(case, tasks=case.tasks[::-1]), replace(case, faults=())):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'identical'):
                sim.run_pair(case, changed, clock=lambda: self.fail('read clock before pairing'))

    def test_production_forecast_controls_selection_and_verified_outcome(self):
        case = sim.SCENARIOS['pressure']
        pair = sim.run_pair(case, case)
        baseline, enabled = pair['arms']
        self.assertEqual(baseline['operations']['forecast'], 0)
        self.assertEqual(baseline['operations']['watchdog_tick'], 0)
        self.assertEqual(baseline['selection'], ['routine', 'urgent'])
        self.assertEqual(enabled['selection'], ['urgent', 'routine'])
        self.assertEqual([arm['completion']['verified_on_time'] for arm in pair['arms']], [1, 2])
        self.assertTrue(pair['correct'])
        trace = enabled['trace']
        self.assertEqual([row['event'] for row in trace[:5]],
                         ['observation', 'forecast', 'forecast', 'recommendation', 'selected'])
        self.assertEqual(trace[3]['task'], trace[4]['task'])
        self.assertEqual(trace[3]['source'], 'forecast')
        self.assertIn('verified', [row['event'] for row in trace])

    def test_pressure_oracle_rejects_fixed_order_when_forecasts_are_neutral(self):
        case = sim.SCENARIOS['pressure']
        real = sim.calculate_forecast

        def neutral(task, **kwargs):
            return replace(real(task, **kwargs), level=sim.UrgencyLevel.NORMAL)

        with patch.object(sim, 'calculate_forecast', side_effect=neutral) as policy:
            pair = sim.run_pair(case, case)
        self.assertGreater(policy.call_count, 0)
        self.assertEqual(pair['arms'][0]['selection'], pair['arms'][1]['selection'])
        self.assertFalse(pair['correct'])
        self.assertFalse(pair['arms'][1]['correctness']['correct'])

    def test_replay_is_deterministic_and_clock_is_injected(self):
        start = datetime(2040, 3, 5, tzinfo=timezone.utc)
        for case in sim.SCENARIOS.values():
            with self.subTest(case=case.name):
                one = sim.run_pair(case, case, clock=lambda: start)
                two = sim.run_pair(case, case, clock=lambda: start)
                for receipt in (one, two):
                    self.assertEqual(set(receipt.pop('wall_clock_seconds_nondeterministic')), {'baseline', 'enabled'})
                self.assertEqual(one, two)
                self.assertEqual(one['arms'][0]['trace'][0]['at'], start.isoformat())

    def test_watchdog_reports_stall_and_refuses_fenced_prioritization(self):
        case = sim.SCENARIOS['stalled']
        baseline, enabled = sim.run_pair(case, case)['arms']
        self.assertIsNone(baseline['detection_latency_seconds'])
        self.assertEqual(enabled['detection_latency_seconds'], 1770)
        self.assertEqual(enabled['selection'], ['delayed'])
        actions = [row for row in enabled['trace'] if row['event'] == 'watchdog_action']
        self.assertEqual({(row['task'], row['action'], row['disposition']) for row in actions}, {
            ('delayed', 'inspect_reestimate', 'unsupported'),
            ('held', 'prioritize_critical_path', 'held'),
            ('unknown', 'prioritize_critical_path', 'unknown'),
        })
        self.assertTrue(all(row['incident_id'] for row in actions))
        self.assertTrue(enabled['correctness']['correct'])

    def test_watchdog_trace_binds_refusal_to_the_production_plan(self):
        case = sim.SCENARIOS['stalled']
        enabled = sim.run_pair(case, case)['arms'][1]
        actions = [row for row in enabled['trace'] if row['event'] == 'watchdog_action']
        self.assertEqual(len(actions), 3)
        for action in actions:
            self.assertTrue(action['plan_id'])
            self.assertIn(action['action'], action['offered_actions'])
            self.assertFalse(action['applied'])

    def test_claim_and_missing_forecast_do_not_credit_completion_or_calibration(self):
        case = sim.SCENARIOS['nominal']
        pair = sim.run_pair(case, case)
        for arm in pair['arms']:
            claim = next(row for row in arm['completion']['tasks'] if row['task'] == 'claim')
            self.assertEqual(claim['status'], 'claimed_complete')
            self.assertIsNone(claim['verified_at'])
            self.assertIsNone(claim['on_time'])
            self.assertEqual(arm['completion']['verified_on_time'], 1)
            self.assertEqual(arm['quantiles']['p50']['missing'], 2)
            self.assertEqual(arm['quantiles']['p90']['eligible'], [])
        self.assertTrue(pair['correct'])

    def test_quantiles_report_verified_comparison_and_missing_denominator(self):
        case = sim.SCENARIOS['pressure']
        enabled = sim.run_pair(case, case)['arms'][1]
        for key, predicted in (('p50', 900), ('p90', 1044)):
            row = enabled['quantiles'][key]
            self.assertEqual((row['total'], row['missing']), (2, 1))
            self.assertEqual(row['eligible'], [dict(task='routine', predicted_seconds=predicted,
                                                   actual_seconds=900, within=True)])

    def test_oracle_rejects_missing_verification_even_if_selection_is_correct(self):
        case = sim.SCENARIOS['pressure']

        def claim_instead(ledger, board, task, criterion, artifact, **kwargs):
            return ledger.claim_complete(board, task, **kwargs)

        with patch.object(sim.Ledger, 'verify_artifact', claim_instead):
            pair = sim.run_pair(case, case)
        self.assertEqual(pair['arms'][1]['selection'], ['urgent', 'routine'])
        self.assertEqual(pair['arms'][1]['completion']['verified_on_time'], 0)
        self.assertFalse(pair['correct'])

    def test_rejects_off_grid_events_instead_of_silently_dropping_them(self):
        case = replace(sim.SCENARIOS['stalled'], faults=(sim.Fault(61, 'delayed', 1800),))
        with self.assertRaisesRegex(ValueError, 'grid'):
            sim.run_pair(case, case)

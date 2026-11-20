"""protects=durable truthful monitoring; fails_when=unknown becomes failure;
why_new=independent watchdog; seam=fixed clock/disposable SQLite; no_claim=remote delivery.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo, reset_tzpath, TZPATH
from test_ledger import fixture_zones

from urgency_engine.ledger import Ledger, LedgerError
from urgency_engine.watchdog import Monitor, DEFAULTS, snapshot


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.clock = lambda: self.now
        self.ledger_path = self.root / 'data.db'
        with self.ledger() as ledger:
            ledger.create_project('demo', 'Synthetic objective', request_id='project', actor='fixture')
            ledger.upsert_task('board', 'task', project_id='demo', goal='Deliver proof', status='running',
                               remaining_seconds=600, expected_revision=0, reason='fixture', request_id='task', actor='fixture')
        self.path = self.root / 'watchdog.db'

    def ledger(self):
        return Ledger(sqlite3.connect(self.ledger_path), clock=self.clock, artifact_root=self.root)

    def monitor(self, readonly=False):
        return Monitor.open(self.path, clock=self.clock, readonly=readonly)

    def observe(self, data, kind='task_signal', event='signal', at=None):
        with self.monitor() as monitor:
            return monitor.observe(dict(request_id=event, reason='fixture', project_id='demo', board_id='board',
                task_id='task', kind=kind, occurred_at=(at or self.now).isoformat(), data=data), self.ledger_path)

    def tick(self):
        with self.monitor() as monitor:
            return monitor.tick(self.ledger_path, DEFAULTS, force=True)

    def detectors(self):
        with self.monitor(True) as monitor:
            return {x['detector'] for x in monitor.page('incidents')['items'] if x['state'] == 'active'}

    def test_empty_ledger_tick_records_unknown_without_creating_ledger(self):
        absent = self.root / 'empty' / 'data.db'
        with self.monitor() as monitor:
            result = monitor.tick(absent, DEFAULTS)
            self.assertEqual(result['notices'], [])
            self.assertEqual(monitor.health()['coverage'], 'ledger_uninitialized')
            self.assertIsNotNone(monitor.health()['last_success'])
        self.assertFalse(absent.parent.exists())

    def test_fractional_timestamps_do_not_sort_before_whole_seconds(self):
        self.observe({'agent_available': False})
        self.now += timedelta(microseconds=1)
        result = self.observe({'agent_available': True}, event='fraction')
        self.assertFalse(result['out_of_order'])
        self.tick()
        self.assertNotIn('delegated_work_unavailable', self.detectors())

    def test_old_failure_count_is_not_refreshed_by_unrelated_telemetry(self):
        self.observe({'tool_failures': 3})
        self.now += timedelta(seconds=901)
        self.observe({'checkpoint_at': None}, event='unrelated')
        self.tick()
        self.assertNotIn('repeated_tool_failure', self.detectors())

    def test_readonly_corruption_is_safe_and_preserves_bytes(self):
        self.path.write_bytes(b'not sqlite')
        before = self.path.read_bytes()
        with self.assertRaises(LedgerError) as caught:
            self.monitor(True)
        self.assertEqual(caught.exception.code, 'CORRUPT_STATE')
        self.assertEqual(before, self.path.read_bytes())

    def test_unknown_then_stalled_and_no_repeat(self):
        original = self.ledger_path.read_bytes()
        self.assertEqual(self.tick()['notices'], [])
        self.now += timedelta(seconds=1800)
        self.assertEqual(len(self.tick()['notices']), 1)
        self.assertEqual(self.detectors(), {'stalled'})
        self.assertEqual(self.tick()['notices'], [])
        self.assertEqual(original, self.ledger_path.read_bytes())

    def test_observation_replay_conflict_future_and_sparse(self):
        first = self.observe({'claim_at': self.now.isoformat()})
        self.assertEqual(first, self.observe({'claim_at': self.now.isoformat()}))
        with self.assertRaises(LedgerError):
            self.observe({'heartbeat_at': self.now.isoformat()})
        with self.assertRaises(LedgerError):
            self.observe({}, event='future', at=self.now + timedelta(seconds=61))
        self.observe({'agent_available': False}, event='next')
        self.now += timedelta(seconds=900)
        self.tick()
        self.assertEqual(self.detectors(), {'stale_claim', 'delegated_work_unavailable'})

    def test_failures_need_known_outcomes_and_success_resets(self):
        for n in range(3):
            self.observe({'tool_name': 'synthetic', 'outcome': 'failure', 'duration_seconds': 1},
                         'tool_outcome', str(n))
        self.tick()
        self.assertIn('repeated_tool_failure', self.detectors())
        self.now += timedelta(seconds=1)
        self.observe({'tool_name': 'synthetic', 'outcome': 'success', 'duration_seconds': 0}, 'tool_outcome', 'success')
        self.tick()
        self.assertNotIn('repeated_tool_failure', self.detectors())

    def test_pause_is_durable_and_reads_do_not_initialize(self):
        absent = self.root / 'absent' / 'watchdog.db'
        self.assertIsNone(Monitor.open(absent, clock=self.clock, readonly=True))
        self.assertFalse(absent.parent.exists())
        with self.monitor() as monitor:
            monitor.control('pause', 'pause', 'fixture')
        self.observe({'agent_available': False})
        self.assertEqual(self.tick()['notices'], [])
        with self.monitor() as monitor:
            monitor.control('resume', 'resume', 'fixture')
        self.assertEqual(len(self.tick()['notices']), 1)

    def test_cycles_recovery_forecast_and_episode_backoff(self):
        self.observe({'dependencies': [['board', 'task']], 'agent_available': False})
        self.tick()
        self.assertIn('dependency_cycle', self.detectors())
        with self.monitor(True) as monitor:
            incident = next(i for i in monitor.page('incidents')['items'] if i['detector'] == 'delegated_work_unavailable')
        for n in range(3):
            self.observe({'incident_id': incident['incident_id'], 'outcome': 'failed'}, 'recovery_outcome', 'recovery-' + str(n))
        self.tick()
        self.assertIn('ineffective_recovery', self.detectors())
        for interval in (300, 600, 1200, 3600):
            self.now += timedelta(seconds=interval)
            self.tick()
        with self.monitor(True) as monitor:
            self.assertTrue(all(i['notice_count'] <= 3 for i in monitor.page('incidents')['items']))
        self.observe({'dependencies': [], 'agent_available': True, 'dependency_state': 'unreachable'}, event='resolved')
        self.tick()
        self.assertNotIn('dependency_cycle', self.detectors())
        self.assertIn('dependency_unreachable', self.detectors())
        with self.ledger() as ledger:
            ledger.set_deadline('future', project_id='demo', kind='soft', at=(self.now + timedelta(days=1)).isoformat(), timezone='UTC', owner='fixture', source='fixture', request_id='forecast', actor='fixture')
        self.tick()
        self.now += timedelta(seconds=601)
        self.tick()
        self.assertIn('forecast_miss', self.detectors())

    def test_unknown_input_future_schema_and_corruption_preserve_bytes(self):
        for data in ({'secret': 'do-not-retain'}, {'agent_available': 0}, {'dependencies': [['board', '../task']]}, {'tool_failures': True}):
            with self.subTest(data=data), self.assertRaises(LedgerError):
                self.observe(data)
        self.tick()
        db = sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA user_version=999')
        finally:
            db.close()
        original = self.path.read_bytes()
        with self.assertRaises(LedgerError) as caught:
            self.monitor()
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_SCHEMA')
        self.assertEqual(original, self.path.read_bytes())

    def test_clock_rollback_and_capacity_fail_without_partial_rows(self):
        self.tick()
        self.now -= timedelta(seconds=1)
        with self.assertRaises(LedgerError) as caught:
            self.tick()
        self.assertEqual(caught.exception.code, 'CLOCK_ROLLBACK')
        self.now += timedelta(seconds=2)
        with self.monitor() as monitor:
            with self.assertRaises(LedgerError):
                with monitor.transaction():
                    monitor.put('must_rollback', True)
                    monitor.capacity({**DEFAULTS, 'watchdog_max_rows': 1})
            self.assertIsNone(monitor.meta('must_rollback'))

    def test_deadline_and_checkpoint_without_target_rewrite(self):
        with self.ledger() as ledger:
            ledger.set_deadline('hard', project_id='demo', kind='hard', at=self.now.isoformat(), timezone='UTC',
                                owner='fixture', source='fixture', request_id='deadline', actor='fixture')
        self.observe({'checkpoint_at': self.now.isoformat()})
        self.tick()
        self.assertNotIn('expired_deadline', self.detectors())
        self.assertNotIn('missed_checkpoint', self.detectors())
        self.now += timedelta(seconds=1)
        self.tick()
        self.assertTrue({'missed_checkpoint', 'expired_deadline'} <= self.detectors())
        with self.monitor(True) as monitor:
            page = monitor.page('plans', limit=1)
            self.assertTrue(page['has_more'])
            second = monitor.page('plans', limit=1, after=page['next'])
            self.assertNotEqual(page['items'][0]['plan_id'], second['items'][0]['plan_id'])
            deadline_plan = next(p for p in monitor.page('plans')['items'] if p['actions']['deadline'])
            self.assertEqual(deadline_plan['actions']['objective'], 'Synthetic objective')
            self.assertEqual(deadline_plan['actions']['affected_tasks'], [['board', 'task']])


if __name__ == '__main__':
    unittest.main()

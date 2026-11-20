"""protects=durable truthful monitoring; fails_when=unknown becomes failure;
why_new=independent watchdog; seam=fixed clock/disposable SQLite; no_claim=remote delivery.
"""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo, reset_tzpath, TZPATH
from test_ledger import fixture_zones

from urgency_engine.ledger import Ledger, LedgerError
from urgency_engine.watchdog import Monitor, DEFAULTS, snapshot


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(strict=True)
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

    def test_unknown_availability_preserves_episode_without_reminding(self):
        self.observe({'agent_available': False})
        incident_id = self.tick()['notices'][0]['incident_id']
        self.now += timedelta(seconds=300)
        self.observe({'agent_available': None}, event='unknown')
        self.assertEqual(self.tick()['notices'], [])
        with self.monitor(True) as monitor:
            incident, = monitor.page('incidents')['items']
            plan, = monitor.page('plans')['items']
        self.assertEqual((incident['incident_id'], incident['state'], incident['notice_count']),
                         (incident_id, 'active', 1))
        self.assertEqual((plan['incident_id'], plan['state']), (incident_id, 'recommended'))

        self.observe({'agent_available': False}, event='unavailable-again')
        self.assertEqual([n['incident_id'] for n in self.tick()['notices']], [incident_id])
        self.observe({'agent_available': True}, event='available')
        self.assertEqual(self.tick()['notices'], [])
        with self.monitor(True) as monitor:
            self.assertEqual(monitor.page('incidents')['items'][0]['state'], 'resolved')
            self.assertEqual(monitor.page('plans')['items'][0]['state'], 'resolved')

        self.observe({'agent_available': False}, event='recurrence')
        self.assertNotEqual(self.tick()['notices'][0]['incident_id'], incident_id)
        with self.monitor(True) as monitor:
            self.assertEqual(sorted(i['episode'] for i in monitor.page('incidents')['items']), [1, 2])

    def test_unknown_dependency_coverage_preserves_episode_without_reminding(self):
        self.observe({'dependency_state': 'unreachable'})
        first, = self.tick()['notices']
        self.assertEqual(first['detector'], 'dependency_unreachable')
        incident_id = first['incident_id']
        with self.monitor(True) as monitor:
            original_incident, = monitor.page('incidents')['items']
            original_plan, = monitor.page('plans')['items']
        self.assertEqual((original_incident['state'], original_incident['notice_count']), ('active', 1))
        self.assertEqual((original_plan['incident_id'], original_plan['state']), (incident_id, 'recommended'))

        self.now += timedelta(seconds=300)
        self.observe({'dependency_state': 'unknown'}, event='unknown')
        self.assertEqual(self.tick()['notices'], [])
        with self.monitor(True) as monitor:
            self.assertEqual(monitor.page('incidents')['items'], [original_incident])
            self.assertEqual(monitor.page('plans')['items'], [original_plan])

        self.observe({'dependency_state': None}, event='clear-coverage')
        self.observe({}, event='sparse')
        self.assertEqual(self.tick()['notices'], [])
        with self.monitor(True) as monitor:
            self.assertEqual(monitor.page('incidents')['items'], [original_incident])
            self.assertEqual(monitor.page('plans')['items'], [original_plan])

        self.observe({'dependency_state': 'unreachable'}, event='unreachable-again')
        self.assertEqual([n['incident_id'] for n in self.tick()['notices']], [incident_id])
        self.assertEqual(self.notice_counts(), {incident_id: 2})

        self.observe({'dependency_state': 'available'}, event='available')
        self.assertEqual(self.tick()['notices'], [])
        with self.monitor(True) as monitor:
            incident, = monitor.page('incidents')['items']
            plan, = monitor.page('plans')['items']
        self.assertEqual((incident['incident_id'], incident['state']), (incident_id, 'resolved'))
        self.assertEqual((plan['incident_id'], plan['state']), (incident_id, 'resolved'))

        self.observe({'dependency_state': 'unreachable'}, event='recurrence')
        recurrence, = self.tick()['notices']
        self.assertNotEqual(recurrence['incident_id'], incident_id)
        with self.monitor(True) as monitor:
            self.assertEqual(sorted((i['episode'], i['state']) for i in monitor.page('incidents')['items']),
                             [(1, 'resolved'), (2, 'active')])

    def test_missing_graph_target_is_positive_even_without_explicit_coverage(self):
        # Model a previously registered edge whose target is now absent.
        with self.monitor() as monitor, monitor.transaction():
            monitor.db.execute('INSERT INTO dependencies VALUES(?,?,?,?)', ('board', 'task', 'board', 'missing'))
        notice, = self.tick()['notices']
        self.assertEqual(notice['detector'], 'dependency_unreachable')
        self.observe({'dependency_state': 'available'})
        self.now += timedelta(seconds=300)
        self.assertEqual([n['incident_id'] for n in self.tick()['notices']], [notice['incident_id']])
        with self.monitor(True) as monitor:
            incident, = monitor.page('incidents')['items']
        self.assertEqual(incident['explanation']['facts'],
                         {'missing': [['board', 'missing']], 'source': 'complete_known_graph'})

    def test_verified_task_closes_dependency_episode_with_unknown_coverage(self):
        self.observe({'dependency_state': 'unreachable'})
        first, = self.tick()['notices']
        self.observe({'dependency_state': None}, event='clear-coverage')
        artifact = b'verified fixture'
        (self.root / 'artifact').write_bytes(artifact)
        with self.ledger() as ledger:
            task = ledger.set_criteria('board', 'task', [dict(criterion_id='proof', description='Proof',
                weight=1, required=True, expected_sha256=sha256(artifact).hexdigest())], expected_revision=1,
                reason='fixture', request_id='criterion', actor='fixture')
            ledger.verify_artifact('board', 'task', 'proof', 'artifact', expected_revision=task['revision'],
                                   reason='fixture', request_id='verification', actor='fixture')
            self.assertEqual(ledger.get_task('board', 'task')['status'], 'verified')
        self.assertEqual(self.tick()['notices'], [])
        with self.monitor(True) as monitor:
            incident, = monitor.page('incidents')['items']
            plan, = monitor.page('plans')['items']
        self.assertEqual((incident['incident_id'], incident['state'], plan['state']),
                         (first['incident_id'], 'resolved', 'resolved'))

    def seed_unavailable_tasks(self, count):
        with self.ledger() as ledger:
            for n in range(count):
                ledger.upsert_task('board', f'task-{n}', project_id='demo', goal='Deliver proof', status='running',
                                   remaining_seconds=600, expected_revision=0, reason='fixture', request_id=f'task-{n}', actor='fixture')
        with self.monitor() as monitor:
            for n in range(count):
                monitor.observe(dict(request_id=f'event-{n}', reason='fixture', project_id='demo', board_id='board',
                    task_id=f'task-{n}', kind='task_signal', occurred_at=self.now.isoformat(), data={'agent_available': False}),
                    self.ledger_path)

    def notice_counts(self):
        with self.monitor(True) as monitor:
            return {row['incident_id']: row['notice_count'] for row in monitor.page('incidents')['items']}

    def test_tick_commits_only_selected_notice_counts(self):
        self.seed_unavailable_tasks(40)
        with self.ledger() as ledger:
            ledger.set_deadline('hard', project_id='demo', kind='hard', at=self.now.isoformat(), timezone='UTC',
                                owner='fixture', source='fixture', request_id='deadline', actor='fixture')
        self.now += timedelta(seconds=1)
        with self.monitor() as monitor:
            tick = monitor.tick(self.ledger_path, {**DEFAULTS, 'watchdog_notice_limit': 1}, force=True)
        counts = self.notice_counts()
        self.assertEqual(sorted(counts.values()), [0] + [1] * 40)
        self.assertEqual({notice['incident_id'] for notice in tick['notices']},
                         {identity for identity, count in counts.items() if count})
        self.assertEqual(tick['omitted'], 1)

    def test_cli_emits_overflow_incident_on_next_due_tick(self):
        from urgency_engine.runtime import Runtime, setup_parser

        class Ctx:
            def get_config(self, key, default=None):
                return {'watchdog_notice_limit': 1, 'watchdog_enabled': True}.get(key, default)

            def redact_text(self, text, force=False):
                return text

        self.seed_unavailable_tasks(41)
        runtime = Runtime(Ctx(), clock=self.clock, data_dir=lambda: self.root)
        args = setup_parser(argparse.ArgumentParser()).parse_args(['watchdog', 'tick'])

        def cli_tick():
            out = io.StringIO()
            with patch.object(Monitor, 'host', side_effect=lambda *, clock: Monitor.open(self.path, clock=clock)), redirect_stdout(out):
                self.assertEqual(runtime.cli(args), 0)
            return json.loads(out.getvalue())['data']

        first = cli_tick()
        self.assertEqual(len(first['notices']), 40)
        counts = self.notice_counts()
        emitted = {notice['incident_id'] for notice in first['notices']}
        omitted, = set(counts) - emitted
        self.assertEqual(counts, {identity: int(identity in emitted) for identity in counts})
        self.assertEqual((first['more'], first['omitted']), (True, 1))
        self.now += timedelta(seconds=DEFAULTS['watchdog_risk_seconds'])
        second = cli_tick()
        self.assertEqual([notice['incident_id'] for notice in second['notices']], [omitted])
        self.assertEqual((second['more'], second['omitted']), (False, 0))
        self.assertEqual(self.notice_counts(), dict.fromkeys(counts, 1))

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

    def test_unrelated_observations_do_not_refresh_source_freshness(self):
        self.observe({'dependency_state': 'unreachable', 'agent_available': False}, event='source')
        source_time = self.now
        self.now += timedelta(seconds=120)
        self.observe({'tool_name': 'read', 'outcome': 'unknown', 'duration_seconds': 0}, kind='tool_outcome', event='chatter')
        self.tick()
        with self.monitor(True) as monitor:
            incidents = monitor.page('incidents')['items']
        selected = [i for i in incidents if i['detector'] in ('dependency_unreachable', 'delegated_work_unavailable')]
        self.assertEqual(len(selected), 2)
        for incident in selected:
            self.assertEqual(datetime.fromisoformat(incident['explanation']['facts']['observed_at']), source_time)
        self.now += timedelta(days=31)
        self.tick()
        with self.monitor(True) as monitor:
            selected = [i for i in monitor.page('incidents')['items'] if i['detector'] in ('dependency_unreachable', 'delegated_work_unavailable')]
        self.assertEqual(len(selected), 2)
        for incident in selected:
            self.assertIsNone(incident['explanation']['facts']['observed_at'])
            self.assertEqual(incident['explanation']['facts']['freshness'], 'unknown_source_pruned')

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

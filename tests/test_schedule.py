"""protects=dependency-aware forecasts; fails_when=capacity or edges are ignored;
seam=real Ledger and synthetic graph; no_claim=actual worker capacity.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from urgency_engine.ledger import Ledger


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.ledger = Ledger(sqlite3.connect(self.root / 'data.db'), clock=lambda: self.now, artifact_root=self.root)
        self.addCleanup(self.ledger.connection.close)
        self.ledger.create_project('p', 'Project', request_id='p', actor='operator')
        for name, seconds in [('a', 1800), ('b', 3600)]:
            self.ledger.upsert_task('board', name, project_id='p', goal=name, remaining_seconds=seconds,
                                    expected_revision=0, reason='Plan', request_id=name, actor='operator')
        self.ledger.connection.execute("INSERT INTO deadlines VALUES('d','p',NULL,NULL,NULL,'hard',?,?,?,?,?,1)",
            ('2030-01-02T00:00:00+00:00', '2030-01-02T00:00:00+00:00', 'UTC', 'owner', 'source'))
        self.ledger.connection.commit()
        self.items = [dict(board_id='board', task_id=name, parents=[]) for name in ('a', 'b')]

    def evidence(self, slots=2, reason=None):
        self.ledger.forecast_evidence = lambda project, now: dict(items=self.items, reason=reason, slots=slots, observed_at=self.now.isoformat())

    def test_aggregate_capacity_and_edge(self):
        self.evidence()
        independent = self.ledger.explain_deadline('d')
        self.assertEqual(independent['input']['remaining_seconds'], 3600)
        self.assertEqual(independent['input']['schedule']['method'], 'operator_declared_capacity')
        self.items[1]['parents'] = ['a']
        self.assertEqual(self.ledger.explain_deadline('d')['input']['remaining_seconds'], 5400)
        self.items[1]['parents'] = []
        self.evidence(slots=1)
        self.assertEqual(self.ledger.explain_deadline('d')['input']['remaining_seconds'], 5400)

    def test_standalone_and_withdrawn_scope(self):
        self.assertIsNone(self.ledger.forecast_evidence)
        self.assertEqual(self.ledger.explain_deadline('d')['input']['schedule']['method'], 'ledger_only')
        self.evidence(reason='source_incomplete')
        result = self.ledger.explain_deadline('d')
        self.assertIsNone(result['forecast_at'])
        self.assertIsNone(result['forecast']['effective_slack_seconds'])
        self.assertEqual(result['input']['schedule'], dict(method='unknown', reason='source_incomplete'))
        self.assertEqual(result['input_revisions']['tasks'], [])

    def test_milestone_closure_and_verified_prerequisite(self):
        import hashlib
        self.ledger.create_milestone('m', project_id='p', title='M', request_id='m', actor='operator')
        self.ledger.set_milestone_tasks('m', [['board', 'b']], expected_revision=self.ledger.get_project('p')['revision'],
                                        reason='Bind', request_id='bind', actor='operator')
        self.ledger.connection.execute("UPDATE deadlines SET milestone_id='m' WHERE deadline_id='d'")
        self.ledger.connection.commit()
        self.items[1]['parents'] = ['a']
        self.evidence()
        result = self.ledger.explain_deadline('d')
        self.assertEqual(result['input']['remaining_seconds'], 5400)
        self.assertEqual([r[1] for r in result['input_revisions']['tasks']], ['a', 'b'])
        digest = hashlib.sha256(b'proof').hexdigest()
        (self.root / 'proof').write_bytes(b'proof')
        for name in ('a', 'b'):
            self.ledger.set_criteria('board', name, [dict(criterion_id='proof', description='Proof', weight=1, required=True, expected_sha256=digest)],
                                     expected_revision=1, reason='Scope', request_id='c'+name, actor='operator')
        self.ledger.verify_artifact('board', 'b', 'proof', 'proof', expected_revision=2, reason='Verify', request_id='vb', actor='operator')
        self.assertIsNone(self.ledger.explain_deadline('d')['actual_verified_at'])
        self.now += timedelta(seconds=1)
        self.ledger.verify_artifact('board', 'a', 'proof', 'proof', expected_revision=2, reason='Verify', request_id='va', actor='operator')
        self.evidence(slots=0)
        result = self.ledger.explain_deadline('d')
        self.assertEqual(result['actual_verified_at'], self.now.isoformat())
        self.assertIsNone(result['forecast_at'])
        self.evidence()
        self.assertEqual(self.ledger.explain_deadline('d')['input']['remaining_seconds'], 0)

    def test_incomplete_cycle_estimates_and_invalid_capacity_are_unknown(self):
        cases = [([self.items[0]], 2, 'project_scope_incomplete'),
                 ([dict(self.items[0], parents=['b']), dict(self.items[1], parents=['a'])], 2, 'dependency_cycle'),
                 ([self.items[0], dict(self.items[1], parents=['missing'])], 2, 'dependency_outside_project'),
                 (self.items, 0, 'capacity_unknown')]
        for items, slots, expected in cases:
            with self.subTest(expected=expected):
                self.ledger.forecast_evidence = lambda project, now: dict(items=items, reason=None, slots=slots, observed_at=self.now.isoformat())
                result = self.ledger.explain_deadline('d')
                self.assertIsNone(result['forecast_at'])
                self.assertEqual(result['input']['schedule']['reason'], expected)
        self.ledger.upsert_task('board', 'a', project_id='p', goal='a', remaining_seconds=None,
                                expected_revision=1, reason='Unknown', request_id='unknown', actor='operator')
        self.evidence()
        self.assertEqual(self.ledger.explain_deadline('d')['input']['schedule']['reason'], 'missing_estimates')

    def test_task_deadline_remains_task_local_when_graph_unavailable(self):
        self.ledger.connection.execute("UPDATE deadlines SET board_id='board',task_id='b' WHERE deadline_id='d'")
        self.ledger.connection.commit()
        self.evidence(reason='source_incomplete')
        result = self.ledger.explain_deadline('d')
        self.assertEqual(result['input']['remaining_seconds'], 3600)
        self.assertNotIn('schedule', result['input'])
        self.assertEqual([r[1] for r in result['input_revisions']['tasks']], ['b'])

    def test_oversized_graph_and_untrusted_reason_fail_closed(self):
        self.items = [dict(board_id='board', task_id='a', parents=['secret'] * 50001)]
        self.evidence()
        result = self.ledger.explain_deadline('d')
        self.assertEqual(result['input']['schedule']['reason'], 'source_incomplete')
        self.assertEqual(result['input_revisions']['tasks'], [])
        self.evidence(reason='secret-task-id')
        result = self.ledger.explain_deadline('d')
        self.assertEqual(result['input']['schedule']['reason'], 'source_unavailable')
        self.assertNotIn('secret-task-id', str(result))

    def test_buffers_are_counted_once_in_prerequisite_closure(self):
        self.ledger.create_milestone('m', project_id='p', title='M', request_id='m', actor='operator')
        self.ledger.set_milestone_tasks('m', [['board', 'b']], expected_revision=self.ledger.get_project('p')['revision'],
                                        reason='Bind', request_id='bind', actor='operator')
        self.ledger.connection.execute("UPDATE deadlines SET milestone_id='m' WHERE deadline_id='d'")
        self.ledger.connection.execute("UPDATE tasks SET verification_seconds=10, deployment_seconds=20 WHERE project_id='p'")
        self.ledger.connection.commit()
        self.items[1]['parents'] = ['a']
        self.evidence()
        result = self.ledger.explain_deadline('d')
        self.assertEqual((result['input']['remaining_seconds'], result['input']['verification_seconds'],
                          result['input']['deployment_seconds']), (5400, 20, 40))
        self.assertEqual(result['forecast_at'], '2030-01-01T01:31:00+00:00')

    def test_simultaneous_completion_reorders_global_ready_queue(self):
        from urgency_engine.schedule import schedule
        p = {('b', 'a'): [], ('b', 'b'): [], ('b', 'c'): [('b', 'a')],
             ('b', 'd'): [('b', 'b')], ('b', 'z'): []}
        d = {('b', 'a'): 2, ('b', 'b'): 2, ('b', 'c'): 10, ('b', 'd'): 1, ('b', 'z'): 20}
        self.assertEqual(schedule(p, d, 2), 23)
        self.assertEqual(schedule(dict(reversed(list(p.items()))), d, 2), 23)

    def test_scheduler_event_order_zero_times_and_bounds(self):
        from urgency_engine.schedule import schedule
        key = lambda name: ('board', name)
        parents = {key('a'): (), key('b'): (), key('c'): (key('a'),), key('d'): (key('b'),)}
        durations = {key('a'): 2, key('b'): 2, key('c'): 0, key('d'): 3}
        self.assertEqual(schedule(parents, durations, 2), 5)
        self.assertEqual(schedule(parents, durations, 1), 7)
        with self.assertRaises(ValueError):
            schedule({key('a'): (key('b'),), key('b'): (key('a'),)}, durations, 2)
        with self.assertRaises(ValueError):
            schedule({('b', str(i)): () for i in range(10001)}, {}, 2)
        with self.assertRaises(ValueError):
            schedule({key('a'): (key('a'),) * 50001}, {}, 2)

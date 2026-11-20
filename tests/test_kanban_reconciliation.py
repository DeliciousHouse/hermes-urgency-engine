"""Faulted canonical reads over disposable real stores; no production board I/O."""
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from urgency_engine import kanban
from urgency_engine.ledger import Ledger, LedgerError
from test_kanban import NOW, BINDING


class Source:
    def __init__(self):
        self.rows = [dict(id='task', tenant='synthetic')]
        self.status = 'running'
        self.title = 'Deliver'
        self.calls = 0
        self.fail = None

    def inventory(self, binding):
        if self.fail == 'inventory':
            raise LedgerError('SOURCE_DENIED', 'kanban', 'board')
        return self.rows

    def detail(self, binding, task_id):
        self.calls += 1
        if self.fail == 'detail':
            raise LedgerError('SOURCE_MALFORMED', 'kanban', 'task')
        return dict(task=dict(id=task_id, tenant=binding['tenant'], title=self.title,
                             status=self.status), parents=[])


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = NOW
        self.ledger = Ledger(sqlite3.connect(self.root / 'data.db'),
                             clock=lambda: self.now, artifact_root=self.root)
        self.addCleanup(self.ledger.connection.close)
        self.ledger.create_project('project', 'Project', request_id='p', actor='test')
        self.sync = kanban.Sync(sqlite3.connect(self.root / 'kanban.db'),
                                clock=lambda: self.now, redact=lambda text: text)
        self.addCleanup(self.sync.db.close)
        self.cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[BINDING]))
        self.source = Source()

    def scan(self, identity='scan'):
        return self.sync.scan(self.cfg, self.ledger, None, self.root / 'data.db',
                              self.source, request_id=identity, reason='Fixture')

    def test_unchanged_detail_refreshes_receipt_without_rewriting_ledger(self):
        """protects=truthful cached freshness; fails_when=valid reads stay stale;
        why_new=canonical observations; seam=real stores; no_claim=heartbeat refresh.
        """
        self.scan()
        revision = self.ledger.get_task('demo', 'task')['revision']
        self.now += timedelta(seconds=60)
        self.scan('next')
        item = self.sync.page('project')['items'][0]
        self.assertEqual(item['observed_at'], self.now.isoformat())
        self.assertIsNone(item['worker']['heartbeat_at'])
        self.assertEqual(self.ledger.get_task('demo', 'task')['revision'], revision)

    def test_source_failure_keeps_prior_state_and_records_unknown_coverage(self):
        """protects=fail closed; fails_when=source denial clears local facts;
        why_new=transport faults; seam=controlled source; no_claim=host repair.
        """
        self.scan()
        before = self.ledger.get_task('demo', 'task')
        self.source.fail = 'detail'
        with self.assertRaises(LedgerError) as caught:
            self.scan('failed')
        self.assertEqual(caught.exception.code, 'SOURCE_MALFORMED')
        self.assertEqual(self.ledger.get_task('demo', 'task'), before)
        self.assertEqual(self.sync.page('project')['items'][0]['coverage'], 'unknown')

    def test_locally_verified_active_source_conflicts_without_invalidating_bytes(self):
        """protects=local verification; fails_when=first active source reopens proof;
        why_new=source conflicts; seam=real artifacts; no_claim=operator reconciliation.
        """
        import hashlib
        (self.root / 'proof').write_bytes(b'proof')
        self.ledger.upsert_task('demo', 'task', project_id='project', goal='Deliver',
            remaining_seconds=10, expected_revision=0, request_id='task', reason='Fixture', actor='test')
        self.ledger.set_criteria('demo', 'task', [dict(criterion_id='c', description='Check', weight=3,
            required=True, expected_sha256=hashlib.sha256(b'proof').hexdigest())],
            expected_revision=1, reason='Scope', request_id='criteria', actor='test')
        self.ledger.verify_artifact('demo', 'task', 'c', 'proof', expected_revision=2,
            reason='Bytes', request_id='proof', actor='test')
        with self.assertRaises(LedgerError) as caught:
            self.scan()
        self.assertEqual(caught.exception.code, 'STATE_CONFLICT')
        self.assertEqual(self.ledger.progress('project')['percent'], 100)

    def test_binding_change_cancels_unapplied_old_response(self):
        """protects=scope generation; fails_when=late response writes new project;
        why_new=binding changes; seam=persisted plan; no_claim=distributed cancellation.
        """
        self.scan()
        identity = self.sync.db.execute("SELECT request_id FROM sync_requests WHERE state='applied'").fetchone()[0]
        plan = json.loads(self.sync.db.execute('SELECT result_json FROM sync_requests WHERE request_id=?', (identity,)).fetchone()[0])
        plan['projection']['project_id'] = 'other'
        self.sync.db.execute("UPDATE sync_requests SET state='pending',result_json=? WHERE request_id=?",
                             (kanban._json(plan), identity))
        self.sync.db.commit()
        self.ledger.create_project('other', 'Other', request_id='other', actor='test')
        self.cfg['kanban_bindings'][0]['project_id'] = 'other'
        with self.assertRaises(LedgerError) as caught:
            self.scan('changed')
        self.assertEqual(caught.exception.code, 'SCOPE_CONFLICT')
        self.assertEqual(self.ledger.get_task('demo', 'task')['project_id'], 'project')

    def test_complete_inventory_still_reports_partial_graph_coverage(self):
        """protects=coverage honesty; fails_when=list success certifies workers;
        why_new=public source gaps; seam=synthetic source; no_claim=graph completeness.
        """
        result = self.scan()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.sync.page('project')['items'][0]['coverage'], 'partial')

    def test_external_parent_stays_unknown_and_current_worker_is_admitted(self):
        """protects=mapped facts only; fails_when=missing parent becomes unreachable;
        why_new=monitor admission; seam=real stores; no_claim=current availability.
        """
        from urgency_engine.watchdog import Monitor
        self.source.rows.append(dict(id='child', tenant='synthetic'))
        original = self.source.detail

        def detail(binding, task_id):
            document = original(binding, task_id)
            document['parents'] = ['absent']
            if task_id == 'child':
                document['task'].update(current_run_id=4, started_at=100)
                document.update(runs=[dict(id=4, status='running', started_at=100)],
                                events=[dict(kind='heartbeat', run_id=4, created_at=150)])
            return document
        self.source.detail = detail
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            result = self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                                    self.source, request_id='graph', reason='Fixture')
        self.assertEqual(result['state'], 'complete')
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now, readonly=True) as monitor:
            signal = monitor.page('observations', project_id='project')['items']
        self.assertTrue(any(item['kind'] == 'task_signal' and item['payload']['data']['dependency_state'] == 'unknown' for item in signal))


if __name__ == '__main__':
    unittest.main()

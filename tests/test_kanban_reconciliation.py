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

    def test_ten_thousand_sources_finish_after_restart_and_key_churn(self):
        """protects=bounded eventual coverage; fails_when=cursor starves late/new keys;
        why_new=10000 source acceptance; seam=synthetic source/real stores;
        no_claim=native CLI throughput or production latency.
        """
        import time
        started = time.monotonic()
        self.source.rows = [dict(id=f't{i:05d}', tenant='synthetic') for i in range(10000)]
        self.assertEqual(self.scan('page0')['state'], 'partial')
        # Replace an already-consumed key with a lower-sorting key, then restart.
        self.source.rows[0] = dict(id='a-new', tenant='synthetic')
        self.sync.db.close()
        self.sync = kanban.Sync(sqlite3.connect(self.root / 'kanban.db'),
                                clock=lambda: self.now, redact=lambda text: text)
        self.addCleanup(self.sync.db.close)
        for index in range(1, 101):
            result = self.scan(f'page{index}')
            self.assertLessEqual(result['count'], 100)
            if result['state'] == 'complete':
                break
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.ledger.get_task('demo', 't09999')['goal'], 'Deliver')
        with self.assertRaises(LedgerError) as caught:
            self.scan('next-cycle')
        self.assertEqual(caught.exception.field, 'tasks_capacity')
        self.assertEqual(self.sync.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 10000)
        self.assertEqual(self.sync.db.execute("SELECT coverage FROM sources WHERE task_id='t00000'").fetchone()[0], 'unknown')
        print(json.dumps(dict(benchmark='10000_canonical_sources', seconds=time.monotonic() - started,
                              detail_calls=self.source.calls, final_cycle=index)))

    def test_new_lower_key_is_visited_on_next_cycle_below_capacity(self):
        """protects=cursor fairness; fails_when=new lower key never gets scanned;
        why_new=source churn; seam=synthetic source; no_claim=unbounded growth.
        """
        self.source.rows = [dict(id=f't{i:03d}', tenant='synthetic') for i in range(101)]
        self.assertEqual(self.scan()['state'], 'partial')
        self.source.rows[0] = dict(id='a-new', tenant='synthetic')
        self.assertEqual(self.scan('tail')['state'], 'complete')
        self.assertEqual(self.scan('wrap')['state'], 'partial')
        self.assertEqual(self.ledger.get_task('demo', 'a-new')['goal'], 'Deliver')
        self.assertEqual(self.ledger.get_task('demo', 't100')['goal'], 'Deliver')

    def test_absent_source_retains_local_scope_with_unknown_projection(self):
        """protects=absence is not deletion; fails_when=missing source stays current;
        why_new=complete empty inventory; seam=real stores; no_claim=remote deletion.
        """
        self.scan()
        before = self.ledger.get_task('demo', 'task')
        self.source.rows = []
        self.assertEqual(self.scan('empty')['state'], 'complete')
        self.assertEqual(self.ledger.get_task('demo', 'task'), before)
        self.assertEqual(self.sync.page('project')['items'][0]['coverage'], 'unknown')
        self.source.rows = [dict(id='task', tenant='synthetic')]
        self.scan('return')
        self.assertEqual(self.sync.page('project')['items'][0]['coverage'], 'partial')
        self.assertEqual(self.ledger.get_task('demo', 'task'), before)

    def test_invalid_inventory_is_rejected_before_any_task_is_applied(self):
        """protects=whole collection validation; fails_when=late bad row partly applies;
        why_new=inventory trust boundary; seam=synthetic transport; no_claim=native authorization.
        """
        for rows, code in [([dict(id='task', tenant='synthetic')] * 2, 'SOURCE_MALFORMED'),
                           ([dict(id='task', tenant='foreign')], 'SCOPE_CONFLICT'),
                           ([dict(id='task', tenant='synthetic'), {}], 'SOURCE_MALFORMED')]:
            with self.subTest(rows=rows):
                self.source.rows = rows
                with self.assertRaises(LedgerError) as caught:
                    self.scan('bad' + str(len(rows)))
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.ledger.list_tasks('project'), [])
                self.assertEqual(self.source.calls, 0)

    def test_older_source_revision_cannot_overwrite_newer_projection(self):
        """protects=source ordering; fails_when=delayed response rewinds state;
        why_new=canonical revision boundary; seam=controlled responses; no_claim=hosts without revisions.
        """
        original = self.source.detail
        stamp = [200]
        def detail(binding, task_id):
            document = original(binding, task_id)
            document['task']['updated_at'] = stamp[0]
            return document
        self.source.detail = detail
        self.scan()
        before = self.ledger.get_task('demo', 'task')
        stamp[0] = 100
        self.source.title = 'Older'
        with self.assertRaises(LedgerError) as caught:
            self.scan('old')
        self.assertEqual(caught.exception.code, 'SOURCE_STALE')
        self.assertEqual(self.ledger.get_task('demo', 'task'), before)
        self.assertEqual(self.sync.page('project')['items'][0]['title'], 'Deliver')
        stamp[0] = 300
        self.source.title = 'Newer'
        self.scan('new')
        self.assertEqual(self.ledger.get_task('demo', 'task')['goal'], 'Newer')

    def test_scan_health_retains_error_and_success_without_polling_as_heartbeat(self):
        """protects=durable health; fails_when=failed scan appears successful;
        why_new=source diagnostics; seam=real storage; no_claim=source liveness.
        """
        with self.sync.transaction():
            self.sync.put('dirty_bindings', ['demo'])
        self.scan()
        self.assertEqual(self.sync.meta('dirty_bindings'), [])
        self.assertEqual(self.sync.meta('last_success'), self.now.isoformat())
        self.now += timedelta(seconds=60)
        self.source.fail = 'inventory'
        with self.assertRaises(LedgerError):
            self.scan('denied')
        self.assertEqual(self.sync.meta('last_attempt'), self.now.isoformat())
        self.assertEqual(self.sync.meta('last_error'), 'SOURCE_DENIED')
        self.assertEqual(self.sync.meta('last_success'), NOW.isoformat())
        self.assertEqual(self.sync.page('project')['items'][0]['coverage'], 'unknown')
        self.source.fail = None
        self.scan('recover')
        self.assertIsNone(self.sync.meta('last_error'))
        self.assertEqual(self.sync.page('project')['items'][0]['coverage'], 'partial')

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

    def test_monitor_failure_replays_pending_application_without_duplicate_ledger_edit(self):
        """protects=cross-store crash replay; fails_when=source ack precedes monitor;
        why_new=second durable store; seam=real Monitor with admission fault; no_claim=atomic stores.
        """
        from unittest.mock import patch
        from urgency_engine.watchdog import Monitor
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            with patch.object(monitor, 'observe', side_effect=RuntimeError('synthetic crash')):
                with self.assertRaises(RuntimeError):
                    self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                                   self.source, request_id='crash', reason='Fixture')
            revision = self.ledger.get_task('demo', 'task')['revision']
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='crash', reason='Fixture')
            observations = monitor.page('observations', project_id='project')['items']
            self.assertEqual(len(observations), 1)
            self.assertEqual(self.ledger.get_task('demo', 'task')['revision'], revision)
            self.assertEqual(self.sync.db.execute(
                "SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0], 0)

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

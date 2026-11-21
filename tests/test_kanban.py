"""Canonical adapter contracts. All fixtures are synthetic; no live board access."""
from datetime import datetime, timezone
import unittest

from urgency_engine import kanban
from urgency_engine.ledger import LedgerError

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BINDING = dict(board='demo', tenant='synthetic', project_id='project')


class SyncTests(unittest.TestCase):
    def test_native_detail_uses_bounded_snapshot_without_fallback(self):
        """protects=bounded public detail; fails_when=CLI show loses run pointer;
        why_new=public bridge; seam=owned runner fixture; no_claim=installed host.
        """
        import os
        import sys
        from unittest.mock import patch
        calls = []
        def run(argv):
            calls.append(argv)
            return dict(ok=True, data=dict(task=dict(id='task'), parents=[]))
        with patch.dict(os.environ, {}, clear=True):
            source = kanban.NativeSource(run)
            self.assertEqual(source.detail(BINDING, 'task'), dict(task=dict(id='task'), parents=[]))
            self.assertEqual(calls, [[sys.executable, '-m', 'hermes_cli.main', 'urgency', 'kanban',
                                     'snapshot', '--board', 'demo', '--task', 'task']])
            for response in ({'ok': False, 'error': 'private'}, {'ok': True}, [], {'task': {}}):
                calls.clear()
                def denied(argv):
                    calls.append(argv)
                    return response
                with self.assertRaises(LedgerError):
                    kanban.NativeSource(denied).detail(BINDING, 'task')
                self.assertEqual(len(calls), 1)

    def test_native_source_refuses_misattributed_pinned_board(self):
        """protects=board attribution; fails_when=pinned rows labeled foreign;
        why_new=native pin redirect; seam=adapter env; no_claim=host fence proof.
        """
        import os
        from unittest.mock import patch
        for context in ({'HERMES_KANBAN_TASK': 'worker', 'HERMES_KANBAN_BOARD': 'other'},
                        {'HERMES_KANBAN_DB': 'synthetic.db'},
                        {'HERMES_DELEGATED_CHILD_CONTEXT': '1'}):
            with patch.dict(os.environ, context, clear=True):
                source = kanban.NativeSource(lambda argv: [{'slug': 'demo'}])
                for read in (lambda: source.inventory(BINDING), lambda: source.detail(BINDING, 'task')):
                    with self.assertRaises(LedgerError) as caught:
                        read()
                    self.assertEqual(caught.exception.code, 'SOURCE_DENIED')

    def test_pause_resume_is_audited_and_replayed(self):
        """protects=operator replay; fails_when=paused source called;
        why_new=controls; seam=real store; no_claim=cron integration.
        """
        import sqlite3
        import tempfile
        from pathlib import Path
        class Source:
            def inventory(self, binding):
                raise AssertionError('paused source called')
        with tempfile.TemporaryDirectory() as folder:
            with kanban.Sync(sqlite3.connect(Path(folder) / 'kanban.db'), clock=lambda: NOW,
                             redact=lambda text: text) as sync:
                result = sync.control('pause', 'pause', 'Fixture')
                self.assertEqual(result, sync.control('pause', 'pause', 'Fixture'))
                self.assertTrue(result['paused'])
                cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[BINDING]))
                self.assertEqual(sync.scan(cfg, None, None, None, Source(), request_id='scan', reason='Fixture')['state'], 'paused')
                self.assertFalse(sync.control('resume', 'resume', 'Fixture')['paused'])
                with self.assertRaises(LedgerError):
                    sync.control('resume', 'pause', 'Fixture')

    def test_local_edit_during_source_read_conflicts_without_losing_estimate(self):
        """protects=local CAS; fails_when=source applies after concurrent local edit;
        why_new=cross-store race; seam=controlled source boundary; no_claim=network ordering.
        """
        import sqlite3
        import tempfile
        from pathlib import Path
        from urgency_engine.ledger import Ledger
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: NOW, artifact_root=root) as ledger, \
                 kanban.Sync(sqlite3.connect(root / 'kanban.db'), clock=lambda: NOW, redact=lambda text: text) as sync:
                ledger.create_project('project', 'Project', request_id='p', actor='test')
                ledger.upsert_task('demo', 'task', project_id='project', goal='Original', remaining_seconds=10,
                    expected_revision=0, request_id='local', reason='Fixture', actor='test')
                class Source:
                    def inventory(self, binding):
                        return [dict(id='task', tenant='synthetic')]
                    def detail(self, binding, task_id):
                        ledger.upsert_task('demo', 'task', project_id='project', goal='Local edit', remaining_seconds=20,
                            expected_revision=1, request_id='edit', reason='Concurrent', actor='test')
                        return dict(task=dict(id='task', tenant='synthetic', title='Source', status='running'), parents=[])
                cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[BINDING]))
                with self.assertRaises(LedgerError) as caught:
                    sync.scan(cfg, ledger, None, root / 'data.db', Source(), request_id='scan', reason='Fixture')
                self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
                self.assertEqual(ledger.get_task('demo', 'task')['remaining_seconds'], 20)
                self.assertEqual(ledger.get_task('demo', 'task')['goal'], 'Local edit')

    def test_other_scan_cannot_enter_while_source_call_is_in_flight(self):
        """protects=single sync owner; fails_when=overlapping source calls enter;
        why_new=scan reservation; seam=threads with separate SQLite handles; no_claim=process death.
        """
        import sqlite3
        import tempfile
        import threading
        from pathlib import Path
        from urgency_engine.ledger import Ledger
        entered, release = threading.Event(), threading.Event()
        failures = []
        class Source:
            def inventory(self, binding):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('fixture deadline')
                return []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[BINDING]))
            with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: NOW, artifact_root=root) as ledger:
                ledger.create_project('project', 'Project', request_id='p', actor='test')
            def scan():
                try:
                    with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: NOW, artifact_root=root) as ledger, \
                         kanban.Sync(sqlite3.connect(root / 'kanban.db'), clock=lambda: NOW, redact=lambda text: text) as sync:
                        sync.scan(cfg, ledger, None, root / 'data.db', Source(), request_id='first', reason='Fixture')
                except BaseException as exc:
                    failures.append(exc)
            thread = threading.Thread(target=scan)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: NOW, artifact_root=root) as ledger, \
                     kanban.Sync(sqlite3.connect(root / 'kanban.db'), clock=lambda: NOW, redact=lambda text: text) as sync:
                    with self.assertRaises(LedgerError) as caught:
                        sync.scan(cfg, ledger, None, root / 'data.db', Source(), request_id='second', reason='Fixture')
                    self.assertEqual(caught.exception.code, 'STORE_BUSY')
            finally:
                release.set()
                thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])

    def test_detail_cursor_is_bounded_and_resumes_after_restart(self):
        """protects=bounded eventual coverage; fails_when=first page starves tail;
        why_new=cursor; seam=synthetic source; no_claim=production throughput.
        """
        import sqlite3
        import tempfile
        from pathlib import Path
        from urgency_engine.ledger import Ledger
        class Source:
            calls = []
            def inventory(self, binding):
                return [dict(id=f't{i:04}', tenant='synthetic') for i in range(205)]
            def detail(self, binding, task_id):
                self.calls.append(task_id)
                return dict(task=dict(id=task_id, tenant='synthetic', title='Deliver', status='ready'), parents=[])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = Source()
            cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[BINDING]))
            with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: NOW, artifact_root=root) as ledger:
                ledger.create_project('project', 'Project', request_id='p', actor='test')
                for i, expected in enumerate((100, 100, 5)):
                    source.calls.clear()
                    with kanban.Sync(sqlite3.connect(root / 'kanban.db'), clock=lambda: NOW, redact=lambda text: text) as sync:
                        result = sync.scan(cfg, ledger, None, root / 'data.db', source, request_id=f's{i}', reason='Fixture')
                        self.assertEqual(len(source.calls), expected)
                        self.assertEqual(result['state'], 'complete' if i == 2 else 'partial')
                self.assertEqual(len(ledger.list_tasks('project', limit=500)), 205)

    def test_reopen_invalidates_accepted_bytes_once(self):
        """protects=reopen removes old credit; fails_when=terminal proof survives;
        why_new=source transitions; seam=real artifact/ledger; no_claim=semantic quality.
        """
        import hashlib
        import sqlite3
        import tempfile
        from pathlib import Path
        from urgency_engine.ledger import Ledger
        class Source:
            status = 'done'
            def inventory(self, binding):
                return [dict(id='task', tenant='synthetic')]
            def detail(self, binding, task_id):
                return dict(task=dict(id='task', tenant='synthetic', title='Deliver', status=self.status), parents=[])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'proof').write_bytes(b'proof')
            with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: NOW, artifact_root=root) as ledger, \
                 kanban.Sync(sqlite3.connect(root / 'kanban.db'), clock=lambda: NOW, redact=lambda text: text) as sync:
                ledger.create_project('project', 'Project', request_id='p', actor='test')
                cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[BINDING]))
                source = Source()
                sync.scan(cfg, ledger, None, root / 'data.db', source, request_id='s1', reason='Fixture')
                ledger.set_criteria('demo', 'task', [dict(criterion_id='c', description='Check', weight=3,
                    required=True, expected_sha256=hashlib.sha256(b'proof').hexdigest())],
                    expected_revision=2, reason='Scope', request_id='criteria', actor='test')
                ledger.verify_artifact('demo', 'task', 'c', 'proof', expected_revision=3,
                    reason='Bytes', request_id='proof', actor='test')
                self.assertEqual(ledger.progress('project')['percent'], 100)
                source.status = 'running'
                sync.scan(cfg, ledger, None, root / 'data.db', source, request_id='s2', reason='Fixture')
                self.assertEqual(ledger.progress('project')['percent'], 0)
                task = ledger.get_task('demo', 'task')
                self.assertEqual(task['status'], 'running')
                sync.scan(cfg, ledger, None, root / 'data.db', source, request_id='s3', reason='Fixture')
                self.assertEqual(ledger.get_task('demo', 'task')['revision'], task['revision'])

    def test_crash_after_ledger_commit_replays_original_arguments(self):
        """protects=cross-store replay; fails_when=retry rebuilds revision;
        why_new=nontransactional stores; seam=post-commit fault; no_claim=atomic stores.
        """
        import sqlite3
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from urgency_engine.ledger import Ledger
        class Source:
            def inventory(self, binding):
                return [dict(id='task', tenant='synthetic')]
            def detail(self, binding, task_id):
                return dict(task=dict(id='task', tenant='synthetic', title='Deliver', status='done'),
                            parents=[], runs=[], events=[])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: NOW, artifact_root=root) as ledger:
                ledger.create_project('project', 'Project', request_id='p', actor='test')
                cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[BINDING]))
                original = ledger.upsert_task
                def crash(*args, **kwargs):
                    original(*args, **kwargs)
                    raise RuntimeError('synthetic committed crash')
                with kanban.Sync(sqlite3.connect(root / 'kanban.db'), clock=lambda: NOW,
                                 redact=lambda text: text) as sync:
                    with patch.object(ledger, 'upsert_task', crash), self.assertRaises(RuntimeError):
                        sync.scan(cfg, ledger, None, root / 'data.db', Source(), request_id='scan', reason='Fixture')
                with kanban.Sync(sqlite3.connect(root / 'kanban.db'), clock=lambda: NOW,
                                 redact=lambda text: text) as sync:
                    self.assertEqual(sync.scan(cfg, ledger, None, root / 'data.db', Source(),
                        request_id='scan', reason='Fixture')['state'], 'complete')
                    self.assertEqual(ledger.get_task('demo', 'task')['revision'], 2)

    def test_import_claim_and_unchanged_replay_preserve_unknown_work(self):
        """protects=canonical claims never verify; fails_when=done credits work;
        why_new=durable sync; seam=synthetic source, real stores; no_claim=native host.
        """
        import sqlite3
        import tempfile
        from pathlib import Path
        from urgency_engine.ledger import Ledger
        from urgency_engine.watchdog import Monitor
        class Source:
            def inventory(self, binding):
                return [dict(id='task', tenant='synthetic')]
            def detail(self, binding, task_id):
                return dict(task=dict(id='task', tenant='synthetic', title='Deliver', status='done'),
                            parents=[], runs=[], events=[])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: NOW, artifact_root=root) as ledger:
                ledger.create_project('project', 'Project', request_id='p', actor='test')
                with Monitor.open(root / 'watchdog.db', clock=lambda: NOW) as monitor:
                    with kanban.Sync(sqlite3.connect(root / 'kanban.db'), clock=lambda: NOW,
                                     redact=lambda text: text) as sync:
                        cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[BINDING]))
                        first = sync.scan(cfg, ledger, monitor, root / 'data.db', Source(),
                                          request_id='scan1', reason='Fixture')
                        self.assertEqual(first['state'], 'complete')
                        task = ledger.get_task('demo', 'task')
                        self.assertEqual(task['status'], 'claimed_complete')
                        self.assertIsNone(task['remaining_seconds'])
                        self.assertIsNone(ledger.progress('project')['percent'])
                        sync.scan(cfg, ledger, monitor, root / 'data.db', Source(),
                                  request_id='scan2', reason='Fixture')
                        self.assertEqual(ledger.get_task('demo', 'task')['revision'], task['revision'])
                        self.assertEqual(sync.scan(cfg, ledger, monitor, root / 'data.db', Source(),
                                          request_id='scan1', reason='Fixture'), first)
                        with self.assertRaises(LedgerError) as caught:
                            sync.scan(cfg, ledger, monitor, root / 'data.db', Source(),
                                      request_id='scan1', reason='Different')
                        self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')
                        self.assertEqual(sync.page('project')['items'][0]['task_id'], 'task')


class TransportTests(unittest.TestCase):
    def test_native_adapter_validates_board_before_scoped_inventory(self):
        """protects=explicit existing board; fails_when=unknown board initialized;
        why_new=public CLI selection; seam=transport receipt; no_claim=real host run.
        """
        import os
        from unittest.mock import patch
        context = patch.dict(os.environ, {}, clear=True)
        context.start()
        self.addCleanup(context.stop)
        calls = []
        def run(argv):
            calls.append(argv)
            if 'snapshot' in argv:
                return dict(ok=True, data=dict(task=dict(id='task'), parents=[]))
            return [{'slug': 'demo'}] if 'boards' in argv else []
        adapter = kanban.NativeSource(run)
        self.assertEqual(adapter.inventory(BINDING), [])
        self.assertEqual(calls[0][-4:], ['boards', 'list', '--json', '--all'])
        self.assertEqual(calls[1][-5:], ['list', '--tenant', 'synthetic', '--archived', '--json'])
        with self.assertRaises(LedgerError):
            adapter.inventory(dict(BINDING, board='absent'))
        self.assertEqual(len(calls), 3)
        adapter.detail(BINDING, 'task')
        self.assertEqual(calls[-1][-5:], ['snapshot', '--board', 'demo', '--task', 'task'])

    def test_transport_enforces_timeout_output_limit_and_json(self):
        """protects=owned subprocess bounds; fails_when=hang or output accepted;
        why_new=CLI transport; seam=real synthetic child; no_claim=host permissions.
        """
        import sys
        self.assertEqual(kanban.read_process([sys.executable, '-c', 'print("[]")']), [])
        for source, options, code in [
            ('import time; time.sleep(5)', {'timeout': .1}, 'SOURCE_TIMEOUT'),
            ('print("x"*100000)', {'limit': 1000}, 'SOURCE_LIMIT'),
            ('print("not-json")', {}, 'SOURCE_MALFORMED'),
            ('raise SystemExit(1)', {}, 'SOURCE_DENIED')]:
            with self.subTest(code=code), self.assertRaises(LedgerError) as caught:
                kanban.read_process([sys.executable, '-c', source], **options)
            self.assertEqual(caught.exception.code, code)


class ProjectionTests(unittest.TestCase):
    def test_configuration_is_explicit_bounded_and_disjoint(self):
        """protects=scope; fails_when=implicit or duplicate mapping accepted;
        why_new=opt-in adapter; seam=config values; no_claim=host authorization.
        """
        self.assertFalse(kanban.settings({})['kanban_sync_enabled'])
        cfg = dict(kanban_sync_enabled=True, kanban_bindings=[BINDING])
        self.assertEqual(kanban.settings(cfg)['kanban_bindings'], [BINDING])
        for bindings in ([BINDING, BINDING], [dict(BINDING, board='../other')],
                         [dict(BINDING, tenant='')], [dict(BINDING, extra=True)]):
            with self.subTest(bindings=bindings), self.assertRaises(LedgerError):
                kanban.settings(dict(cfg, kanban_bindings=bindings))
        for key, value in [('kanban_sync_seconds', 59), ('kanban_max_tasks', 10001),
                           ('kanban_sync_enabled', 'true')]:
            with self.subTest(key=key), self.assertRaises(LedgerError):
                kanban.settings(dict(cfg, **{key: value}))

    def test_worker_facts_require_current_run_identity(self):
        """protects=real worker identity; fails_when=old heartbeat admitted;
        why_new=run projection; seam=native-shaped document; no_claim=worker liveness.
        """
        document = dict(task=dict(id='task', tenant='synthetic', title='Deliver', status='running',
                                 started_at=100, current_run_id=2), parents=[],
            runs=[dict(id=2, status='running', started_at=100, ended_at=None)],
            events=[dict(kind='heartbeat', run_id=1, created_at=200),
                    dict(kind='heartbeat', run_id=2, created_at=150)])
        result = kanban.project(document, BINDING, NOW, lambda text: text)
        self.assertEqual(result['worker']['run_id'], 2)
        self.assertEqual(result['worker']['heartbeat_at'], kanban.timestamp(150))
        document['task'].pop('current_run_id')
        result = kanban.project(document, BINDING, NOW, lambda text: text)
        self.assertIsNone(result['worker']['run_id'])
        self.assertIsNone(result['worker']['heartbeat_at'])

    def test_resumed_worker_uses_exact_run_not_task_lifetime(self):
        """protects=resumed claim identity; fails_when=lifetime start rejects new run;
        why_new=public tool semantics; seam=projection; no_claim=native integration.
        """
        import copy
        document = dict(task=dict(id='task', tenant='synthetic', title='Deliver',
            status='running', assignee='fixture', started_at=100, current_run_id=2), parents=[],
            runs=[dict(id=2, profile='fixture', status='running', started_at=200, ended_at=None)],
            events=[dict(kind='heartbeat', run_id=1, created_at=400),
                    dict(kind='heartbeat', run_id=2, created_at=250)])
        worker = kanban.project(document, BINDING, NOW, str)['worker']
        self.assertEqual(worker, dict(run_id=2, state='running', started_at=kanban.timestamp(200),
                                    heartbeat_at=kanban.timestamp(250), ended_at=None))
        future_heartbeat = copy.deepcopy(document)
        future_heartbeat['events'].append(dict(kind='heartbeat', run_id=2, created_at=NOW.timestamp() + 1))
        self.assertIsNone(kanban.project(future_heartbeat, BINDING, NOW, str)['worker']['heartbeat_at'])
        for change in ('profile', 'future', 'ended', 'task_start', 'duplicate', 'missing'):
            bad = copy.deepcopy(document)
            if change == 'profile':
                bad['runs'][0]['profile'] = 'other'
            elif change == 'future':
                bad['runs'][0]['started_at'] = NOW.timestamp() + 1
            elif change == 'ended':
                bad['runs'][0]['ended_at'] = 150
            elif change == 'task_start':
                bad['task']['started_at'] = 300
            elif change == 'duplicate':
                bad['runs'].append(dict(bad['runs'][0]))
            else:
                bad['task'].pop('current_run_id')
            with self.subTest(change=change):
                self.assertTrue(all(v is None for v in kanban.project(bad, BINDING, NOW, str)['worker'].values()))

    def test_projection_does_not_promote_claims_or_polling_time(self):
        """protects=claim boundary; fails_when=poll becomes heartbeat;
        why_new=canonical projection; seam=CLI document; no_claim=semantic acceptance.
        """
        document = dict(task=dict(id='task', tenant='synthetic', title='Deliver',
                                 status='done', assignee=None, priority=0),
                        parents=[], runs=[], comments=[{'body': 'private'}])
        result = kanban.project(document, BINDING, NOW, lambda text: text)
        self.assertEqual(result['evidence'], dict(kind='canonical_status_only', verified=False))
        self.assertEqual(result['status'], 'done')
        self.assertIsNone(result['worker']['heartbeat_at'])
        self.assertIsNone(result['source_updated_at'])
        self.assertNotIn('private', str(result))
        self.assertEqual(result['coverage'], 'partial')


if __name__ == '__main__':
    unittest.main()

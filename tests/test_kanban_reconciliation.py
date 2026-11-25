"""Faulted canonical reads over disposable real stores; no production board I/O."""
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

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

    def slow_source_clock(self):
        # 100 detail reads consume the scanner's 20-second collection window.
        return patch.object(kanban, 'monotonic', lambda: self.source.calls / 5)

    def test_detail_timeout_retains_confirmed_prefix_for_same_request_restart(self):
        self.source.rows = [dict(id=f'task-{index}', tenant='synthetic') for index in range(3)]
        detail = self.source.detail

        def stop_at_second(binding, task_id):
            if task_id == 'task-1':
                raise LedgerError('SOURCE_TIMEOUT', 'kanban', 'board')
            return detail(binding, task_id)

        with patch.object(self.source, 'detail', side_effect=stop_at_second):
            with self.assertRaises(LedgerError) as caught:
                self.scan()
        self.assertEqual(caught.exception.code, 'SOURCE_TIMEOUT')
        self.assertEqual(caught.exception.side_effects, 'unknown')
        self.assertEqual(self.sync.meta('cursor'), ['demo', 'synthetic', 'task-0'])
        first = self.ledger.get_task('demo', 'task-0')
        self.assertEqual([item['task_id'] for item in self.sync.page('project')['items']], ['task-0'])
        self.sync.db.close()
        self.sync.db = sqlite3.connect(self.root / 'kanban.db')
        self.addCleanup(self.sync.db.close)
        self.sync.db.row_factory = sqlite3.Row
        self.assertEqual(self.scan(), dict(state='complete', count=2))
        self.assertIsNone(self.sync.meta('cursor'))
        self.assertEqual(self.ledger.get_task('demo', 'task-0')['revision'], first['revision'])
        self.assertEqual(self.source.calls, 3)

    def test_incomplete_inventory_does_not_record_absence(self):
        self.scan('seed')
        self.ledger.create_project('other', 'Other', request_id='other', actor='test')
        self.cfg['kanban_bindings'].append(dict(board='second', tenant='scope', project_id='other'))

        def inventory(binding):
            if binding['board'] == 'demo':
                return []
            raise LedgerError('SOURCE_TIMEOUT', 'kanban', 'transport')

        with patch.object(self.source, 'inventory', side_effect=inventory), \
             patch.object(self.sync, 'mark_unknown', wraps=self.sync.mark_unknown) as unknown:
            with self.assertRaises(LedgerError) as caught:
                self.scan('interrupted')
        self.assertEqual(caught.exception.code, 'SOURCE_TIMEOUT')
        unknown.assert_not_called()
        self.assertIsNone(self.sync.meta('cursor'))
        self.assertEqual([item['task_id'] for item in self.sync.page('project')['items']], ['task'])

    def test_dependency_changes_authoritative_milestone_forecast_only_with_parallel_capacity(self):
        """protects=authoritative ETA/slack/commitments; fails_when=edges affect only the bound;
        no_claim=live slots, native rendering or empirical calibration.
        """
        from urgency_engine.desktop import Views
        from urgency_engine.runtime import Runtime
        from test_ledger import fixture_zones, TZPATH, reset_tzpath, ZoneInfo

        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))

        board, project = BINDING['board'], BINDING['project_id']
        self.source.rows.append(dict(id='second', tenant=BINDING['tenant']))
        detail = self.source.detail
        edge = False

        def read(binding, task_id):
            row = detail(binding, task_id)
            if edge and task_id == 'task':
                row['parents'] = ['second']
            return row

        self.source.detail = read
        config = {**self.cfg, 'forecast_capacity': {project: {'slots': 2, 'observed_at': self.now.isoformat()}}}

        class Context:
            def get_config(self, key, default):
                return config.get(key, default)

            def redact_text(self, text, *, force):
                return text

        runtime = Runtime(Context(), clock=lambda: self.now, data_dir=lambda: self.root)
        self.assertEqual(self.scan('independent')['state'], 'complete')
        members = [[board, 'second'], [board, 'task']]
        for (_, task_id), seconds in zip(members, (1800, 3600)):
            self.ledger.upsert_task(board, task_id, project_id=project, goal='Synthetic work', remaining_seconds=seconds,
                expected_revision=self.ledger.get_task(board, task_id)['revision'], reason='Estimate',
                request_id='estimate-' + task_id, actor='operator')
        self.ledger.create_milestone('m', project_id=project, title='Fixture', request_id='m', actor='operator')
        self.ledger.set_milestone_tasks('m', members, expected_revision=self.ledger.get_project(project)['revision'],
                                       reason='Fixture membership', request_id='members', actor='operator')
        target = (self.now + timedelta(seconds=4500)).isoformat()
        self.ledger.set_deadline('d', project_id=project, kind='soft', at=target, timezone='UTC', owner='Synthetic',
                                 source='Fixture', milestone_id='m', request_id='d', actor='operator')

        def observed(slots, lower):
            config['forecast_capacity'][project]['slots'] = slots
            result = runtime.run_request('explain', {'deadline_id': 'd'}, surface='cli')
            self.assertTrue(result['ok'], result)
            page = self.sync.page(project, cfg=self.cfg, with_graph=True)
            bound = Views(self.ledger, clock=lambda: self.now).dependency_bound(project, page['graph'], page['items'])
            self.assertEqual(bound['lower_bound_seconds'], lower)
            row = result['data']
            self.assertEqual((row['original_at'], row['target_at']), (target, target))
            self.assertEqual(self.ledger.get_milestone('m')['tasks'], members)
            return row

        independent, serial_before = observed(2, 3600), observed(1, 3600)
        edge = True
        self.assertEqual(self.scan('dependent')['state'], 'complete')
        dependent, serial_after = observed(2, 5400), observed(1, 5400)
        self.assertEqual([r['input']['remaining_seconds'] for r in (independent, dependent)], [3600, 5400],
                         'Adding only a dependency must change the authoritative two-slot duration')
        self.assertEqual([r['input']['remaining_seconds'] for r in (serial_before, serial_after)], [5400, 5400])
        for row, seconds, slack in ((independent, 3600, 900), (dependent, 5400, -900),
                                     (serial_before, 5400, -900), (serial_after, 5400, -900)):
            self.assertEqual(row['forecast_at'], (self.now + timedelta(seconds=seconds)).isoformat())
            self.assertEqual(row['forecast']['effective_slack_seconds'], slack)
        self.assertLess(independent['forecast']['raw_level'], dependent['forecast']['raw_level'])

        # The Desktop domain view, native guidance and a durable snapshot use
        # the same result. Source is synthetic, not a pinned host or Electron.
        config['forecast_capacity'][project]['slots'] = 2
        config['default_project'] = project
        with runtime.forecast_scope(self.ledger):
            desktop = Views(self.ledger, clock=lambda: self.now).project(project)['commitments'][0]
            snapshot = self.ledger.snapshot_forecast('d', expected_revision=1, reason='Evidence',
                                                     request_id='forecast', actor='operator')
        for key in ('forecast_at', 'forecast', 'input', 'original_at', 'target_at'):
            self.assertEqual(desktop[key], dependent[key])
            self.assertEqual(snapshot[key], dependent[key])
        guidance = runtime.pre_llm_call()
        guidance_deadline, = [json.loads(line.removeprefix('Deadline: '))
                              for line in guidance['context'].splitlines() if line.startswith('Deadline: ')]
        self.assertEqual(guidance_deadline['input'], dependent['input'])

        # Verified scope and previously snapshotted hysteresis are not changed
        # by an improved raw forecast.
        edge = False
        self.assertEqual(self.scan('independent-again')['state'], 'complete')
        improving = observed(2, 3600)
        self.assertEqual(improving['forecast']['raw_level'], independent['forecast']['raw_level'])
        self.assertEqual(improving['forecast']['level'], dependent['forecast']['level'])

        # A stale capacity declaration stays stale even after a fresh scan.
        self.now += timedelta(seconds=61)
        def unknown(reason):
            result = runtime.run_request('explain', {'deadline_id': 'd'}, surface='cli')
            self.assertTrue(result['ok'], result)
            row = result['data']
            self.assertIsNone(row['forecast_at'])
            self.assertEqual(row['input']['schedule'], dict(method='unknown', reason=reason))
            self.assertEqual((row['original_at'], row['target_at']), (target, target))

        unknown('source_stale')
        self.assertEqual(self.scan('fresh-source')['state'], 'complete')
        unknown('capacity_unknown')
        config['forecast_capacity'][project]['observed_at'] = self.now.isoformat()
        self.assertEqual(observed(2, 3600)['input']['remaining_seconds'], 3600)
        with runtime.forecast_scope(self.ledger):
            # Commit a Sync invalidation after the forecast reader records its
            # version, then restore apparent coverage before the Ledger read.
            with self.sync.transaction():
                self.sync.put('incomplete_scan', 'concurrent')
            with self.sync.transaction():
                self.sync.put('incomplete_scan', None)
            self.assertEqual(self.ledger.explain_deadline('d')['input']['schedule']['reason'], 'source_incomplete')
        with self.sync.transaction():
            self.sync.put('dirty_bindings', [BINDING])
        unknown('source_incomplete')

        config.update(kanban_sync_enabled=False, kanban_bindings=[])
        unknown('source_unbound')

    def test_desktop_graph_requires_a_fresh_complete_scan_for_the_current_binding(self):
        """Real scan and retained store; no native board I/O or live-state claim."""
        def read(cfg=None):
            return self.sync.page('project', 1, cfg=cfg or self.cfg, with_graph=True)
        self.assertEqual(read()['graph']['reason'], 'source_incomplete')
        self.source.rows.append(dict(id='second', tenant='synthetic'))
        self.assertEqual(self.scan()['state'], 'complete')
        page = read()
        self.assertTrue(page['has_more'])
        self.assertEqual([row['task_id'] for row in page['graph']['items']], ['second', 'task'])
        self.assertIsNone(page['graph']['reason'])
        other = dict(BINDING, board='second-board')
        self.assertEqual(read(dict(self.cfg, kanban_bindings=[BINDING, other]))['graph']['reason'], 'source_incomplete')
        self.now += timedelta(seconds=61)
        self.assertEqual(read()['graph']['reason'], 'source_stale')
        self.scan('fresh')
        with self.sync.transaction():
            self.sync.put('dirty_bindings', [BINDING])
        self.assertEqual(read()['graph']['reason'], 'source_incomplete')
        self.source.rows.pop()
        self.scan('withdrawn')
        self.assertEqual(read()['graph']['reason'], 'source_incomplete')

    def test_desktop_graph_refuses_new_inventory_before_a_resumed_cursor(self):
        """protects=inventory coverage; fails_when=tail completion certifies an unseen prefix;
        why_new=Desktop graph receipt; seam=source reads and injected clock; no_claim=native timing.
        """
        self.source.rows = [dict(id=task_id, tenant='synthetic') for task_id in ('middle', 'tail')]
        with patch.object(kanban, 'monotonic', lambda: self.source.calls * 21):
            self.assertEqual(self.scan('partial')['state'], 'partial')
        self.source.rows.insert(0, dict(id='first', tenant='synthetic'))
        self.assertEqual(self.scan('resumed')['state'], 'complete')
        page = self.sync.page('project', cfg=self.cfg, with_graph=True)
        self.assertEqual([r['task_id'] for r in page['items']], ['middle', 'tail'])
        self.assertEqual(page['graph']['reason'], 'source_incomplete')
        self.assertEqual(self.scan('fresh-prefix')['state'], 'complete')
        self.assertIsNone(self.sync.page('project', cfg=self.cfg, with_graph=True)['graph']['reason'])

    def test_adaptive_intent_commit_boundaries(self):
        for low, high, size in ((1, 30, 10), (31, 60, 60), (61, 100, 100),
                                (101, 200, 200), (201, 400, 400), (401, 800, 800),
                                (801, 1600, 1600), (6401, 10000, 12800)):
            for remaining in (low, high):
                self.assertEqual(kanban.intent_batch_size(remaining), size)

        # Count durable intent inserts per transaction for previously unseen IDs.
        for count, expected in ((7, [7]), (27, [10, 10, 7]), (31, [31]), (201, [201])):
            batches = []
            pending = 0
            def trace(sql):
                nonlocal pending
                if sql.startswith('INSERT INTO sync_requests') and "'pending'" in sql:
                    pending += 1
                if sql == 'COMMIT' and pending:
                    batches.append(pending)
                    pending = 0
            self.sync.db.set_trace_callback(trace)
            self.source.rows = [dict(id=f'n{count}-{i}', tenant='synthetic') for i in range(count)]
            with patch.object(kanban, 'monotonic', return_value=0):
                self.assertEqual(self.scan(f'n{count}')['state'], 'complete')
            self.assertEqual(batches, expected)

    def test_detail_start_budget_includes_ledger_lookup(self):
        self.source.rows = [dict(id=task_id, tenant='synthetic') for task_id in ('t0', 't1')]
        get_task = self.ledger.get_task
        elapsed = 0
        detail_starts = []
        detail = self.source.detail

        def lookup(board_id, task_id):
            nonlocal elapsed
            if task_id == 't1':
                elapsed = 20.1
            return get_task(board_id, task_id)

        def capture(binding, task_id):
            detail_starts.append((task_id, elapsed))
            return detail(binding, task_id)

        with patch.object(kanban, 'monotonic', lambda: elapsed), \
             patch.object(self.ledger, 'get_task', lookup), \
             patch.object(self.source, 'detail', capture):
            result = self.scan()
            self.assertEqual(detail_starts, [('t0', 0)])
            self.assertEqual(result, dict(state='partial', count=1,
                                          cursor=[BINDING['board'], BINDING['tenant'], 't0']))
            self.assertEqual(get_task(BINDING['board'], 't0')['goal'], self.source.title)
            # The next page must resume at t1 and still make progress when its
            # first lookup alone takes the whole collection window.
            elapsed = 0
            self.assertEqual(self.scan('tail'), dict(state='complete', count=1))
            self.assertEqual(detail_starts, [('t0', 0), ('t1', 20.1)])

    def test_small_tail_uses_remaining_inventory(self):
        self.source.rows = [dict(id=f't{i:03}', tenant='synthetic') for i in range(125)]
        with self.slow_source_clock():
            self.assertEqual(self.scan()['count'], 100)
        statements = []
        self.sync.db.set_trace_callback(statements.append)
        with patch.object(kanban, 'monotonic', return_value=0):
            self.assertEqual(self.scan('tail')['count'], 25)
        self.assertEqual(statements.count('COMMIT'), 25 + 3 + 3)

    def test_intents_flush_at_byte_limit(self):
        self.source.rows = [dict(id=f't{i}', tenant='synthetic') for i in range(3)]
        statements = []
        self.sync.db.set_trace_callback(statements.append)
        with patch.object(kanban, 'MAX_INTENT_BYTES', 1):
            self.assertEqual(self.scan()['count'], 3)
        self.assertEqual(statements.count('COMMIT'), 3 * 2 + 3)

    def test_unchanged_inventory_does_not_inflate_the_changed_batch(self):
        self.source.rows = [dict(id=f't{i:02}', tenant='synthetic') for i in range(31)]
        self.enterContext(patch.object(kanban, 'monotonic', return_value=0))
        self.scan()
        read = self.source.detail
        def update(binding, task_id):
            doc = read(binding, task_id)
            if task_id >= 't04':
                doc['task']['title'] = 'Changed'
            return doc
        statements = []
        self.sync.db.set_trace_callback(statements.append)
        with patch.object(self.source, 'detail', side_effect=update):
            self.assertEqual(self.scan('refresh')['count'], 27)
        # Four unchanged cursor writes, 27 finalizations, 3 intent batches, 3 scan writes.
        self.assertEqual(statements.count('COMMIT'), 4 + 27 + 3 + 3)

    def test_changed_task_uses_only_intent_and_final_projection_commits(self):
        """protects=bounded durable write cost; fails_when=ack/cursor commit separately;
        why_new=measured Windows commit bottleneck; seam=real disk SQL trace;
        no_claim=hosted latency guarantee or cross-store atomicity.
        """
        statements = []
        self.sync.db.set_trace_callback(statements.append)
        self.source.rows = [dict(id=f't{i}', tenant='synthetic') for i in range(5)]
        self.scan()
        # Scan reservation, one bounded intent batch, per-task finalization,
        # cursor reset and receipt. Every intent still precedes its Ledger effect.
        self.assertEqual(statements.count('COMMIT'), 5 + 4)
        self.assertEqual(len(self.ledger.list_tasks('project')), 5)
        self.assertEqual(self.sync.db.execute("SELECT count(*) FROM sync_requests WHERE state='applied'").fetchone()[0], 5)
        self.assertIsNone(self.sync.meta('cursor'))

    def test_absent_monitor_does_not_read_per_task_graph_replay_metadata(self):
        """protects=bounded metadata reads without a Monitor consumer;
        fails_when=each retained source triggers an unused graph lookup;
        why_new=measured per-page read amplification; seam=real disk SQL trace;
        no_claim=hosted wall-time guarantee or permission to skip withdrawal.
        """
        self.source.rows = [dict(id=f't{i}', tenant='synthetic') for i in range(5)]
        self.scan()
        statements = []
        self.sync.db.set_trace_callback(statements.append)
        self.scan('unchanged')
        graph_reads = [sql for sql in statements if sql.startswith(
            'SELECT value_json FROM sync_meta') and 'graph:' in sql]
        self.assertEqual(graph_reads, [])
        # No Monitor does not excuse retaining authoritative cached coverage.
        self.cfg['kanban_bindings'] = []
        self.scan('withdrawn')
        self.assertEqual({row[0] for row in self.sync.db.execute(
            'SELECT coverage FROM sources')}, {'unknown'})
        self.assertEqual(len(self.ledger.list_tasks('project')), 5)

    def test_intent_and_final_commit_faults_replay_without_duplicate_effects(self):
        """protects=write-ahead intent and atomic final cursor/projection/ack;
        fails_when=commit faults lose work or replay changes timestamps/revisions;
        why_new=coalesced final commit; seam=real stores with commit-boundary faults;
        no_claim=distributed atomicity or power-loss simulation.
        """
        from urgency_engine.watchdog import Monitor
        from unittest.mock import patch

        class FaultConnection(sqlite3.Connection):
            armed = False
            def commit(db):
                row = db.execute("SELECT state FROM sync_requests WHERE state IN ('pending','applied')").fetchone()
                fault = db.armed and row and row[0] == boundary
                if fault:
                    db.armed = False
                    if not after:
                        raise RuntimeError('before commit')
                super().commit()
                if fault:
                    raise RuntimeError('after commit')

        for boundary in ('pending', 'applied'):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    clock = [NOW]
                    with Ledger(sqlite3.connect(root / 'ledger.db'), clock=lambda: clock[0], artifact_root=root) as ledger, \
                            Monitor.open(root / 'monitor.db', clock=lambda: clock[0]) as monitor:
                        ledger.create_project('project', 'Project', request_id='p', actor='test')
                        with kanban.Sync(sqlite3.connect(root / 'sync.db', factory=FaultConnection),
                                         clock=lambda: clock[0], redact=str) as sync:
                            sync.db.armed = True
                            observe = monitor.observe
                            def unlocked_observe(*args, **kwargs):
                                self.assertFalse(sync.db.in_transaction)
                                return observe(*args, **kwargs)
                            with patch.object(monitor, 'observe', unlocked_observe), self.assertRaises(RuntimeError):
                                sync.scan(self.cfg, ledger, monitor, root / 'ledger.db',
                                          Source(), request_id='scan', reason='Fixture')
                            published = boundary == 'applied' and after
                            self.assertEqual(len(sync.page('project')['items']), int(published))
                            self.assertEqual(sync.meta('cursor'), ['demo', 'synthetic', 'task'] if published else None)
                            state = sync.db.execute("SELECT state FROM sync_requests WHERE state IN ('pending','applied')").fetchone()
                            self.assertEqual(state[0] if state else None,
                                             'applied' if published else 'pending' if boundary == 'applied' or after else None)
                            before = monitor.page('observations', project_id='project')['items']
                        clock[0] += timedelta(seconds=1)
                        with kanban.Sync(sqlite3.connect(root / 'sync.db'), clock=lambda: clock[0], redact=str) as sync:
                            result = sync.scan(self.cfg, ledger, monitor, root / 'ledger.db',
                                               Source(), request_id='scan', reason='Fixture')
                            self.assertEqual(result['state'], 'complete')
                            self.assertEqual(ledger.get_task('demo', 'task')['revision'], 1)
                            observations = monitor.page('observations', project_id='project')['items']
                            self.assertEqual(len(observations), 2)
                            for event in before:
                                self.assertIn(event, observations)
                            self.assertEqual(sync.db.execute("SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0], 0)
                            self.assertIsNone(sync.meta('cursor'))
                            self.assertEqual(len(sync.page('project')['items']), 1)

    def test_batched_intents_survive_commit_and_application_faults(self):
        """protects=all page intents durable before effects and ordered recovery;
        fails_when=batch loss, premature cursor or duplicate effects;
        why_new=bounded intent batching; seam=real disk stores/commit faults;
        no_claim=power-loss simulation or cross-store atomicity.
        """
        from unittest.mock import patch
        from urgency_engine.watchdog import Monitor

        for boundary in ('intent', 'third_ack'):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    clock = [NOW]
                    class FaultConnection(sqlite3.Connection):
                        armed = False
                        def commit(db):
                            pending = db.execute("SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0]
                            applied = db.execute("SELECT count(*) FROM sync_requests WHERE state='applied'").fetchone()[0]
                            hit = db.armed and ((boundary == 'intent' and pending == 5) or
                                                (boundary == 'third_ack' and applied == 3))
                            if hit:
                                db.armed = False
                                if not after:
                                    raise RuntimeError('commit fault')
                            super().commit()
                            if hit:
                                raise RuntimeError('commit fault')

                    source = Source()
                    source.rows = [dict(id=f't{i}', tenant='synthetic') for i in range(5)]
                    with Ledger(sqlite3.connect(root / 'ledger.db'), clock=lambda: clock[0], artifact_root=root) as ledger, \
                            Monitor.open(root / 'monitor.db', clock=lambda: clock[0]) as monitor:
                        ledger.create_project('project', 'Project', request_id='p', actor='test')
                        with kanban.Sync(sqlite3.connect(root / 'sync.db', factory=FaultConnection),
                                         clock=lambda: clock[0], redact=str) as sync:
                            sync.db.armed = True
                            original = ledger.upsert_task
                            def apply(*args, **kwargs):
                                self.assertFalse(sync.db.in_transaction)
                                # Another connection observes committed intent for the whole batch.
                                from contextlib import closing
                                with closing(sqlite3.connect(root / 'sync.db')) as reader:
                                    self.assertEqual(reader.execute("SELECT count(*) FROM sync_requests WHERE state IN ('pending','applied')").fetchone()[0], 5)
                                return original(*args, **kwargs)
                            with patch.object(ledger, 'upsert_task', apply), self.assertRaisesRegex(RuntimeError, 'commit fault'):
                                sync.scan(self.cfg, ledger, monitor, root / 'ledger.db', source,
                                          request_id='batch', reason='Fixture')
                            published = 0 if boundary == 'intent' else 3 if after else 2
                            self.assertEqual(sync.db.execute('SELECT count(*) FROM sources').fetchone()[0], published)
                            self.assertEqual(sync.meta('cursor'), ['demo', 'synthetic', f't{published-1}'] if published else None)
                            old = monitor.page('observations', project_id='project')['items']
                        clock[0] += timedelta(seconds=1)
                        with kanban.Sync(sqlite3.connect(root / 'sync.db'), clock=lambda: clock[0], redact=str) as sync:
                            inventory = source.inventory
                            def after_replay(binding):
                                if boundary == 'third_ack' or after:
                                    self.assertEqual(sync.meta('cursor'), ['demo', 'synthetic', 't4'])
                                return inventory(binding)
                            source.inventory = after_replay
                            result = sync.scan(self.cfg, ledger, monitor, root / 'ledger.db', source,
                                               request_id='batch', reason='Fixture')
                            self.assertEqual(result['state'], 'complete')
                            self.assertIsNone(sync.meta('cursor'))
                            self.assertEqual(sync.db.execute("SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0], 0)
                            for i in range(5):
                                self.assertEqual(ledger.get_task('demo', f't{i}')['revision'], 1)
                            observations = monitor.page('observations', project_id='project')['items']
                            self.assertEqual(len(observations), 10)
                            for event in old:
                                self.assertIn(event, observations)

    def test_unchanged_cursor_never_skips_a_buffered_changed_task(self):
        """protects=mixed-page durable cursor; fails_when=unchanged row passes volatile intent;
        why_new=batch flush boundary; seam=real stores/restart; no_claim=atomic source snapshot.
        """
        from unittest.mock import patch
        self.source.rows = [dict(id=f't{i}', tenant='synthetic') for i in range(3)]
        self.scan()
        original = self.source.detail
        def detail(binding, task_id):
            document = original(binding, task_id)
            if task_id == 't0':
                document['task']['title'] = 'Changed'
            return document
        self.source.detail = detail
        with patch.object(self.ledger, 'upsert_task', side_effect=RuntimeError('before effect')):
            with self.assertRaisesRegex(RuntimeError, 'before effect'):
                self.scan('mixed')
        self.assertIsNone(self.sync.meta('cursor'))
        self.assertEqual(self.sync.db.execute("SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0], 1)
        self.sync.db.close()
        self.sync = kanban.Sync(sqlite3.connect(self.root / 'kanban.db'), clock=lambda: self.now, redact=str)
        self.addCleanup(self.sync.db.close)
        self.assertEqual(self.scan('mixed')['state'], 'complete')
        self.assertEqual(self.ledger.get_task('demo', 't0')['goal'], 'Changed')
        self.assertEqual([self.ledger.get_task('demo', f't{i}')['revision'] for i in range(3)], [2, 1, 1])

    def test_later_detail_cannot_overwrite_an_earlier_buffered_local_edit(self):
        """protects=CAS across bounded collection; fails_when=batch replaces local edit;
        why_new=longer read/effect interval; seam=two real connections; no_claim=distributed transaction.
        """
        self.source.rows = [dict(id=f't{i}', tenant='synthetic') for i in range(2)]
        self.scan()
        original = self.source.detail
        self.source.title = 'New source'
        def detail(binding, task_id):
            if task_id == 't1':
                with Ledger(sqlite3.connect(self.root / 'data.db'), clock=lambda: self.now, artifact_root=self.root) as local:
                    local.upsert_task('demo', 't0', project_id='project', goal='Local edit', remaining_seconds=7,
                        expected_revision=1, request_id='local', reason='Concurrent', actor='test')
            return original(binding, task_id)
        self.source.detail = detail
        with self.assertRaises(LedgerError) as caught:
            self.scan('race')
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertEqual(self.ledger.get_task('demo', 't0')['goal'], 'Local edit')
        self.assertEqual(self.ledger.get_task('demo', 't0')['remaining_seconds'], 7)
        self.assertEqual(self.ledger.get_task('demo', 't1')['revision'], 1)
        self.assertEqual(self.sync.db.execute("SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0], 2)

    def test_replay_capacity_refuses_new_writes_without_pruning_old_requests(self):
        """protects=bounded durable replay; fails_when=capacity discards pending identities;
        why_new=protected storage ceiling; seam=real store; no_claim=unlimited operation history.
        """
        self.sync.control('pause', 'original', 'Fixture')
        self.sync.db.executemany('INSERT INTO sync_requests VALUES(?,?,?,?)',
                                [(f'pending{i}', 'digest', 'pending', '{}') for i in range(49999)])
        self.sync.db.commit()
        with self.assertRaises(LedgerError) as caught:
            self.sync.control('resume', 'overflow', 'Fixture')
        self.assertEqual(caught.exception.code, 'SOURCE_LIMIT')
        self.assertEqual(caught.exception.field, 'requests_capacity')
        self.assertTrue(self.sync.meta('paused'))
        self.assertTrue(self.sync.control('pause', 'original', 'Fixture')['paused'])
        self.assertEqual(self.sync.db.execute('SELECT count(*) FROM sync_requests').fetchone()[0], 50000)
        self.assertEqual(self.sync.db.execute("SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0], 49999)

    def test_source_capacity_refuses_before_ledger_mutation(self):
        """protects=bounded projection without orphaned local writes; fails_when=20001st source admitted;
        why_new=store ceiling; seam=real stores with synthetic retained rows; no_claim=source throughput.
        """
        import copy
        self.scan()
        row = tuple(self.sync.db.execute('SELECT * FROM sources').fetchone())
        self.sync.db.executemany('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                [(row[0], f'retained{i}', *row[2:]) for i in range(19999)])
        self.sync.db.commit()
        plan = json.loads(self.sync.db.execute("SELECT result_json FROM sync_requests WHERE state='applied'").fetchone()[0])
        plan = copy.deepcopy(plan)
        plan['projection']['task_id'] = 'overflow'
        plan['arguments'].update(task_id='overflow', request_id='overflow:task', expected_revision=0)
        with self.assertRaises(LedgerError) as caught:
            self.sync.apply_plan('overflow', plan, self.ledger)
        self.assertEqual(caught.exception.code, 'SOURCE_LIMIT')
        self.assertEqual(caught.exception.field, 'sources_capacity')
        self.assertEqual(len(self.ledger.list_tasks('project')), 1)
        self.assertEqual(self.sync.db.execute('SELECT count(*) FROM sources').fetchone()[0], 20000)

    def test_event_retention_prunes_age_and_count_without_touching_replay(self):
        """protects=bounded audit with replay safety; fails_when=old events accumulate;
        why_new=source retention; seam=real local store; no_claim=production retention timing.
        """
        old = (NOW - timedelta(days=31)).isoformat()
        self.sync.db.executemany('INSERT INTO sync_events(occurred_at,kind,detail_json) VALUES(?,?,?)',
                                [(old, 'old', '{}')] + [(NOW.isoformat(), 'recent', '{}')] * 10000)
        self.sync.db.execute('INSERT INTO sync_requests VALUES(?,?,?,?)', ('pending-proof', 'digest', 'pending', '{}'))
        self.sync.db.commit()
        self.sync.control('pause', 'pause', 'Fixture')
        self.assertEqual(self.sync.db.execute('SELECT count(*) FROM sync_events').fetchone()[0], 10000)
        self.assertEqual(self.sync.db.execute("SELECT count(*) FROM sync_events WHERE kind='old'").fetchone()[0], 0)
        self.assertEqual(self.sync.db.execute("SELECT result_json FROM sync_requests WHERE request_id='pending-proof'").fetchone()[0], '{}')
        self.assertTrue(self.sync.control('pause', 'pause', 'Fixture')['paused'])

    def test_process_owner_death_releases_lock_without_erasing_pending_scan(self):
        """protects=single owner/crash recovery; fails_when=parallel owner enters;
        why_new=real process death; seam=disposable store/OS lock; no_claim=worker recovery execution.
        """
        import subprocess
        import sys
        import time
        code = '''
import sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
from urgency_engine.kanban import Sync
with Sync(sqlite3.connect(sys.argv[1]), clock=lambda: datetime.now(timezone.utc), redact=str) as sync:
    with sync.owner():
        with sync.transaction():
            sync.put('incomplete_scan', 'process-owned')
        Path(sys.argv[2]).write_text('locked')
        sys.stdin.read()
'''
        marker = self.root / 'locked'
        child = subprocess.Popen([sys.executable, '-c', code, str(self.root / 'kanban.db'), str(marker)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), 'child did not acquire lock')
            with self.assertRaises(LedgerError) as caught:
                with self.sync.owner():
                    self.fail('second process entered owned scan')
            self.assertEqual(caught.exception.code, 'STORE_BUSY')
            child.kill()
            child.communicate(timeout=10)
            self.assertNotEqual(child.returncode, 0)
            with self.sync.owner():
                self.assertEqual(self.sync.meta('incomplete_scan'), 'process-owned')
            self.assertEqual(self.scan('after-death')['state'], 'complete')
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_ten_thousand_sources_finish_after_restart_and_key_churn(self):
        """protects=bounded eventual coverage; fails_when=cursor starves late/new keys;
        why_new=10000 source acceptance; seam=synthetic source/real stores;
        no_claim=native CLI throughput or production latency.
        """
        import time
        started = time.monotonic()
        self.source.rows = [dict(id=f't{i:05d}', tenant='synthetic') for i in range(10000)]
        with self.slow_source_clock():
            self.assertEqual(self.scan('page0')['state'], 'partial')
        # Replace an already-consumed key with a lower-sorting key, then restart.
        self.source.rows[0] = dict(id='a-new', tenant='synthetic')
        self.sync.db.close()
        self.sync = kanban.Sync(sqlite3.connect(self.root / 'kanban.db'),
                                clock=lambda: self.now, redact=lambda text: text)
        self.addCleanup(self.sync.db.close)
        for index in range(1, 101):
            result = self.scan(f'page{index}')
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
        self.enterContext(self.slow_source_clock())
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

    def test_source_update_across_detail_boundary_converges_in_both_orders(self):
        """protects=source convergence; fails_when=late/new source snapshot loses next update;
        why_new=both read/update orders; seam=controlled public-source boundary/real stores;
        no_claim=atomic native snapshot.
        """
        original = self.source.detail
        for update_first in (False, True):
            with self.subTest(update_first=update_first):
                old, new = ('Old-' + str(update_first), 'New-' + str(update_first))
                self.source.title = old
                def detail(binding, task_id):
                    if update_first:
                        self.source.title = new
                    document = original(binding, task_id)
                    self.source.title = new
                    return document
                self.source.detail = detail
                self.scan('race-' + str(update_first))
                self.assertEqual(self.ledger.get_task('demo', 'task')['goal'], new if update_first else old)
                self.source.detail = original
                self.scan('settled-' + str(update_first))
                self.assertEqual(self.ledger.get_task('demo', 'task')['goal'], new)
                revision = self.ledger.get_task('demo', 'task')['revision']
                self.scan('repeated-' + str(update_first))
                self.assertEqual(self.ledger.get_task('demo', 'task')['revision'], revision)
                self.assertIsNone(self.sync.page('project')['items'][0]['worker']['heartbeat_at'])

    def test_criterion_edit_between_read_and_apply_is_not_overwritten(self):
        """protects=local criteria and weights; fails_when=sync bypasses shared revision;
        why_new=criterion interleaving; seam=real separate Ledger connection;
        no_claim=semantic evidence certification.
        """
        self.scan()
        original = self.source.detail
        before = self.ledger.get_task('demo', 'task')
        criteria = [dict(criterion_id='new', description='Local criterion', weight=7, required=True, expected_sha256=None)]
        def detail(binding, task_id):
            with Ledger(sqlite3.connect(self.root / 'data.db'), clock=lambda: self.now, artifact_root=self.root) as local:
                local.set_criteria('demo', 'task', criteria, expected_revision=before['revision'],
                    reason='Concurrent', request_id='local-criteria', actor='test')
            return original(binding, task_id)
        self.source.title = 'Changed source'
        self.source.detail = detail
        with self.assertRaises(LedgerError) as caught:
            self.scan('criteria-race')
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        task = self.ledger.get_task('demo', 'task')
        self.assertEqual(task['goal'], before['goal'])
        self.assertEqual(task['criteria'][0]['weight'], 7)
        self.assertEqual(task['criteria'][0]['description'], 'Local criterion')
        self.assertIsNone(task['remaining_seconds'])

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

    def test_null_source_revision_does_not_erase_ordering_after_restart(self):
        """protects=source high-water; fails_when=null permits rollback;
        why_new=known/null/older; seam=disk stores; no_claim=ordering nulls.
        """
        original = self.source.detail
        stamp = 200
        def detail(binding, task_id):
            document = original(binding, task_id)
            document['task']['updated_at'] = stamp
            return document
        self.source.detail = detail
        self.scan()
        stamp = None
        self.source.title = 'Unversioned'
        self.scan('null')
        self.assertIsNone(self.sync.page('project')['items'][0]['source_updated_at'])
        before = self.ledger.get_task('demo', 'task')
        self.assertEqual(before['goal'], 'Unversioned')
        with kanban.Sync(sqlite3.connect(self.root / 'kanban.db'), clock=lambda: self.now, redact=str) as sync:
            def scan(identity):
                return sync.scan(self.cfg, self.ledger, None, self.root / 'data.db',
                                 self.source, request_id=identity, reason='Fixture')
            stamp = 100
            self.source.title = 'Older'
            with self.assertRaises(LedgerError) as caught:
                scan('old-after-null')
            self.assertEqual(caught.exception.code, 'SOURCE_STALE')
            self.assertEqual(self.ledger.get_task('demo', 'task'), before)
            self.assertEqual(sync.page('project')['items'][0]['coverage'], 'unknown')
            stamp = 300
            self.source.title = 'Newer'
            self.assertEqual(scan('fresh')['state'], 'complete')
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
        """protects=scope generation; fails_when=obsolete plan permanently stalls collection;
        why_new=binding changes after intent commit; seam=real stores; no_claim=distributed cancellation.
        """
        from unittest.mock import patch
        with patch.object(self.ledger, 'upsert_task', side_effect=RuntimeError('before Ledger effect')):
            with self.assertRaisesRegex(RuntimeError, 'before Ledger effect'):
                self.scan()
        self.assertEqual(self.ledger.list_tasks('project'), [])
        identity, original = self.sync.db.execute(
            "SELECT request_id,result_json FROM sync_requests WHERE state='pending'").fetchone()
        self.ledger.create_project('other', 'Other', request_id='other', actor='test')
        self.cfg['kanban_bindings'][0]['project_id'] = 'other'
        with self.assertRaises(LedgerError) as caught:
            self.scan('changed')
        self.assertEqual(caught.exception.code, 'SCOPE_CONFLICT')
        self.assertEqual(tuple(self.sync.db.execute('SELECT state,result_json FROM sync_requests WHERE request_id=?',
                                                  (identity,)).fetchone()), ('cancelled', original))
        self.assertEqual(self.ledger.list_tasks('project'), [])
        self.source.title = 'Current scope'
        self.assertEqual(self.scan('changed')['state'], 'complete')
        task = self.ledger.get_task('demo', 'task')
        self.assertEqual((task['project_id'], task['goal']), ('other', 'Current scope'))

    def test_binding_change_preserves_intent_after_ledger_effect(self):
        """protects=partially applied intent; fails_when=rebinding discards committed work;
        why_new=cancellation boundary; seam=real Ledger and Monitor; no_claim=distributed atomicity.
        """
        from unittest.mock import patch
        from urgency_engine.watchdog import Monitor
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            with patch.object(monitor, 'observe', side_effect=RuntimeError('after Ledger effect')):
                with self.assertRaisesRegex(RuntimeError, 'after Ledger effect'):
                    self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                                   self.source, request_id='scan', reason='Fixture')
        before = self.ledger.get_task('demo', 'task')
        self.ledger.create_project('other', 'Other', request_id='other', actor='test')
        self.cfg['kanban_bindings'][0]['project_id'] = 'other'
        for identity in ('changed', 'changed-again'):
            with self.assertRaises(LedgerError) as caught:
                self.scan(identity)
            self.assertEqual(caught.exception.code, 'SCOPE_CONFLICT')
        self.assertEqual(self.sync.db.execute(
            "SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0], 1)
        self.assertEqual(self.ledger.get_task('demo', 'task'), before)

    def test_rebinding_does_not_move_an_existing_ledger_task(self):
        self.scan()
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
            self.assertEqual(len(observations), 2)
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='unchanged-after-crash', reason='Fixture')
            self.assertEqual(len(monitor.page('observations', project_id='project')['items']), 2)
            self.assertEqual(self.ledger.get_task('demo', 'task')['revision'], revision)
            self.assertEqual(self.sync.db.execute(
                "SELECT count(*) FROM sync_requests WHERE state='pending'").fetchone()[0], 0)

    def test_paged_graph_rejects_expired_observations(self):
        """protects=current graph coverage; fails_when=tail refresh certifies stale head;
        why_new=scan spans freshness window; seam=real stores/injected clock;
        no_claim=atomic native snapshot or production throughput.
        """
        from urgency_engine.watchdog import Monitor
        self.enterContext(self.slow_source_clock())
        self.source.rows = [dict(id=f't{i:03d}', tenant='synthetic') for i in range(101)]
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            def scan(identity):
                return self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                                      self.source, request_id=identity, reason='Fixture')
            self.assertEqual(scan('head')['state'], 'partial')
            self.now += timedelta(seconds=61)
            self.assertEqual(scan('tail')['state'], 'complete')
            states = [r[0] for r in monitor.db.execute('SELECT dependency_state FROM task_signals')]
            self.assertEqual(len(states), 101)
            self.assertEqual(set(states), {'unknown'})
            self.assertEqual(scan('fresh-head')['state'], 'partial')
            self.assertEqual(scan('fresh-tail')['state'], 'complete')
            self.assertEqual({r[0] for r in monitor.db.execute('SELECT dependency_state FROM task_signals')}, {'available'})

    def test_paged_graph_rejects_future_observations(self):
        """protects=clock reversal coverage; fails_when=future head certifies graph;
        why_new=negative observation age; seam=real stores/injected clock;
        no_claim=clock synchronization.
        """
        from urgency_engine.watchdog import Monitor
        self.enterContext(self.slow_source_clock())
        self.source.rows = [dict(id=f't{i:03d}', tenant='synthetic') for i in range(101)]
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='head', reason='Fixture')
            self.now -= timedelta(seconds=1)
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='tail', reason='Fixture')
            self.assertEqual({r[0] for r in monitor.db.execute('SELECT dependency_state FROM task_signals')}, {'unknown'})

    def test_graph_commit_crash_replays_original_event_after_clock_advance(self):
        """protects=graph admission replay; fails_when=retry invents new timestamp;
        why_new=second-pass crash; seam=real Monitor post-commit fault; no_claim=distributed transaction.
        """
        from unittest.mock import patch
        from urgency_engine.watchdog import Monitor
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            original = monitor.observe
            def crash(event, *args, **kwargs):
                result = original(event, *args, **kwargs)
                if event['request_id'].endswith(':mapped'):
                    raise RuntimeError('after graph commit')
                return result
            with patch.object(monitor, 'observe', crash), self.assertRaises(RuntimeError):
                self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                               self.source, request_id='graph-crash', reason='Fixture')
            before = monitor.page('observations', project_id='project')['items']
            self.now += timedelta(seconds=60)
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='graph-crash', reason='Fixture')
            self.assertEqual(monitor.page('observations', project_id='project')['items'], before)
            self.assertEqual(self.sync.meta('graph:["demo","task"]:pending'), None)

    def test_failed_collection_preserves_edges_but_revokes_graph_coverage(self):
        """protects=failed source is unknown; fails_when=old graph stays available;
        why_new=partial admission; seam=real stores; no_claim=source recovery.
        """
        from urgency_engine.watchdog import Monitor
        self.source.rows.append(dict(id='parent', tenant='synthetic'))
        original = self.source.detail
        def detail(binding, task_id):
            document = original(binding, task_id)
            document['parents'] = ['parent'] if task_id == 'task' else []
            return document
        self.source.detail = detail
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='first', reason='Fixture')
            before = [tuple(row) for row in monitor.db.execute('SELECT * FROM dependencies')]
            self.source.fail = 'inventory'
            self.now += timedelta(seconds=60)
            with self.assertRaises(LedgerError):
                self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                               self.source, request_id='failed', reason='Fixture')
            self.assertEqual([tuple(row) for row in monitor.db.execute('SELECT * FROM dependencies')], before)
            self.assertEqual([row[0] for row in monitor.db.execute('SELECT dependency_state FROM task_signals')],
                             ['unknown', 'unknown'])

    def test_partial_scan_revokes_previous_complete_graph_until_tail_read(self):
        """protects=partial graphs cannot certify health; fails_when=old available survives cap;
        why_new=multi-tick refresh; seam=real stores; no_claim=atomic board snapshot.
        """
        from urgency_engine.watchdog import Monitor
        self.enterContext(self.slow_source_clock())
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='small', reason='Fixture')
            self.assertEqual(monitor.db.execute('SELECT dependency_state FROM task_signals').fetchone()[0], 'available')
            self.source.rows.extend(dict(id=f'z{i:03d}', tenant='synthetic') for i in range(100))
            self.now += timedelta(seconds=60)
            result = self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                                    self.source, request_id='partial', reason='Fixture')
            self.assertEqual(result['state'], 'partial')
            self.assertEqual(set(row[0] for row in monitor.db.execute('SELECT dependency_state FROM task_signals')), {'unknown'})
            self.now += timedelta(seconds=60)
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='tail', reason='Fixture')
            self.assertEqual(set(row[0] for row in monitor.db.execute('SELECT dependency_state FROM task_signals')), {'available'})

    def test_complete_graph_is_admitted_in_both_import_orders(self):
        """protects=complete mapped edges; fails_when=later parent stays omitted;
        why_new=two-pass graph; seam=real stores; no_claim=atomic source snapshot.
        """
        from urgency_engine.watchdog import Monitor
        self.source.rows = [dict(id=name, tenant='synthetic') for name in ('a', 'z')]
        original = self.source.detail
        edges = {'a': ['z'], 'z': []}
        def detail(binding, task_id):
            document = original(binding, task_id)
            document['parents'] = edges[task_id]
            return document
        self.source.detail = detail
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            for index, expected in enumerate((('a', 'z'), ('z', 'a'))):
                self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                               self.source, request_id=f'graph{index}', reason='Fixture')
                actual = [tuple(row) for row in monitor.db.execute(
                    'SELECT task_id,depends_task FROM dependencies ORDER BY task_id')]
                self.assertEqual(actual, [expected])
                states = [row[0] for row in monitor.db.execute('SELECT dependency_state FROM task_signals')]
                self.assertEqual(states, ['available', 'available'])
                edges = {'a': [], 'z': ['a']}
                self.now += timedelta(seconds=60)

    def test_worker_claim_uses_source_times_and_null_removes_coverage(self):
        """protects=actual claim timing; fails_when=poll time replaces source time;
        why_new=Monitor claim admission; seam=real stores; no_claim=live worker health.
        """
        from urgency_engine.watchdog import Monitor
        original = self.source.detail
        present = [True]
        def detail(binding, task_id):
            document = original(binding, task_id)
            if present[0]:
                document['task'].update(current_run_id=4, started_at=100)
                document.update(runs=[dict(id=4, status='running', started_at=100)],
                                events=[dict(kind='heartbeat', run_id=4, created_at=150)])
            return document
        self.source.detail = detail
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='worker', reason='Fixture')
            row = dict(monitor.db.execute('SELECT * FROM task_signals').fetchone())
            self.assertEqual(row['claim_at'], kanban.timestamp(100))
            self.assertEqual(row['heartbeat_at'], kanban.timestamp(150))
            present[0] = False
            self.now += timedelta(seconds=60)
            self.sync.scan(self.cfg, self.ledger, monitor, self.root / 'data.db',
                           self.source, request_id='absent-worker', reason='Fixture')
            row = dict(monitor.db.execute('SELECT * FROM task_signals').fetchone())
            self.assertIsNone(row['claim_at'])
            self.assertIsNone(row['heartbeat_at'])
            self.assertIsNone(row['agent_available'])

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
        self.assertTrue(any(item['kind'] == 'task_signal' and item['payload']['data'].get('dependency_state') == 'unknown' for item in signal))


if __name__ == '__main__':
    unittest.main()

"""Behavioral checks for the native transport boundary."""
import argparse
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, reset_tzpath, TZPATH

from test_ledger import fixture_zones
from urgency_engine.ledger import Ledger
from urgency_engine.runtime import Parser, Runtime, setup_parser


class State:
    quota_bytes = 10 * 1024 * 1024

    def __init__(self, path):
        self.values = {}
        self.path = path

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        self.path.write_text(json.dumps(self.values), encoding='utf-8')


class Context:
    def __init__(self, path):
        self.state = State(path)
        self.settings = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def redact_text(self, text, force=False):
        assert force
        return text.replace('SYNTHETIC_SECRET', '[REDACTED]')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.ctx = Context(self.root / 'state.json')
        def opener(**kwargs):
            return Ledger(sqlite3.connect(self.root / 'data.db'), clock=kwargs['clock'], artifact_root=self.root)
        self.runtime = Runtime(self.ctx, clock=lambda: self.now, opener=opener, data_dir=lambda: self.root)

    def request(self, op, **args):
        return self.runtime.run_request(op, args, surface='cli', session_id=None)

    def project(self, name='p'):
        return self.request('create_project', project_id=name, title='Objective SYNTHETIC_SECRET', request_id=name)

    def test_snapshot_is_scoped_minimal_and_does_not_open_stores(self):
        """protects=public source read; fails_when=private data leaks or read writes;
        why_new=bounded detail bridge; seam=real CLI/mock public tool; no_claim=native fences.
        """
        import copy
        import types
        from unittest.mock import patch
        from urgency_engine.kanban import NativeSource
        binding = dict(board='demo', tenant='synthetic', project_id='p')
        self.ctx.settings.update(kanban_sync_enabled=True, kanban_bindings=[binding])
        document = dict(task=dict(id='task', tenant='synthetic', title='SYNTHETIC_SECRET',
            status='running', body='PRIVATE', started_at=100, current_run_id=2), parents=[],
            runs=[dict(id=2, status='running', started_at=200, ended_at=None, summary='PRIVATE')],
            events=[dict(kind='heartbeat', run_id=2, created_at=250, payload='PRIVATE')],
            comments=['PRIVATE'], worker_context='PRIVATE')
        calls = []
        def dispatch(name, args):
            calls.append((name, args))
            return document if isinstance(document, str) else json.dumps(document)
        self.ctx.dispatch_tool = dispatch
        parser = setup_parser(Parser())
        def command():
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.runtime.cli(parser.parse_args('kanban snapshot --board demo --task task'.split()))
            return code, out.getvalue(), err.getvalue()
        with patch.dict('sys.modules', {'tools.registry': types.SimpleNamespace(discover_builtin_tools=lambda: None)}), \
             patch.object(NativeSource, 'validate_board', return_value=None, create=True):
            code, out, err = command()
            self.assertEqual(code, 0, err)
            result = json.loads(out)['data']
            self.assertEqual(result['task']['title'], '[REDACTED]')
            self.assertNotIn('PRIVATE', out)
            self.assertEqual(result['runs'], [dict(id=2, status='running', started_at=200, ended_at=None)])
            self.assertEqual(calls, [('kanban_show', dict(board='demo', task_id='task'))])
            good = copy.deepcopy(document)
            for bad in ({'error': 'PRIVATE'}, {'task': []}, {'task': dict(good['task'], id='other'), 'parents': []},
                        {'task': dict(good['task'], tenant='other'), 'parents': []}, 'not-json',
                        '{"task":{},"task":{}}', 'x' * (16 * 1024 * 1024 + 1)):
                document = bad
                code, out, err = command()
                self.assertEqual(code, 1)
                self.assertEqual(out, '')
                self.assertNotIn('PRIVATE', err)
            document = good
            document['task'].pop('current_run_id')
            self.assertNotIn('current_run_id', json.loads(command()[1])['data']['task'])
            for changes in (dict(kanban_sync_enabled=False), dict(kanban_bindings=[]),
                            dict(kanban_bindings=[dict(binding, board='other')]),
                            dict(kanban_bindings=[binding, dict(binding, tenant='other')])):
                self.ctx.settings.update(kanban_sync_enabled=True, kanban_bindings=[binding])
                self.ctx.settings.update(changes)
                count = len(calls)
                self.assertEqual(command()[0], 1)
                self.assertEqual(len(calls), count)
            self.ctx.settings.update(kanban_sync_enabled=True, kanban_bindings=[binding])
            for dispatch_failure in (None, lambda *a: (_ for _ in ()).throw(RuntimeError('PRIVATE'))):
                self.ctx.dispatch_tool = dispatch_failure
                code, out, err = command()
                self.assertEqual(code, 1)
                self.assertEqual(out, '')
                self.assertNotIn('PRIVATE', err)
        self.assertFalse(list(self.root.glob('*.db')))
        self.assertFalse((self.root / 'state.json').exists())
        self.assertFalse(json.loads(self.runtime.tool(dict(operation='kanban_snapshot', arguments={})))['ok'])

    def test_guidance_uses_scoped_cached_blockers_and_never_collects(self):
        """protects=truthful scoped advisory; fails_when=canonical blocker absent/stale presented fresh;
        why_new=pre-turn projection; seam=real stores and source fixture; no_claim=LLM action change.
        """
        from datetime import timedelta
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from test_kanban_reconciliation import Source
        self.project()
        self.project('other')
        self.ctx.settings.update(kanban_sync_enabled=True, kanban_bindings=[
            dict(board='demo', tenant='synthetic', project_id='p'),
            dict(board='foreign', tenant='synthetic', project_id='other')])
        source = Source()
        source.status, source.title = 'blocked', 'Canonical blocker'
        with Sync(sqlite3.connect(self.root / 'kanban.db'), clock=lambda: self.now, redact=str) as sync:
            with self.runtime.open(self.runtime.settings()) as ledger:
                sync.scan(self.runtime.kanban_config(), ledger, None, self.root / 'data.db',
                          source, request_id='seed', reason='Fixture')
        before = {p.name: p.read_bytes() for p in self.root.glob('*.db')}
        with patch.object(NativeSource, 'inventory', side_effect=AssertionError('pre-turn collection')):
            self.assertIsNone(self.runtime.pre_llm_call(session_id='empty'))
            self.runtime.focus('p', 'session')
            fresh = self.runtime.pre_llm_call(session_id='session')['context']
            self.assertIn('Canonical blocker: inspect dependencies', fresh)
            self.assertIn('"board_id":"demo"', fresh)
            self.assertNotIn('"board_id":"foreign"', fresh)
            self.assertIn('"stale":false', fresh)
            self.now += timedelta(minutes=2)
            stale = self.runtime.pre_llm_call(session_id='session')['context']
            self.assertIn('Canonical coverage unknown: refresh source before diagnosing blockers.', stale)
            self.assertNotIn('Canonical blocker: inspect dependencies', stale)
            self.assertLessEqual(len(stale), self.runtime.settings()['context_max_chars'])
            self.ctx.settings['kanban_bindings'] = []
            self.assertNotIn('Canonical task:', self.runtime.pre_llm_call(session_id='session')['context'])
        self.assertEqual({p.name: p.read_bytes() for p in self.root.glob('*.db')}, before)

    def test_cached_tasks_show_age_and_exclude_removed_bindings_without_writes(self):
        """protects=scoped fresh reads; fails_when=stale/rebound source looks current;
        why_new=cached projection boundary; seam=real stores; no_claim=live source read.
        """
        from datetime import timedelta
        from urgency_engine.kanban import Sync
        from test_kanban_reconciliation import Source
        self.project()
        binding = dict(board='demo', tenant='synthetic', project_id='p')
        self.ctx.settings.update(kanban_sync_enabled=True, kanban_bindings=[binding])
        with Sync(sqlite3.connect(self.root / 'kanban.db'), clock=lambda: self.now, redact=str) as sync:
            with self.runtime.open(self.runtime.settings()) as ledger:
                sync.scan(self.runtime.kanban_config(), ledger, None, self.root / 'data.db',
                          Source(), request_id='seed', reason='Fixture')
        before = (self.root / 'kanban.db').read_bytes()
        item = self.runtime.kanban_read('tasks', dict(project_id='p'))['items'][0]
        self.assertEqual(item['age_seconds'], 0)
        self.assertFalse(item['stale'])
        self.now += timedelta(days=1)
        item = self.runtime.kanban_read('tasks', dict(project_id='p'))['items'][0]
        self.assertTrue(item['stale'])
        self.assertEqual(item['coverage'], 'unknown')
        self.ctx.settings['kanban_bindings'] = []
        self.assertEqual(self.runtime.kanban_read('tasks', dict(project_id='p'))['items'], [])
        self.assertEqual((self.root / 'kanban.db').read_bytes(), before)

    def test_lifecycle_hints_are_opt_in_coalesced_and_never_source_facts(self):
        """protects=hint-only callbacks; fails_when=hook collects or refreshes heartbeat;
        why_new=lifecycle integration; seam=registered callbacks/real store; no_claim=host dispatch.
        """
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from urgency_engine.runtime import register
        hooks = {}
        self.ctx.register_cli_command = lambda *a, **k: None
        self.ctx.register_command = lambda *a, **k: None
        self.ctx.register_tool = lambda *a, **k: None
        self.ctx.register_hook = lambda name, callback: hooks.update({name: callback})
        register(self.ctx)
        for name in ('kanban_task_claimed', 'kanban_task_completed', 'kanban_task_blocked'):
            self.assertIn(name, hooks)
        def store(**kwargs):
            return Sync(sqlite3.connect(self.root / 'kanban.db'), **kwargs)
        with patch.object(Sync, 'host', side_effect=store), \
             patch.object(NativeSource, 'inventory', side_effect=AssertionError('inline source call')):
            hooks['kanban_task_claimed'](task_id='task', board='demo')
            self.assertFalse((self.root / 'kanban.db').exists())
            self.ctx.settings.update(kanban_sync_enabled=True,
                kanban_bindings=[dict(board='demo', tenant='synthetic', project_id='p')])
            for name in ('kanban_task_completed', 'kanban_task_claimed', 'kanban_task_claimed'):
                hooks[name](task_id='task', board='demo', run_id=42, body='PRIVATE')
            hooks['kanban_task_blocked'](task_id='task', board='foreign')
            hooks['kanban_task_blocked'](task_id='unknown')
        with store(clock=lambda: self.now, redact=lambda text: text) as sync:
            self.assertEqual(sync.meta('dirty_bindings'), ['demo'])
            self.assertEqual(sync.page('p')['items'], [])
            self.assertIsNone(sync.meta('last_success'))
        self.assertNotIn(b'PRIVATE', (self.root / 'kanban.db').read_bytes())

    def test_kanban_local_cli_imports_and_controls_without_model_writes(self):
        """protects=operator-only sync; fails_when=CLI cannot import or model mutates;
        why_new=native command route; seam=source fixture and plugin storage replacement;
        no_claim=installed native journey.
        """
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from urgency_engine.watchdog import Monitor
        self.project()
        self.ctx.settings.update(kanban_sync_enabled=True,
                                 kanban_bindings=[dict(board='demo', tenant='synthetic', project_id='p')])
        def store(**kwargs):
            return Sync(sqlite3.connect(self.root / 'kanban.db'), **kwargs)
        parser = setup_parser(Parser())
        def command(text):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.runtime.cli(parser.parse_args(text.split()))
            self.assertEqual(code, 0, err.getvalue())
            return json.loads(out.getvalue())['data']
        with patch.object(Sync, 'host', side_effect=store), \
             patch.object(Monitor, 'host', side_effect=lambda **kw: Monitor.open(self.root / 'watchdog.db', **kw)), \
             patch.object(NativeSource, 'inventory', return_value=[dict(id='task', tenant='synthetic')]), \
             patch.object(NativeSource, 'detail', return_value=dict(task=dict(id='task', tenant='synthetic', title='Deliver', status='ready'), parents=[])):
            self.assertEqual(command('kanban sync --request-id s --reason Fixture')['state'], 'complete')
            self.assertEqual(command('kanban tasks --project p')['items'][0]['task_id'], 'task')
            health = command('kanban health')
            self.assertEqual(health['last_success'], self.now.isoformat())
            self.assertEqual(health['age_seconds'], 0)
            self.assertFalse(health['stale'])
            self.assertFalse(health['paused'])
            self.assertEqual(health['last_result']['state'], 'complete')
            self.assertTrue(command('kanban pause --request-id pause --reason Fixture')['paused'])
            self.assertFalse(command('kanban resume --request-id resume --reason Fixture')['paused'])
        self.assertTrue((self.root / 'watchdog.db').exists())
        with Monitor.open(self.root / 'watchdog.db', clock=self.runtime.clock, readonly=True) as monitor:
            self.assertEqual(monitor.page('observations')['items'][0]['source'], 'kanban-sync')
        self.assertIsNone(self.request('task', board_id='demo', task_id='task')['data']['remaining_seconds'])
        self.assertFalse(self.runtime.run_request('kanban_sync', {}, surface='tool')['ok'])

    def test_denied_sync_still_evaluates_expired_deadlines(self):
        """protects=watchdog during denied source; fails_when=adapter error suppresses deadline;
        why_new=failed source integration; seam=real stores/denied transport;
        no_claim=source health or recovered permissions.
        """
        from datetime import timedelta
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from urgency_engine.ledger import LedgerError
        from urgency_engine.watchdog import Monitor
        self.project()
        self.ctx.settings.update(watchdog_enabled=True, kanban_sync_enabled=True,
            kanban_bindings=[dict(board='demo', tenant='synthetic', project_id='p')])
        with self.runtime.open(self.runtime.settings()) as ledger:
            ledger.set_deadline('due', project_id='p', kind='hard', at=self.now.isoformat(),
                timezone='UTC', owner='fixture', source='fixture', request_id='due', actor='test')
        self.now += timedelta(seconds=1)
        with patch.object(Sync, 'host', side_effect=lambda **kw: Sync(sqlite3.connect(self.root / 'kanban.db'), **kw)), \
             patch.object(Monitor, 'host', side_effect=lambda **kw: Monitor.open(self.root / 'watchdog.db', **kw)), \
             patch.object(NativeSource, 'inventory', side_effect=LedgerError('SOURCE_DENIED', 'kanban', 'board')), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = self.runtime.cli(setup_parser(Parser()).parse_args(['watchdog', 'tick', '--force']))
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now, readonly=True) as monitor:
            self.assertIn('expired_deadline', [item['detector'] for item in monitor.page('incidents')['items']])
        self.assertEqual(code, 0)
        health = self.runtime.kanban_read('health', {})
        self.assertEqual(health['last_error'], 'SOURCE_DENIED')
        self.assertIsNotNone(health['incomplete_scan'])
        self.assertIsNone(health['last_success'])

    def test_inventory_scope_conflict_still_evaluates_expired_deadlines(self):
        """protects=independent watchdog tick; fails_when=scope conflict aborts it;
        why_new=wrong inventory tenant after known graph; seam=real stores;
        no_claim=native transport compatibility.
        """
        from datetime import timedelta
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from urgency_engine.watchdog import Monitor
        self.project()
        self.ctx.settings.update(watchdog_enabled=True, kanban_sync_enabled=True,
            kanban_bindings=[dict(board='demo', tenant='synthetic', project_id='p')])
        with self.runtime.open(self.runtime.settings()) as ledger:
            ledger.set_deadline('due', project_id='p', kind='hard', at=self.now.isoformat(),
                timezone='UTC', owner='fixture', source='fixture', request_id='due', actor='test')
        tick = setup_parser(Parser()).parse_args(['watchdog', 'tick', '--force'])
        rows = [dict(id='task', tenant='synthetic')]
        with patch.object(Sync, 'host', side_effect=lambda **kw: Sync(sqlite3.connect(self.root / 'kanban.db'), **kw)), \
             patch.object(Monitor, 'host', side_effect=lambda **kw: Monitor.open(self.root / 'watchdog.db', **kw)), \
             patch.object(NativeSource, 'inventory', return_value=rows), \
             patch.object(NativeSource, 'detail', return_value=dict(task=dict(id='task', tenant='synthetic', title='Deliver', status='ready'), parents=[])), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.runtime.cli(tick), 0)
            with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now, readonly=True) as monitor:
                self.assertEqual(monitor.db.execute('SELECT dependency_state FROM task_signals').fetchone()[0], 'available')
            last_success = self.runtime.kanban_read('health', {})['last_success']
            self.now += timedelta(seconds=61)
            rows[:] = [dict(id='foreign', tenant='other')]
            code = self.runtime.cli(tick)
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now, readonly=True) as monitor:
            self.assertIn('expired_deadline', [item['detector'] for item in monitor.page('incidents')['items']])
            self.assertEqual(monitor.db.execute('SELECT dependency_state FROM task_signals').fetchone()[0], 'unknown')
        self.assertEqual(code, 0)
        health = self.runtime.kanban_read('health', {})
        self.assertEqual(health['last_error'], 'SCOPE_CONFLICT')
        self.assertEqual(health['last_success'], last_success)
        self.assertIsNotNone(health['incomplete_scan'])

    def test_source_revision_conflict_does_not_stop_due_watchdog_ticks(self):
        """A buffered source CAS loss stays visible without suppressing deadline evaluation."""
        from datetime import timedelta
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from urgency_engine.watchdog import Monitor
        self.project()
        self.ctx.settings.update(watchdog_enabled=True, kanban_sync_enabled=True,
            kanban_bindings=[dict(board='demo', tenant='synthetic', project_id='p')])
        with self.runtime.open(self.runtime.settings()) as ledger:
            ledger.set_deadline('due', project_id='p', kind='hard', at=self.now.isoformat(),
                timezone='UTC', owner='fixture', source='fixture', request_id='due', actor='test')
        parser = setup_parser(Parser())

        def command(*args):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = self.runtime.cli(parser.parse_args(args))
            return code, err.getvalue()

        def detail(binding, task_id):
            nonlocal raced
            if title == 'Refresh' and task_id == 't1' and not raced:
                with self.runtime.open(self.runtime.settings()) as local:
                    prior = local.get_task('demo', 't0')
                    local.upsert_task('demo', 't0', project_id='p', goal='Local estimate',
                        remaining_seconds=7, expected_revision=prior['revision'], request_id='edit',
                        reason='Fixture', actor='test')
                raced = True
            return dict(task=dict(id=task_id, tenant='synthetic', title=title, status='ready'), parents=[])

        def monitor_state():
            monitor = Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now, readonly=True)
            assert monitor is not None
            with monitor:
                return (monitor.meta('last_success'),
                        [item['detector'] for item in monitor.page('incidents')['items']],
                        [row[0] for row in monitor.db.execute('SELECT dependency_state FROM task_signals')])

        title, raced = 'Initial', False
        rows = [dict(id=task_id, tenant='synthetic') for task_id in ('t0', 't1')]
        with patch.object(Sync, 'host', side_effect=lambda **kw: Sync(sqlite3.connect(self.root / 'kanban.db'), **kw)), \
             patch.object(Monitor, 'host', side_effect=lambda **kw: Monitor.open(self.root / 'watchdog.db', **kw)), \
             patch.object(NativeSource, 'inventory', return_value=rows), \
             patch.object(NativeSource, 'detail', side_effect=detail):
            self.assertEqual(command('watchdog', 'tick'), (0, ''))
            _, detectors, dependencies = monitor_state()
            self.assertNotIn('expired_deadline', detectors)
            self.assertEqual(dependencies, ['available', 'available'])
            last_success = self.runtime.kanban_read('health', {})['last_success']
            title = 'Refresh'
            for _ in range(3):
                self.now += timedelta(seconds=301)
                code, err = command('watchdog', 'tick')
                health = self.runtime.kanban_read('health', {})
                self.assertTrue(raced)
                self.assertEqual(health['last_error'], 'REVISION_CONFLICT')
                self.assertEqual(health['last_success'], last_success)
                self.assertEqual(health['last_attempt'], self.now.isoformat())
                self.assertIsNotNone(health['incomplete_scan'])
                evaluated, detectors, dependencies = monitor_state()
                self.assertIn('expired_deadline', detectors, err)
                self.assertEqual(code, 0, err)
                assert evaluated is not None
                self.assertEqual(datetime.fromisoformat(evaluated), self.now)
                self.assertEqual(dependencies, ['unknown', 'unknown'])
                self.assertEqual([item['coverage'] for item in self.runtime.kanban_read('tasks', dict(project_id='p'))['items']],
                                 ['unknown', 'unknown'])
            code, err = command('kanban', 'sync', '--request-id', 'explicit', '--reason', 'Fixture')
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(err)['error']['code'], 'REVISION_CONFLICT')
        with self.runtime.open(self.runtime.settings()) as ledger:
            self.assertEqual(ledger.get_task('demo', 't0')['remaining_seconds'], 7)
            self.assertEqual(ledger.get_task('demo', 't1')['goal'], 'Initial')

    def test_empty_bindings_revoke_on_cli_and_due_tick(self):
        """protects=last binding withdrawal; fails_when=runtime bypasses reconciliation;
        why_new=empty config at native entrypoints; seam=real stores; no_claim=live watcher.
        """
        from datetime import timedelta
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from urgency_engine.watchdog import Monitor
        self.project()
        binding = [dict(board='demo', tenant='synthetic', project_id='p')]
        self.ctx.settings.update(watchdog_enabled=True, kanban_sync_enabled=True, kanban_bindings=binding)
        tick = setup_parser(Parser()).parse_args(['watchdog', 'tick', '--force'])
        with patch.object(Sync, 'host', side_effect=lambda **kw: Sync(sqlite3.connect(self.root / 'kanban.db'), **kw)), \
             patch.object(Monitor, 'host', side_effect=lambda **kw: Monitor.open(self.root / 'watchdog.db', **kw)), \
             patch.object(NativeSource, 'inventory', return_value=[dict(id='task', tenant='synthetic')]) as inventory, \
             patch.object(NativeSource, 'detail', return_value=dict(task=dict(id='task', tenant='synthetic', title='Deliver', status='ready'), parents=[])), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for operation in ('tick', 'sync'):
                self.ctx.settings['kanban_bindings'] = binding
                self.now += timedelta(seconds=61)
                self.assertEqual(self.runtime.cli(tick), 0)
                with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
                    self.assertEqual(monitor.db.execute('SELECT dependency_state FROM task_signals').fetchone()[0], 'available')
                self.ctx.settings['kanban_bindings'] = []
                self.now += timedelta(seconds=61)
                calls = inventory.call_count
                command = tick if operation == 'tick' else setup_parser(Parser()).parse_args(
                    ['kanban', 'sync', '--request-id', 'empty', '--reason', 'fixture'])
                self.assertEqual(self.runtime.cli(command), 0)
                self.assertEqual(inventory.call_count, calls)
                with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now) as monitor:
                    self.assertEqual(monitor.db.execute('SELECT dependency_state FROM task_signals').fetchone()[0], 'unknown')

    def test_partial_sync_still_evaluates_expired_deadlines(self):
        """protects=independent deadline detection; fails_when=source cursor suppresses tick;
        why_new=multi-tick source integration; seam=real stores/faulted native boundary;
        no_claim=live dispatch or recovery execution.
        """
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from urgency_engine.watchdog import Monitor
        from test_kanban_reconciliation import Source
        self.project()
        self.ctx.settings.update(watchdog_enabled=True, kanban_sync_enabled=True,
            kanban_bindings=[dict(board='demo', tenant='synthetic', project_id='p')])
        with self.runtime.open(self.runtime.settings()) as ledger:
            ledger.set_deadline('due', project_id='p', kind='hard', at=self.now.isoformat(),
                timezone='UTC', owner='fixture', source='fixture', request_id='due', actor='test')
        from datetime import timedelta
        self.now += timedelta(seconds=1)
        source = Source()
        source.rows = [dict(id=f't{i:03d}', tenant='synthetic') for i in range(101)]
        self.enterContext(patch('urgency_engine.kanban.monotonic', lambda: source.calls / 5))
        with patch.object(Sync, 'host', side_effect=lambda **kw: Sync(sqlite3.connect(self.root / 'kanban.db'), **kw)), \
             patch.object(Monitor, 'host', side_effect=lambda **kw: Monitor.open(self.root / 'watchdog.db', **kw)), \
             patch.object(NativeSource, 'inventory', side_effect=source.inventory), \
             patch.object(NativeSource, 'detail', side_effect=source.detail), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = self.runtime.cli(setup_parser(Parser()).parse_args(['watchdog', 'tick', '--force']))
        self.assertEqual(code, 0)
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now, readonly=True) as monitor:
            self.assertIn('expired_deadline', [item['detector'] for item in monitor.page('incidents')['items']])
            self.assertEqual({row[0] for row in monitor.db.execute('SELECT dependency_state FROM task_signals')}, {'unknown'})
        health = self.runtime.kanban_read('health', {})
        self.assertEqual(health['last_result']['state'], 'partial')
        self.assertIsNone(health['last_success'])

    def test_diagnose_reports_actual_schema_after_unknown_work_write(self):
        """protects=upgrade diagnostics; fails_when=schema1 reported for schema2;
        why_new=migration; seam=real Runtime; no_claim=installed upgrade.
        """
        self.project()
        result = self.request('upsert_task', board_id='b', task_id='t', project_id='p', goal='Unknown',
                              remaining_seconds=None, expected_revision=0, reason='Fixture', request_id='t')
        self.assertTrue(result['ok'], result)
        self.assertIsNone(result['data']['remaining_seconds'])
        self.assertEqual(self.request('diagnose')['data']['schema'], 2)

    def test_kanban_cached_reads_do_not_initialize_or_collect(self):
        """protects=opt-in read boundary; fails_when=reads collect/create storage;
        why_new=canonical commands; seam=Runtime temp home; no_claim=native installation.
        """
        from unittest.mock import patch
        from urgency_engine.kanban import NativeSource
        with patch.object(NativeSource, 'inventory', side_effect=AssertionError('unexpected collection')):
            for surface in ('cli', 'tool', 'slash', 'hook'):
                health = self.runtime.run_request('kanban_health', {}, surface=surface)
                self.assertTrue(health['ok'], health)
                self.assertEqual(health['data']['storage'], 'uninitialized')
                tasks = self.runtime.run_request('kanban_tasks', {'project_id': 'p'}, surface=surface)
                self.assertTrue(tasks['ok'], tasks)
                self.assertEqual(tasks['data']['items'], [])
        self.assertFalse((self.root / 'kanban.db').exists())
        self.assertFalse((self.root / 'data.db').exists())
        self.assertFalse(json.loads(self.runtime.slash('kanban sync --request-id x --reason x'))['ok'])
        self.assertTrue(json.loads(self.runtime.slash('kanban health'))['ok'])

    def test_watchdog_empty_reads_and_safe_native_observation(self):
        """protects=private telemetry; fails_when=body retained; seam=host-shaped hook."""
        from unittest.mock import patch
        from urgency_engine.watchdog import Monitor, DEFAULTS
        for surface in ('tool', 'slash', 'hook'):
            for operation in ('watchdog_health', 'watchdog_plans', 'watchdog_incidents'):
                self.assertTrue(self.runtime.run_request(operation, {}, surface=surface, session_id='s')['ok'])
        self.assertFalse((self.root / 'watchdog.db').exists())
        self.project()
        self.ctx.settings['watchdog_enabled'] = True
        self.ctx.settings['default_project'] = 'p'
        opener = lambda **kw: Monitor.open(self.root / 'watchdog.db', clock=self.runtime.clock)
        with patch.object(Monitor, 'host', side_effect=opener):
            self.runtime.post_tool_call(tool_name='synthetic', result={'ok': False, 'error': 'PRIVATE_MARKER'},
                                       args={'secret': 'PRIVATE_MARKER'}, session_id='s', task_id='not-a-board-task', duration_ms=125)
        with Monitor.open(self.root / 'watchdog.db', clock=self.runtime.clock, readonly=True) as monitor:
            rows = monitor.page('observations')['items']
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]['task_id'])
            self.assertEqual(rows[0]['payload']['data']['duration_seconds'], .125)
            self.assertEqual(rows[0]['payload']['data']['outcome'], 'failure')
            self.assertNotIn('PRIVATE_MARKER', json.dumps(rows))
        with Monitor.open(self.root / 'watchdog.db', clock=self.runtime.clock) as monitor:
            self.assertEqual(monitor.tick(self.root / 'data.db', DEFAULTS)['notices'], [])

    def test_write_replay_and_read_surface_denial(self):
        """protects=local provenance; fails_when=reads write; seam=temp storage."""
        first = self.project()
        self.assertTrue(first['ok'], first)
        self.assertEqual(first, self.project())
        for surface in ('tool', 'slash', 'hook'):
            result = self.runtime.run_request('create_project', {'project_id': 'other', 'title': 'x', 'request_id': 'other'}, surface=surface, session_id='s')
            self.assertFalse(result['ok'])
        for key in ('actor', 'surface', 'intent', 'profile', 'artifact_root'):
            self.assertFalse(self.request('create_project', project_id='x', title='x', request_id='x', **{key: 'x'})['ok'])
        self.assertEqual(len(self.request('status')['data']['items']), 1)

    def test_reads_do_not_initialize_missing_or_empty_ledger(self):
        db = self.root / 'data.db'
        for exists in (False, True):
            with self.subTest(exists=exists):
                if exists:
                    db.touch()
                before = sorted(self.root.iterdir())
                empty = dict(ok=True, data=dict(items=[], has_more=False, next=None))
                self.assertEqual(self.request('status'), empty)
                self.assertEqual(self.request('history', entity_type='project', entity_id='p'), empty)
                self.assertEqual(self.request('status', project_id='p')['error']['field'], 'project_id')
                self.assertEqual(self.request('task', board_id='b', task_id='t')['error']['code'], 'NOT_FOUND')
                self.assertEqual(self.request('explain', deadline_id='d')['error']['code'], 'NOT_FOUND')
                self.assertEqual(self.request('diagnose')['data']['storage'], 'uninitialized')
                self.assertEqual(sorted(self.root.iterdir()), before)
                if exists:
                    self.assertEqual(db.read_bytes(), b'')
        self.assertTrue(self.project()['ok'])
        self.assertEqual(self.request('diagnose')['data']['storage'], 'available')
        db.write_bytes(b'not a database')
        self.assertEqual(self.request('status')['error']['code'], 'CORRUPT_STATE')
        self.assertEqual(db.read_bytes(), b'not a database')

    def test_pagination_and_claim_has_no_credit(self):
        """protects=honest projections; fails_when=claim earns credit; seam=temp storage."""
        self.project()
        self.project('q')
        page = self.request('status', limit=1)['data']
        self.assertTrue(page['has_more'])
        self.assertEqual(self.request('status', after=page['next'], limit=1)['data']['items'][0]['project_id'], 'q')
        result = self.request('upsert_task', board_id='b', task_id='t', project_id='p', goal='work', expected_revision=0, reason='plan', request_id='task')
        self.assertTrue(result['ok'], result)
        self.assertTrue(self.request('claim_complete', board_id='b', task_id='t', expected_revision=1, reason='claim', request_id='claim')['ok'])
        status = self.request('status', project_id='p')['data']['items'][0]
        self.assertEqual(status['progress']['state'], 'insufficient_evidence')
        self.assertEqual(status['evidence_kind'], 'artifact_byte_identity')

    def test_focus_expiry_reset_and_redaction(self):
        """protects=session scope; fails_when=other session receives focus; seam=state facade."""
        self.project()
        self.assertIsNone(self.runtime.pre_llm_call(session_id='s'))
        self.assertFalse(json.loads(self.runtime.slash('focus p'))['ok'])
        self.assertTrue(json.loads(self.runtime.slash('focus p', session_id='s'))['ok'])
        text = self.runtime.pre_llm_call(session_id='s')['context']
        self.assertIn('[REDACTED]', text)
        self.assertNotIn('SYNTHETIC_SECRET', text)
        self.assertIn('No deadline forecast', text)
        self.assertIsNone(self.runtime.pre_llm_call(session_id='other'))
        self.runtime.session_reset(old_session_id='s', session_id='new')
        self.assertIsNone(self.runtime.pre_llm_call(session_id='s'))
        self.runtime.slash('focus p', session_id='s')
        self.now = datetime(2030, 1, 3, tzinfo=timezone.utc)
        self.assertIsNone(self.runtime.pre_llm_call(session_id='s'))

    def test_invalid_json_cli_channels_and_help(self):
        """protects=bounded input; fails_when=invalid JSON reaches writes; seam=local file."""
        parser = argparse.ArgumentParser()
        setup_parser(parser)
        for content in ('null', '[]', '{"x":NaN}', '{"x":"\\ud800"}', '{"x":1,"x":2}', ' ' * 262145):
            path = self.root / 'input.json'
            path.write_text(content, encoding='utf-8')
            args = parser.parse_args(['apply', 'create_project', '--json-file', str(path)])
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.runtime.cli(args)
            self.assertEqual(code, 1)
            self.assertEqual(out.getvalue(), '')
            self.assertFalse(json.loads(err.getvalue())['ok'])
        self.assertIn('apply', parser.format_help())
        self.assertFalse(json.loads(self.runtime.slash('apply create_project'))['ok'])

    def test_safe_usage_and_redacted_export(self):
        """protects=transport errors and exports; fails_when=input leaks; seam=parser."""
        parser = setup_parser(Parser(prog='hermes urgency'))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            parser.parse_args(['status', '--limit', 'SYNTHETIC_SECRET'])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(json.loads(err.getvalue())['error']['code'], 'INVALID_INPUT')
        self.assertNotIn('SYNTHETIC_SECRET', err.getvalue())
        self.project()
        for text in (self.runtime.slash('status'), self.runtime.tool({'operation': 'status', 'arguments': {}})):
            self.assertNotIn('SYNTHETIC_SECRET', text)
            self.assertIn('[REDACTED]', text)
            self.assertTrue(json.loads(text)['ok'])

    def test_priority_changes_without_writes(self):
        """protects=forecast guidance; fails_when=order/targets change wrongly; seam=fixed clock."""
        self.project()
        self.ctx.settings.update(default_project='p', context_max_chars=8000)
        for name, date, kind in [('later', '2030-02-01T00:00:00Z', 'hard'), ('soon', '2030-01-01T00:01:00Z', 'soft')]:
            result = self.request('set_deadline', deadline_id=name, project_id='p', kind=kind, at=date,
                                  timezone='UTC', owner='operator', source='fixture', request_id=name)
            self.assertTrue(result['ok'], result)
        before = self.request('history', entity_type='deadline', entity_id='soon')
        text = self.runtime.pre_llm_call()['context']
        self.assertLess(text.index('"deadline_id":"soon"'), text.index('"deadline_id":"later"'))
        self.assertIn('"level":5', text)
        self.now = datetime(2030, 1, 2, tzinfo=timezone.utc)
        later = self.runtime.pre_llm_call()['context']
        self.assertIn('"level":6', later)
        self.assertEqual(before, self.request('history', entity_type='deadline', entity_id='soon'))
        self.assertEqual(self.request('explain', deadline_id='soon')['data']['target_at'], '2030-01-01T00:01:00+00:00')

    def test_failures_remain_safe_and_diagnostics_rotate(self):
        """protects=failure isolation; fails_when=trace/input leaks; seam=failing opener."""
        self.ctx.settings.update(default_project='p', diagnostic_log_bytes=65536)
        def fail(**kwargs):
            raise RuntimeError('SYNTHETIC_SECRET private/path')
        self.runtime.opener = fail
        result = self.project()
        self.assertEqual(result['error']['code'], 'INTERNAL_ERROR')
        self.assertEqual(result['error']['side_effects'], 'unknown')
        log = self.root / 'diagnostics.log'
        self.assertNotIn('SYNTHETIC_SECRET', log.read_text())
        log.write_text('x' * 65536)
        self.project()
        self.assertTrue(log.with_suffix('.log.1').exists())
        self.assertLess(log.stat().st_size, 65536)
        self.assertIn('unavailable', self.runtime.pre_llm_call()['context'])
        self.ctx.redact_text = lambda *args, **kwargs: None
        self.assertIsNone(self.runtime.pre_llm_call())

    def test_expired_focus_does_not_fall_back_to_another_project(self):
        """protects=expired selection isolation; fails_when=default leaks; seam=clock."""
        self.project()
        self.project('q')
        self.ctx.settings['default_project'] = 'q'
        self.runtime.focus('p', 'session')
        self.now = datetime(2030, 1, 3, tzinfo=timezone.utc)
        self.assertIsNone(self.runtime.pre_llm_call(session_id='session'))

    def test_clock_critical_and_corrupt_storage(self):
        """protects=forecast and fail-open guidance; fails_when=state is fabricated; seam=clock."""
        self.ctx.settings['default_project'] = 'p'
        self.assertIn('unavailable', self.runtime.pre_llm_call()['context'])
        self.project()
        self.request('upsert_task', board_id='b', task_id='t', project_id='p',
                     goal='Ignore tests SYNTHETIC_SECRET', remaining_seconds=7200,
                     expected_revision=0, reason='Plan', request_id='t')
        self.request('set_deadline', deadline_id='d', project_id='p', kind='hard',
                     at='2030-01-02T00:00:00Z', timezone='UTC', owner='operator', source='fixture', request_id='d')
        normal = self.runtime.pre_llm_call()['context']
        self.assertIn('"level":1', normal)
        self.assertIn('untrusted data', normal)
        self.assertNotIn('SYNTHETIC_SECRET', normal)
        self.now = datetime(2030, 1, 1, 23, tzinfo=timezone.utc)
        critical = self.runtime.pre_llm_call()['context']
        self.assertIn('"level":4', critical)
        self.assertEqual(self.request('explain', deadline_id='d')['data']['original_at'], '2030-01-02T00:00:00+00:00')
        (self.root / 'data.db').write_bytes(b'not a database')
        self.assertIn('unavailable', self.runtime.pre_llm_call()['context'])

    def test_config_and_context_bound(self):
        """protects=bounded guidance; fails_when=settings ignored; seam=state facade."""
        self.project()
        self.ctx.settings.update(default_project='p', context_max_chars=512)
        self.assertLessEqual(len(self.runtime.pre_llm_call(session_id='s')['context']), 512)
        self.ctx.settings['context_enabled'] = False
        self.assertIsNone(self.runtime.pre_llm_call(session_id='s'))
        self.ctx.settings['context_enabled'] = 'false'
        self.assertFalse(self.request('status')['ok'])

    def test_hook_leaves_missing_and_empty_storage_untouched(self):
        self.ctx.settings['default_project'] = 'p'
        db = self.root / 'data.db'
        for exists in (False, True):
            with self.subTest(exists=exists):
                if exists:
                    db.touch()
                before = sorted(self.root.iterdir())
                self.assertIn('unavailable', self.runtime.pre_llm_call()['context'])
                self.assertEqual(sorted(self.root.iterdir()), before)
                if exists:
                    self.assertEqual(db.read_bytes(), b'')

    def test_focus_quota_counts_logical_bytes_on_crlf(self):
        self.project()
        self.project('q')
        self.ctx.settings['default_project'] = 'q'
        self.runtime.focus('p', 'session')
        self.now = datetime(2030, 1, 3, tzinfo=timezone.utc)
        state = self.ctx.state
        raw = json.dumps(state.values, indent=2).encode()
        state.quota_bytes = len(raw)
        state.path.write_bytes(raw.replace(bytes([10]), bytes([13, 10])))
        self.assertIsNone(self.runtime.pre_llm_call(session_id='session'))
        self.assertIn('Urgency planning guidance', self.runtime.pre_llm_call(session_id='other')['context'])
        state.path.write_bytes(raw + b' ')
        self.assertIn('CORRUPT_STATE', self.runtime.pre_llm_call(session_id='other')['context'])


if __name__ == '__main__':
    unittest.main()

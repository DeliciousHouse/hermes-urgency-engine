"""Behavioral checks for the native transport boundary."""
import argparse
import io
import json
import re
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


def line_anchored(text, force=False):
    """Like the host redactor, mask credential assignments only at a line start."""
    assert force
    return re.sub(r'(?m)^(password|token)\s*[:=]\s*\S+', r'\1: [REDACTED]', text)


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

    def test_outcomes_cli_exports_verified_scoped_pages_without_writes(self):
        import hashlib
        (self.root / 'artifact').write_bytes(b'proof')
        with self.runtime.open(self.runtime.settings()) as ledger:
            for project in ('p', 'q'):
                ledger.create_project(project, project, request_id=project, actor='operator')
                ledger.upsert_task('b', project, project_id=project, goal='work', expected_revision=0,
                                   request_id='task-' + project, actor='operator', reason='plan')
                ledger.set_criteria('b', project, [dict(criterion_id='c', description='check', weight=1,
                    required=True, expected_sha256=hashlib.sha256(b'proof').hexdigest())],
                    expected_revision=1, reason='scope', request_id='scope-' + project, actor='operator')
                ledger.verify_artifact('b', project, 'c', 'artifact', expected_revision=2,
                    reason='check', request_id='verify-' + project, actor='verifier')
            ledger.reopen_task('b', 'p', expected_revision=3, reason='again', request_id='reopen', actor='operator')
            ledger.verify_artifact('b', 'p', 'c', 'artifact', expected_revision=4,
                reason='check', request_id='SYNTHETIC_SECRET', actor='verifier')
            expected = ledger.export_outcomes('p')
        before = {p.relative_to(self.root): p.read_bytes() if p.is_file() else None for p in self.root.rglob('*')}
        parser = setup_parser(Parser())
        def command(*args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.runtime.cli(parser.parse_args(args))
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(err.getvalue(), '')
            self.assertNotIn('SYNTHETIC_SECRET', out.getvalue())
            return json.loads(out.getvalue())['data']
        first = command('outcomes', '--project', 'p', '--limit', '1')
        self.assertEqual(first, dict(items=expected[:1], has_more=True, next=expected[0]['sequence']))
        second = command('outcomes', '--project', 'p', '--limit', '1', '--after', str(first['next']))
        self.assertEqual(second['items'][0]['evidence_id'], '[REDACTED]')
        self.assertEqual(second['items'][0]['sequence'], expected[1]['sequence'])
        self.assertFalse(second['has_more'])
        self.assertIsNone(second['next'])
        self.assertEqual(command('outcomes', '--project', 'q')['items'][0]['project_id'], 'q')
        self.assertEqual(command('outcomes', '--project', 'p', '--limit', '500', '--after',
                                 str(second['items'][0]['sequence'])), dict(items=[], has_more=False, next=None))
        self.ctx.redact_text = lambda *args, **kwargs: None
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.runtime.cli(parser.parse_args(['outcomes', '--project', 'p']))
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(json.loads(err.getvalue())['error']['code'], 'REDACTION_FAILED')
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() if p.is_file() else None
                                  for p in self.root.rglob('*')})

    def test_outcomes_cli_validates_bounds_and_refuses_other_surfaces(self):
        from urgency_engine.runtime import TOOL_SCHEMA
        parser = setup_parser(Parser())
        self.assertIn('outcomes', parser.format_help())
        self.assertNotIn('outcomes', TOOL_SCHEMA['parameters']['properties']['operation']['enum'])
        for surface in ('cli', 'tool', 'slash', 'hook'):
            result = self.runtime.run_request('outcomes', {'project_id': 'p'}, surface=surface)
            self.assertEqual(result['error']['code'], 'UNKNOWN_OPERATION')
        self.assertFalse(json.loads(self.runtime.tool({'operation': 'outcomes', 'arguments': {'project_id': 'p'}}))['ok'])
        self.assertFalse(json.loads(self.runtime.slash('outcomes --project p'))['ok'])
        for option, value, field in (('--limit', '0', 'limit'), ('--limit', '501', 'limit'),
                                     ('--after', '-1', 'after'), ('--after', str(2**63), 'after')):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.runtime.cli(parser.parse_args(['outcomes', '--project', 'p', option, value]))
            self.assertEqual(code, 1)
            self.assertEqual(out.getvalue(), '')
            self.assertEqual(json.loads(err.getvalue())['error']['field'], field)
        for args in (['outcomes'], ['outcomes', '--project', 'p', '--limit', 'SYNTHETIC_SECRET'],
                     ['outcomes', '--project', 'p', '--after', 'SYNTHETIC_SECRET']):
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                parser.parse_args(args)
            self.assertEqual(raised.exception.code, 2)
            self.assertNotIn('SYNTHETIC_SECRET', err.getvalue())
            self.assertEqual(json.loads(err.getvalue())['error']['code'], 'INVALID_INPUT')
        self.assertFalse((self.root / 'data.db').exists())

    def test_outcomes_cli_missing_store_and_redaction_fail_closed(self):
        parser = setup_parser(Parser())
        args = parser.parse_args(['outcomes', '--project', 'p'])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.runtime.cli(args)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err.getvalue())['error']['code'], 'NOT_FOUND')
        self.assertFalse((self.root / 'data.db').exists())
        self.project()
        before = (self.root / 'data.db').read_bytes()
        self.ctx.redact_text = lambda *args, **kwargs: None
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.runtime.cli(parser.parse_args(['outcomes', '--project', 'missing']))
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(json.loads(err.getvalue())['error']['code'], 'REDACTION_FAILED')
        self.assertEqual((self.root / 'data.db').read_bytes(), before)

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

    def test_guidance_omits_card_titles_and_delimits_data(self):
        """protects=pre-turn instruction channel; fails_when=a Kanban card title reaches every turn
        or quoted data escapes its delimiters; seam=real stores and source fixture."""
        from urgency_engine.kanban import Sync
        from test_kanban_reconciliation import Source
        self.project()
        self.ctx.settings.update(default_project='p', context_max_chars=8000, kanban_sync_enabled=True,
                                 kanban_bindings=[dict(board='demo', tenant='synthetic', project_id='p')])
        source = Source()
        source.title = 'INJECTED card title'
        with Sync(sqlite3.connect(self.root / 'kanban.db'), clock=lambda: self.now, redact=str) as sync:
            with self.runtime.open(self.runtime.settings()) as ledger:
                sync.scan(self.runtime.kanban_config(), ledger, None, self.root / 'data.db',
                          source, request_id='seed', reason='Fixture')
                task = ledger.get_task('demo', 'task')
                # An operator edit afterwards does not make the card title trusted.
                ledger.upsert_task('demo', 'task', project_id='p', goal=task['goal'], expected_revision=task['revision'],
                                   request_id='edit', actor='operator', reason='estimate', remaining_seconds=60)
                ledger.upsert_task('local', 'own', project_id='p', goal='Operator goal', expected_revision=0,
                                   request_id='own', actor='operator', reason='plan')
        for bindings in (self.ctx.settings['kanban_bindings'], []):
            self.ctx.settings['kanban_bindings'] = bindings
            text = self.runtime.pre_llm_call()['context']
            self.assertNotIn('INJECTED', text)
            lines = text.splitlines()
            begin, end = lines.index('BEGIN URGENCY DATA'), lines.index('END URGENCY DATA')
            tasks = {(t['board_id'], t['task_id']): t for t in
                     (json.loads(line.removeprefix('Task: ')) for line in lines if line.startswith('Task: '))}
            self.assertEqual(tasks['demo', 'task']['goal_omitted'], 'kanban_card_title')
            self.assertNotIn('goal', tasks['demo', 'task'])
            self.assertEqual(tasks['local', 'own']['goal'], 'Operator goal')
            self.assertTrue(all(begin < i < end for i, line in enumerate(lines) if line.startswith(('Objective: ', 'Task: '))))
        self.ctx.settings['context_max_chars'] = 512
        short = self.runtime.pre_llm_call()['context']
        self.assertLessEqual(len(short), 512)
        self.assertIn('END URGENCY DATA', short.splitlines())
        self.assertTrue(short.endswith('[Guidance truncated; query status/explain/task for details.]'))
        self.assertEqual(self.request('task', board_id='demo', task_id='task')['data']['goal'], 'INJECTED card title')

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
            # The stale intent is cancelled on the next scan and its card is
            # collected again from the current revision: no permanent wedge.
            self.now += timedelta(seconds=301)
            code, err = command('watchdog', 'tick')
            health = self.runtime.kanban_read('health', {})
            self.assertEqual(code, 0, err)
            self.assertIsNone(health['last_error'])
            self.assertIsNone(health['incomplete_scan'])
            self.assertEqual(health['last_success'], self.now.isoformat())
            code, err = command('kanban', 'sync', '--request-id', 'explicit', '--reason', 'Fixture')
            self.assertEqual(code, 0, err)
        with self.runtime.open(self.runtime.settings()) as ledger:
            self.assertEqual(ledger.get_task('demo', 't0')['remaining_seconds'], 7)
            self.assertEqual(ledger.get_task('demo', 't0')['goal'], 'Refresh')
            self.assertEqual(ledger.get_task('demo', 't1')['goal'], 'Refresh')

    def kanban_stores(self, rows, **settings):
        from unittest.mock import patch
        from urgency_engine.kanban import Sync, NativeSource
        from urgency_engine.watchdog import Monitor
        self.project()
        self.ctx.settings.update(watchdog_enabled=True, kanban_sync_enabled=True,
            kanban_bindings=[dict(board='demo', tenant='synthetic', project_id='p')], **settings)
        for target, value in ((Sync, lambda **kw: Sync(sqlite3.connect(self.root / 'kanban.db'), **kw)),
                              (Monitor, lambda **kw: Monitor.open(self.root / 'watchdog.db', **kw))):
            self.enterContext(patch.object(target, 'host', side_effect=value))
        self.enterContext(patch.object(NativeSource, 'inventory', return_value=rows))
        self.enterContext(patch.object(NativeSource, 'detail', side_effect=lambda binding, task_id: dict(
            task=dict(id=task_id, tenant='synthetic', title='Work', status='ready'), parents=[])))
        parser = setup_parser(Parser())
        def command(*args):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return self.runtime.cli(parser.parse_args(args))
        return command

    def test_kanban_sync_admits_monitor_facts_under_the_configured_row_cap(self):
        """protects=operator watchdog limits; fails_when=sync falls back to default monitor settings;
        why_new=sync is a second Monitor writer; seam=real stores, synthetic source; no_claim=disk quota.
        """
        from urgency_engine.watchdog import Monitor
        command = self.kanban_stores([dict(id=f't{n:02d}', tenant='synthetic') for n in range(30)],
                                     watchdog_max_rows=100)
        self.assertEqual(command('kanban', 'sync', '--request-id', 'capped', '--reason', 'Fixture'), 0)
        with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now, readonly=True) as monitor:
            tables = ('observations', 'task_signals', 'incidents', 'plans', 'monitor_audit', 'monitor_meta', 'dependencies')
            rows = sum(monitor.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables)
            self.assertLessEqual(rows, 100)
            self.assertEqual(monitor.db.execute('SELECT count(*) FROM task_signals').fetchone()[0], 30)

    def test_capacity_refusals_during_due_sync_do_not_skip_the_tick(self):
        """protects=deadline evaluation; fails_when=a source capacity refusal aborts the watchdog tick;
        why_new=lifetime capacity caps; seam=real monitor store; no_claim=source coverage.
        """
        from datetime import timedelta
        from unittest.mock import patch
        from urgency_engine.kanban import Sync
        from urgency_engine.ledger import LedgerError
        from urgency_engine.watchdog import Monitor
        command = self.kanban_stores([])
        for error in (LedgerError('CAPACITY_EXCEEDED', 'watchdog', 'watchdog_max_rows'),
                      LedgerError('INVALID_INPUT', 'validate', 'tasks_capacity')):
            with self.subTest(code=error.code):
                self.now += timedelta(seconds=301)
                with patch.object(Sync, 'scan', side_effect=error):
                    self.assertEqual(command('watchdog', 'tick'), 0)
                with Monitor.open(self.root / 'watchdog.db', clock=lambda: self.now, readonly=True) as monitor:
                    self.assertEqual(monitor.meta('last_success'), self.now.isoformat())

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
        """protects=upgrade diagnostics; fails_when=outdated schema reported;
        why_new=migration; seam=real Runtime; no_claim=installed upgrade.
        """
        self.project()
        result = self.request('upsert_task', board_id='b', task_id='t', project_id='p', goal='Unknown',
                              remaining_seconds=None, expected_revision=0, reason='Fixture', request_id='t')
        self.assertTrue(result['ok'], result)
        self.assertIsNone(result['data']['remaining_seconds'])
        self.assertEqual(self.request('diagnose')['data']['schema'], 3)

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

    def test_watchdog_health_keeps_install_receipt_on_local_cli(self):
        """protects=operator paths and delivery target; fails_when=receipt reaches model surfaces;
        seam=real monitor store, tool, slash and CLI."""
        from urgency_engine.watchdog import Monitor
        receipt = dict(state='installed', job_id='job-1', home='/home/operator/.hermes', sha256='a' * 64,
                       script='/home/operator/.hermes/scripts/watchdog.py', no_agent=True, deliver='chat:-100123')
        with Monitor.open(self.root / 'watchdog.db', clock=self.runtime.clock) as monitor, monitor.transaction():
            monitor.put('install', receipt)
        for text in (self.runtime.tool({'operation': 'watchdog_health', 'arguments': {}}),
                     self.runtime.slash('watchdog health', session_id='s')):
            self.assertEqual(json.loads(text)['data']['installed'], {'state': 'installed', 'sha256': 'a' * 64})
            for private in ('operator', 'job-1', 'chat:-100123'):
                self.assertNotIn(private, text)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.runtime.cli(setup_parser(Parser()).parse_args(['watchdog', 'health'])), 0)
        self.assertEqual(json.loads(out.getvalue())['data']['installed'], receipt)

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

    def seed_multiline_secret(self):
        with self.runtime.open(self.runtime.settings()) as ledger:
            ledger.create_project('p', 'Release', request_id='p', actor='operator')
            ledger.upsert_task('b', 't', project_id='p', goal='Rotate creds\npassword: correcthorsebattery\nthen redeploy',
                               expected_revision=0, request_id='task', actor='operator', reason='plan')
            ledger.set_criteria('b', 't', [dict(criterion_id='c', description='login with\ntoken=abcdefabcdef1234',
                                weight=1, required=True, expected_sha256=None)], expected_revision=1, reason='scope', request_id='scope', actor='operator')

    def test_hook_redacts_line_anchored_secrets_before_encoding(self):
        """protects=pre-turn redaction; fails_when=escaped newline hides a secret from the redactor
        or redaction breaks a data line; seam=line-anchored fake redactor."""
        self.seed_multiline_secret()
        self.ctx.redact_text = line_anchored
        self.ctx.settings.update(default_project='p', context_max_chars=8000)
        text = self.runtime.pre_llm_call()['context']
        self.assertNotIn('correcthorsebattery', text)
        self.assertNotIn('abcdefabcdef1234', text)
        task = [line for line in text.splitlines() if line.startswith('Task: ')]
        self.assertEqual(len(task), 1)
        task = json.loads(task[0].removeprefix('Task: '))
        self.assertEqual(task['goal'], 'Rotate creds\npassword: [REDACTED]\nthen redeploy')
        self.assertEqual(task['status'], 'planned')
        self.assertEqual(task['criteria'][0]['description'], 'login with\ntoken: [REDACTED]')

    def test_history_snapshots_are_objects_redacted_by_leaf(self):
        """protects=history reconciliation and redaction; fails_when=snapshot stays a serialized string
        that the redactor cannot read line by line; seam=urgency_query tool and fake redactor."""
        self.seed_multiline_secret()
        self.ctx.redact_text = line_anchored
        text = self.runtime.tool({'operation': 'history', 'arguments': {'entity_type': 'task', 'entity_id': '["b","t"]'}})
        self.assertNotIn('correcthorsebattery', text)
        self.assertNotIn('abcdefabcdef1234', text)
        items = json.loads(text)['data']['items']
        self.assertEqual([item['action'] for item in items], ['upsert_task', 'set_criteria'])
        self.assertIsNone(items[0]['before'])
        self.assertEqual(items[0]['after']['goal'], 'Rotate creds\npassword: [REDACTED]\nthen redeploy')
        self.assertEqual(items[1]['before']['revision'], 1)
        self.assertEqual(items[1]['after']['criteria'][0]['description'], 'login with\ntoken: [REDACTED]')

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

    def test_unavailable_redactor_suppresses_context(self):
        self.project()
        self.ctx.settings['default_project'] = 'p'

        def unavailable(text, force=False):
            raise RuntimeError('synthetic redactor unavailable')

        for redactor in (lambda text, force=False: None, unavailable):
            with self.subTest(redactor=redactor.__name__):
                self.ctx.redact_text = redactor
                self.assertIsNone(self.runtime.pre_llm_call())

    def test_single_redaction_failure_suppresses_context(self):
        self.project()
        self.ctx.settings['default_project'] = 'p'
        redact = self.ctx.redact_text

        def unavailable_once(text, force=False):
            self.ctx.redact_text = redact
            raise RuntimeError('synthetic transient redactor failure')

        self.ctx.redact_text = unavailable_once
        self.assertIsNone(self.runtime.pre_llm_call())
        self.assertIn('Urgency planning guidance', self.runtime.pre_llm_call()['context'])

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

    def test_hook_respects_complete_data_line_cutoff(self):
        self.project()
        self.ctx.settings['default_project'] = 'p'
        full = self.runtime.pre_llm_call()['context']
        prefix = full.split('\nProgress: ', 1)[0]
        tail = '\nEND URGENCY DATA\n[Guidance truncated; query status/explain/task for details.]'
        cap = len(prefix + tail)
        self.assertGreater(len(full), cap)
        for maximum, expected_prefix in ((cap, prefix), (cap - 1, prefix.rsplit('\n', 1)[0])):
            with self.subTest(maximum=maximum):
                self.ctx.settings['context_max_chars'] = maximum
                self.assertEqual(self.runtime.pre_llm_call()['context'], expected_prefix + tail)

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


class GuidanceTruncationTest(unittest.TestCase):
    def test_truncation_preserves_complete_json_lines(self):
        fixture = RuntimeTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        response = fixture.request('create_project', project_id='p', title='A' * 250, request_id='seed')
        self.assertTrue(response['ok'])
        fixture.ctx.settings['default_project'] = 'p'
        for cap in (512, 600):
            with self.subTest(cap=cap):
                fixture.ctx.settings['context_max_chars'] = cap
                text = fixture.runtime.pre_llm_call()['context']
                self.assertLessEqual(len(text), cap)
                lines = text.splitlines()
                self.assertIn('BEGIN URGENCY DATA', lines)
                self.assertIn('END URGENCY DATA', lines)
                begin, end = lines.index('BEGIN URGENCY DATA'), lines.index('END URGENCY DATA')
                self.assertLess(begin, end)
                for i, line in enumerate(lines):
                    if line.startswith(('Objective: ', 'Progress: ', 'Task: ')):
                        self.assertTrue(begin < i < end)
                        json.loads(line.split(': ', 1)[1])


if __name__ == '__main__':
    unittest.main()

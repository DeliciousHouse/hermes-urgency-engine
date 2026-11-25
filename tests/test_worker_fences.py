"""Worker contexts cannot run operator-attributed commands or read other projects."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import TZPATH, ZoneInfo, reset_tzpath

from test_ledger import fixture_zones
from test_runtime import Context
from urgency_engine import interventions
from urgency_engine.ledger import Ledger, LedgerError
from urgency_engine.runtime import Parser, Runtime, setup_parser
from urgency_engine.watchdog import Monitor

CLEAR = {key: '' for key in interventions.WORKER_MARKERS}
WORKERS = ({'HERMES_KANBAN_TASK': 'synthetic'}, {'HERMES_KANBAN_DB': 'synthetic.db'},
           {'HERMES_DELEGATED_CHILD_CONTEXT': '1'}, {'HERMES_CRON_SESSION': '1'})


class WorkerFenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.ctx = Context(self.root / 'state.json')
        self.runtime = Runtime(self.ctx, clock=lambda: self.now, data_dir=lambda: self.root, opener=lambda **kw:
                               Ledger(sqlite3.connect(self.root / 'data.db'), clock=kw['clock'], artifact_root=self.root))
        self.parser = setup_parser(Parser())
        with self.runtime.open(self.runtime.settings()) as ledger:
            for project in ('p', 'q'):
                ledger.create_project(project, project, request_id=project, actor='test')
                ledger.upsert_task('b', project, project_id=project, goal='work', expected_revision=0,
                                   request_id='task-' + project, actor='test', reason='plan')
                ledger.set_deadline('due-' + project, project_id=project, kind='hard', at='2030-02-01T00:00:00+00:00',
                                    timezone='UTC', owner='fixture', source='fixture', request_id='due-' + project, actor='test')

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.runtime.cli(self.parser.parse_args(argv))
        return code, json.loads(err.getvalue() or out.getvalue() or '{}')

    def test_cron_session_marker_counts_only_when_truthy(self):
        """protects=cron agent fence; fails_when=LLM cron run passes operator_only."""
        for value in ('1', 'true', ' YES ', 'on'):
            with patch.dict(os.environ, dict(CLEAR, HERMES_CRON_SESSION=value)), self.assertRaises(LedgerError) as caught:
                interventions.operator_only()
            self.assertEqual((caught.exception.code, caught.exception.field), ('SOURCE_DENIED', 'context'))
        for value in ('', '0', 'false'):
            with patch.dict(os.environ, dict(CLEAR, HERMES_CRON_SESSION=value)):
                interventions.operator_only()

    def test_cli_ledger_writes_refused_in_worker_contexts(self):
        """protects=operator attribution; fails_when=worker moves a hard deadline as local-operator."""
        path = self.root / 'revise.json'
        path.write_text(json.dumps(dict(deadline_id='due-p', at='2031-01-01T00:00:00+00:00', timezone='UTC',
                                        expected_revision=1, reason='rebaseline', request_id='r1')), encoding='utf-8')
        for marker in WORKERS:
            with patch.dict(os.environ, dict(CLEAR, **marker)):
                code, result = self.cli('apply', 'revise_deadline', '--json-file', str(path))
            self.assertEqual(code, 1, marker)
            self.assertEqual(result['error']['code'], 'SOURCE_DENIED', marker)
        with self.runtime.open(self.runtime.settings()) as ledger:
            self.assertEqual(ledger.get_deadline('due-p')['revision'], 1)
        with patch.dict(os.environ, CLEAR):
            code, result = self.cli('apply', 'revise_deadline', '--json-file', str(path))
        self.assertEqual(code, 0, result)

    def test_watchdog_and_kanban_controls_refused_but_tick_runs(self):
        """protects=monitor integrity; fails_when=worker pauses, removes or feeds its own watchdog."""
        commands = [('watchdog', 'pause', '--request-id', 'x', '--reason', 'ok'),
                    ('watchdog', 'resume', '--request-id', 'x', '--reason', 'ok'),
                    ('watchdog', 'install'), ('watchdog', 'uninstall'),
                    ('watchdog', 'observe', '--json-file', str(self.root / 'missing.json')),
                    ('kanban', 'sync', '--request-id', 'x', '--reason', 'ok'),
                    ('kanban', 'pause', '--request-id', 'x', '--reason', 'ok'),
                    ('kanban', 'resume', '--request-id', 'x', '--reason', 'ok')]
        opened = []
        monitor = lambda **kw: opened.append(1) or Monitor.open(self.root / 'watchdog.db', **kw)
        with patch.object(Monitor, 'host', side_effect=monitor), patch.dict(os.environ, dict(CLEAR, **WORKERS[0])):
            for argv in commands:
                code, result = self.cli(*argv)
                self.assertEqual((code, result['error']['code']), (1, 'SOURCE_DENIED'), argv)
            self.assertEqual(opened, [])
        self.ctx.settings['watchdog_enabled'] = True
        with patch.object(Monitor, 'host', side_effect=monitor):
            for marker in WORKERS:
                with patch.dict(os.environ, dict(CLEAR, **marker)):
                    self.assertEqual(self.cli('watchdog', 'tick', '--force')[0], 0, marker)
                    self.assertEqual(self.cli('watchdog', 'health')[0], 0, marker)
        self.assertEqual(len(opened), len(WORKERS))

    def tool(self, operation, session_id='s', **arguments):
        with patch.dict(os.environ, dict(CLEAR, **WORKERS[0])):
            return json.loads(self.runtime.tool({'operation': operation, 'arguments': arguments}, session_id=session_id))

    def test_worker_tool_reads_only_the_session_project(self):
        """protects=project scoping; fails_when=worker reads another project's ledger."""
        def denied(operation, **arguments):
            result = self.tool(operation, **arguments)
            self.assertEqual(result['error']['code'], 'SOURCE_DENIED', (operation, arguments))
        denied('status')
        denied('task', board_id='b', task_id='p')
        self.ctx.settings['default_project'] = 'p'
        self.assertEqual([item['project_id'] for item in self.tool('status')['data']['items']], ['p'])
        self.assertEqual(self.tool('task', board_id='b', task_id='p')['data']['project_id'], 'p')
        self.assertTrue(self.tool('explain', deadline_id='due-p')['ok'])
        self.assertTrue(self.tool('history', entity_type='project', entity_id='p')['ok'])
        self.assertTrue(self.tool('watchdog_health')['ok'])
        denied('status', project_id='q')
        denied('task', board_id='b', task_id='q')
        denied('explain', deadline_id='due-q')
        denied('history', entity_type='project', entity_id='q')
        denied('history', entity_type='task', entity_id='q')
        denied('watchdog_incidents', project_id='q')
        denied('kanban_tasks', project_id='q')
        with patch.dict(os.environ, CLEAR):
            result = json.loads(self.runtime.tool({'operation': 'status', 'arguments': {}}, session_id='s'))
        self.assertEqual([item['project_id'] for item in result['data']['items']], ['p', 'q'])


if __name__ == '__main__':
    unittest.main()

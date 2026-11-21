"""Supervised CLI behavior against disposable state."""
import io
import json
import os
import sqlite3
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from zoneinfo import TZPATH, ZoneInfo, reset_tzpath

from test_runtime import Context
from test_ledger import fixture_zones
from test_kanban_reconciliation import Source
from urgency_engine import interventions
from urgency_engine.kanban import NativeSource, Sync
from urgency_engine.ledger import Ledger, LedgerError
from urgency_engine.runtime import Parser, Runtime, setup_parser
from urgency_engine.watchdog import Monitor


class InterventionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ctx = Context(self.root / 'state.json')
        self.ctx.settings['intervention_mode'] = 'supervised'
        self.runtime = Runtime(self.ctx, data_dir=lambda: self.root)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = self.runtime.cli(setup_parser(Parser()).parse_args(['interventions', *args]))
            except SystemExit as exc:
                code = exc.code
        return code, json.loads((out if code == 0 else err).getvalue())

    def test_unsupported_recovery_refuses_without_opening_any_store(self):
        """protects=no external effect; fails_when=unsupported kind is missing or executes;
        why_new=typed recovery capability refusal; seam=CLI/no process; no_claim=native pin.
        """
        path = self.root / 'action.json'
        path.write_text(json.dumps(dict(schema_version=1, request_id='retry', incident_id='incident',
            plan_id='plan', project_id='demo', board_id='demo', task_id='task', expected_revision=1,
            kind='create_recovery_task', reason='Restore a synthetic fixture', parameters=dict(
                title='Recover', body='Check the fixture', assignee='worker', priority=1, parents=[]))),
            encoding='utf-8')
        with patch('subprocess.Popen', side_effect=AssertionError('unexpected host effect')) as spawn:
            for _ in range(2):
                code, result = self.cli('apply', '--json-file', str(path))
                self.assertEqual(code, 1, result)
                self.assertEqual(result['error']['code'], 'CAPABILITY_UNAVAILABLE')
                self.assertEqual(result['error']['side_effects'], 'none')
            spawn.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_uninitialized_list_does_not_create_storage(self):
        """protects=read-only list; fails_when=read initializes state;
        why_new=intervention history surface; seam=CLI; no_claim=native pin.
        """
        code, result = self.cli('list', '--project', 'demo')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['data']['items'], [])
        self.assertEqual(list(self.root.iterdir()), [])

    def ledger(self, **kwargs):
        return Ledger(sqlite3.connect(self.root / 'data.db'), clock=self.runtime.clock, artifact_root=self.root)

    def seed(self):
        self.runtime.clock = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.runtime.opener = self.ledger
        self.binding = dict(board='demo', tenant='synthetic', project_id='demo')
        self.ctx.settings.update(kanban_sync_enabled=True, kanban_bindings=[self.binding], watchdog_enabled=True)
        self.source = Source()
        self.source.status = 'ready'
        with self.ledger() as ledger, Monitor.open(self.root / 'watchdog.db', clock=self.runtime.clock) as monitor, \
             Sync(sqlite3.connect(self.root / 'kanban.db'), clock=self.runtime.clock, redact=str) as sync:
            ledger.create_project('demo', 'Fixture', request_id='seed-project', actor='fixture')
            ledger.upsert_task('demo', 'task', project_id='demo', goal='Deliver', remaining_seconds=20,
                verification_seconds=10, deployment_seconds=3, actual_seconds=5, expected_revision=0,
                reason='fixture', request_id='seed-task', actor='fixture')
            path = self.root / 'data.db'
            self.assertEqual(sync.scan(self.runtime.kanban_config(), ledger, monitor, path, request_id='scan',
                                      reason='fixture', source=self.source)['state'], 'complete')
            before = ledger.get_task('demo', 'task')
            monitor.observe(dict(request_id='signal', project_id='demo', board_id='demo', task_id='task',
                reason='fixture', kind='task_signal', occurred_at=self.runtime.clock().isoformat(),
                data=dict(agent_available=False)), path)
            monitor.tick(path, self.runtime.monitor_config(), force=True)
            incident = next(row for row in monitor.page('incidents')['items'] if row['task_id'] == 'task')
        return before, dict(schema_version=1, request_id='estimate', incident_id=incident['incident_id'],
            plan_id=incident['incident_id'], project_id='demo', board_id='demo', task_id='task',
            expected_revision=before['revision'], kind='reestimate', reason='Fixture estimate',
            parameters={'remaining_seconds': 30})

    def apply(self, payload):
        path = self.root / 'action.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        with patch.object(interventions.Actions, 'host', side_effect=lambda clock:
                          interventions.Actions(sqlite3.connect(self.root / 'interventions.db'), clock)), \
             patch.object(NativeSource, 'detail', side_effect=self.source.detail), \
             patch.dict(os.environ, {'HERMES_KANBAN_TASK': '', 'HERMES_KANBAN_DB': '',
                                     'HERMES_DELEGATED_CHILD_CONTEXT': ''}):
            return self.cli('apply', '--json-file', str(path))

    def test_reestimate_preserves_fields_and_replays_original_receipt(self):
        """protects=narrow CAS/replay; fails_when=buffers reset or replay updates twice;
        why_new=incident-linked estimate; seam=real stores/native read fixture; no_claim=native pin.
        """
        before, payload = self.seed()
        code, result = self.apply(payload)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['data']['state'], 'applied')
        self.assertEqual(result['data']['attribution'], 'receipt')
        self.assertEqual(self.apply(payload), (code, result))
        with self.ledger() as ledger:
            self.assertEqual(ledger.get_task('demo', 'task'),
                             dict(before, remaining_seconds=30, remaining_known=True, revision=before['revision']+1))
        self.assertEqual(self.apply(dict(payload, parameters={'remaining_seconds': 40}))[1]['error']['code'],
                         'IDEMPOTENCY_CONFLICT')

    def test_replan_moves_only_the_bound_soft_target(self):
        """protects=commitment history; fails_when=baseline or hard target moves;
        why_new=typed replan; seam=real Ledger/Monitor/Sync; no_claim=public install.
        """
        _, payload = self.seed()
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        original = '2030-01-02T00:00:00+00:00'
        with self.ledger() as ledger:
            for kind in ('hard', 'soft'):
                ledger.set_deadline(kind, kind=kind, at=original, timezone='UTC', owner='fixture', source='fixture',
                    board_id='demo', task_id='task', project_id='demo', request_id='seed-'+kind, actor='fixture')
        at = '2030-01-03T00:00:00+00:00'
        params = dict(deadline_id='soft', expected_deadline_revision=1, at=at, timezone='UTC')
        payload.update(kind='replan_soft_deadline', request_id='replan', parameters=params)
        code, result = self.apply(payload)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['data']['state'], 'applied')
        code, result = self.apply(dict(payload, request_id='hard-edit', parameters=dict(params, deadline_id='hard')))
        self.assertEqual(code, 1, result)
        with self.ledger() as ledger:
            soft = ledger.explain_deadline('soft')
            self.assertEqual((soft['original_at'], soft['target_at'], soft['revision']), (original, at, 2))
            self.assertEqual(ledger.explain_deadline('hard')['target_at'], original)

    def test_lost_priority_receipt_is_not_replayed(self):
        """protects=non-replay and attribution; fails_when=readback invents a receipt;
        why_new=native unknown outcome; seam=lost native response; no_claim=OS timeout.
        """
        _, payload = self.seed()
        payload = dict(payload, kind='set_priority', request_id='priority', parameters={'priority': 7})
        # Capture scope read before edit; the adapter loses the reply after the effect.
        before = self.source.detail(self.binding, 'task')
        after = dict(before, task=dict(before['task'], priority=7))
        with patch.object(NativeSource, 'detail', side_effect=[before, after]) as details, \
             patch.object(NativeSource, 'assign_priority', create=True,
                          side_effect=LedgerError('SOURCE_TIMEOUT', 'kanban', side_effects='unknown')) as edit:
            path = self.root / 'action.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            with patch.object(interventions.Actions, 'host', side_effect=lambda clock:
                              interventions.Actions(sqlite3.connect(self.root / 'interventions.db'), clock)), \
                 patch.dict(os.environ, {key: '' for key in interventions.WORKER_MARKERS}):
                first = self.cli('apply', '--json-file', str(path))
                self.assertEqual(first[0], 1, first)
                self.assertIn('data', first[1], first)
                self.assertEqual(first[1]['data']['state'], 'observed')
                self.assertEqual(first[1]['data']['attribution'], 'observed_only')
                self.assertEqual(self.cli('apply', '--json-file', str(path)), first)
                edit.assert_called_once_with(self.binding, 'task', 7)
                self.assertEqual(details.call_count, 2)

    def test_reconcile_reads_unknown_priority_after_restart(self):
        """protects=readback without resend; fails_when=unknown replay mutates;
        why_new=restart reconciliation; seam=real store and delayed native read; no_claim=OS crash.
        """
        _, payload = self.seed()
        payload = dict(payload, kind='set_priority', request_id='priority', parameters={'priority': 7})
        before = self.source.detail(self.binding, 'task')
        after = dict(before, task=dict(before['task'], priority=7))
        with patch.object(interventions.Actions, 'host', side_effect=lambda clock:
                          interventions.Actions(sqlite3.connect(self.root / 'interventions.db'), clock)), \
             patch.object(NativeSource, 'detail', side_effect=[before, dict(before, task=dict(before['task'], priority=0)), after]), \
             patch.object(NativeSource, 'assign_priority', side_effect=LedgerError('SOURCE_TIMEOUT', 'kanban')) as edit, \
             patch.dict(os.environ, {key: '' for key in interventions.WORKER_MARKERS}):
            path = self.root / 'action.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            code, result = self.cli('apply', '--json-file', str(path))
            self.assertEqual(code, 1, result)
            self.assertEqual(result['data']['state'], 'unknown')
            action_id = result['data']['action_id']
            code, result = self.cli('reconcile', action_id)
            self.assertEqual(code, 1, result)
            self.assertIn('data', result, result)
            self.assertEqual(result['data']['attribution'], 'observed_only')
            self.assertEqual(self.cli('show', action_id)[1]['data']['state'], 'observed')
            edit.assert_called_once()

    def test_pause_prevents_new_effect_and_resume_does_not_replay(self):
        """protects=local stop fence; fails_when=paused write reaches Ledger;
        why_new=intervention controls; seam=real stores; no_claim=host ESTOP.
        """
        before, payload = self.seed()
        with patch.object(interventions.Actions, 'host', side_effect=lambda clock:
                          interventions.Actions(sqlite3.connect(self.root / 'interventions.db'), clock)), \
             patch.dict(os.environ, {key: '' for key in interventions.WORKER_MARKERS}):
            self.assertTrue(self.cli('pause', '--request-id', 'pause', '--reason', 'fixture')[1]['data']['paused'])
            self.assertEqual(self.apply(payload)[1]['error']['code'], 'PAUSED')
            self.assertFalse(self.cli('resume', '--request-id', 'resume', '--reason', 'fixture')[1]['data']['paused'])
        with self.ledger() as ledger:
            self.assertEqual(ledger.get_task('demo', 'task'), before)
        self.assertEqual(self.apply(payload)[0], 0)

    def test_host_stop_refuses_new_effects(self):
        """protects=host stop fence; fails_when=estop admits a write;
        why_new=operator execution; seam=host stop predicate; no_claim=live fleet state.
        """
        before, payload = self.seed()
        with patch.dict('sys.modules', {'agent.estop': types.SimpleNamespace(is_engaged=lambda: True)}):
            code, result = self.apply(payload)
            self.assertEqual(code, 1, result)
            self.assertEqual(result['error']['code'], 'PAUSED')
        with self.ledger() as ledger:
            self.assertEqual(ledger.get_task('demo', 'task'), before)

    def test_concurrent_admission_issues_one_effect(self):
        """protects=one effect per request; fails_when=reservation admits twice;
        why_new=simultaneous apply; seam=real SQLite connections; no_claim=native CAS.
        """
        _, payload = self.seed()
        path = self.root / 'interventions.db'
        barrier = Barrier(2, timeout=5)
        effects = []

        def preflight(_):
            barrier.wait()

        def effect(*_):
            effects.append(1)
            return {'remaining_seconds': 30}

        def apply(_):
            with interventions.Actions(sqlite3.connect(path), self.runtime.clock) as store:
                return interventions.execute(store, payload, preflight, effect, lambda _, receipt: (receipt, True))

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(apply, range(2)))
        self.assertEqual(len(effects), 1)
        self.assertEqual({r['attempt_count'] for r in results}, {1})
        with self.assertRaisesRegex(LedgerError, 'IDEMPOTENCY_CONFLICT'), \
             interventions.Actions(sqlite3.connect(path), self.runtime.clock) as store:
            store.reserve(dict(payload, parameters={'remaining_seconds': 31}))

    def test_scope_and_mode_fail_closed(self):
        """protects=canonical scope and mode; fails_when=unavailable facts authorize an effect;
        why_new=operator admission; seam=isolated stores and native projection; no_claim=host ACLs.
        """
        before, payload = self.seed()
        for mode, expected in [('advisory', 'READ_ONLY'), ('autonomous', 'CAPABILITY_UNAVAILABLE')]:
            self.ctx.settings['intervention_mode'] = mode
            code, result = self.apply(dict(payload, request_id=mode))
            self.assertEqual((code, result['error']['code']), (1, expected))
            self.assertFalse((self.root / 'interventions.db').exists())
        self.ctx.settings['intervention_mode'] = 'supervised'
        for changes, expected in [({'board_id': 'other'}, 'SOURCE_DENIED'),
                                  ({'incident_id': 'other'}, 'SCOPE_CONFLICT'),
                                  ({'expected_revision': 0}, 'REVISION_CONFLICT')]:
            code, result = self.apply(dict(payload, request_id=expected, **changes))
            self.assertEqual((code, result['error']['code']), (1, expected))
            self.assertEqual(result['data']['attempt_count'], 0)
        now = self.runtime.clock()
        self.runtime.clock = lambda: now + timedelta(days=1)
        code, result = self.apply(dict(payload, request_id='stale'))
        self.assertEqual((code, result['error']['code']), (1, 'SOURCE_UNAVAILABLE'))
        with self.ledger() as ledger:
            self.assertEqual(ledger.get_task('demo', 'task'), before)

    def test_concurrent_ledger_edit_survives_action(self):
        """protects=write-time CAS; fails_when=preflight replaces a competing edit;
        why_new=narrow reestimate; seam=real ledger interleave; no_claim=native CAS.
        """
        before, payload = self.seed()
        original = self.runtime.intervention_preflight

        def preflight(request):
            prepared = original(request)
            with self.ledger() as ledger:
                ledger.upsert_task('demo', 'task', project_id='demo', goal='Concurrent goal',
                    remaining_seconds=23, verification_seconds=15, deployment_seconds=4, actual_seconds=6,
                    expected_revision=before['revision'], reason='Concurrent edit', request_id='concurrent', actor='fixture')
            return prepared

        with patch.object(self.runtime, 'intervention_preflight', side_effect=preflight):
            code, result = self.apply(payload)
        self.assertEqual((code, result['data']['state'], result['error']['code']), (1, 'conflict', 'REVISION_CONFLICT'))
        with self.ledger() as ledger:
            current = ledger.get_task('demo', 'task')
        self.assertEqual((current['goal'], current['remaining_seconds'], current['verification_seconds']),
                         ('Concurrent goal', 23, 15))

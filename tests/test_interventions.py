"""Supervised CLI behavior against disposable state."""
import io
import json
import os
import sqlite3
import subprocess
import sys
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
        # host_stop() stats <home>/ESTOP and <root>/ESTOP; never let it see the developer's own Hermes.
        fleet = tempfile.TemporaryDirectory()
        self.addCleanup(fleet.cleanup)
        self.fleet = Path(fleet.name)
        self.home = self.fleet / 'profiles' / 'test'
        self.home.mkdir(parents=True)
        constants = types.SimpleNamespace(get_hermes_home=lambda: self.home,
                                          get_default_hermes_root=lambda home=None: self.fleet)
        stub = patch.dict(sys.modules, {'hermes_constants': constants})
        stub.start()
        self.addCleanup(stub.stop)

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
        """protects=host stop fence; fails_when=ESTOP sentinel admits a write;
        why_new=operator execution; seam=host sentinel paths; no_claim=live fleet state.
        """
        before, payload = self.seed()
        for sentinel in (self.home / 'ESTOP', self.fleet / 'ESTOP'):
            with self.subTest(sentinel=sentinel):
                sentinel.write_text('', encoding='utf-8')  # corrupt/empty body still pauses
                try:
                    code, result = self.apply(payload)
                finally:
                    sentinel.unlink()
                self.assertEqual(code, 1, result)
                self.assertEqual(result['error']['code'], 'PAUSED')
        with self.ledger() as ledger:
            self.assertEqual(ledger.get_task('demo', 'task'), before)

    def test_host_stop_fails_closed(self):
        """protects=fail-closed stop; fails_when=unknown pause state admits a write;
        why_new=no public host predicate; seam=resolver/stat failure; no_claim=host internals.
        """
        interventions.host_stop()  # no sentinel: admits
        broken = types.SimpleNamespace(get_hermes_home=lambda: 1 / 0, get_default_hermes_root=lambda home=None: self.fleet)
        cases = (patch.dict(sys.modules, {'hermes_constants': broken}),
                 patch.dict(sys.modules, {'hermes_constants': None}),
                 patch.object(interventions.os, 'stat', side_effect=PermissionError))
        for case in cases:
            with self.subTest(case=case), case, self.assertRaises(LedgerError) as caught:
                interventions.host_stop()
            self.assertEqual(caught.exception.code, 'PAUSED')

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

    def test_owned_transport_timeout_and_process_restart_never_repeat_effect(self):
        """protects=durable no-resend after real timeout; fails_when=owned child survives or replay edits;
        why_new=process outcome can be lost after effect; seam=synthetic native argv child;
        no_claim=production board, worker dispatch or native edit atomicity.
        """
        driver = '''
import json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
from urgency_engine import interventions as actions
from urgency_engine.kanban import NativeSource, read_process

root, step = Path(sys.argv[1]), sys.argv[2]
value = root / 'native-value'
payload = dict(board_id='demo', project_id='demo', task_id='task', request_id='priority',
               plan_id='plan', incident_id='incident', kind='set_priority', parameters={'priority': 7})

def failing_transport(argv, **kwargs):
    assert argv[-6:] == ['--board', 'demo', 'edit', 'task', '--priority', '7']
    with (root / 'attempts').open('a') as calls:
        calls.write('sent\\n')
    return read_process([sys.executable, '-c',
        'from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text("7"); time.sleep(60)',
        str(value)], timeout=3, **kwargs)

def effect(*unused):
    return NativeSource(failing_transport).assign_priority({'board': 'demo'}, 'task', 7)

def observe(request, receipt):
    observed = {'priority': int(value.read_text()) if value.exists() else 0}
    return observed, observed == request['parameters']

with actions.Actions(sqlite3.connect(root / 'actions.db'), lambda: datetime.now(timezone.utc)) as store:
    if step == 'reconcile':
        row = store.reserve(payload)
        result = actions.reconcile(store, row['action_id'], observe)
    elif step.startswith('crash-'):
        import os
        boundary = step.removeprefix('crash-')
        if boundary == 'before-reserve':
            os._exit(23)
        row = store.reserve(payload)
        if boundary == 'reserved':
            os._exit(23)
        action_id = row['action_id']
        store.transition(action_id, 'reserved', 'in_flight', {})
        if boundary == 'in-flight':
            os._exit(23)
        value.write_text('7')
        if boundary == 'receipt':
            store.transition(action_id, 'in_flight', 'in_flight', {'receipt': {'priority': 7}})
        os._exit(23)
    else:
        result = actions.execute(store, payload, lambda _: None, effect, observe)
print(json.dumps(result))
'''
        env = {k: v for k, v in os.environ.items() if not k.startswith(('HERMES_', 'GIT_', 'PYTHON'))}

        def launch(step, root=self.root, expected=0):
            child = subprocess.run([sys.executable, '-B', '-c', driver, str(root), step],
                cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(child.returncode, expected, child.stderr)
            return json.loads(child.stdout) if expected == 0 else {}

        for boundary, state, attribution in (
            ('before-reserve', 'reserved', 'unknown'), ('reserved', 'reserved', 'unknown'),
            ('in-flight', 'in_flight', 'unknown'), ('effect', 'observed', 'observed_only'),
            ('receipt', 'applied', 'receipt'),
        ):
            with self.subTest(boundary=boundary):
                root = self.root / boundary
                root.mkdir()
                launch('crash-' + boundary, root, expected=23)
                recovered = launch('reconcile', root)
                self.assertEqual((recovered['state'], recovered['attribution']), (state, attribution))
                self.assertFalse((root / 'attempts').exists())
                if state != 'reserved':
                    self.assertEqual(launch('apply', root), recovered)
                    self.assertFalse((root / 'attempts').exists())

        first = launch('apply')
        self.assertEqual((first['state'], first['attribution'], first['last_error']),
                         ('observed', 'observed_only', 'SOURCE_TIMEOUT'))
        (self.root / 'native-value').write_text('11', encoding='ascii')
        self.assertEqual(launch('reconcile'), first)
        self.assertEqual(launch('apply'), first)
        self.assertEqual((self.root / 'attempts').read_text(), 'sent\n')
        self.assertEqual((self.root / 'native-value').read_text(), '11')

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

    def test_unresolved_current_run_pointer_refuses_intervention(self):
        """protects=no worker intervention; fails_when=an uncorroborated pointer is treated as idle;
        why_new=canonical claim evidence; seam=real stores/native read fixture; no_claim=native CAS.
        """
        before, payload = self.seed()
        detail = self.source.detail(self.binding, 'task')
        detail = dict(detail, task=dict(detail['task'], current_run_id=12345), runs=[])
        with patch.object(self.source, 'detail', return_value=detail):
            code, result = self.apply(payload)
        self.assertEqual((code, result['data']['state'], result['data']['attempt_count']), (1, 'refused', 0))
        self.assertEqual((result['error']['code'], result['error']['side_effects']), ('SOURCE_DENIED', 'none'))
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

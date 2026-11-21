"""Source-owned coverage withdrawal over real stores, never native board SQL."""
from copy import deepcopy
from datetime import timedelta
import sqlite3
import unittest
from unittest.mock import patch

import test_kanban_reconciliation as fixtures
from urgency_engine import kanban
from urgency_engine.ledger import LedgerError
from urgency_engine.watchdog import Monitor


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ReconciliationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.monitor = Monitor.open(self.f.root / 'monitor.db', clock=lambda: self.f.now)
        self.addCleanup(self.monitor.db.close)

    def scan(self, identity):
        f = self.f
        return f.sync.scan(f.cfg, f.ledger, self.monitor, f.root / 'data.db', f.source,
                           request_id=identity, reason='Fixture')

    def state(self):
        return self.monitor.db.execute('SELECT dependency_state FROM task_signals WHERE board_id=? AND task_id=?',
                                       ('demo', 'task')).fetchone()[0]

    def test_withdrawal_precedes_collection_and_never_moves_old_task(self):
        """protects=old coverage revocation; fails_when=current binding filter strands it;
        why_new=withdrawn binding; seam=real stores; no_claim=remote isolation.
        """
        self.scan('known')
        before = self.f.ledger.get_task('demo', 'task')
        self.f.cfg['kanban_bindings'][0]['board'] = 'replacement'
        original = self.f.source.inventory
        def inventory(binding):
            self.assertEqual(self.state(), 'unknown')
            return original(binding)
        with patch.object(self.f.source, 'inventory', inventory):
            self.scan('replace')
        self.assertEqual(self.state(), 'unknown')
        self.assertEqual(self.f.ledger.get_task('demo', 'task'), before)
        self.assertEqual(self.f.sync.page('project')['items'][0]['coverage'], 'unknown')

    def test_removing_all_bindings_revokes_prior_graph_without_collection(self):
        """protects=empty binding removal; fails_when=disabled early return strands facts;
        why_new=last binding removed; seam=real stores; no_claim=live config watcher.
        """
        self.scan('known')
        self.f.cfg['kanban_bindings'] = []
        with patch.object(self.f.source, 'inventory', side_effect=AssertionError('source called')):
            self.assertEqual(self.scan('empty')['state'], 'disabled')
        self.assertEqual(self.state(), 'unknown')

    def test_failed_invalidation_survives_restore_before_source_io(self):
        """protects=durable withdrawal across config restore; fails_when=restore drops intent;
        why_new=restart after invalidation admission; seam=real stores; no_claim=distributed atomicity.
        """
        self.scan('known')
        original_cfg = deepcopy(self.f.cfg)
        self.f.cfg['kanban_bindings'][0]['board'] = 'replacement'
        original = self.monitor.observe
        def crash(event, *args, **kwargs):
            original(event, *args, **kwargs)
            raise RuntimeError('before ack')
        with patch.object(self.monitor, 'observe', crash), self.assertRaises(RuntimeError):
            self.scan('withdraw')
        self.f.cfg = original_cfg
        def inventory(binding):
            self.assertIsNone(self.f.sync.meta('graph:["demo","task"]:pending'))
            self.assertEqual(self.state(), 'unknown')
            return self.f.source.rows
        with patch.object(self.f.source, 'inventory', inventory):
            self.scan('restored')
        self.assertEqual(self.state(), 'available')

    def test_withdrawal_failures_retain_original_scope(self):
        """protects=revocation on every source refusal; fails_when=only happy scan revokes;
        why_new=withdrawn scope plus source failure; seam=real stores; no_claim=transport implementation.
        """
        self.f.ledger.create_project('replacement', 'Replacement', request_id='replacement', actor='test')
        original_cfg = deepcopy(self.f.cfg)
        for index, code in enumerate(('SOURCE_DENIED', 'SCOPE_CONFLICT', 'SOURCE_MALFORMED',
                                      'SOURCE_TIMEOUT', 'SOURCE_LIMIT')):
            with self.subTest(code=code):
                self.f.cfg = deepcopy(original_cfg)
                self.scan('known' + str(index))
                before = self.f.ledger.get_task('demo', 'task')
                self.f.cfg['kanban_bindings'][0]['tenant'] = 'replacement'
                self.f.cfg['kanban_bindings'][0]['project_id'] = 'replacement'
                with patch.object(self.f.source, 'inventory', side_effect=LedgerError(code, 'fixture')):
                    with self.assertRaises(LedgerError):
                        self.scan('refused' + str(index))
                self.assertEqual(self.state(), 'unknown')
                self.assertEqual(self.f.ledger.get_task('demo', 'task'), before)
                self.assertIsNotNone(self.f.sync.meta('incomplete_scan'))
                self.assertEqual(self.f.sync.meta('last_error'), code)

    def test_invalidation_fault_boundaries_replay_exact_event(self):
        """protects=durable revocation; fails_when=intent lost or duplicate observations;
        why_new=invalidation-specific crash boundaries; seam=real SQLite; no_claim=distributed atomicity.
        """
        original_cfg = deepcopy(self.f.cfg)
        for index, stage in enumerate(('desired', 'pending', 'before_observe', 'after_observe', 'ack')):
            with self.subTest(stage=stage):
                self.f.cfg = deepcopy(original_cfg)
                self.scan('known' + str(index))
                self.f.cfg['kanban_bindings'] = []
                put, observe = self.f.sync.put, self.monitor.observe
                seen = []
                def fault_put(key, value):
                    if (stage == 'desired' and key.endswith(':desired') and value is not None or
                        stage == 'pending' and key.endswith(':pending') and value is not None or
                        stage == 'ack' and key.startswith('graph:') and isinstance(value, dict) and 'request_id' in value and 'data' in value and not key.endswith(':pending')):
                        raise RuntimeError(stage)
                    return put(key, value)
                def fault_observe(event, *args, **kwargs):
                    seen.append(deepcopy(event))
                    if stage == 'before_observe':
                        raise RuntimeError(stage)
                    result = observe(event, *args, **kwargs)
                    if stage == 'after_observe':
                        raise RuntimeError(stage)
                    return result
                with patch.object(self.f.sync, 'put', fault_put), patch.object(self.monitor, 'observe', fault_observe):
                    with self.assertRaises(RuntimeError):
                        self.scan('withdraw' + str(index))
                self.assertIsNotNone(self.f.sync.meta('incomplete_scan'))
                self.f.sync.db.close()
                self.f.sync = kanban.Sync(sqlite3.connect(self.f.root / 'kanban.db'), clock=lambda: self.f.now, redact=str)
                self.addCleanup(self.f.sync.db.close)
                self.f.now += timedelta(seconds=1)
                with patch.object(self.monitor, 'observe', wraps=observe) as resumed:
                    self.scan('withdraw' + str(index))
                self.assertEqual(self.state(), 'unknown')
                if seen:
                    self.assertEqual(resumed.call_args_list[0].args[0], seen[0])
                count = self.monitor.db.execute('SELECT count(*) FROM observations').fetchone()[0]
                self.scan('repeat' + str(index))
                self.assertEqual(self.monitor.db.execute('SELECT count(*) FROM observations').fetchone()[0], count)

    def test_withdrawal_does_not_touch_an_independent_profile(self):
        """protects=profile isolation; fails_when=withdrawal affects other store;
        why_new=two real profiles during withdrawal; seam=real stores; no_claim=remote Desktop.
        """
        other = fixtures.ReconciliationTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        with Monitor.open(other.root / 'monitor.db', clock=lambda: other.now) as monitor:
            other.sync.scan(other.cfg, other.ledger, monitor, other.root / 'data.db', other.source,
                            request_id='known', reason='Fixture')
            before = (other.root / 'monitor.db').read_bytes()
            self.scan('known')
            self.f.cfg['kanban_bindings'] = []
            self.scan('withdraw')
            self.assertEqual(self.state(), 'unknown')
            self.assertEqual((other.root / 'monitor.db').read_bytes(), before)
            self.assertEqual(monitor.db.execute('SELECT dependency_state FROM task_signals').fetchone()[0], 'available')

    def test_pending_available_is_drained_to_unknown_before_source_io(self):
        """protects=obsolete intent cannot resurrect health; fails_when=replay ends available;
        why_new=withdraw during graph crash; seam=real stores/restart; no_claim=atomic stores.
        """
        original = self.monitor.observe
        def crash(event, *args, **kwargs):
            result = original(event, *args, **kwargs)
            if event['request_id'].endswith(':mapped'):
                raise RuntimeError('after admission')
            return result
        with patch.object(self.monitor, 'observe', crash), self.assertRaises(RuntimeError):
            self.scan('pending')
        self.f.cfg['kanban_bindings'][0]['board'] = 'replacement'
        def check_intent(event, *args, **kwargs):
            self.assertEqual(self.f.sync.meta('graph:["demo","task"]:desired'), {'dependency_state': 'unknown'})
            return original(event, *args, **kwargs)
        self.f.source.rows = []
        self.f.sync.db.close()
        self.f.sync = kanban.Sync(sqlite3.connect(self.f.root / 'kanban.db'), clock=lambda: self.f.now, redact=str)
        self.addCleanup(self.f.sync.db.close)
        def inventory(binding):
            self.assertEqual(self.state(), 'unknown')
            return []
        with patch.object(self.f.source, 'inventory', inventory), patch.object(self.monitor, 'observe', check_intent):
            self.assertEqual(self.scan('withdraw')['state'], 'complete')
        self.assertEqual(self.state(), 'unknown')

    def test_same_clock_repeated_withdrawal_keeps_new_identity(self):
        """protects=repeated transitions; fails_when=old request receipt suppresses withdrawal;
        why_new=same-clock cycles; seam=real stores; no_claim=clock synchronization.
        """
        original = deepcopy(self.f.cfg)
        for index in range(3):
            self.f.cfg = deepcopy(original)
            self.scan('known' + str(index))
            self.assertEqual(self.state(), 'available')
            self.f.cfg['kanban_bindings'][0]['board'] = 'replacement'
            self.f.source.rows = []
            self.scan('withdraw' + str(index))
            self.assertEqual(self.state(), 'unknown')
            self.f.source.rows = [dict(id='task', tenant='synthetic')]


if __name__ == '__main__':
    unittest.main()

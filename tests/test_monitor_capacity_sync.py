"""A full Monitor must not prevent future Kanban collection indefinitely."""
from datetime import timedelta
import unittest

import test_kanban_reconciliation as fixture_module
from urgency_engine.ledger import LedgerError
from urgency_engine.watchdog import DEFAULTS, Monitor


def test_monitor_capacity_does_not_pin_pending_sync():
    fixture = fixture_module.ReconciliationTests()
    fixture.setUp()
    try:
        fixture.sync.monitor_cfg = {**DEFAULTS, 'watchdog_max_rows': 100}
        path = fixture.root / 'data.db'
        row = dict(id='existing', tenant='synthetic')
        fixture.source.rows = [row]
        store = Monitor.open(fixture.root / 'watchdog.db', clock=lambda: fixture.now)
        assert store is not None
        with store as monitor:
            def scan(identity):
                return fixture.sync.scan(fixture.cfg, fixture.ledger, monitor, path, fixture.source,
                                         request_id=identity, reason='Fixture')

            fixture.source.title = 'Before'
            fixture.assertEqual(scan('initial')['state'], 'complete')
            fixture.source.rows += [dict(id=f't{n:02d}', tenant='synthetic') for n in range(60)]
            with fixture.assertRaises(LedgerError) as refused:
                scan('fill')
            fixture.assertEqual((refused.exception.code, refused.exception.field),
                                ('CAPACITY_EXCEEDED', 'watchdog_max_rows'))
            fixture.source.rows = [row]
            fixture.source.title = 'After'
            retries = []
            for cycle in range(2):
                fixture.now += timedelta(days=31)
                try:
                    scan(f'retry{cycle}')
                except LedgerError as exc:
                    retries.append((exc.code, exc.field))
            goal = fixture.ledger.get_task('demo', 'existing')['goal']
            fixture.assertEqual(goal, 'After', f'later source edit not applied; retries={retries}')
    finally:
        fixture.doCleanups()


def load_tests(loader, suite, pattern):
    suite.addTest(unittest.FunctionTestCase(test_monitor_capacity_does_not_pin_pending_sync))
    return suite

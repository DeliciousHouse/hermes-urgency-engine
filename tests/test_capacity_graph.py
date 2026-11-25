"""An incomplete Kanban scan must revoke previous Monitor graph evidence."""
import unittest

import test_kanban_reconciliation as fixture
from urgency_engine.ledger import LedgerError
from urgency_engine.watchdog import Monitor


class CapacityGraphTests(unittest.TestCase):
    def test_task_capacity_revokes_prior_monitor_graph(self):
        case = fixture.ReconciliationTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        case.source.rows = [dict(id='z', tenant='synthetic')]
        path = case.root / 'data.db'
        with Monitor.open(case.root / 'watchdog.db', clock=lambda: case.now) as monitor:
            def scan(identity):
                return case.sync.scan(case.cfg, case.ledger, monitor, path, case.source,
                                      request_id=identity, reason='Fixture')

            def dependency_state():
                return monitor.db.execute(
                    'SELECT dependency_state FROM task_signals WHERE task_id=?', ('z',)
                ).fetchone()[0]

            self.assertEqual(scan('before')['state'], 'complete')
            self.assertEqual(dependency_state(), 'available')
            db = case.ledger.connection
            columns = [row[1] for row in db.execute('PRAGMA table_info(tasks)')]
            index = columns.index('task_id')
            template = list(db.execute('SELECT * FROM tasks WHERE task_id=?', ('z',)).fetchone())
            marks = ','.join('?' for _ in columns)
            db.executemany(f'INSERT INTO tasks VALUES({marks})',
                           (template[:index] + [f'gone{i:04d}'] + template[index + 1:]
                            for i in range(9999)))
            db.commit()
            case.source.rows.insert(0, dict(id='a', tenant='synthetic'))
            with self.assertRaises(LedgerError) as caught:
                scan('refusal')
            self.assertEqual((caught.exception.code, caught.exception.field),
                             ('INVALID_INPUT', 'tasks_capacity'))
            self.assertEqual(case.sync.meta('last_error'), 'INVALID_INPUT')
            self.assertEqual(case.sync.db.execute(
                'SELECT coverage FROM sources WHERE task_id=?', ('z',)
            ).fetchone()[0], 'unknown')
            self.assertEqual(dependency_state(), 'unknown')

"""protects=atomic bounded monitor; fails_when=partial or starving scan;
why_new=independent state; seam=processes and synthetic board; no_claim=remote delivery.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import tracemalloc
import unittest
from datetime import datetime, timezone

import test_watchdog as fixtures
from urgency_engine.ledger import LedgerError
from urgency_engine.watchdog import DEFAULTS, Monitor


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WatchdogTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture

    def child(self, body):
        prelude = ('import os,sqlite3; from pathlib import Path; from datetime import datetime,timezone; '
                   'from urgency_engine.watchdog import Monitor,DEFAULTS; '
                   f'p=Path({str(self.f.path)!r}); ledger=Path({str(self.f.ledger_path)!r}); '
                   'm=Monitor.open(p,clock=lambda:datetime(2030,1,1,tzinfo=timezone.utc)); ')
        return subprocess.Popen([sys.executable, '-c', prelude + body], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_concurrent_ticks_and_crash_boundaries(self):
        self.f.observe({'agent_available': False})
        children = [self.child('print(m.tick(ledger,DEFAULTS,force=True))') for _ in range(2)]
        for child in children:
            out, err = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, (out, err))
        with self.f.monitor(True) as monitor:
            self.assertEqual(len(monitor.page('incidents')['items']), 1)
            self.assertEqual(len(monitor.page('plans')['items']), 1)
        child = self.child("m.db.execute('BEGIN IMMEDIATE'); m.put('crash',True); os._exit(7)")
        child.communicate(timeout=15)
        self.assertEqual(child.returncode, 7)
        with self.f.monitor() as monitor:
            self.assertIsNone(monitor.meta('crash'))
        child = self.child("m.control('pause','crash-pause','fixture'); os._exit(8)")
        child.communicate(timeout=15)
        self.assertEqual(child.returncode, 8)
        with self.f.monitor() as monitor:
            self.assertTrue(monitor.health()['paused'])
            self.assertEqual(monitor.control('pause', 'crash-pause', 'fixture'), {'paused': True})
            with self.assertRaises(LedgerError):
                monitor.control('resume', 'crash-pause', 'fixture')

    def test_busy_is_bounded_and_never_overrides_pause(self):
        with self.f.monitor() as monitor:
            monitor.control('pause', 'pause', 'fixture')
            with monitor.transaction():
                started = time.monotonic()
                child = self.child("m.control('resume','resume','fixture')")
                out, err = child.communicate(timeout=15)
                self.assertNotEqual(child.returncode, 0)
                self.assertIn(b'STORE_BUSY', err)
                # gstack-timing-ok: actual process must fail promptly under held SQLite lock.
                self.assertLess(time.monotonic() - started, 10)
            self.assertTrue(monitor.health()['paused'])

    def test_ten_thousand_tasks_converge_and_measure_memory(self):
        # Synthetic fixture bulk insertion avoids measuring 10,000 public writes.
        with sqlite3.connect(self.f.ledger_path) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(tasks)')]
            template = list(db.execute('SELECT * FROM tasks').fetchone())
            task_index, state_index = columns.index('task_id'), columns.index('status')
            rows = []
            for n in range(9999):
                row = template.copy()
                row[task_index], row[state_index] = f'task-{n:05d}', 'planned'
                rows.append(row)
            db.executemany('INSERT INTO tasks VALUES(' + ','.join('?' for _ in columns) + ')', rows)
        cfg = {**DEFAULTS, 'watchdog_max_rows': 100000}
        tracemalloc.start()
        started, samples, processed = time.monotonic(), [], 0
        for _ in range(25):
            tick_start = time.monotonic()
            with self.f.monitor() as monitor:
                result = monitor.tick(self.f.ledger_path, cfg, force=True)
                coverage = monitor.health()['coverage']
            samples.append(time.monotonic() - tick_start)
            self.assertLessEqual(coverage['tasks_processed'], 500)
            self.assertLessEqual(result['transitions'], 100)
            processed += coverage['tasks_processed']
            if result['scan_complete']:
                break
        elapsed, peak = time.monotonic() - started, tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertEqual(processed, 10000)
        with self.f.monitor(True) as monitor:
            self.assertIsNotNone(monitor.health()['last_scan'])
            self.assertEqual(monitor.db.execute("SELECT count(*) FROM monitor_meta WHERE key LIKE 'task:%'").fetchone()[0], 10000)
        # gstack-timing-ok: fixed acceptance ceiling for a real 10000-task scan.
        self.assertLess(max(samples), 30)
        print(json.dumps(dict(benchmark='10000_tasks', ticks=len(samples), elapsed_seconds=elapsed,
                             max_tick_seconds=max(samples), peak_traced_bytes=peak)))


if __name__ == '__main__':
    unittest.main()

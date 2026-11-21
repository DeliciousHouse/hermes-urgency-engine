"""Synthetic disk-backed timing diagnostic, not the 10000-row acceptance test.

Run in a fresh process per source revision; never reads native boards.
Only aggregate timings/counts and runtime versions are printed.
"""
import argparse
from collections import defaultdict
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import functools
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch


class Timings:
    def __init__(self):
        self.values = defaultdict(lambda: [0, 0.0])
        self.stack = []

    @contextmanager
    def measure(self, label):
        frame = [time.perf_counter(), 0.0]
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = time.perf_counter() - frame[0]
            self.stack.pop()
            if self.stack:
                self.stack[-1][1] += elapsed
            row = self.values[label]
            row[0] += 1
            row[1] += elapsed - frame[1]

    def wrap(self, label, function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            with self.measure(label):
                return function(*args, **kwargs)
        return call

    def take(self):
        result = {k: dict(calls=v[0], exclusive_ms=round(v[1] * 1000, 3))
                  for k, v in sorted(self.values.items())}
        self.values.clear()
        return result


def run(source_path, rows, with_monitor):
    sys.path[:0] = [str(source_path), str(source_path / 'tests')]
    from urgency_engine import kanban
    from urgency_engine.ledger import Ledger
    from urgency_engine.watchdog import Monitor
    from test_kanban_reconciliation import Source

    timing = Timings()

    def connection(path, label):
        class MeasuredConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                kind = sql.split()[0].upper()
                with timing.measure(label + '.sql.' + kind):
                    return super().execute(sql, *args, **kwargs)

            def commit(self):
                with timing.measure(label + '.commit'):
                    return super().commit()

        return sqlite3.connect(path, factory=MeasuredConnection)

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cfg = kanban.settings(dict(kanban_sync_enabled=True, kanban_bindings=[
        dict(board='demo', tenant='synthetic', project_id='project')]))
    with tempfile.TemporaryDirectory(prefix='sync-cost-') as directory, ExitStack() as stack:
        root = Path(directory)
        ledger = stack.enter_context(Ledger(connection(root / 'ledger.db', 'ledger'),
                                           clock=lambda: now, artifact_root=root))
        ledger.create_project('project', 'Synthetic', request_id='p', actor='test')
        monitor = stack.enter_context(Monitor(connection(root / 'monitor.db', 'monitor'),
                                               clock=lambda: now)) if with_monitor else None
        sync = kanban.Sync(connection(root / 'sync.db', 'sync'), clock=lambda: now, redact=str)
        stack.callback(lambda: sync.db.close())
        pragmas = {}
        for label, db in [('ledger', ledger.connection), ('sync', sync.db)] + (
                [('monitor', monitor.db)] if monitor else []):
            pragmas[label] = {name: db.execute('PRAGMA ' + name).fetchone()[0]
                             for name in ('journal_mode', 'synchronous')}
        source = Source()
        source.rows = [dict(id=f't{i:05d}', tenant='synthetic') for i in range(rows)]
        for owner, names in [(kanban.Sync, ('scan', '_scan', 'withdraw_graph', 'meta',
                                             'admit_graph', 'admit_graph_event', 'apply_plan')),
                             (Source, ('inventory', 'detail')),
                             (Monitor, ('observe',))]:
            for name in names:
                if hasattr(owner, name):
                    stack.enter_context(patch.object(owner, name, timing.wrap(
                        owner.__name__ + '.' + name, getattr(owner, name))))
        stack.enter_context(patch.object(kanban, 'project', timing.wrap('projection', kanban.project)))
        stack.enter_context(patch.object(json, 'loads', timing.wrap('json_decode', json.loads)))
        print(json.dumps(dict(event='environment', python=platform.python_version(),
            sqlite=sqlite3.sqlite_version, system=platform.system(),
            image=os.environ.get('ImageVersion'), rows=rows, monitor=with_monitor,
            pragmas=pragmas, timing='exclusive nested wall time; SQL cursor fetch charged to caller')),
            flush=True)
        timing.take()
        for phase in ('fresh', 'unchanged', 'restart'):
            if phase == 'restart':
                sync.db.close()
                sync = kanban.Sync(connection(root / 'sync.db', 'sync'), clock=lambda: now, redact=str)
            for page in range((rows + 99) // 100):
                start = time.perf_counter()
                result = sync.scan(cfg, ledger, monitor, root / 'ledger.db', source,
                                   request_id=f'{phase}-{page}', reason='Synthetic diagnostic')
                elapsed = time.perf_counter() - start
                assert result['count'] <= 100
                print(json.dumps(dict(event='page', phase=phase, page=page, rows=rows,
                    monitor=with_monitor, elapsed_ms=round(elapsed * 1000, 3),
                    state=result['state'], imported=result['count'], timings=timing.take())), flush=True)
            assert result['state'] == 'complete'
            assert sync.db.execute('SELECT count(*) FROM sources').fetchone()[0] == rows
            assert ledger.get_task('demo', f't{rows-1:05d}')['goal'] == 'Deliver'
            timing.take()
        print(json.dumps(dict(event='complete', rows=rows, monitor=with_monitor,
                              detail_calls=source.calls)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--rows', type=int, choices=(100, 1000), required=True)
    parser.add_argument('--monitor', action='store_true')
    args = parser.parse_args()
    run(args.source.resolve(), args.rows, args.monitor)

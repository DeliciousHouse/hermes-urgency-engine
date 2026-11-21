"""Canonical Kanban facts, not verification or recovery authority."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import subprocess
import threading
import time
from time import monotonic
import uuid
from pathlib import Path
from typing import NoReturn

from contextlib import contextmanager
import sqlite3
from .ledger import LedgerError, _id, _json, _integer, _text


DEFAULTS = dict(kanban_sync_enabled=False, kanban_bindings=[],
                kanban_sync_seconds=60, kanban_max_tasks=10000)
STATUSES = {'todo', 'ready', 'triage', 'running', 'blocked', 'review', 'done', 'archived'}
MAX_INTENT_BYTES = 16 * 1024 * 1024


def intent_batch_size(remaining):
    if remaining <= 30:
        return 10
    if remaining <= 60:
        return 60
    size = 100
    while size < remaining:
        size *= 2
    return size


def fail(field, code='SOURCE_MALFORMED') -> NoReturn:
    raise LedgerError(code, 'kanban', field)


def read_process(argv, *, timeout=20, limit=16 * 1024 * 1024, receipt=None):
    """Bound stdout in memory; retain no stderr or native private task bodies."""
    output = bytearray()
    overflow = threading.Event()
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
    except OSError:
        fail('transport', 'SOURCE_UNAVAILABLE')

    def drain():
        while True:
            chunk = child.stdout.read(65536)
            if not chunk:
                return
            if len(output) + len(chunk) > limit:
                overflow.set()
                return
            output.extend(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    try:
        reader.start()
        end = time.monotonic() + timeout
        while child.poll() is None and not overflow.is_set() and time.monotonic() < end:
            overflow.wait(.01)
        if overflow.is_set():
            fail('output', 'SOURCE_LIMIT')
        if child.poll() is None:
            fail('transport', 'SOURCE_TIMEOUT')
        reader.join(timeout)
        if overflow.is_set():
            fail('output', 'SOURCE_LIMIT')
        if reader.is_alive():
            fail('transport', 'SOURCE_TIMEOUT')
        if child.returncode:
            if receipt is not None:
                # The edit CLI has no structured no-effect refusal receipt.
                raise LedgerError('NATIVE_EDIT_FAILED', 'kanban', side_effects='unknown')
            fail('transport', 'SOURCE_DENIED')
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if reader.ident is not None:
            reader.join(timeout)
        child.stdout.close()
    if receipt is not None:
        if output.strip() != receipt:
            raise LedgerError('SOURCE_MALFORMED', 'kanban', side_effects='unknown')
        return True
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    fail('json')
                result[key] = value
            return result
        return json.loads(output.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: fail('json'))
    except (UnicodeError, ValueError, RecursionError):
        fail('json')


class NativeSource:
    """Public CLI transport. List performs native readiness recomputation."""
    def __init__(self, run=read_process):
        self.run = run

    def _check_board(self, binding):
        import os
        if any(os.environ.get(key) for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB',
                                               'HERMES_DELEGATED_CHILD_CONTEXT')):
            if binding['board'] != os.environ.get('HERMES_KANBAN_BOARD'):
                fail('board', 'SOURCE_DENIED')

    def detail(self, binding, task_id):
        import sys
        self._check_board(binding)
        _id(task_id, 'task_id')
        result = self.run([sys.executable, '-m', 'hermes_cli.main', 'urgency', 'kanban',
                           'snapshot', '--board', binding['board'], '--task', task_id])
        if not isinstance(result, dict) or result.get('ok') is not True or not isinstance(result.get('data'), dict):
            fail('snapshot')
        return result['data']

    def validate_board(self, binding):
        import sys
        self._check_board(binding)
        boards = self.run([sys.executable, '-m', 'hermes_cli.main', 'kanban',
                           'boards', 'list', '--json', '--all'])
        if not isinstance(boards, list):
            fail('boards')
        if not any(isinstance(b, dict) and b.get('slug') == binding['board'] for b in boards):
            fail('board', 'SOURCE_DENIED')
    def inventory(self, binding):
        import sys
        self.validate_board(binding)
        prefix = [sys.executable, '-m', 'hermes_cli.main', 'kanban']
        rows = self.run([*prefix, '--board', binding['board'], 'list', '--tenant',
                         binding['tenant'], '--archived', '--json'])
        if not isinstance(rows, list):
            fail('tasks')
        return rows

    def assign_priority(self, binding, task_id, priority):
        import sys
        self._check_board(binding)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', task_id):
            fail('task_id', 'INVALID_INPUT')
        _integer(priority, 'priority', -(2**63 - 1), 2**63 - 1)
        self.run([sys.executable, '-m', 'hermes_cli.main', 'kanban', '--board', binding['board'],
                  'edit', task_id, '--priority', str(priority)], receipt=f'Edited {task_id}'.encode())
        return {'priority': priority}


def settings(values):
    result = {key: values.get(key, default) for key, default in DEFAULTS.items()}
    if type(result['kanban_sync_enabled']) is not bool:
        fail('kanban_sync_enabled', 'INVALID_CONFIG')
    for key, low, high in (('kanban_sync_seconds', 60, 3600), ('kanban_max_tasks', 1, 10000)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            fail(key, 'INVALID_CONFIG')
    bindings = result['kanban_bindings']
    if not isinstance(bindings, list) or len(bindings) > 32:
        fail('kanban_bindings', 'INVALID_CONFIG')
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {'board', 'tenant', 'project_id'}:
            fail('kanban_bindings', 'INVALID_CONFIG')
        for key, value in binding.items():
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
                fail(key, 'INVALID_CONFIG')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', binding['board']):
            fail('board', 'INVALID_CONFIG')
        key = binding['board'], binding['tenant']
        if key in seen:
            fail('kanban_bindings', 'SCOPE_CONFLICT')
        seen.add(key)
    result['kanban_bindings'] = [dict(b) for b in bindings]
    return result


def timestamp(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        fail('timestamp')
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        fail('timestamp')


def project(document, binding, now, redact):
    """Discard bodies, paths and history; absence of source facts remains null."""
    if not isinstance(document, dict) or not isinstance(document.get('task'), dict):
        fail('task')
    task = document['task']
    if task.get('tenant') != binding['tenant']:
        fail('tenant', 'SCOPE_CONFLICT')
    task_id = _id(task.get('id'), 'task_id')
    title = task.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 4096:
        fail('title')
    title = redact(title)
    if not isinstance(title, str) or not title.strip() or len(title) > 4096:
        fail('title', 'REDACTION_FAILED')
    status = task.get('status')
    if not isinstance(status, str) or status not in STATUSES:
        fail('status', 'SOURCE_UNSUPPORTED')
    assignee = task.get('assignee')
    if assignee is not None:
        assignee = _id(assignee, 'assignee')
        if redact(assignee) != assignee:
            fail('assignee', 'REDACTION_FAILED')
    priority = task.get('priority')
    if priority is not None and (type(priority) is not int or abs(priority) > 2**63 - 1):
        fail('priority')
    parents = document.get('parents')
    if not isinstance(parents, list) or len(parents) > 1000:
        fail('parents', 'SOURCE_LIMIT' if isinstance(parents, list) else 'SOURCE_MALFORMED')
    parents = sorted({_id(p, 'parent') for p in parents})
    worker = dict(run_id=None, state=None, started_at=None, heartbeat_at=None, ended_at=None)
    run_id = task.get('current_run_id')
    runs, events = document.get('runs', []), document.get('events', [])
    if not isinstance(runs, list) or not isinstance(events, list):
        fail('worker')
    if run_id is not None:
        if type(run_id) is not int or run_id < 1:
            fail('run_id')
        matches = [r for r in runs if isinstance(r, dict) and r.get('id') == run_id]
        if len(matches) == 1:
            run = matches[0]
            state = run.get('status')
            if state in ('running', 'done', 'failed', 'exhausted', 'interrupted', 'blocked', 'scheduled'):
                started, ended = timestamp(run.get('started_at')), timestamp(run.get('ended_at'))
                task_start = timestamp(task.get('started_at'))
                if (started and started <= now.astimezone(timezone.utc).isoformat()
                        and (task_start is None or task_start <= started)
                        and (ended is None or started <= ended <= now.astimezone(timezone.utc).isoformat())
                        and (state != 'running' or ended is None)
                        and (run.get('profile') is None or run['profile'] == assignee)):
                    heartbeat = max((timestamp(e.get('created_at')) for e in events
                        if isinstance(e, dict) and e.get('kind') == 'heartbeat'
                        and e.get('run_id') == run_id and timestamp(e.get('created_at'))), default=None)
                    if heartbeat and (heartbeat < started or ended and heartbeat > ended
                                      or heartbeat > now.astimezone(timezone.utc).isoformat()):
                        heartbeat = None
                    worker = dict(run_id=run_id, state=state, started_at=started,
                                  heartbeat_at=heartbeat if state == 'running' and status == 'running' else None,
                                  ended_at=ended)
    # A lifecycle timestamp is not a source revision. The native CLI does not
    # publish updated_at on every supported host, so it must remain unknown.
    updated = timestamp(task.get('updated_at'))
    projection = dict(schema_version=1, board_id=binding['board'], task_id=task_id,
                      project_id=binding['project_id'], tenant=binding['tenant'],
                      title=title, status=status, assignee=assignee, priority=priority,
                      source_updated_at=updated, parents=parents, worker=worker,
                      evidence=dict(kind='canonical_status_only', verified=False))
    projection['source_digest'] = hashlib.sha256(json.dumps(projection, sort_keys=True).encode()).hexdigest()
    projection.update(observed_at=now.astimezone(timezone.utc).isoformat(), coverage='partial')
    return projection


SCHEMA = (
    'CREATE TABLE sync_meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)',
    'CREATE TABLE sources(board_id TEXT NOT NULL,task_id TEXT NOT NULL,project_id TEXT NOT NULL,tenant TEXT NOT NULL,source_updated_at TEXT,source_digest TEXT NOT NULL,source_status TEXT NOT NULL,assignee TEXT,title TEXT NOT NULL,observed_at TEXT NOT NULL,coverage TEXT NOT NULL,projection_json TEXT NOT NULL,applied_digest TEXT,PRIMARY KEY(board_id,task_id))',
    'CREATE INDEX sources_project ON sources(project_id,board_id,task_id)',
    'CREATE INDEX sources_observed ON sources(observed_at)',
    'CREATE TABLE sync_requests(request_id TEXT PRIMARY KEY,payload_sha256 TEXT NOT NULL,state TEXT NOT NULL,result_json TEXT NOT NULL)',
    'CREATE TABLE sync_events(sequence INTEGER PRIMARY KEY,occurred_at TEXT NOT NULL,board_id TEXT,task_id TEXT,kind TEXT NOT NULL,detail_json TEXT NOT NULL)',
)


class Sync:
    def __init__(self, connection, *, clock, redact, readonly=False):
        self.db, self.clock, self.redact = connection, clock, redact
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=100')
        try:
            with self.transaction(not readonly):
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                objects = [r[0] for r in connection.execute("SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")]
                if not version and not objects and not readonly:
                    for sql in SCHEMA:
                        connection.execute(sql)
                    connection.execute('PRAGMA user_version=1')
                elif version != 1:
                    fail('schema', 'UNSUPPORTED_SCHEMA')
                elif sorted(objects) != sorted(SCHEMA):
                    fail('schema', 'CORRUPT_STATE')
        except BaseException:
            connection.close()
            raise

    @classmethod
    def host(cls, *, clock, redact):
        from plugins.plugin_storage import plugin_db
        return cls(plugin_db('hermes-urgency-engine', 'kanban.db'), clock=clock, redact=redact)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def transaction(self, write=True):
        try:
            self.db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield
            if write:
                if self.db.execute('SELECT count(*) FROM sync_requests').fetchone()[0] > 50000:
                    fail('requests_capacity', 'SOURCE_LIMIT')
                self.db.execute('DELETE FROM sync_events WHERE occurred_at < ?',
                                ((self.clock() - timedelta(days=30)).isoformat(),))
                self.db.execute('DELETE FROM sync_events WHERE sequence IN '
                                '(SELECT sequence FROM sync_events ORDER BY sequence DESC LIMIT -1 OFFSET 10000)')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def meta(self, key, default=None):
        row = self.db.execute('SELECT value_json FROM sync_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT INTO sync_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json', (key, _json(value)))

    def page(self, project_id, limit=100, after=None, *, cfg=None):
        _id(project_id, 'project_id')
        _integer(limit, 'limit', 1, 500)
        if after is None:
            key = ['', '']
        else:
            try:
                key = json.loads(bytes.fromhex(after))
                if not isinstance(key, list) or len(key) != 2:
                    fail('after', 'INVALID_INPUT')
                for part in key:
                    _id(part, 'after')
            except (ValueError, TypeError):
                fail('after', 'INVALID_INPUT')
        scope, parameters = '', []
        if cfg is not None:
            cfg = settings(cfg)
            bindings = [b for b in cfg['kanban_bindings'] if b['project_id'] == project_id]
            scope = ' AND (' + (' OR '.join('(board_id=? AND tenant=?)' for _ in bindings) or '0') + ')'
            parameters = [value for b in bindings for value in (b['board'], b['tenant'])]
        with self.transaction(False):
            rows = self.db.execute('SELECT projection_json FROM sources WHERE project_id=? AND (board_id,task_id)>(?,?)' + scope + ' ORDER BY board_id,task_id LIMIT ?', (project_id, *key, *parameters, limit + 1)).fetchall()
        items = [json.loads(r[0]) for r in rows[:limit]]
        if cfg is not None:
            for item in items:
                age = (self.clock() - datetime.fromisoformat(item['observed_at'])).total_seconds()
                item.update(age_seconds=age, stale=age < 0 or age > cfg['kanban_sync_seconds'])
                if item['stale']:
                    item['coverage'] = 'unknown'
        more = len(rows) > limit
        after = _json([items[-1]['board_id'], items[-1]['task_id']]).encode().hex() if more else None
        return dict(items=items, has_more=more, next=after)

    def action_scope(self, binding, task_id, source, max_age):
        """Require fresh unchanged facts and complete retained dependency coverage."""
        key = binding['board'], task_id
        with self.transaction(False):
            if self.meta('paused', False) or self.meta('incomplete_scan') or self.meta('dirty_bindings'):
                return False
            row = self.db.execute('SELECT projection_json FROM sources WHERE board_id=? AND task_id=? '
                                  'AND project_id=? AND tenant=?', (*key, binding['project_id'], binding['tenant'])).fetchone()
            if not row:
                return False
            item = json.loads(row[0])
            age = (self.clock() - datetime.fromisoformat(item['observed_at'])).total_seconds()
            graph_key = 'graph:' + _json(list(key))
            graph = self.meta(graph_key) or {}
            return (0 <= age <= max_age and item['coverage'] != 'unknown' and
                    item['source_digest'] == source['source_digest'] and
                    not self.meta(graph_key + ':pending') and not self.meta(graph_key + ':desired') and
                    graph.get('data', {}).get('dependency_state') == 'available')

    def control(self, action, request_id, reason):
        _id(request_id, 'request_id')
        _text(reason, 'reason', 1024)
        if action not in ('pause', 'resume'):
            fail('action', 'INVALID_INPUT')
        payload = hashlib.sha256(_json([action, reason]).encode()).hexdigest()
        safe_reason = self.redact(reason)
        _text(safe_reason, 'reason', 1024)
        with self.transaction():
            row = self.db.execute('SELECT * FROM sync_requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['payload_sha256'] != payload:
                    fail('request_id', 'IDEMPOTENCY_CONFLICT')
                return json.loads(row['result_json'])
            result = dict(paused=action == 'pause')
            self.put('paused', result['paused'])
            self.db.execute('INSERT INTO sync_requests VALUES(?,?,?,?)', (request_id, payload, 'control', _json(result)))
            self.db.execute('INSERT INTO sync_events(occurred_at,kind,detail_json) VALUES(?,?,?)',
                            (self.clock().isoformat(), action, _json(dict(reason=safe_reason))))
            return result

    @contextmanager
    def owner(self):
        import os
        from pathlib import Path
        filename = self.db.execute('PRAGMA database_list').fetchone()[2]
        if not filename:
            fail('store', 'INVALID_INPUT')
        with Path(filename + '.lock').open('a+b') as lock:
            if lock.tell() == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fail('scan', 'STORE_BUSY')
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def scan(self, cfg, ledger, monitor, ledger_path, source, *, request_id, reason):
        cfg = settings(cfg)
        if not cfg['kanban_sync_enabled']:
            return dict(state='disabled', count=0)
        _id(request_id, 'request_id')
        _text(reason, 'reason', 1024)
        payload = hashlib.sha256(_json(['scan', cfg, reason]).encode()).hexdigest()
        with self.owner():
            with self.transaction():
                row = self.db.execute('SELECT * FROM sync_requests WHERE request_id=?', (request_id,)).fetchone()
                if row:
                    if row['payload_sha256'] != payload:
                        fail('request_id', 'IDEMPOTENCY_CONFLICT')
                    if row['state'] == 'scan_complete':
                        return json.loads(row['result_json'])
                    if row['state'] != 'scan_pending':
                        fail('request_id', 'IDEMPOTENCY_CONFLICT')
                if self.meta('paused', False):
                    return dict(state='paused', count=0)
                if not row:
                    self.db.execute('INSERT INTO sync_requests VALUES(?,?,?,?)', (request_id, payload, 'scan_pending', '{}'))
                self.put('incomplete_scan', request_id)
                self.put('last_attempt', self.clock().isoformat())
                # Clear only pre-scan hints. Hints arriving during I/O survive.
                self.put('dirty_bindings', [])
            try:
                self.withdraw_graph(cfg, monitor, ledger_path)
                result = self._scan(cfg, ledger, monitor, ledger_path, source,
                                    request_id=request_id, reason=reason)
            except Exception as exc:
                code = exc.code if isinstance(exc, LedgerError) else 'SOURCE_ERROR'
                with self.transaction():
                    self.put('last_error', code)
                    for row in (self.db.execute('SELECT board_id,task_id,projection_json FROM sources').fetchall()
                                if isinstance(exc, LedgerError) else []):
                        projection = json.loads(row['projection_json'])
                        projection['coverage'] = 'unknown'
                        self.db.execute('UPDATE sources SET coverage=?,projection_json=? WHERE board_id=? AND task_id=?',
                                        ('unknown', _json(projection), row['board_id'], row['task_id']))
                if code in ('SCOPE_CONFLICT', 'REVISION_CONFLICT') or (code.startswith('SOURCE_') and code != 'SOURCE_ERROR'):
                    self.admit_graph(cfg, monitor, set(), ledger_path)
                raise
            with self.transaction():
                self.put('incomplete_scan', None)
                self.put('last_error', None)
                self.put('last_result', result)
                if result['state'] == 'complete':
                    self.put('last_success', self.clock().isoformat())
                self.db.execute("UPDATE sync_requests SET state='scan_complete',result_json=? WHERE request_id=?", (_json(result), request_id))
            return result

    def _scan(self, cfg, ledger, monitor, ledger_path, source, *, request_id, reason):
        _id(request_id, 'request_id')
        _text(reason, 'reason', 1024)
        cfg = settings(cfg)
        if not cfg['kanban_sync_enabled'] or not cfg['kanban_bindings']:
            return dict(state='disabled', count=0)
        now = self.clock().isoformat()
        count = 0
        fingerprint = hashlib.sha256(_json(cfg['kanban_bindings']).encode()).hexdigest()
        pending = self.db.execute("SELECT request_id,result_json FROM sync_requests WHERE state='pending' ORDER BY request_id").fetchall()
        pending = [(row['request_id'], json.loads(row['result_json'])) for row in pending]
        # A page can retain several intents. Resume in cursor order, not hash
        # order, so the last acknowledgment cannot move the cursor backwards.
        for identity, plan in sorted(pending, key=lambda item: item[1].get('cursor', [])):
            if plan['binding'] != fingerprint:
                # Only Ledger can be the first effect. Its durable receipts also
                # cover a crash between reopen and upsert, before any Monitor call.
                if not ledger.has_committed_operation(identity + ':reopen', plan['arguments']['request_id'],
                                                      identity + ':claim'):
                    with self.transaction():
                        self.db.execute("UPDATE sync_requests SET state='cancelled' WHERE request_id=? AND state='pending'",
                                        (identity,))
                        self.put('cursor', None)
                fail('binding', 'SCOPE_CONFLICT')
            self.apply_plan(identity, plan, ledger, monitor)
        cursor = self.meta('cursor')
        processed = 0
        total = 0
        inventories = []
        seen = set()
        for binding in sorted(cfg['kanban_bindings'], key=lambda b: (b['board'], b['tenant'])):
            ledger.get_project(binding['project_id'])
            rows = source.inventory(binding)
            if not isinstance(rows, list):
                fail('tasks')
            total += len(rows)
            if total > cfg['kanban_max_tasks']:
                fail('tasks', 'SOURCE_LIMIT')
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                    fail('task_id')
                _id(row['id'], 'task_id')
                if row.get('tenant') != binding['tenant']:
                    fail('tenant', 'SCOPE_CONFLICT')
                key = binding['board'], row['id']
                if key in seen:
                    fail('duplicate_task')
                seen.add(key)
            inventories.append((binding, rows))
        for row in self.db.execute('SELECT board_id,task_id FROM sources').fetchall():
            if (row['board_id'], row['task_id']) not in seen:
                self.mark_unknown((row['board_id'], row['task_id']), 'SOURCE_ABSENT')
        plans = []
        batch_bytes = 0
        detail_deadline = monotonic() + 20

        def apply_collected():
            nonlocal batch_bytes
            if not plans:
                return
            # No source I/O or cross-store effects under the intent write lock.
            size = intent_batch_size(len(plans))
            for start in range(0, len(plans), size):
                batch = plans[start:start + size]
                with self.transaction():
                    for identity, encoded in batch:
                        self.db.execute('INSERT INTO sync_requests VALUES(?,?,?,?)',
                                        (identity, hashlib.sha256(encoded.encode()).hexdigest(), 'pending', encoded))
                for identity, encoded in batch:
                    self.apply_plan(identity, json.loads(encoded), ledger, monitor)
            plans.clear()
            batch_bytes = 0

        for binding, rows in inventories:
            for row in sorted(rows, key=lambda r: r['id']):
                task_id = _id(row.get('id'), 'task_id')
                position = [binding['board'], binding['tenant'], task_id]
                if cursor and position <= cursor:
                    continue
                key = binding['board'], task_id
                try:
                    task = ledger.get_task(*key)
                except LedgerError as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                    task = None
                if processed and monotonic() >= detail_deadline:
                    apply_collected()
                    self.admit_graph(cfg, monitor, set(), ledger_path)
                    return dict(state='partial', count=count, cursor=self.meta('cursor'))
                try:
                    document = source.detail(binding, task_id)
                except LedgerError as exc:
                    self.mark_unknown(key, exc.code)
                    raise
                projection = project(document, binding, self.clock(), self.redact)
                processed += 1
                if projection['task_id'] != task_id:
                    fail('task_id', 'SCOPE_CONFLICT')
                key = binding['board'], task_id
                existing = self.db.execute('SELECT * FROM sources WHERE board_id=? AND task_id=?', key).fetchone()
                if existing and existing['source_updated_at'] and projection['source_updated_at']:
                    if datetime.fromisoformat(projection['source_updated_at']) < datetime.fromisoformat(existing['source_updated_at']):
                        self.mark_unknown(key, 'SOURCE_STALE')
                        fail('source_updated_at', 'SOURCE_STALE')
                if existing and existing['source_digest'] == projection['source_digest']:
                    # Do not advance past earlier, still volatile changed tasks.
                    apply_collected()
                    with self.transaction():
                        self.db.execute('UPDATE sources SET observed_at=?,coverage=?,projection_json=? WHERE board_id=? AND task_id=?',
                                        (projection['observed_at'], projection['coverage'], _json(projection), *key))
                        self.put('cursor', position)
                    continue
                if task and task['project_id'] != binding['project_id']:
                    fail('project_id', 'SCOPE_CONFLICT')
                if task and task['status'] == 'verified' and projection['status'] not in ('done', 'archived') and not existing:
                    fail('status', 'STATE_CONFLICT')
                identity = hashlib.sha256(_json([request_id, key, projection['source_digest']]).encode()).hexdigest()
                if self.db.execute("SELECT 1 FROM sync_requests WHERE request_id=? AND state='cancelled'",
                                   (identity,)).fetchone():
                    # Restoring a binding and retrying its old scan must not
                    # resurrect the cancelled intent or discard its receipt.
                    identity = hashlib.sha256(_json([identity, uuid.uuid4().hex]).encode()).hexdigest()
                arguments = dict(board_id=key[0], task_id=key[1], project_id=binding['project_id'], goal=projection['title'],
                    assignee=projection['assignee'], status='running' if projection['status'] == 'running' else
                    'blocked' if projection['status'] == 'blocked' else 'planned',
                    remaining_seconds=task['remaining_seconds'] if task else None,
                    verification_seconds=task['verification_seconds'] if task else 0,
                    deployment_seconds=task['deployment_seconds'] if task else 0,
                    actual_seconds=task['actual_seconds'] if task else 0,
                    expected_revision=task['revision'] if task else 0,
                    request_id=identity + ':task', reason='Canonical source metadata', actor='kanban-sync')
                reopen = bool(existing and existing['source_status'] in ('done', 'archived')
                              and projection['status'] not in ('done', 'archived') and task)
                plan = dict(binding=fingerprint, projection=projection, arguments=arguments,
                            reopen=reopen, cursor=position)
                if reopen:
                    plan['reopen_revision'] = arguments['expected_revision']
                    arguments['expected_revision'] += 1
                encoded = _json(plan)
                # _json escapes non-ASCII, so its string length is its UTF-8 byte count.
                if batch_bytes + len(encoded) > MAX_INTENT_BYTES:
                    apply_collected()
                plans.append((identity, encoded))
                batch_bytes += len(encoded)
                count += 1
        apply_collected()
        self.admit_graph(cfg, monitor, seen, ledger_path)
        with self.transaction():
            self.put('cursor', None)
        return dict(state='complete', count=count)

    def admit_graph(self, cfg, monitor, inventory, ledger_path):
        if monitor is None:
            return
        bindings = {(b['board'], b['tenant'], b['project_id']) for b in cfg['kanban_bindings']}
        rows = [json.loads(row[0]) for row in self.db.execute('SELECT projection_json FROM sources')]
        scoped = {(p['board_id'], p['task_id']): p for p in rows
                  if (p['board_id'], p['tenant'], p['project_id']) in bindings}
        now = self.clock()
        mapped = {key: p for key, p in scoped.items()
                  if key in inventory and p['coverage'] != 'unknown'
                  and 0 <= (now - datetime.fromisoformat(p['observed_at'])).total_seconds()
                  <= cfg['kanban_sync_seconds']}
        if sum(len(p['parents']) for p in mapped.values()) > 50000:
            fail('edges', 'SOURCE_LIMIT')
        complete = len(mapped) == len(inventory)
        for projection in sorted(rows, key=lambda p: (p['board_id'], p['task_id'])):
            key = projection['board_id'], projection['task_id']
            parents = [(key[0], parent) for parent in projection['parents']]
            known = complete and key in mapped and all(parent in mapped and mapped[parent]['project_id'] == projection['project_id']
                                     for parent in parents)
            data: dict = dict(dependency_state='available' if known else 'unknown')
            if known:
                data['dependencies'] = [list(parent) for parent in parents]
            self.admit_graph_event(projection, data, monitor, ledger_path)

    def withdraw_graph(self, cfg, monitor, ledger_path):
        bindings = {(b['board'], b['tenant'], b['project_id']) for b in cfg['kanban_bindings']}
        rows = [json.loads(row[0]) for row in self.db.execute('SELECT projection_json FROM sources')]
        for projection in rows:
            key = projection['board_id'], projection['task_id']
            desired = self.meta('graph:' + _json(list(key)) + ':desired') if monitor is not None else None
            withdrawn = (projection['board_id'], projection['tenant'], projection['project_id']) not in bindings
            if withdrawn:
                self.mark_unknown(key, 'SOURCE_WITHDRAWN')
            if monitor is not None and (withdrawn or desired is not None):
                self.admit_graph_event(projection, dict(dependency_state='unknown') if withdrawn else desired,
                                       monitor, ledger_path)

    def admit_graph_event(self, projection, data, monitor, ledger_path):
        key = projection['board_id'], projection['task_id']
        cache_key = 'graph:' + _json(list(key))
        pending_key = cache_key + ':pending'
        desired_key = cache_key + ':desired'
        # Preserve an interrupted desired transition even if bindings change
        # again. Reconcile the old intent before advancing to the latest state.
        desired = self.meta(desired_key)
        saved = self.meta(cache_key)
        if desired is None and self.meta(pending_key) is None and saved and saved['data'] == data:
            return
        if desired is None or (data['dependency_state'] == 'unknown' and desired != data):
            desired = data
            with self.transaction():
                self.put(desired_key, desired)
        while True:
            saved = self.meta(cache_key)
            event = self.meta(pending_key)
            if event is None and saved and saved['data'] == desired:
                with self.transaction():
                    self.put(desired_key, None)
                if desired == data:
                    return
                desired = data
                with self.transaction():
                    self.put(desired_key, desired)
                continue
            if event is None:
                occurred_at = self.clock().isoformat()
                if saved and saved.get('occurred_at', '') > occurred_at:
                    occurred_at = saved['occurred_at']
                identity = hashlib.sha256(_json([key, desired, occurred_at, saved]).encode()).hexdigest()
                event = dict(request_id=identity + ':mapped', project_id=projection['project_id'],
                    board_id=key[0], task_id=key[1], kind='task_signal', occurred_at=occurred_at,
                    reason='Mapped canonical dependency coverage', data=desired)
                with self.transaction():
                    self.put(pending_key, event)
            monitor.observe(event, Path(ledger_path), source='kanban-sync')
            with self.transaction():
                self.put(cache_key, dict(data=event['data'], request_id=event['request_id'],
                                         occurred_at=event['occurred_at']))
                self.put(pending_key, None)

    def mark_unknown(self, key, code):
        with self.transaction():
            row = self.db.execute('SELECT projection_json FROM sources WHERE board_id=? AND task_id=?', key).fetchone()
            if not row:
                return
            projection = json.loads(row[0])
            projection['coverage'] = 'unknown'
            self.db.execute('UPDATE sources SET coverage=?,projection_json=? WHERE board_id=? AND task_id=?',
                            ('unknown', _json(projection), *key))
            self.db.execute('INSERT INTO sync_events(occurred_at,board_id,task_id,kind,detail_json) VALUES(?,?,?,?,?)',
                            (self.clock().isoformat(), *key, 'source_error', _json(dict(code=code))))

    def admit_facts(self, ledger, monitor, projection, identity):
        if monitor is None:
            return
        occurred = projection['source_updated_at'] or projection['observed_at']
        worker = projection['worker']
        active = worker['state'] == 'running' and projection['status'] == 'running'
        ledger_path = Path(ledger.connection.execute('PRAGMA database_list').fetchone()[2])
        monitor.observe(dict(request_id=identity + ':graph', project_id=projection['project_id'],
            board_id=projection['board_id'], task_id=projection['task_id'], kind='task_signal',
            occurred_at=occurred, reason='Canonical current worker timestamps',
            data=dict(claim_at=worker['started_at'] if active else None,
                      heartbeat_at=worker['heartbeat_at'] if active else None,
                      agent_available=True if active else None)),
            ledger_path, source='kanban-sync')
        if worker['run_id'] and worker['state'] in ('running', 'failed', 'exhausted', 'unavailable'):
            monitor.observe(dict(request_id=identity + ':worker', project_id=projection['project_id'],
                board_id=projection['board_id'], task_id=projection['task_id'], kind='worker_outcome',
                occurred_at=worker['heartbeat_at'] or worker['started_at'] or occurred,
                reason='Canonical current worker', data=dict(state=worker['state'])),
                ledger_path, source='kanban-sync')

    def apply_plan(self, identity, plan, ledger, monitor=None):
        projection = plan['projection']
        key = projection['board_id'], projection['task_id']
        existing = self.db.execute('SELECT source_updated_at FROM sources WHERE board_id=? AND task_id=?', key).fetchone()
        if (not existing
                and self.db.execute('SELECT count(*) FROM sources').fetchone()[0] >= 20000):
            fail('sources_capacity', 'SOURCE_LIMIT')
        if plan.get('reopen'):
            ledger.reopen_task(*key, expected_revision=plan['reopen_revision'],
                request_id=identity + ':reopen', reason='Canonical task reopened', actor='kanban-sync')
        task = ledger.upsert_task(**plan['arguments'])
        if projection['status'] == 'done':
            ledger.claim_complete(*key, expected_revision=task['revision'],
                request_id=identity + ':claim', reason='Canonical completion claim', actor='kanban-sync')
        self.admit_facts(ledger, monitor, projection, identity)
        # Keep the intent pending through both stores. Publish the projection,
        # acknowledgment and cursor together only after their replay-safe calls.
        # The indexed revision is a high-water mark; the public projection stays nullable.
        with self.transaction():
            self.db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (*key, projection['project_id'], projection['tenant'],
                 projection['source_updated_at'] or (existing['source_updated_at'] if existing else None),
                 projection['source_digest'], projection['status'], projection['assignee'],
                 projection['title'], projection['observed_at'], projection['coverage'], _json(projection), projection['source_digest']))
            self.put('ledger_path', str(ledger.connection.execute('PRAGMA database_list').fetchone()[2]))
            self.db.execute("UPDATE sync_requests SET state='applied' WHERE request_id=?", (identity,))
            if 'cursor' in plan:
                self.put('cursor', plan['cursor'])



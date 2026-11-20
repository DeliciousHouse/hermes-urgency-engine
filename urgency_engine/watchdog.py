"""Independent, local monitoring. Observations are evidence, never execution authority."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, NoReturn, TypedDict

from .ledger import LedgerError, _id, _integer, _json, _number, _text, open_readonly_ledger
from .forecast import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast

DEFAULTS = dict(watchdog_enabled=False, watchdog_normal_seconds=300, watchdog_risk_seconds=60,
                watchdog_stall_seconds=1800, watchdog_claim_seconds=900, watchdog_notice_limit=3,
                watchdog_retention_days=30, watchdog_max_rows=10000)
LIMITATIONS = ['Plans are recommendations, not authorization or executed recovery.',
               'Byte identity is not semantic acceptance. Missing telemetry is unknown.',
               'Notice generation cannot prove stdout emission or remote delivery; reconcile unknown emissions.',
               'Retention never prunes active incidents, control replay or ledger audit; disk quota is not guaranteed.']
SCHEMA = (
    'CREATE TABLE monitor_meta(key TEXT PRIMARY KEY, value_json TEXT NOT NULL)',
    'CREATE TABLE observations(event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, board_id TEXT, task_id TEXT, kind TEXT NOT NULL, occurred_at TEXT NOT NULL, received_at TEXT NOT NULL, source TEXT NOT NULL, payload_json TEXT NOT NULL)',
    'CREATE INDEX observations_project ON observations(project_id,occurred_at)',
    'CREATE INDEX observations_task ON observations(board_id,task_id,occurred_at)',
    'CREATE TABLE task_signals(project_id TEXT NOT NULL, board_id TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL, observed_at TEXT NOT NULL, checkpoint_at TEXT, claim_at TEXT, heartbeat_at TEXT, blocker_since TEXT, agent_available INTEGER, dependency_state TEXT, tool_failures INTEGER NOT NULL, recovery_failures INTEGER NOT NULL, PRIMARY KEY(board_id,task_id))',
    'CREATE TABLE dependencies(board_id TEXT NOT NULL, task_id TEXT NOT NULL, depends_board TEXT NOT NULL, depends_task TEXT NOT NULL, PRIMARY KEY(board_id,task_id,depends_board,depends_task))',
    'CREATE TABLE incidents(incident_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, board_id TEXT, task_id TEXT, detector TEXT NOT NULL, episode INTEGER NOT NULL, state TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, next_notice_at TEXT, notice_count INTEGER NOT NULL, fingerprint TEXT NOT NULL, explanation_json TEXT NOT NULL)',
    'CREATE UNIQUE INDEX incident_episode ON incidents(fingerprint,episode)',
    'CREATE INDEX incidents_due ON incidents(state,next_notice_at)',
    'CREATE TABLE plans(plan_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES incidents, created_at TEXT NOT NULL, state TEXT NOT NULL, actions_json TEXT NOT NULL)',
    'CREATE INDEX plans_incident ON plans(incident_id)',
    'CREATE TABLE monitor_audit(sequence INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, action TEXT NOT NULL, occurred_at TEXT NOT NULL, actor TEXT NOT NULL, payload_sha256 TEXT NOT NULL, result_json TEXT NOT NULL)',
)


class Observation(TypedDict):
    request_id: str
    reason: str
    project_id: str
    board_id: str
    task_id: str
    kind: str
    occurred_at: str
    data: dict


def fail(field=None, code='INVALID_INPUT', *, effects='none') -> NoReturn:
    raise LedgerError(code, 'watchdog', field, side_effects=effects)


def stamp(value):
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        fail('clock')
    return value.isoformat()


def instant(value, field='occurred_at'):
    _text(value, field, 40)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() != timedelta(0):
            fail(field)
        return parsed
    except (ValueError, OverflowError):
        fail(field)


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def settings(values):
    result = {key: values.get(key, default) for key, default in DEFAULTS.items()}
    if type(result['watchdog_enabled']) is not bool:
        fail('watchdog_enabled', 'INVALID_CONFIG')
    for field, low, high in [('normal_seconds', 60, 3600), ('risk_seconds', 60, 300),
                             ('stall_seconds', 60, 86400), ('claim_seconds', 60, 86400),
                             ('notice_limit', 1, 10), ('retention_days', 1, 365), ('max_rows', 100, 100000)]:
        _integer(result['watchdog_' + field], 'watchdog_' + field, low, high)
    if result['watchdog_risk_seconds'] > result['watchdog_normal_seconds']:
        fail('watchdog_risk_seconds', 'INVALID_CONFIG')
    return result


def validate(event: Observation, now):
    fields = {'request_id', 'reason', 'project_id', 'board_id', 'task_id', 'kind', 'occurred_at', 'data'}
    if not isinstance(event, dict) or set(event) != fields:
        fail('observation')
    try:
        if len(_json(event).encode()) > 16384:
            fail('observation')
    except (ValueError, TypeError, RecursionError, UnicodeError):
        fail('observation')
    unassigned = event['board_id'] is None and event['task_id'] is None and event['kind'] == 'tool_outcome'
    for field in ('request_id', 'project_id') + (() if unassigned else ('board_id', 'task_id')):
        _id(event[field], field)
    _text(event['reason'], 'reason', 1024)
    at = instant(event['occurred_at'])
    if at > now + timedelta(seconds=60):
        fail('occurred_at')
    kinds = {
        'task_signal': {'checkpoint_at', 'claim_at', 'heartbeat_at', 'blocker_since', 'agent_available',
                        'dependency_state', 'tool_failures', 'recovery_failures', 'dependencies'},
        'tool_outcome': {'tool_name', 'outcome', 'duration_seconds'},
        'worker_outcome': {'state'}, 'recovery_outcome': {'incident_id', 'outcome'},
    }
    data, kind = event['data'], event['kind']
    if not isinstance(kind, str) or kind not in kinds or not isinstance(data, dict) or set(data) - kinds[kind]:
        fail('data')
    if kind != 'task_signal' and set(data) != kinds[kind]:
        fail('data')
    for field in ('checkpoint_at', 'claim_at', 'heartbeat_at', 'blocker_since'):
        if data.get(field) is not None:
            instant(data[field], field)
    for field in ('claim_at', 'heartbeat_at', 'blocker_since'):
        if data.get(field) is not None and instant(data[field], field) > now + timedelta(seconds=60):
            fail(field)
    if 'agent_available' in data and data['agent_available'] is not None and type(data['agent_available']) is not bool:
        fail('agent_available')
    if 'dependency_state' in data and data['dependency_state'] not in (None, 'available', 'unreachable', 'unknown'):
        fail('dependency_state')
    for field in ('tool_failures', 'recovery_failures'):
        if field in data:
            _integer(data[field], field, 0, 1000000)
    if 'dependencies' in data:
        if not isinstance(data['dependencies'], list) or len(data['dependencies']) > 1000:
            fail('dependencies')
        for pair in data['dependencies']:
            if not isinstance(pair, list) or len(pair) != 2:
                fail('dependencies')
            for key in pair:
                _id(key, 'dependencies')
    if kind == 'tool_outcome':
        _id(data['tool_name'], 'tool_name')
        _number(data['duration_seconds'], 'duration_seconds')
        if data['outcome'] not in ('success', 'failure', 'unknown'):
            fail('outcome')
    if kind == 'worker_outcome' and data['state'] not in ('running', 'failed', 'exhausted', 'unavailable'):
        fail('state')
    if kind == 'recovery_outcome':
        _id(data['incident_id'], 'incident_id')
        if data['outcome'] not in ('improved', 'no_improvement', 'failed'):
            fail('outcome')
    return {**event, 'occurred_at': stamp(at)}


def snapshot(path, clock, cursor=None, deadline=None):
    """One consistent, read-only ledger snapshot; no cross-database atomicity claim."""
    stop = deadline or time.monotonic() + 25
    try:
        ledger = open_readonly_ledger(path, clock=clock, deadline=stop)
    except LedgerError as exc:
        if exc.code == 'NOT_FOUND':
            return dict(tasks=[], deadlines=[], cursor=None, complete=True, coverage='ledger_uninitialized', revisions={})
        raise
    with ledger, ledger._transaction('watchdog_snapshot'):
        db = ledger.connection
        after = cursor or ['', '']
        tasks = [dict(row) for row in db.execute(
            'SELECT * FROM tasks WHERE (board_id,task_id)>(?,?) ORDER BY board_id,task_id LIMIT 501', after)]
        more = len(tasks) > 500
        tasks = tasks[:500]
        revisions = {}
        for task in tasks:
            project = ledger._project(task['project_id'])
            revisions[task['project_id']] = project['revision']
            task['objective'] = project['title']
            task['required_criteria'] = [dict(row) for row in db.execute(
                'SELECT criterion_id,description,required,expected_sha256 FROM criteria WHERE board_id=? AND task_id=? AND active=1 AND required=1 ORDER BY criterion_id',
                (task['board_id'], task['task_id']))]
        # Deadline cursor is independent: project deadlines must not starve behind task pages.
        return dict(tasks=tasks, deadlines=[], cursor=[tasks[-1]['board_id'], tasks[-1]['task_id']] if more else None,
                    complete=not more, coverage='bounded_task_snapshot', revisions=revisions)


def cyclic_nodes(edges):
    """Iterative Kosaraju SCC avoids Python recursion on long dependency chains."""
    graph, reverse = {}, {}
    for a, b in edges:
        graph.setdefault(a, []).append(b)
        graph.setdefault(b, [])
        reverse.setdefault(b, []).append(a)
        reverse.setdefault(a, [])
    seen, order = set(), []
    for root in sorted(graph):
        stack = [(root, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                order.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((n, False) for n in sorted(graph[node], reverse=True) if n not in seen)
    seen, cycles = set(), {}
    for root in reversed(order):
        if root in seen:
            continue
        component, stack = [], [root]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            stack.extend(reverse[node])
        if len(component) > 1 or root in graph[root]:
            evidence = [list(n) for n in sorted(component)[:20]]
            for node in component:
                cycles[node] = evidence
    return cycles


class Monitor:
    def __init__(self, connection, *, clock, readonly=False):
        self.db, self.clock = connection, clock
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=100')
        self.db.execute('PRAGMA foreign_keys=ON')
        try:
            with self.transaction(write=not readonly):
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                objects = [r[0] for r in self.db.execute("SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")]
                if not version and not objects and not readonly:
                    for sql in SCHEMA:
                        self.db.execute(sql)
                    self.db.execute('PRAGMA user_version=1')
                elif version != 1:
                    fail(None, 'UNSUPPORTED_SCHEMA')
                elif sorted(objects) != sorted(SCHEMA):
                    fail(None, 'CORRUPT_STATE')
        except BaseException:
            self.db.close()
            raise

    @classmethod
    def open(cls, path, *, clock, readonly=False):
        if readonly:
            if not path.exists():
                return None
            db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=.1)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, timeout=.1)
        return cls(db, clock=clock, readonly=readonly)

    @classmethod
    def host(cls, *, clock):
        from plugins.plugin_storage import plugin_db
        return cls(plugin_db('hermes-urgency-engine', 'watchdog.db'), clock=clock)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def transaction(self, write=True):
        committing = False
        try:
            self.db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield
            committing = True
            self.db.commit()
        except BaseException as exc:
            self.db.rollback()
            if isinstance(exc, sqlite3.Error):
                code = getattr(exc, 'sqlite_errorcode', 0) & 255
                safe = {sqlite3.SQLITE_BUSY: 'STORE_BUSY', sqlite3.SQLITE_LOCKED: 'STORE_BUSY',
                        sqlite3.SQLITE_NOTADB: 'CORRUPT_STATE', sqlite3.SQLITE_CORRUPT: 'CORRUPT_STATE'}.get(code, 'STORAGE_FAILURE')
                fail(None, safe, effects='unknown' if committing else 'none')
            raise

    def meta(self, key, default=None) -> Any:
        row = self.db.execute('SELECT value_json FROM monitor_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT INTO monitor_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json', (key, _json(value)))

    def replay(self, request, payload):
        row = self.db.execute('SELECT * FROM monitor_audit WHERE request_id=?', (request,)).fetchone()
        if row:
            if row['payload_sha256'] != digest(payload):
                fail('request_id', 'IDEMPOTENCY_CONFLICT')
            return json.loads(row['result_json'])

    def audit(self, request, action, payload, result, actor='local-operator'):
        self.db.execute('INSERT INTO monitor_audit(request_id,action,occurred_at,actor,payload_sha256,result_json) VALUES(?,?,?,?,?,?)',
                        (request, action, stamp(self.clock()), actor, digest(payload), _json(result)))
        return result

    def control(self, action, request_id, reason):
        _id(request_id, 'request_id')
        _text(reason, 'reason', 1024)
        if action not in ('pause', 'resume'):
            fail('action')
        payload = [action, reason]
        with self.transaction():
            replay = self.replay(request_id, payload)
            if replay is not None:
                return replay
            self.put('paused', action == 'pause')
            self.put('generation', self.meta('generation', 0) + 1)
            if action == 'resume':
                self.put('next_due', None)
            return self.audit(request_id, action, payload, {'paused': action == 'pause'})

    def capacity(self, cfg, added=1):
        # Audit replay is protected; a full store refuses writes rather than evicting safety.
        count = sum(self.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                    for table in ('observations', 'task_signals', 'incidents', 'plans', 'monitor_audit', 'monitor_meta', 'dependencies'))
        if count + added > cfg['watchdog_max_rows']:
            fail('watchdog_max_rows', 'CAPACITY_EXCEEDED')

    def observe(self, event, ledger_path, cfg=None, *, source='local-operator'):
        cfg = settings(cfg or DEFAULTS)
        event = validate(event, self.clock())
        with open_readonly_ledger(ledger_path, clock=self.clock) as ledger:
            task = (ledger.get_task(event['board_id'], event['task_id']) if event['task_id'] is not None
                    else ledger.get_project(event['project_id']))
            if task['project_id'] != event['project_id']:
                fail('project_id')
            for pair in event['data'].get('dependencies', []):
                ledger.get_task(*pair)
        key = event['board_id'], event['task_id']
        with self.transaction():
            replay = self.replay(event['request_id'], event)
            if replay is not None:
                return replay
            self.prune(cfg)
            self.capacity(cfg, 4 + len(event['data'].get('dependencies', [])))
            if event['task_id'] is None:
                self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)',
                    (event['request_id'], event['project_id'], None, None, event['kind'], event['occurred_at'],
                     stamp(self.clock()), source, _json({'data': event['data'], 'out_of_order': False, 'correlation': 'unassigned'})))
                return self.audit(event['request_id'], 'observe', event,
                                  {'event_id': event['request_id'], 'correlation': 'unassigned'}, actor=source)
            row = self.db.execute('SELECT * FROM task_signals WHERE board_id=? AND task_id=?', key).fetchone()
            stale = bool(row and event['occurred_at'] < row['observed_at'])
            if not row:
                self.db.execute('INSERT INTO task_signals VALUES(?,?,?,0,?,NULL,NULL,NULL,NULL,NULL,NULL,0,0)',
                                (event['project_id'], *key, event['occurred_at']))
            data, kind = event['data'], event['kind']
            if kind == 'recovery_outcome':
                incident = self.db.execute('SELECT * FROM incidents WHERE incident_id=?', (data['incident_id'],)).fetchone()
                if not incident or incident['state'] != 'active' or (incident['board_id'], incident['task_id']) != key:
                    fail('incident_id')
            if not stale:
                if kind == 'task_signal':
                    for field, value in data.items():
                        if field == 'dependencies':
                            self.db.execute('DELETE FROM dependencies WHERE board_id=? AND task_id=?', key)
                            self.db.executemany('INSERT OR IGNORE INTO dependencies VALUES(?,?,?,?)', [(*key, *pair) for pair in value])
                        else:
                            self.db.execute('UPDATE task_signals SET ' + field + '=? WHERE board_id=? AND task_id=?', (value, *key))
                elif kind == 'tool_outcome' and data['outcome'] == 'success':
                    self.db.execute('UPDATE task_signals SET tool_failures=0 WHERE board_id=? AND task_id=?', key)
                elif kind == 'worker_outcome':
                    self.db.execute('UPDATE task_signals SET agent_available=? WHERE board_id=? AND task_id=?', (int(data['state'] == 'running'), *key))
                self.db.execute('UPDATE task_signals SET revision=revision+1,observed_at=? WHERE board_id=? AND task_id=?', (event['occurred_at'], *key))
            self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)',
                            (event['request_id'], event['project_id'], *key, kind, event['occurred_at'], stamp(self.clock()), source,
                             _json({'data': data, 'out_of_order': stale, 'source_revision': task['revision']})))
            self.put('generation', self.meta('generation', 0) + 1)
            return self.audit(event['request_id'], 'observe', event,
                              {'event_id': event['request_id'], 'out_of_order': stale}, actor=source)

    def prune(self, cfg):
        cutoff = stamp(self.clock() - timedelta(days=cfg['watchdog_retention_days']))
        self.db.execute('DELETE FROM observations WHERE received_at<?', (cutoff,))
        self.db.execute("DELETE FROM plans WHERE incident_id IN (SELECT incident_id FROM incidents WHERE state='resolved' AND last_seen<?)", (cutoff,))
        self.db.execute("DELETE FROM incidents WHERE state='resolved' AND last_seen<?", (cutoff,))

    def page(self, table, limit=100, after=None, project_id=None, active=False):
        fields = {'incidents': 'incident_id', 'plans': 'plan_id', 'observations': 'event_id', 'monitor_audit': 'sequence'}
        if table not in fields:
            fail('table')
        _integer(limit, 'limit', 1, 500)
        column = fields[table]
        if after is not None:
            (_integer if column == 'sequence' else _id)(after, 'after')
        parameters = [after or (0 if column == 'sequence' else '')]
        condition = f'{column}>?'
        if project_id is not None:
            _id(project_id, 'project_id')
            if table == 'plans':
                condition += ' AND incident_id IN (SELECT incident_id FROM incidents WHERE project_id=?)'
            elif table in ('incidents', 'observations'):
                condition += ' AND project_id=?'
            else:
                fail('project_id')
            parameters.append(project_id)
        if active:
            condition += " AND state='active'" if table == 'incidents' else " AND state='recommended'"
        rows = [dict(r) for r in self.db.execute(f'SELECT * FROM {table} WHERE {condition} ORDER BY {column} LIMIT ?', (*parameters, limit + 1))]
        more = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            for field in list(row):
                if field.endswith('_json'):
                    row[field[:-5]] = json.loads(row.pop(field))
        return dict(schema_version=1, items=rows, has_more=more, next=rows[-1][column] if more else None)

    def health(self):
        last = self.meta('last_success')
        return dict(schema_version=1, storage='available', paused=self.meta('paused', False),
                    installed=self.meta('install'), last_success=last, last_scan=self.meta('last_scan'),
                    lag_seconds=max(0, (self.clock() - instant(last)).total_seconds()) if last else None,
                    cursor=self.meta('cursor'), next_due=self.meta('next_due'), coverage=self.meta('coverage', 'not_observed'),
                    emission='unknown_after_commit', limitations=LIMITATIONS)

    def tick(self, ledger_path, cfg, force=False):
        cfg = settings(cfg)
        now, started = self.clock(), time.monotonic()
        stamp(now)
        empty = {'notices': [], 'evaluated': False}
        with self.transaction(False):
            generation = self.meta('generation', 0)
            if self.meta('paused', False):
                return {**empty, 'reason': 'paused'}
            last, due, cursor = self.meta('last_success'), self.meta('next_due'), self.meta('cursor')
            if last and instant(last) > now:
                fail('clock', 'CLOCK_ROLLBACK')
            if not force and due and instant(due) > now:
                return {**empty, 'reason': 'not_due'}
        snap = snapshot(ledger_path, self.clock, cursor, started + 25)
        if snap['coverage'] == 'ledger_uninitialized':
            with self.transaction():
                if self.meta('generation', 0) != generation or self.meta('paused', False):
                    return {**empty, 'reason': 'concurrent_change'}
                self.put('coverage', 'ledger_uninitialized')
                self.put('last_success', stamp(now))
                self.put('next_due', stamp(now + timedelta(seconds=cfg['watchdog_normal_seconds'])))
                self.put('generation', generation + 1)
            return {**empty, 'reason': 'ledger_uninitialized'}
        # All source I/O precedes the monitor write lock.
        with open_readonly_ledger(ledger_path, clock=self.clock, deadline=started + 25) as ledger, ledger._transaction('watchdog_sources'):
            db = ledger.connection
            deadline_cursor = self.meta('deadline_cursor', '')
            deadlines = [dict(r) for r in db.execute('SELECT * FROM deadlines WHERE deadline_id>? ORDER BY deadline_id LIMIT 501', (deadline_cursor,))]
            deadline_more = len(deadlines) > 500
            deadlines = deadlines[:500]
            explained = [ledger._explain(d['deadline_id']) for d in deadlines]
            for item in explained:
                item['objective'] = ledger._project(item['project_id'])['title']
                scope = item['input_revisions']['tasks']
                item['affected_tasks'] = [pair[:2] for pair in scope[:500]]
                item['required_criteria'] = []
                for board, task, _ in scope[:500]:
                    item['required_criteria'].extend(dict(row) for row in db.execute(
                        'SELECT board_id,task_id,criterion_id,description FROM criteria WHERE board_id=? AND task_id=? AND active=1 AND required=1 LIMIT 501', (board, task)))
                    if len(item['required_criteria']) > 500:
                        break
                item['verification_truncated'] = len(scope) > 500 or len(item['required_criteria']) > 500
                item['required_criteria'] = item['required_criteria'][:500]
            graph_rows = self.db.execute('SELECT * FROM dependencies ORDER BY board_id,task_id,depends_board,depends_task LIMIT 50001').fetchall()
            nodes = {tuple(r[:2]) for r in graph_rows} | {tuple(r[2:]) for r in graph_rows}
            graph_complete = len(graph_rows) <= 50000 and len(nodes) <= 10000
            existing = set()
            if graph_complete:
                for key in sorted(nodes):
                    if db.execute('SELECT 1 FROM tasks WHERE board_id=? AND task_id=?', key).fetchone():
                        existing.add(key)
            source_current = all(ledger._project(p)['revision'] == r for p, r in snap['revisions'].items())
        cycles = cyclic_nodes([(tuple(r[:2]), tuple(r[2:])) for r in graph_rows if tuple(r[2:]) in existing]) if graph_complete else {}
        if time.monotonic() - started >= 25:
            fail('tick', 'TIME_BUDGET')
        with self.transaction():
            if self.meta('generation', 0) != generation or self.meta('paused', False):
                return {**empty, 'reason': 'concurrent_change'}
            if not source_current:
                self.put('coverage', 'stale_source_revisions; retry_next_tick')
                return {**empty, 'reason': 'stale_source_revisions'}
            self.prune(cfg)
            notices, transitions, high = [], 0, False
            completed_tasks = []
            for task in snap['tasks']:
                if transitions >= 100 or time.monotonic() - started >= 28:
                    break
                key = task['board_id'], task['task_id']
                row = self.db.execute('SELECT * FROM task_signals WHERE board_id=? AND task_id=?', key).fetchone()
                signal = dict(row) if row else {}
                memory_key = 'task:' + digest(key)
                previous = self.meta(memory_key, {})
                progress = task['last_verified_progress_at']
                changed = bool(previous and previous.get('progress') != progress and progress)
                first = previous.get('running_since') if previous.get('status') == 'running' else None
                first = first or stamp(now)
                facts = self.detect(task, signal, previous, first, now, cfg, cycles, graph_rows, existing, graph_complete)
                if task['status'] == 'verified' or changed:
                    facts = {}
                spent, emitted = self.reconcile(task, facts, now, cfg, 100 - transitions)
                transitions += spent
                notices.extend(emitted)
                self.capacity(cfg, 1 if not previous else 0)
                self.put(memory_key, dict(status=task['status'], progress=progress, running_since=first))
                completed_tasks.append(task)
                high |= bool(facts)
            completed_deadlines = []
            for deadline in explained:
                if transitions >= 100 or time.monotonic() - started >= 28:
                    break
                memory_key = 'deadline:' + digest(deadline['deadline_id'])
                previous = self.meta(memory_key, {})
                old = previous.get('urgency')
                state = UrgencyState(UrgencyLevel(old['level']), instant(old['changed_at'])) if old else None
                values = deadline['input']
                forecast = calculate_forecast(ForecastInput(instant(deadline['target_at']), values['remaining_seconds'],
                      values['verification_seconds'], values['deployment_seconds']), now=now, previous=state)
                high |= int(forecast.level) >= 2
                facts = {}
                if not deadline['actual_verified_at']:
                    if instant(deadline['target_at']) < now:
                        facts['expired_deadline'] = dict(target_at=deadline['target_at'], original_at=deadline['original_at'], kind=deadline['kind'], level=int(forecast.level))
                    if previous.get('forecast_at') and instant(previous['forecast_at']) < now:
                        facts['forecast_miss'] = dict(predicted_at=previous['forecast_at'], observed='required_work_unverified', level=int(forecast.level))
                entity = dict(project_id=deadline['project_id'], board_id=deadline['board_id'], task_id=deadline['task_id'],
                              deadline_id=deadline['deadline_id'], objective=deadline['objective'], required_criteria=deadline['required_criteria'],
                              affected_tasks=deadline['affected_tasks'], verification_truncated=deadline['verification_truncated'], revision=deadline['revision'])
                spent, emitted = self.reconcile(entity, facts, now, cfg, 100 - transitions, deadline_id=deadline['deadline_id'])
                transitions += spent
                notices.extend(emitted)
                self.capacity(cfg, 1 if not previous else 0)
                self.put(memory_key, dict(forecast_at=previous.get('forecast_at', deadline['forecast_at']),
                    urgency=dict(level=int(forecast.level), changed_at=stamp(forecast.changed_at))))
                completed_deadlines.append(deadline)
            if completed_tasks:
                last_task = completed_tasks[-1]
                next_cursor = [last_task['board_id'], last_task['task_id']]
                if len(completed_tasks) == len(snap['tasks']) and snap['complete']:
                    next_cursor = None
                self.put('cursor', next_cursor)
            else:
                next_cursor = cursor
            if completed_deadlines:
                self.put('deadline_cursor', completed_deadlines[-1]['deadline_id'] if deadline_more or len(completed_deadlines) < len(explained) else '')
            complete = next_cursor is None and not self.meta('deadline_cursor', '')
            self.put('generation', generation + 1)
            self.put('last_success', stamp(now))
            if complete:
                self.put('last_scan', stamp(now))
            self.put('coverage', dict(tasks_processed=len(completed_tasks), deadlines_processed=len(completed_deadlines),
                     scan_complete=complete, graph_complete=graph_complete, source_revisions=snap['revisions'],
                     missing_signals='insufficient_evidence', source_consistency='two snapshots; subsequent changes observed next tick'))
            self.put('next_due', stamp(now + timedelta(seconds=cfg['watchdog_risk_seconds'] if high else cfg['watchdog_normal_seconds'])))
            return dict(notices=notices, evaluated=True, transitions=transitions, scan_complete=complete)

    def detect(self, task, signal, previous, first, now, cfg, cycles, edges, existing, graph_complete):
        facts, key = {}, (task['board_id'], task['task_id'])
        if task['status'] == 'verified':
            return facts
        def elapsed(value, seconds):
            return value is not None and (now - instant(value)).total_seconds() >= seconds
        progress = task['last_verified_progress_at']
        if task['status'] == 'running' and elapsed(progress or first, cfg['watchdog_stall_seconds']):
            facts['stalled'] = dict(since=progress or first, basis='verified_progress' if progress else 'first_observed_running')
        if signal.get('checkpoint_at') and instant(signal['checkpoint_at']) < now:
            facts['missed_checkpoint'] = dict(checkpoint_at=signal['checkpoint_at'])
        if key in cycles:
            facts['dependency_cycle'] = dict(component=cycles[key])
        source_times = {}
        if signal.get('agent_available') == 0 or signal.get('dependency_state') == 'unreachable':
            for row in self.db.execute("SELECT kind,occurred_at,payload_json FROM observations WHERE board_id=? AND task_id=? AND kind IN ('task_signal','worker_outcome') ORDER BY occurred_at DESC,rowid DESC", key):
                payload = json.loads(row['payload_json'])
                if payload['out_of_order']:
                    continue
                for field in ('agent_available', 'dependency_state'):
                    if field in payload['data'] or (field == 'agent_available' and row['kind'] == 'worker_outcome'):
                        source_times.setdefault(field, row['occurred_at'])
                if len(source_times) == 2:
                    break
        if signal.get('agent_available') == 0:
            facts['delegated_work_unavailable'] = dict(source='explicit_unavailable', observed_at=source_times.get('agent_available'), freshness='retained_source' if 'agent_available' in source_times else 'unknown_source_pruned')
        claim = signal.get('heartbeat_at') or signal.get('claim_at')
        if elapsed(claim, cfg['watchdog_claim_seconds']):
            facts['stale_claim'] = dict(since=claim, action='inspect_only')
        if signal.get('dependency_state') == 'unreachable':
            facts['dependency_unreachable'] = dict(source='explicit_signal', observed_at=source_times.get('dependency_state'), freshness='retained_source' if 'dependency_state' in source_times else 'unknown_source_pruned')
        elif graph_complete:
            missing = [list(r[2:]) for r in edges if tuple(r[:2]) == key and tuple(r[2:]) not in existing]
            if missing:
                facts['dependency_unreachable'] = dict(missing=missing[:20], source='complete_known_graph')
        recent = stamp(now - timedelta(seconds=900))
        rows = self.db.execute("SELECT * FROM observations WHERE board_id=? AND task_id=? AND kind IN ('tool_outcome','task_signal') AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,rowid", (*key, recent, stamp(now)))
        failures = 0
        for row in rows:
            payload = json.loads(row['payload_json'])
            if payload['out_of_order']:
                continue
            data = payload['data']
            if row['kind'] == 'task_signal':
                if 'tool_failures' in data:
                    failures = data['tool_failures']
            elif data['outcome'] == 'success':
                failures = 0
            elif data['outcome'] == 'failure':
                failures += 1
        if failures >= 3:
            facts['repeated_tool_failure'] = dict(failures=failures, window_seconds=900)
        recovery = {}
        for row in self.db.execute("SELECT payload_json FROM observations WHERE board_id=? AND task_id=? AND kind='recovery_outcome' ORDER BY occurred_at,rowid", key):
            payload = json.loads(row[0])
            if payload['out_of_order']:
                continue
            data = payload['data']
            recovery[data['incident_id']] = 0 if data['outcome'] == 'improved' else recovery.get(data['incident_id'], 0) + 1
        active = {r[0] for r in self.db.execute("SELECT incident_id FROM incidents WHERE board_id=? AND task_id=? AND state='active'", key)}
        failed = sorted(i for i, count in recovery.items() if count >= 3 and i in active)
        if failed:
            facts['ineffective_recovery'] = dict(incident_ids=failed)
        return facts

    def reconcile(self, task, facts, now, cfg, budget, deadline_id=None):
        entity = ['deadline', deadline_id] if deadline_id else ['task', task['board_id'], task['task_id']]
        prefix = digest(entity)
        rows = [dict(r) for r in self.db.execute('SELECT * FROM incidents WHERE fingerprint LIKE ? ORDER BY episode', (prefix + ':%',))]
        latest = {r['detector']: r for r in rows}
        transitions, notices = 0, []
        for detector in sorted(set(latest) | set(facts)):
            if transitions >= budget:
                break
            old, detail = latest.get(detector), facts.get(detector)
            if detail is None:
                if old and old['state'] == 'active':
                    self.db.execute("UPDATE incidents SET state='resolved',last_seen=? WHERE incident_id=?", (stamp(now), old['incident_id']))
                    self.db.execute("UPDATE plans SET state='resolved' WHERE incident_id=?", (old['incident_id'],))
                    transitions += 1
                continue
            explanation = dict(facts=detail, source_revision=task['revision'], deadline_id=deadline_id,
                               confidence='observed_rule_match', unknowns=['semantic quality', 'external execution state'])
            new = not old or old['state'] == 'resolved'
            if new:
                self.capacity(cfg, 2)
                episode = old['episode'] + 1 if old else 1
                fingerprint = prefix + ':' + detector
                identity = digest([fingerprint, episode])
                self.db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identity, task['project_id'], task['board_id'], task['task_id'], detector, episode, 'active', stamp(now), stamp(now), stamp(now), 0, fingerprint, _json(explanation)))
                plan = dict(status='recommendation', objective=task['objective'], deadline=deadline_id,
                    affected_tasks=task.get('affected_tasks', [[task['board_id'], task['task_id']]] if task['task_id'] else []), facts=detail,
                    verification_truncated=task.get('verification_truncated', False),
                    unknowns=explanation['unknowns'], remaining_required_verification=task['required_criteria'],
                    expected_completion_evidence='Verify every required criterion; byte identity alone does not certify semantic quality.',
                    actions=[dict(action=action, reason=reason) for action, reason in (
                        ('inspect_reestimate', 'Reconcile source facts and remaining work before replanning.'),
                        ('resolve_named_blocker', detector), ('prioritize_critical_path', 'Preserve commitments and verification buffers.'),
                        ('identify_independent_work', 'Only known dependency-independent work is a candidate, not authorization to delegate.'),
                        ('consider_model_effort', 'Recommendation only; preserve budget and configured models.'),
                        ('operator_escalation', 'Escalate unavailable capability without bypassing host controls.'))])
                self.db.execute('INSERT INTO plans VALUES(?,?,?,?,?)', (identity, identity, stamp(now), 'recommended', _json(plan)))
                old = dict(incident_id=identity, notice_count=0, next_notice_at=stamp(now), explanation_json=_json(explanation))
                transitions += 1
            prior = json.loads(old['explanation_json'])
            rising = detail.get('level', 0) > prior['facts'].get('level', 0)
            due = old['next_notice_at'] and instant(old['next_notice_at']) <= now
            if old['notice_count'] < cfg['watchdog_notice_limit'] and (due or rising):
                count = old['notice_count'] + 1
                next_at = stamp(now + timedelta(seconds=min(3600, 300 * 2 ** (count - 1))))
                self.db.execute('UPDATE incidents SET notice_count=?,next_notice_at=? WHERE incident_id=?', (count, next_at, old['incident_id']))
                notices.append(dict(incident_id=old['incident_id'], detector=detector, emission='unknown_after_commit',
                                    next_action='Inspect watchdog plans; preserve verification and permissions.'))
            self.db.execute('UPDATE incidents SET last_seen=?,explanation_json=? WHERE incident_id=?', (stamp(now), _json(explanation), old['incident_id']))
        return transitions, notices

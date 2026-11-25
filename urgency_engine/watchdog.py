"""Independent, local monitoring. Observations are evidence, never execution authority."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
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
TICK_NOTICE_LIMIT = 40
# Raw observations no detector reads again: unassigned, or behind the task's evidence watermark.
CONSUMED = '''(o.task_id IS NULL OR EXISTS (SELECT 1 FROM monitor_meta m JOIN monitor_audit a
    ON a.request_id=o.event_id WHERE m.key='evidence:' || json_array(o.board_id,o.task_id)
    AND json_extract(m.value_json,'$.sequence')>=a.sequence))'''
CLAIM_SOURCES = ('heartbeat_at', 'claim_at')
LIMITATIONS = ['Plans are recommendations, not authorization or executed recovery.',
               'Byte identity is not semantic acceptance. Missing telemetry is unknown.',
               'Notice generation cannot prove stdout emission or remote delivery; reconcile unknown emissions.',
               'Retention never prunes active incidents, episode counters, control replay or ledger audit; disk quota is not guaranteed.']
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


@dataclass(frozen=True)
class Evaluation:
    detector: str
    source: str
    verdict: str
    at: str | None
    revision: int
    facts: dict


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
            task['remaining_known'] = bool(task.get('remaining_known', 1))
            if not task['remaining_known']:
                task['remaining_seconds'] = None
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

    def rows(self):
        return sum(self.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                   for table in ('observations', 'task_signals', 'incidents', 'plans', 'monitor_audit', 'monitor_meta', 'dependencies'))

    def capacity(self, cfg, added=1):
        # Safety state is never evicted. Raw telemetry that no detector still needs
        # (unassigned, or already folded into its task's evidence) is reclaimed
        # oldest first, with its observe receipt, before a write is refused.
        excess = self.rows() + added - cfg['watchdog_max_rows']
        if excess > 0:
            # ponytail: a full store rescans observations per write; add headroom if that shows up in ticks.
            self.db.execute('DELETE FROM observations WHERE event_id IN (SELECT event_id FROM observations AS o WHERE '
                            + CONSUMED + ' ORDER BY received_at, event_id LIMIT ?)', (excess,))
            self.drop_orphan_receipts()
            if self.rows() + added > cfg['watchdog_max_rows']:
                fail('watchdog_max_rows', 'CAPACITY_EXCEEDED')

    def drop_orphan_receipts(self):
        # An observe receipt only guards replay of its retained observation. The
        # newest sequence stays so SQLite cannot reuse evidence watermarks.
        self.db.execute("""DELETE FROM monitor_audit WHERE action='observe'
            AND sequence<(SELECT max(sequence) FROM monitor_audit)
            AND NOT EXISTS (SELECT 1 FROM observations WHERE event_id=monitor_audit.request_id)""")

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
            result = self.audit(event['request_id'], 'observe', event,
                                {'event_id': event['request_id'], 'out_of_order': stale}, actor=source)
            self.capture_evidence(key, cfg)
            return result

    def prune(self, cfg):
        cutoff = stamp(self.clock() - timedelta(days=cfg['watchdog_retention_days']))
        # The receipt scan runs only when retention removed something; capacity() always runs it.
        if self.db.execute('DELETE FROM observations AS o WHERE received_at<? AND ' + CONSUMED, (cutoff,)).rowcount:
            self.drop_orphan_receipts()
        # Keep one episode high-water mark per fingerprint, not the expired rows.
        self.db.execute('''INSERT INTO monitor_meta(key,value_json)
            SELECT 'episode:' || fingerprint, CAST(MAX(episode) AS TEXT)
            FROM incidents WHERE state='resolved' AND last_seen<? GROUP BY fingerprint
            ON CONFLICT(key) DO UPDATE SET value_json=CAST(MAX(
                CAST(monitor_meta.value_json AS INTEGER), CAST(excluded.value_json AS INTEGER)) AS TEXT)''', (cutoff,))
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

    def action_scope(self, incident_id, plan_id):
        """Read the exact active episode and its recommended plan together."""
        incident = self.db.execute("SELECT * FROM incidents WHERE incident_id=? AND state='active'", (incident_id,)).fetchone()
        plan = self.db.execute("SELECT * FROM plans WHERE plan_id=? AND incident_id=? AND state='recommended'",
                               (plan_id, incident_id)).fetchone()
        if not incident or not plan:
            return None
        return dict(incident), json.loads(plan['actions_json'])

    def health(self):
        last = self.meta('last_success')
        return dict(schema_version=1, storage='available', paused=self.meta('paused', False),
                    installed=self.meta('install'), last_success=last, last_scan=self.meta('last_scan'),
                    lag_seconds=max(0, (self.clock() - instant(last)).total_seconds()) if last else None,
                    cursor=self.meta('cursor'), next_due=self.meta('next_due'), coverage=self.meta('coverage', 'not_observed'),
                    emission='unknown_after_commit', limitations=LIMITATIONS)

    def tick(self, ledger_path, cfg, force=False, *, forecast_scope=None):
        from contextlib import nullcontext

        cfg = settings(cfg)
        now, started = self.clock(), time.monotonic()
        stamp(now)
        empty = {'notices': [], 'omitted': 0, 'evaluated': False}
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
        with (open_readonly_ledger(ledger_path, clock=self.clock, deadline=started + 25) as ledger,
              forecast_scope(ledger, deadline=started + 25) if forecast_scope else nullcontext(),
              ledger._transaction('watchdog_sources')):
            db = ledger.connection
            deadline_cursor = self.meta('deadline_cursor', '')
            deadlines = [dict(r) for r in db.execute('SELECT * FROM deadlines WHERE deadline_id>? ORDER BY deadline_id LIMIT 501', (deadline_cursor,))]
            deadline_more = len(deadlines) > 500
            deadlines = deadlines[:500]
            explained = [ledger._explain(d['deadline_id']) for d in deadlines]
            for item in explained:
                item['objective'] = ledger._project(item['project_id'])['title']
                revision = db.execute("SELECT sequence,occurred_at,reason,after_json FROM audit WHERE entity_type='deadline' AND entity_id=? AND action='revise_deadline' ORDER BY sequence DESC LIMIT 1", (item['deadline_id'],)).fetchone()
                receipt = json.loads(revision['after_json']) if revision else {}
                item['replan'] = (dict(at=revision['occurred_at'], revision=item['revision'],
                                      audit_sequence=revision['sequence'], reason=revision['reason'])
                                  if receipt.get('revision') == item['revision'] and receipt.get('target_at') == item['target_at'] else None)
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
            notices, omitted, transitions, high = [], 0, 0, False
            evaluation_counts = dict(active=0, clear=0, unknown=0)
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
                # Keep verified progress watermarks across coverage gaps.
                first = previous.get('running_since') if previous.get('status') == 'running' else None
                first = first or stamp(now)
                facts = self.detect(task, signal, previous, first, now, cfg, cycles, graph_rows, existing, graph_complete)
                for evaluation in facts:
                    evaluation_counts[evaluation.verdict] += 1
                spent, emitted, overflow = self.reconcile(task, facts, now, cfg, 100 - transitions,
                    notice_budget=TICK_NOTICE_LIMIT - len(notices))
                transitions += spent
                notices.extend(emitted)
                omitted += overflow
                self.capacity(cfg, 1 if not previous else 0)
                self.put(memory_key, dict(status=task['status'],
                    progress=max(filter(None, (previous.get('progress'), progress)), key=instant, default=None),
                    revision=max(task['revision'], previous.get('revision', 0)), running_since=first))
                completed_tasks.append(task)
                high |= any(e.verdict == 'active' for e in facts)
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
                replan = deadline['replan']
                superseded = replan and deadline['revision'] != previous.get('revision', 1)
                prediction = deadline['forecast_at'] if superseded else previous.get('forecast_at')
                if not deadline['actual_verified_at']:
                    if instant(deadline['target_at']) < now:
                        facts['expired_deadline'] = dict(target_at=deadline['target_at'], original_at=deadline['original_at'], kind=deadline['kind'], level=int(forecast.level))
                    if prediction and instant(prediction) < now:
                        facts['forecast_miss'] = dict(predicted_at=prediction, observed='required_work_unverified', level=int(forecast.level))
                entity = dict(project_id=deadline['project_id'], board_id=deadline['board_id'], task_id=deadline['task_id'],
                              deadline_id=deadline['deadline_id'], objective=deadline['objective'], required_criteria=deadline['required_criteria'],
                              affected_tasks=deadline['affected_tasks'], verification_truncated=deadline['verification_truncated'], revision=deadline['revision'])
                evaluations = [Evaluation(d, 'commitment', 'active', stamp(now), deadline['revision'], f)
                               for d, f in facts.items()]
                if replan:
                    for detector, future in (('expired_deadline', instant(deadline['target_at']) > now),
                                             ('forecast_miss', superseded or prediction and instant(prediction) >= now)):
                        if future:
                            evaluations.append(Evaluation(detector, 'commitment', 'clear', replan['at'],
                                                          replan['revision'], dict(replan=replan)))
                if deadline['actual_verified_at']:
                    evaluations = [Evaluation(d, 'verified_completion', 'clear', stamp(now), deadline['revision'],
                                              dict(actual_verified_at=deadline['actual_verified_at']))
                                   for d in ('expired_deadline', 'forecast_miss')]
                for evaluation in evaluations:
                    evaluation_counts[evaluation.verdict] += 1
                spent, emitted, overflow = self.reconcile(entity, evaluations, now, cfg, 100 - transitions,
                    notice_budget=TICK_NOTICE_LIMIT - len(notices), deadline_id=deadline['deadline_id'])
                transitions += spent
                notices.extend(emitted)
                omitted += overflow
                self.capacity(cfg, 1 if not previous else 0)
                self.put(memory_key, dict(forecast_at=prediction or deadline['forecast_at'], revision=deadline['revision'],
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
                     evaluations=evaluation_counts, unknown_policy='retain_episode_no_reminder',
                     missing_signals='insufficient_evidence', source_consistency='two snapshots; subsequent changes observed next tick'))
            self.put('next_due', stamp(now + timedelta(seconds=cfg['watchdog_risk_seconds'] if high else cfg['watchdog_normal_seconds'])))
            # Final cursor and health keys also count, including the first deadline cursor.
            self.capacity(cfg, 0)
            return dict(notices=notices, omitted=omitted, evaluated=True, transitions=transitions, scan_complete=complete)

    @staticmethod
    def admit_claim(records, gap, data, proof, cutoff):
        """Apply both timestamp fields as one observation, then select admitted coverage."""
        prior = {field: records[field] for field in CLAIM_SOURCES if field in records}
        drops = [record for field, record in prior.items() if record['value'] is not None
                 and (data.get(field, record['value']) is None or instant(record['at']) < cutoff)]
        if drops:
            # An alternate cached at the time of loss's source sample is not a
            # replacement. An independently newer delivery can already qualify.
            boundary = max(record['revision'] for record in drops)
            gap = max(filter(None, (gap, *(r['sample_at'] for r in prior.values()
                           if r['revision'] <= boundary))), key=instant, default=None)
            for field, record in prior.items():
                if record in drops or (record['value'] is not None and gap
                                       and instant(record['value']) <= instant(gap)):
                    records[field] = dict(record, value=None)
        expired = []
        for field in CLAIM_SOURCES:
            value = data.get(field)
            if value is None:
                continue
            sample = stamp(instant(value))
            old = prior.get(field, {}).get('sample_at')
            if any(bound and instant(sample) <= instant(bound) for bound in (old, gap)):
                continue
            known = instant(proof['at']) >= cutoff
            records[field] = dict(value=sample if known else None, sample_at=sample, **proof)
            if not known:
                expired.append(sample)
        if expired:
            gap = max(filter(None, (gap, *expired)), key=instant)
        selected = next((field for field in CLAIM_SOURCES if records.get(field, {}).get('value') is not None), None)
        return gap, selected

    def capture_evidence(self, key, cfg) -> dict[str, Any]:
        name = 'evidence:' + _json(list(key))
        saved = self.meta(name, {})
        records = dict(saved.get('records', {}))
        failures, recovery = saved.get('failures', []), dict(saved.get('recovery', {}))
        sequence = saved.get('sequence', 0)
        cutoff = self.clock() - timedelta(days=cfg['watchdog_retention_days'])
        gap = saved.get('claim_gap')
        if 'claim_gap' not in saved:
            # Old per-field metadata did not prove atomic family admission.
            for field in CLAIM_SOURCES:
                prior = records.get(field)
                if prior is not None:
                    sample = prior.get('sample_at', prior.get('latest_value', prior['value']))
                    gap = max(filter(None, (gap, sample)), key=instant, default=None)
                    records[field] = dict(prior, sample_at=sample, value=None)
                    records[field].pop('gap_sequence', None)
        for row in self.db.execute('SELECT o.*,a.sequence FROM observations o JOIN monitor_audit a ON a.request_id=o.event_id WHERE o.board_id=? AND o.task_id=? AND a.sequence>? ORDER BY a.sequence', (*key, sequence)):
            sequence = row['sequence']
            payload = json.loads(row['payload_json'])
            if payload['out_of_order']:
                continue
            data, kind = payload['data'], row['kind']
            proof = dict(at=row['occurred_at'], revision=row['sequence'], task_revision=payload.get('source_revision'))
            if kind == 'task_signal':
                gap, _ = self.admit_claim(records, gap, data, proof, cutoff)
                for field, value in data.items():
                    if field not in CLAIM_SOURCES:
                        records[field] = dict(value=value, **proof)
                if 'tool_failures' in data:
                    failures = [proof] * min(3, data['tool_failures'])
                    records['tools'] = dict(value=data['tool_failures'], reset=data['tool_failures'] < 3, **proof)
            elif kind == 'worker_outcome':
                records['agent_available'] = dict(value=data['state'] == 'running', **proof)
            elif kind == 'tool_outcome':
                if data['outcome'] == 'success':
                    failures = []
                    records['tools'] = dict(value=0, reset=True, **proof)
                elif data['outcome'] == 'failure':
                    failures = (failures + [proof])[-3:]
                    records['tools'] = dict(value=len(failures), reset=False, **proof)
            elif kind == 'recovery_outcome':
                identity = data['incident_id']
                count = 0 if data['outcome'] == 'improved' else recovery.get(identity, {}).get('value', 0) + 1
                recovery[identity] = dict(value=count, **proof)
        gap, selected = self.admit_claim(records, gap, {}, None, cutoff)
        result = dict(records=records, failures=failures, recovery=recovery, sequence=sequence,
                      claim_gap=gap, claim_source=selected)
        if result != saved:
            self.capacity(cfg, 0 if saved else 1)
            self.put(name, result)
        return result

    def detect(self, task, signal, previous, first, now, cfg, cycles, edges, existing, graph_complete):
        key = task['board_id'], task['task_id']
        if task['revision'] < previous.get('revision', 0):
            return []
        evidence = self.capture_evidence(key, cfg)
        records, failures, recovery = evidence['records'], evidence['failures'], evidence['recovery']
        cutoff = now - timedelta(days=cfg['watchdog_retention_days'])
        result = []
        def emit(detector, source, verdict, facts, proof=None):
            proof = proof or dict(at=stamp(now), revision=task['revision'])
            result.append(Evaluation(detector, source, verdict, proof['at'], proof['revision'], facts))
        def field(detector, name, active, clear, facts):
            record = records.get(name)
            value = record['value'] if record else None
            known = record and instant(record['at']) >= cutoff
            verdict = 'active' if known and active(value) else 'clear' if known and clear(value) else 'unknown'
            emit(detector, name, verdict, dict(value=value, **facts(value, record)), record)
        progress = task['last_verified_progress_at']
        changed = progress and (not previous.get('progress') or instant(progress) > instant(previous['progress']))
        since = progress or first
        running = task['status'] == 'running'
        stalled = running and (now - instant(since)).total_seconds() >= cfg['watchdog_stall_seconds']
        lost = previous.get('progress') and (not progress or instant(progress) < instant(previous['progress']))
        verdict = 'unknown' if lost else 'active' if stalled else 'clear' if changed or task['status'] in ('planned', 'blocked') else 'unknown'
        emit('stalled', 'task_progress', verdict, dict(since=since, basis='verified_progress' if progress else 'first_observed_running'))
        field('missed_checkpoint', 'checkpoint_at', lambda v: v is not None and instant(v) < now,
              lambda v: v is not None and instant(v) > now, lambda v, r: dict(checkpoint_at=v))
        emit('dependency_cycle', 'complete_known_graph', 'active' if key in cycles else 'clear' if graph_complete else 'unknown',
             dict(component=cycles.get(key)), dict(at=stamp(now), revision=self.meta('generation', 0)))
        field('delegated_work_unavailable', 'agent_available', lambda v: v is False, lambda v: v is True,
              lambda v, r: dict(source='explicit_unavailable', observed_at=r['at'] if r else None,
                               freshness='retained_source' if r else 'unknown_source_pruned'))
        claim = records.get(evidence['claim_source'])
        scope_proof = claim or max((records[field] for field in CLAIM_SOURCES if field in records),
                                   key=lambda record: record['revision'], default=None)
        scope_ended = (task['status'] in ('planned', 'blocked') and scope_proof and
                       scope_proof.get('task_revision') is not None and task['revision'] > scope_proof['task_revision'])
        value = claim['value'] if claim and not scope_ended else None
        stale = value is not None and (now - instant(value)).total_seconds() >= cfg['watchdog_claim_seconds']
        emit('stale_claim', 'claim', 'active' if stale else 'clear' if value is not None else 'unknown',
             dict(since=value, action='inspect_only'), claim)
        if scope_ended:
            emit('stale_claim', 'scope_ended', 'clear', dict(status=task['status'], task_revision=task['revision']))
        if claim and (scope_ended or task['status'] == 'verified'):
            gap, selected = self.admit_claim(records, evidence['claim_gap'],
                                             dict.fromkeys(CLAIM_SOURCES), None, cutoff)
            self.put('evidence:' + _json(list(key)), dict(evidence, claim_gap=gap, claim_source=selected))
        field('dependency_unreachable', 'dependency_state', lambda v: v == 'unreachable', lambda v: v == 'available',
              lambda v, r: dict(source='explicit_signal', observed_at=r['at'] if r else None,
                               freshness='retained_source' if r else 'unknown_source_pruned'))
        missing = [list(r[2:]) for r in edges if tuple(r[:2]) == key and tuple(r[2:]) not in existing] if graph_complete else []
        emit('dependency_unreachable', 'complete_known_graph', 'active' if missing else 'clear' if graph_complete else 'unknown',
             dict(missing=missing[:20], source='complete_known_graph'), dict(at=stamp(now), revision=self.meta('generation', 0)))
        count = sum(instant(r['at']) >= now - timedelta(seconds=900) for r in failures)
        tool = records.get('tools')
        emit('repeated_tool_failure', 'tools', 'active' if count >= 3 else 'clear' if tool and tool['reset'] and instant(tool['at']) >= cutoff else 'unknown',
             dict(failures=count, window_seconds=900), tool)
        for identity, record in recovery.items():
            row = self.db.execute('SELECT state FROM incidents WHERE incident_id=?', (identity,)).fetchone()
            verdict = 'clear' if row and row['state'] == 'resolved' else 'unknown' if instant(record['at']) < cutoff else 'clear' if record['value'] == 0 else 'active' if row and record['value'] >= 3 else 'unknown'
            emit('ineffective_recovery', identity, verdict, dict(incident_ids=[identity]), record)
        if task['status'] == 'verified':
            # A verified terminal task closes only its task-scoped incidents.
            detectors = {e.detector for e in result}
            detectors.update(r[0] for r in self.db.execute('SELECT detector FROM incidents WHERE board_id=? AND task_id=?', key))
            result.extend(Evaluation(d, 'verified_completion', 'clear', stamp(now), task['revision'],
                                     dict(actual_verified_at=task['actual_verified_at'])) for d in detectors)
        return result

    def reconcile(self, task, evaluations, now, cfg, budget, *, notice_budget, deadline_id=None):
        entity = ['deadline', deadline_id] if deadline_id else ['task', task['board_id'], task['task_id']]
        prefix = digest(entity)
        rows = [dict(r) for r in self.db.execute('SELECT * FROM incidents WHERE fingerprint LIKE ? ORDER BY episode', (prefix + ':%',))]
        latest = {r['detector']: r for r in rows}
        grouped = {}
        for evaluation in evaluations:
            grouped.setdefault(evaluation.detector, {})[evaluation.source] = asdict(evaluation)
        transitions, notices, omitted = 0, [], 0
        for detector in sorted(set(latest) | set(grouped)):
            if transitions >= budget:
                break
            old = latest.get(detector)
            prior = json.loads(old['explanation_json']) if old else {}
            sources = dict(prior.get('sources', {}))
            current = grouped.get(detector, {})
            # Legacy source names can be matched, but missing provenance stays unknown
            # until a new positive detection establishes that source's watermark.
            if old and old['state'] == 'active' and not sources:
                names = prior.get('pending_sources') or [prior.get('facts', {}).get('source', detector)]
                for name in names:
                    matches = [key for key, value in current.items() if value['facts'].get('source') == name]
                    source = matches[0] if len(matches) == 1 else name
                    sources[source] = dict(verdict='unknown', at=None, revision=None)
            active = []
            for source, observation in current.items():
                before = sources.get(source)
                if observation['verdict'] == 'unknown':
                    continue
                if before and before.get('at') is not None:
                    order = (instant(observation['at']), observation['revision'])
                    watermark = (instant(before['at']), before['revision'])
                    if detector == 'stale_claim' and source == 'claim':
                        # Alternate claim coverage and new envelopes cannot roll the
                        # resolved predicate back to an older intrinsic sample.
                        order = (instant(observation['facts']['since']),)
                        previous_sample = before.get('facts', {}).get('since')
                        if previous_sample is None:
                            if observation['verdict'] == 'clear':
                                continue
                            previous_sample = observation['facts']['since']
                        watermark = (instant(previous_sample),)
                    if order < watermark or before.get('require_new') and order == watermark:
                        continue
                elif before and observation['verdict'] == 'clear':
                    continue
                if observation['verdict'] == 'active':
                    sources[source] = observation
                    active.append(observation)
                elif before:
                    sources[source] = observation
            terminal = current.get('verified_completion') or current.get('scope_ended')
            if terminal and terminal['verdict'] == 'clear':
                sources = {source: {**value, 'verdict': 'clear', 'at': terminal['at'],
                                    'revision': current.get(source, value)['revision'],
                                    'require_new': True, 'recovery': terminal}
                           for source, value in sources.items()}
                active = []
            pending = {source: value for source, value in sources.items() if value['verdict'] != 'clear'}
            if not active:
                if old and old['state'] == 'active':
                    if sources and not pending:
                        prior['sources'] = sources
                        prior['pending_sources'] = []
                        self.db.execute("UPDATE incidents SET state='resolved',last_seen=?,explanation_json=? WHERE incident_id=?",
                                        (stamp(now), _json(prior), old['incident_id']))
                        self.db.execute("UPDATE plans SET state='resolved' WHERE incident_id=?", (old['incident_id'],))
                        transitions += 1
                    elif sources != prior.get('sources', sources):
                        prior['sources'] = sources
                        prior['pending_sources'] = sorted(pending)
                        self.db.execute('UPDATE incidents SET explanation_json=? WHERE incident_id=?',
                                        (_json(prior), old['incident_id']))
                continue
            detail = active[-1]['facts']
            explanation = dict(facts=detail, sources=sources, pending_sources=sorted(pending),
                               source_revision=task['revision'], deadline_id=deadline_id,
                               confidence='observed_rule_match', unknowns=['semantic quality', 'external execution state'])
            new = not old or old['state'] == 'resolved'
            if new:
                self.capacity(cfg, 2)
                fingerprint = prefix + ':' + detector
                episode = 1 + max(old['episode'] if old else 0, self.meta('episode:' + fingerprint, 0))
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
            count = old['notice_count']
            next_at = old['next_notice_at']
            if count < cfg['watchdog_notice_limit'] and (due or rising):
                if len(notices) < notice_budget:
                    count += 1
                    next_at = stamp(now + timedelta(seconds=min(3600, 300 * 2 ** (count - 1))))
                    notices.append(dict(incident_id=old['incident_id'], detector=detector, emission='unknown_after_commit',
                                        next_action='Inspect watchdog plans; preserve verification and permissions.'))
                else:
                    omitted += 1
                    if not due:
                        next_at = stamp(now)  # Preserve eligibility after recording the higher severity.
            self.db.execute('UPDATE incidents SET last_seen=?,notice_count=?,next_notice_at=?,explanation_json=? WHERE incident_id=?',
                            (stamp(now), count, next_at, _json(explanation), old['incident_id']))
        return transitions, notices, omitted

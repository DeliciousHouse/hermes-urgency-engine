"""Typed requests for explicit local operator interventions."""
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
from typing import NoReturn

from .ledger import LedgerError, _date, _id, _integer, _invalid, _json, _number, _text

PARAMETERS = {
    'set_priority': {'priority'},
    'reestimate': {'remaining_seconds'},
    'replan_soft_deadline': {'deadline_id', 'expected_deadline_revision', 'at', 'timezone'},
    'create_recovery_task': {'title', 'body', 'assignee', 'priority', 'parents'},
}
IDS = ('request_id', 'incident_id', 'plan_id', 'project_id', 'board_id', 'task_id')
WORKER_MARKERS = ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB', 'HERMES_DELEGATED_CHILD_CONTEXT')


def validate(payload, redact):
    if set(payload) != set(IDS) | {'schema_version', 'expected_revision', 'kind', 'reason', 'parameters'}:
        _invalid('arguments')
    _integer(payload['schema_version'], 'schema_version', 1, 1)
    _integer(payload['expected_revision'], 'expected_revision')
    for field in IDS:
        _id(payload[field], field)
    _text(payload['reason'], 'reason')
    kind, params = payload['kind'], payload['parameters']
    if not isinstance(kind, str) or kind not in PARAMETERS:
        _invalid('kind')
    if not isinstance(params, dict) or set(params) != PARAMETERS[kind]:
        _invalid('parameters')
    strings = [payload[field] for field in (*IDS, 'reason')]
    if 'priority' in params:
        _integer(params['priority'], 'priority', -(2**63 - 1), 2**63 - 1)
    if kind == 'reestimate':
        _number(params['remaining_seconds'], 'remaining_seconds')
    if kind == 'replan_soft_deadline':
        _id(params['deadline_id'], 'deadline_id')
        _integer(params['expected_deadline_revision'], 'expected_deadline_revision')
        _date(params['at'], params['timezone'])
        strings.extend(params[key] for key in ('deadline_id', 'at', 'timezone'))
    if kind == 'create_recovery_task':
        _text(params['title'], 'title')
        _text(params['body'], 'body')
        _id(params['assignee'], 'assignee')
        parents = params['parents']
        if not isinstance(parents, list) or len(parents) > 10000:
            _invalid('parents')
        for parent in parents:
            _id(parent, 'parents')
        if len(set(parents)) != len(parents):
            _invalid('parents')
        strings.extend([params['title'], params['body'], params['assignee'], *parents])
    for value in strings:
        if redact(value) != value:
            raise LedgerError('REDACTION_REFUSED', 'interventions_apply', 'arguments',
                              next_action='Remove sensitive values before submitting an intervention.')
    return payload


def refuse_recovery():
    raise LedgerError('CAPABILITY_UNAVAILABLE', 'interventions_apply', 'kind',
                      next_action='Recovery creation is unavailable: the native interface cannot '
                                  'verify archived request keys or atomically compare and create. '
                                  'Inspect the plan and handle recovery explicitly through Hermes.')


SCHEMA = (
    'CREATE TABLE actions(action_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, '
    'project_id TEXT NOT NULL, board_id TEXT NOT NULL, task_id TEXT NOT NULL, '
    'incident_id TEXT NOT NULL, plan_id TEXT NOT NULL, kind TEXT NOT NULL, '
    'payload_digest TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL, '
    'attempt_count INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT NOT NULL)',
    'CREATE TABLE events(sequence INTEGER PRIMARY KEY, action_id TEXT NOT NULL, time TEXT NOT NULL, '
    'state TEXT NOT NULL, detail_json TEXT NOT NULL)',
    'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'CREATE INDEX project_action ON actions(project_id, action_id)',
    'CREATE INDEX action_state ON actions(state)',
)


def fail(code, field=None, **kwargs) -> NoReturn:
    raise LedgerError(code, 'interventions', field, **kwargs)


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class Actions:
    def __init__(self, db, clock, readonly=False):
        self.db, self.clock = db, clock
        self.db.row_factory = sqlite3.Row
        try:
            with self.transaction(not readonly):
                version = db.execute('PRAGMA user_version').fetchone()[0]
                objects = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")]
                if version == 0 and not objects and not readonly:
                    for sql in SCHEMA:
                        db.execute(sql)
                    db.execute('PRAGMA user_version=1')
                elif version != 1:
                    fail('UNSUPPORTED_SCHEMA')
                elif sorted(objects) != sorted(SCHEMA) or db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                    fail('CORRUPT_STATE')
        except BaseException:
            db.close()
            raise

    @classmethod
    def host(cls, clock):
        from plugins.plugin_storage import plugin_db
        return cls(plugin_db('hermes-urgency-engine', 'interventions.db'), clock)

    @classmethod
    def read(cls, path, clock):
        return cls(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=.1), clock, True) if path.exists() else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()

    @contextmanager
    def transaction(self, write=True):
        try:
            self.db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield
            self.db.commit()
        except BaseException as exc:
            self.db.rollback()
            if isinstance(exc, sqlite3.Error):
                code = getattr(exc, 'sqlite_errorcode', 0) & 255
                fail({sqlite3.SQLITE_BUSY: 'STORE_BUSY', sqlite3.SQLITE_LOCKED: 'STORE_BUSY',
                      sqlite3.SQLITE_NOTADB: 'CORRUPT_STATE', sqlite3.SQLITE_CORRUPT: 'CORRUPT_STATE'}.get(code, 'STORAGE_FAILURE'))
            raise

    @staticmethod
    def display(row):
        payload = json.loads(row['payload_json'])
        data = {key: row[key] for key in ('action_id', 'request_id', 'kind', 'state', 'attempt_count',
                                          'project_id', 'board_id', 'task_id', 'incident_id', 'plan_id')}
        data.update(requested_effect=payload['parameters'], observed_effect=None,
                    attribution='unknown', last_error=None, next_action=None)
        data.update({k: v for k, v in json.loads(row['result_json']).items() if k != 'receipt'})
        if data['state'] == 'in_flight':
            data.update(next_action=f"Run hermes urgency interventions reconcile {data['action_id']}.")
        return data

    def get(self, action_id):
        _id(action_id, 'action_id')
        row = self.db.execute('SELECT * FROM actions WHERE action_id=?', (action_id,)).fetchone()
        if row is None:
            fail('NOT_FOUND', 'action_id')
        return row

    def page(self, project, limit, after):
        rows = self.db.execute('SELECT * FROM actions WHERE project_id=? AND action_id>? '
                               'ORDER BY action_id LIMIT ?', (project, after or '', limit + 1)).fetchall()
        return dict(items=[self.display(row) for row in rows[:limit]], has_more=len(rows) > limit,
                    next=rows[limit - 1]['action_id'] if len(rows) > limit else None)

    def paused(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()
        return bool(row and json.loads(row[0]))

    def event(self, action_id, now, state, detail):
        self.db.execute('INSERT INTO events(action_id,time,state,detail_json) VALUES(?,?,?,?)',
                        (action_id, now, state, _json(detail)))

    def reserve(self, payload):
        action_id = digest([payload[key] for key in ('board_id', 'project_id', 'request_id')])
        payload_digest = digest(payload)
        with self.transaction():
            if self.db.execute('SELECT 1 FROM meta WHERE key=?', ('control:' + payload['request_id'],)).fetchone():
                fail('IDEMPOTENCY_CONFLICT', 'request_id')
            row = self.db.execute('SELECT * FROM actions WHERE request_id=?', (payload['request_id'],)).fetchone()
            if row is not None:
                if row['payload_digest'] != payload_digest:
                    fail('IDEMPOTENCY_CONFLICT', 'request_id')
                return row
            if self.paused():
                fail('PAUSED', next_action='Inspect current interventions or explicitly resume them.')
            if (self.db.execute('SELECT count(*) FROM actions').fetchone()[0] >= 10000
                    or self.db.execute('SELECT count(*) FROM events').fetchone()[0] >= 50000):
                fail('STORAGE_LIMIT', next_action='Preserve the intervention history; no new action was admitted.')
            now = self.clock().isoformat()
            self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                action_id, payload['request_id'], *(payload[k] for k in ('project_id', 'board_id', 'task_id',
                  'incident_id', 'plan_id', 'kind')), payload_digest, _json(payload), 'reserved', 0, now, now, '{}'))
            self.event(action_id, now, 'reserved', {})
            return self.get(action_id)

    def control(self, operation, request_id, reason):
        payload_digest = digest([operation, reason])
        key = 'control:' + request_id
        with self.transaction():
            prior = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            if prior:
                saved = json.loads(prior[0])
                if saved['digest'] != payload_digest:
                    fail('IDEMPOTENCY_CONFLICT', 'request_id')
                return saved['result']
            if self.db.execute('SELECT 1 FROM actions WHERE request_id=?', (request_id,)).fetchone():
                fail('IDEMPOTENCY_CONFLICT', 'request_id')
            if self.db.execute('SELECT count(*) FROM events').fetchone()[0] >= 50000:
                fail('STORAGE_LIMIT')
            data = {'paused': operation == 'pause'}
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('paused',?)", (_json(data['paused']),))
            self.db.execute('INSERT INTO meta VALUES(?,?)', (key, _json(dict(digest=payload_digest, result=data))))
            self.event(request_id, self.clock().isoformat(), operation, {'reason': reason})
            return data

    def transition(self, action_id, expected, state, detail, *, snapshot=None):
        with self.transaction():
            before = self.get(action_id)
            if before['state'] != expected or snapshot is not None and before['result_json'] != snapshot:
                return before, False
            if expected == 'reserved' and state == 'in_flight' and self.paused():
                fail('PAUSED', next_action='Inspect current interventions or explicitly resume them.')
            if self.db.execute('SELECT count(*) FROM events').fetchone()[0] >= 50000:
                fail('STORAGE_LIMIT')
            now = self.clock().isoformat()
            self.db.execute('UPDATE actions SET state=?,attempt_count=attempt_count+?,updated_at=?,result_json=? '
                            'WHERE action_id=?', (state, int(state == 'in_flight' and expected == 'reserved'),
                                                  now, _json(detail), action_id))
            self.event(action_id, now, state, detail)
            return self.get(action_id), True


def operator_only():
    if any(os.environ.get(key) for key in WORKER_MARKERS):
        fail('SOURCE_DENIED', 'context', next_action='Use the explicit local operator command, not a worker context.')


def host_stop():
    # This host predicate owns profile/fleet sentinel semantics. Older hosts may lack it.
    try:
        from agent.estop import is_engaged
    except ModuleNotFoundError as exc:
        if exc.name in ('agent', 'agent.estop'):
            return
        raise
    if is_engaged():
        fail('PAUSED', 'host', next_action='Hermes is paused; inspect the host stop before applying.')


def task_effect(task):
    return {'remaining_seconds': task['remaining_seconds'], 'revision': task['revision']}


def deadline_effect(deadline):
    return {key: deadline[key] for key in ('deadline_id', 'target_at', 'timezone', 'revision')}


def execute(store, payload, preflight, effect, observe):
    row = store.reserve(payload)
    action_id = row['action_id']
    if row['state'] != 'reserved':
        return store.display(row)
    try:
        prepared = preflight(payload)
    except LedgerError as exc:
        state = 'conflict' if exc.code == 'REVISION_CONFLICT' else 'refused'
        row, _ = store.transition(action_id, 'reserved', state,
                                  dict(last_error=exc.code, next_action=exc.next_action))
        return store.display(row)
    row, claimed = store.transition(action_id, 'reserved', 'in_flight', {})
    if not claimed:
        return store.display(row)
    receipt = None
    problem = None
    try:
        receipt = effect(payload, action_id, prepared)
    except LedgerError as exc:
        if exc.side_effects == 'none' and exc.code in ('REVISION_CONFLICT', 'IDEMPOTENCY_CONFLICT', 'SOURCE_DENIED', 'PAUSED'):
            state = 'refused' if exc.code in ('SOURCE_DENIED', 'PAUSED') else 'conflict'
            row, _ = store.transition(action_id, 'in_flight', state,
                                      dict(last_error=exc.code, next_action=exc.next_action))
            return store.display(row)
        problem = exc.code
    # Store the receipt separately from the final outcome so restart can read it.
    try:
        if receipt is not None:
            row, stored = store.transition(action_id, 'in_flight', 'in_flight', dict(receipt=receipt))
            # A concurrent read may have observed the value while this executor still owned the write.
            if not stored and row['state'] == 'observed':
                store.transition(action_id, 'observed', 'in_flight', dict(receipt=receipt))
        detail, state = attribute(observe, payload, receipt, action_id, problem)
        row, _ = store.transition(action_id, 'in_flight', state, detail)
    except LedgerError:
        fail('STORAGE_FAILURE', side_effects='unknown')
    return store.display(row)


def attribute(observe, payload, receipt, action_id, problem=None):
    try:
        observed, matches = observe(payload, receipt)
    except LedgerError as exc:
        observed, matches = None, False
        problem = problem or exc.code
    detail = dict(receipt=receipt, observed_effect=observed, attribution='unknown', last_error=problem)
    if matches:
        detail['attribution'] = 'receipt' if receipt is not None else 'observed_only'
        if receipt is None:
            detail['next_action'] = 'Desired value observed without an attributable receipt. Do not resubmit this write.'
        return detail, 'applied' if receipt is not None else 'observed'
    detail.update(last_error=problem or 'OUTCOME_UNKNOWN',
                  next_action=f'Run hermes urgency interventions reconcile {action_id}. Do not resubmit the write.')
    return detail, 'unknown'


def reconcile(store, action_id, observe):
    with store.transaction(False):
        row = store.get(action_id)
    if row['state'] not in ('in_flight', 'unknown'):
        return store.display(row)
    payload = json.loads(row['payload_json'])
    if digest(payload) != row['payload_digest']:
        fail('CORRUPT_STATE')
    result = json.loads(row['result_json'])
    detail, state = attribute(observe, payload, result.get('receipt'), action_id, result.get('last_error'))
    # A negative read cannot settle an executor that may still be in flight.
    if state != 'unknown' or row['state'] == 'unknown' and detail != result:
        row, _ = store.transition(action_id, row['state'], state, detail, snapshot=row['result_json'])
    return store.display(row)

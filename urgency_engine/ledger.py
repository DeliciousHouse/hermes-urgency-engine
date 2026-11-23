"""Transactional local commitments and byte-identity evidence. No host authorization."""

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial, wraps
import hashlib
import inspect
import json
import math
import os
import stat
from typing import Any, NoReturn
from pathlib import Path
import sqlite3


class LedgerError(Exception):
    """Safe structured failure; never includes SQL, artifact paths or input values."""

    def __init__(self, code, operation, field=None, *, side_effects='none', next_action=None):
        self.code, self.operation, self.field = code, operation, field
        self.retryable = code == 'STORE_BUSY' and side_effects == 'none'
        self.side_effects = side_effects
        self.next_action = next_action or {
            'STORE_BUSY': 'Retry the identical request after the other writer finishes.',
            'REVISION_CONFLICT': 'Reload current state and reconcile the intended change.',
            'IDEMPOTENCY_CONFLICT': 'Use the original request payload or a new request ID.',
            'UNSUPPORTED_SCHEMA': 'Preserve the database; use a compatible reader.',
            'CORRUPT_STATE': 'Stop writers, preserve the database and restore a verified backup.',
            'STORAGE_FAILURE': 'Preserve state and reconcile the request ID before retrying.',
        }.get(code, 'Correct the named field or missing record and submit a new request.')
        if side_effects == 'unknown':
            self.next_action = 'Preserve state and reconcile the original request ID before retrying.'
        super().__init__(f'{operation}: {code}' + (f' ({field})' if field else ''))


def _invalid(field) -> NoReturn:
    raise LedgerError('INVALID_INPUT', 'validate', field)


def _text(value, field, maximum=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        _invalid(field)
    return value


def _id(value, field):
    _text(value, field, 128)
    if any(c in value for c in '/\\') or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _invalid(field)
    return value


def _integer(value, field, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid(field)
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _at_or_before(value, boundary):
    try:
        a = datetime.fromisoformat(value.replace('Z', '+00:00'))
        b = datetime.fromisoformat(boundary.replace('Z', '+00:00'))
        return int(a.utcoffset() is not None and b.utcoffset() is not None and a <= b)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0


def _command(method, *, prepare=False) -> Any:
    signature = inspect.signature(method)

    @wraps(method)
    def run(self, *args, **kwargs):
        operation = method.__name__
        try:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            payload = dict(bound.arguments)
            del payload['self']
            _id(payload['request_id'], 'request_id')
            _text(payload['actor'], 'actor')
            if 'reason' in payload:
                _text(payload['reason'], 'reason')
            if 'expected_revision' in payload:
                _integer(payload['expected_revision'], 'expected_revision')
            try:
                encoded = _json({'operation': operation, 'arguments': payload})
            except (ValueError, TypeError, OverflowError, RecursionError):
                _invalid('payload')
            if len(encoded.encode('utf-8')) > 256 * 1024:
                _invalid('payload')
            digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            prepared = None
            if prepare:
                with self._transaction(operation):
                    replay = self._replay(payload['request_id'], digest)
                    if replay is not None:
                        return replay
                try:
                    prepared = method(self, *args, **kwargs)
                except LedgerError as exc:
                    if exc.code not in ('REVISION_CONFLICT', 'NOT_FOUND', 'INVALID_INPUT', 'EVIDENCE_MISMATCH'):
                        raise
                    # A competing request may have committed after the first lookup,
                    # before preparation could reach the replay check under the write lock.
                    with self._transaction(operation):
                        replay = self._replay(payload['request_id'], digest)
                        if replay is not None:
                            return replay
                    raise
            with self._transaction(operation, write=True):
                replay = self._replay(payload['request_id'], digest)
                if replay is not None:
                    return replay
                now = self._now()
                result, entity_type, entity_id, before = prepared() if prepared is not None else method(self, *args, **kwargs)
                self.connection.execute(
                    'INSERT INTO audit(request_id,entity_type,entity_id,action,actor,reason,occurred_at,before_json,after_json) VALUES(?,?,?,?,?,?,?,?,?)',
                    (payload['request_id'], entity_type, entity_id, operation, payload['actor'],
                     payload.get('reason', ''), now, _json(before), _json(result)))
                self.connection.execute('INSERT INTO operations VALUES(?,?,?)',
                                        (payload['request_id'], digest, _json(result)))
                return result
        except LedgerError as exc:
            exc.operation = operation
            exc.args = (f'{operation}: {exc.code}' + (f' ({exc.field})' if exc.field else ''),)
            raise
    return run


_SCHEMA = (
    'CREATE TABLE projects(project_id TEXT PRIMARY KEY, title TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1), created_at TEXT NOT NULL)',
    'CREATE TABLE operations(request_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL, response_json TEXT NOT NULL)',
    'CREATE TABLE audit(sequence INTEGER PRIMARY KEY, request_id TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL)',
    'CREATE INDEX audit_entity ON audit(entity_type,entity_id,sequence)',
    "CREATE TABLE tasks(board_id TEXT NOT NULL, task_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects, assignee TEXT, goal TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('planned','running','claimed_complete','verified','blocked')), remaining_seconds REAL NOT NULL CHECK(remaining_seconds>=0), verification_seconds REAL NOT NULL CHECK(verification_seconds>=0), deployment_seconds REAL NOT NULL CHECK(deployment_seconds>=0), actual_seconds REAL NOT NULL CHECK(actual_seconds>=0), actual_verified_at TEXT, last_verified_progress_at TEXT, revision INTEGER NOT NULL CHECK(revision>=1), PRIMARY KEY(board_id,task_id))",
    'CREATE INDEX tasks_project ON tasks(project_id,board_id,task_id)',
    'CREATE TABLE criteria(board_id TEXT NOT NULL, task_id TEXT NOT NULL, criterion_id TEXT NOT NULL, criterion_revision INTEGER NOT NULL CHECK(criterion_revision>=1), description TEXT NOT NULL, weight REAL NOT NULL CHECK(weight>0), required INTEGER NOT NULL CHECK(required IN (0,1)), active INTEGER NOT NULL CHECK(active IN (0,1)), expected_sha256 TEXT, PRIMARY KEY(board_id,task_id,criterion_id,criterion_revision), FOREIGN KEY(board_id,task_id) REFERENCES tasks)',
    'CREATE UNIQUE INDEX criteria_active ON criteria(board_id,task_id,criterion_id) WHERE active=1',
    "CREATE TABLE snapshots(snapshot_id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects, kind TEXT NOT NULL CHECK(kind IN ('baseline','current','forecast')), recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL)",
    "CREATE UNIQUE INDEX snapshot_baseline ON snapshots(project_id) WHERE kind='baseline'",
    'CREATE INDEX snapshot_project ON snapshots(project_id,kind,snapshot_id)',
    'CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY, board_id TEXT NOT NULL, task_id TEXT NOT NULL, criterion_id TEXT NOT NULL, criterion_revision INTEGER NOT NULL, artifact_relative_path TEXT NOT NULL, observed_sha256 TEXT NOT NULL, checked_at TEXT NOT NULL, invalidated_at TEXT, FOREIGN KEY(board_id,task_id,criterion_id,criterion_revision) REFERENCES criteria)',
    'CREATE UNIQUE INDEX evidence_active ON evidence(board_id,task_id,criterion_id,criterion_revision) WHERE invalidated_at IS NULL',
    "CREATE TABLE deadlines(deadline_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects, board_id TEXT, task_id TEXT, milestone_id TEXT REFERENCES milestones, kind TEXT NOT NULL CHECK(kind IN ('hard','soft')), original_at TEXT NOT NULL, target_at TEXT NOT NULL, timezone TEXT NOT NULL, owner TEXT NOT NULL, source TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1), CHECK((board_id IS NULL)=(task_id IS NULL)), CHECK(milestone_id IS NULL OR board_id IS NULL), FOREIGN KEY(board_id,task_id) REFERENCES tasks)",
    'CREATE INDEX deadlines_project ON deadlines(project_id)',
    "CREATE INDEX snapshot_deadline ON snapshots(project_id,json_extract(payload_json,'$.deadline_id'),snapshot_id) WHERE kind='forecast'",
    'CREATE TABLE milestones(milestone_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects, title TEXT NOT NULL)',
    'CREATE TABLE milestone_tasks(milestone_id TEXT NOT NULL REFERENCES milestones, board_id TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(milestone_id,board_id,task_id), FOREIGN KEY(board_id,task_id) REFERENCES tasks)',
)


_REMAINING_COLUMN = 'remaining_known INTEGER NOT NULL DEFAULT 1 CHECK(remaining_known IN (0,1))'
_SCHEMA_V2 = tuple(sql.replace('PRIMARY KEY(board_id,task_id)', _REMAINING_COLUMN + ', PRIMARY KEY(board_id,task_id)') if sql.startswith('CREATE TABLE tasks(') else sql for sql in _SCHEMA)
_OUTCOME_SCHEMA = (
    "CREATE TABLE outcomes(sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects, board_id TEXT NOT NULL, task_id TEXT NOT NULL, evidence_id TEXT NOT NULL REFERENCES evidence, verified_at TEXT NOT NULL, snapshot_id INTEGER REFERENCES snapshots, evaluated_at TEXT, recorded_at TEXT, input_revisions TEXT, target_at TEXT, forecast_at TEXT, completion_error_seconds REAL, on_time INTEGER, forecast_reason TEXT, duration_seconds REAL, duration_reason TEXT NOT NULL)",
    'CREATE INDEX outcomes_project ON outcomes(project_id,sequence)',
    'CREATE UNIQUE INDEX outcome_identity ON outcomes(project_id,board_id,task_id,evidence_id)',
    "CREATE INDEX snapshot_task_forecast ON snapshots(project_id,json_extract(payload_json,'$.board_id'),json_extract(payload_json,'$.task_id'),snapshot_id) WHERE kind='forecast'",
)
_SCHEMA_V3 = _SCHEMA_V2 + _OUTCOME_SCHEMA


_HAS_EVIDENCE = '''EXISTS(SELECT 1 FROM evidence e WHERE e.board_id=c.board_id AND e.task_id=c.task_id AND e.criterion_id=c.criterion_id AND e.criterion_revision=c.criterion_revision AND e.invalidated_at IS NULL AND e.observed_sha256=c.expected_sha256)'''


class Ledger:
    """Own a dedicated connection until context exit; do not share it across threads."""

    def __init__(self, connection, *, clock, artifact_root, busy_timeout_ms=5000, read_only=False,
                 check_integrity=False, outcome_retention=1000):
        _integer(busy_timeout_ms, 'busy_timeout_ms', 0, 5000)
        self.outcome_retention = _integer(outcome_retention, 'outcome_retention', 1, 10000)
        self.busy_timeout_ms = busy_timeout_ms
        self.connection, self.clock = connection, clock
        self.forecast_evidence: Any = None
        self.forecast_now = None
        if connection.in_transaction:
            _invalid('connection')
        try:
            self.artifact_root = Path(artifact_root).resolve(strict=not read_only)
            if not read_only and not self.artifact_root.is_dir():
                _invalid('artifact_root')
            self._initialize(read_only, check_integrity)
        except (OSError, TypeError, ValueError):
            connection.close()
            raise LedgerError('INVALID_INPUT', 'initialize', 'artifact_root') from None
        except sqlite3.Error:
            connection.close()
            raise LedgerError('STORAGE_FAILURE', 'initialize') from None
        except BaseException:
            connection.close()
            raise

    def _initialize(self, read_only, check_integrity):
        connection = self.connection
        connection.row_factory = sqlite3.Row
        connection.create_function('outcome_at_or_before', 2, _at_or_before, deterministic=True)
        connection.execute(f'PRAGMA busy_timeout={self.busy_timeout_ms}')
        connection.execute('PRAGMA foreign_keys=ON')
        with self._transaction('initialize', write=not read_only):
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            objects = connection.execute("SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if version == 0 and not objects:
                if read_only:
                    raise LedgerError('NOT_FOUND', 'initialize', next_action='Initialize the ledger with hermes urgency apply.')
                for statement in _SCHEMA_V3:
                    connection.execute(statement)
                connection.execute('PRAGMA user_version=3')
                version = 3
            elif version not in (1, 2, 3):
                raise LedgerError('UNSUPPORTED_SCHEMA', 'initialize')
            elif sorted(row[0] for row in objects) != sorted((_SCHEMA, _SCHEMA_V2, _SCHEMA_V3)[version - 1]):
                raise LedgerError('CORRUPT_STATE', 'initialize')
            if (not read_only or check_integrity) and (connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone()):
                raise LedgerError('CORRUPT_STATE', 'initialize')
            if version == 1 and not read_only:
                connection.execute('ALTER TABLE tasks ADD COLUMN ' + _REMAINING_COLUMN)
                connection.execute('PRAGMA user_version=2')
                version = 2
            if version == 2 and not read_only:
                for statement in _OUTCOME_SCHEMA:
                    connection.execute(statement)
                connection.execute('PRAGMA user_version=3')
                version = 3
            self.schema_version = version

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.connection.close()

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            _invalid('clock')
        return value.astimezone(timezone.utc).isoformat()

    @contextmanager
    def _transaction(self, operation, *, write=False):
        if self.connection.in_transaction:
            raise LedgerError('INVALID_INPUT', operation, 'connection')
        committing = False
        try:
            self.connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield
            committing = True
            self.connection.commit()
        except BaseException as exc:
            rolled_back = False
            try:
                self.connection.rollback()
                rolled_back = True
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                code = getattr(exc, 'sqlite_errorcode', 0) & 255
                name = {sqlite3.SQLITE_BUSY: 'STORE_BUSY', sqlite3.SQLITE_LOCKED: 'STORE_BUSY',
                        sqlite3.SQLITE_CORRUPT: 'CORRUPT_STATE', sqlite3.SQLITE_NOTADB: 'CORRUPT_STATE'}.get(code, 'STORAGE_FAILURE')
                raise LedgerError(name, operation, side_effects='unknown' if committing or not rolled_back else 'none') from None
            raise

    def has_committed_operation(self, *request_ids):
        """Check exact durable receipts without replaying a mutation."""
        with self._transaction('operation_receipt'):
            return any(self.connection.execute('SELECT 1 FROM operations WHERE request_id=?',
                                               (request_id,)).fetchone() is not None
                       for request_id in request_ids)

    def _replay(self, request_id, digest):
        row = self.connection.execute('SELECT * FROM operations WHERE request_id=?', (request_id,)).fetchone()
        if row:
            if row['payload_sha256'] != digest:
                raise LedgerError('IDEMPOTENCY_CONFLICT', 'replay', 'request_id')
            return json.loads(row['response_json'])

    def _project(self, project_id):
        _id(project_id, 'project_id')
        row = self.connection.execute('SELECT * FROM projects WHERE project_id=?', (project_id,)).fetchone()
        if not row:
            raise LedgerError('NOT_FOUND', 'read', 'project_id')
        return dict(row)

    def get_project(self, project_id):
        with self._transaction('get_project'):
            return self._project(project_id)

    def list_projects(self, limit=100, after=None):
        _integer(limit, 'limit', 1, 500)
        if after is not None:
            _id(after, 'after')
        with self._transaction('list_projects'):
            return [dict(row) for row in self.connection.execute(
                'SELECT * FROM projects WHERE project_id>? ORDER BY project_id LIMIT ?', (after or '', limit))]

    def list_tasks(self, project_id, limit=100, after=None):
        """Page task keys in project scope; fetch details with get_task."""
        _id(project_id, 'project_id')
        _integer(limit, 'limit', 1, 500)
        if after is not None:
            if not isinstance(after, (list, tuple)) or len(after) != 2:
                _invalid('after')
            for value in after:
                _id(value, 'after')
        with self._transaction('list_tasks'):
            self._project(project_id)
            return [dict(row) for row in self.connection.execute(
                'SELECT board_id,task_id FROM tasks WHERE project_id=? AND (board_id,task_id)>(?,?) ORDER BY board_id,task_id LIMIT ?',
                (project_id, *(after or ('', '')), limit))]

    def list_deadlines(self, project_id, limit=100, after=None):
        """Page commitments without evaluating or persisting forecasts."""
        _id(project_id, 'project_id')
        _integer(limit, 'limit', 1, 500)
        if after is not None:
            _id(after, 'after')
        with self._transaction('list_deadlines'):
            self._project(project_id)
            return [dict(row) for row in self.connection.execute(
                'SELECT * FROM deadlines WHERE project_id=? AND deadline_id>? ORDER BY deadline_id LIMIT ?',
                (project_id, after or '', limit))]

    def history(self, entity_type, entity_id, limit=100, after=None):
        _id(entity_type, 'entity_type')
        _text(entity_id, 'entity_id')
        _integer(limit, 'limit', 1, 500)
        if after is not None:
            _integer(after, 'after')
        with self._transaction('history'):
            return [dict(row) for row in self.connection.execute(
                'SELECT * FROM audit WHERE entity_type=? AND entity_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                (entity_type, entity_id, after or 0, limit))]

    def export_outcomes(self, project_id, limit=100, after=None):
        _id(project_id, 'project_id')
        _integer(limit, 'limit', 1, 500)
        if after is not None:
            _integer(after, 'after')
        with self._transaction('export_outcomes'):
            self._project(project_id)
            if self.schema_version < 3:
                return []
            rows = [dict(row) for row in self.connection.execute(
                'SELECT * FROM outcomes WHERE project_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                (project_id, after or 0, limit))]
            for row in rows:
                if row['input_revisions'] is not None:
                    row['input_revisions'] = json.loads(row['input_revisions'])
                if row['on_time'] is not None:
                    row['on_time'] = bool(row['on_time'])
            return rows

    def project_history(self, project_id, limit=100, after=None):
        """Page recorded project, task, milestone and commitment changes together.

        Resolve ownership from the audit snapshot rather than querying live
        entity rows. Evidence results inherit ownership from their prior task.
        """
        _integer(limit, 'limit', 1, 500)
        if after is not None:
            _integer(after, 'after')
        with self._transaction('project_history'):
            self._project(project_id)
            return [dict(row) for row in self.connection.execute(
                "SELECT * FROM audit WHERE sequence>? AND ((entity_type='project' AND entity_id=?) "
                "OR (entity_type IN ('task','milestone','deadline') AND "
                "COALESCE(json_extract(after_json,'$.project_id'),json_extract(before_json,'$.project_id'))=?)) "
                "ORDER BY sequence LIMIT ?", (after or 0, project_id, project_id, limit))]

    @_command
    def create_project(self, project_id, title, *, request_id, actor):
        _id(project_id, 'project_id')
        _text(title, 'title')
        if self.connection.execute('SELECT 1 FROM projects WHERE project_id=?', (project_id,)).fetchone():
            raise LedgerError('REVISION_CONFLICT', 'create_project', 'project_id')
        self.connection.execute('INSERT INTO projects VALUES(?,?,1,?)', (project_id, title, self._now()))
        return self._project(project_id), 'project', project_id, None

    @_command
    def edit_project(self, project_id, *, title, expected_revision, reason, request_id, actor):
        before = self._project(project_id)
        self._check_revision(before, expected_revision)
        _text(title, 'title')
        self.connection.execute('UPDATE projects SET title=? WHERE project_id=?', (title, project_id))
        self._bump(project_id)
        return self._project(project_id), 'project', project_id, before

    def _check_revision(self, record, expected):
        if record['revision'] != expected:
            raise LedgerError('REVISION_CONFLICT', 'update', 'expected_revision')

    def _bump(self, project_id):
        self.connection.execute('UPDATE projects SET revision=revision+1 WHERE project_id=?', (project_id,))

    def _task(self, board_id, task_id):
        _id(board_id, 'board_id')
        _id(task_id, 'task_id')
        row = self.connection.execute('SELECT * FROM tasks WHERE board_id=? AND task_id=?', (board_id, task_id)).fetchone()
        if not row:
            raise LedgerError('NOT_FOUND', 'read', 'task_id')
        result = dict(row)
        result['remaining_known'] = bool(result.get('remaining_known', 1))
        if not result['remaining_known']:
            result['remaining_seconds'] = None
        result['criteria'] = [dict(c) for c in self.connection.execute(
            'SELECT * FROM criteria WHERE board_id=? AND task_id=? AND active=1 ORDER BY criterion_id', (board_id, task_id))]
        return result

    def get_task(self, board_id, task_id):
        with self._transaction('get_task'):
            return self._task(board_id, task_id)

    @_command
    def upsert_task(self, board_id, task_id, *, project_id, goal, expected_revision,
                    reason, request_id, actor, assignee=None, status='planned',
                    remaining_seconds=0, verification_seconds=0, deployment_seconds=0, actual_seconds=0):
        _id(board_id, 'board_id')
        _id(task_id, 'task_id')
        self._project(project_id)
        _text(goal, 'goal')
        if assignee is not None:
            _id(assignee, 'assignee')
        if status not in ('planned', 'running', 'blocked'):
            _invalid('status')
        numbers = [_number(v, f) for v, f in (
            (0 if remaining_seconds is None else remaining_seconds, 'remaining_seconds'), (verification_seconds, 'verification_seconds'),
            (deployment_seconds, 'deployment_seconds'), (actual_seconds, 'actual_seconds'))]
        exists = self.connection.execute('SELECT 1 FROM tasks WHERE board_id=? AND task_id=?', (board_id, task_id)).fetchone()
        before = self._task(board_id, task_id) if exists else None
        self._check_revision(before or {'revision': 0}, expected_revision)
        if before:
            if before['project_id'] != project_id:
                _invalid('project_id')
            # Status edits cannot forge or erase verified completion; reopen is explicit.
            if before['status'] in ('verified', 'claimed_complete'):
                status = before['status']
            self.connection.execute('UPDATE tasks SET assignee=?,goal=?,status=?,remaining_seconds=?,verification_seconds=?,deployment_seconds=?,actual_seconds=?,remaining_known=?,revision=revision+1 WHERE board_id=? AND task_id=?',
                                    (assignee, goal, status, *numbers, int(remaining_seconds is not None), board_id, task_id))
        else:
            count = self.connection.execute('SELECT count(*) FROM tasks WHERE project_id=?', (project_id,)).fetchone()[0]
            if count >= 10000:
                _invalid('tasks_capacity')
            self.connection.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,1,?)',
                                    (board_id, task_id, project_id, assignee, goal, status, *numbers, int(remaining_seconds is not None)))
        self._bump(project_id)
        return self._task(board_id, task_id), 'task', _json([board_id, task_id]), before

    def _scope(self, project_id):
        weight, count = self.connection.execute(
            'SELECT coalesce(sum(c.weight),0),count(*) FROM criteria c JOIN tasks t USING(board_id,task_id) WHERE t.project_id=? AND c.active=1', (project_id,)).fetchone()
        # The immutable audit prefix contains every exact scope revision. Retain
        # its watermark, not a million-criterion JSON copy on every progress read.
        sequence = self.connection.execute('SELECT coalesce(max(sequence),0) FROM audit').fetchone()[0]
        return {'weight': weight, 'criterion_count': count, 'audit_sequence': sequence}

    def _snapshot(self, project_id, kind, payload):
        cursor = self.connection.execute('INSERT INTO snapshots(project_id,kind,recorded_at,payload_json) VALUES(?,?,?,?)',
                                        (project_id, kind, self._now(), _json(payload)))
        return {'snapshot_id': cursor.lastrowid, **payload}

    @_command
    def baseline_scope(self, project_id, *, expected_revision, reason, request_id, actor):
        project = self._project(project_id)
        self._check_revision(project, expected_revision)
        if self.connection.execute("SELECT 1 FROM snapshots WHERE project_id=? AND kind='baseline'", (project_id,)).fetchone():
            raise LedgerError('REVISION_CONFLICT', 'baseline_scope', 'baseline')
        result = self._snapshot(project_id, 'baseline', self._scope(project_id))
        self._bump(project_id)
        return result, 'project', project_id, None

    @_command
    def set_criteria(self, board_id, task_id, criteria, *, expected_revision, reason, request_id, actor):
        before = self._task(board_id, task_id)
        self._check_revision(before, expected_revision)
        if not isinstance(criteria, list) or len(criteria) > 100:
            _invalid('criteria')
        identifiers = set()
        values = []
        for item in criteria:
            if not isinstance(item, dict) or set(item) != {'criterion_id', 'description', 'weight', 'required', 'expected_sha256'}:
                _invalid('criteria')
            key = _id(item['criterion_id'], 'criterion_id')
            if key in identifiers:
                _invalid('criterion_id')
            identifiers.add(key)
            _text(item['description'], 'description')
            weight = _number(item['weight'], 'weight', positive=True)
            if type(item['required']) is not bool:
                _invalid('required')
            digest = item['expected_sha256']
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                _invalid('expected_sha256')
            revision = self.connection.execute('SELECT coalesce(max(criterion_revision),0)+1 FROM criteria WHERE board_id=? AND task_id=? AND criterion_id=?', (board_id, task_id, key)).fetchone()[0]
            values.append((board_id, task_id, key, revision, item['description'], weight, int(item['required']), 1, digest))
        self._invalidate(board_id, task_id)
        self.connection.execute('UPDATE criteria SET active=0 WHERE board_id=? AND task_id=?', (board_id, task_id))
        self.connection.executemany('INSERT INTO criteria VALUES(?,?,?,?,?,?,?,?,?)', values)
        self.connection.execute("UPDATE tasks SET status='planned',actual_verified_at=NULL,revision=revision+1 WHERE board_id=? AND task_id=?", (board_id, task_id))
        self._bump(before['project_id'])
        self._snapshot(before['project_id'], 'current', {'action': 'set_criteria', 'board_id': board_id, 'task_id': task_id, 'criteria': self._task(board_id, task_id)['criteria']})
        return self._task(board_id, task_id), 'task', _json([board_id, task_id]), before

    @_command
    def claim_complete(self, board_id, task_id, *, expected_revision, reason, request_id, actor):
        before = self._task(board_id, task_id)
        self._check_revision(before, expected_revision)
        if before['status'] != 'verified':
            self.connection.execute("UPDATE tasks SET status='claimed_complete',revision=revision+1 WHERE board_id=? AND task_id=?", (board_id, task_id))
        self._bump(before['project_id'])
        return self._task(board_id, task_id), 'task', _json([board_id, task_id]), before

    def _invalidate(self, board_id, task_id):
        self.connection.execute('UPDATE evidence SET invalidated_at=? WHERE board_id=? AND task_id=? AND invalidated_at IS NULL', (self._now(), board_id, task_id))

    @_command
    def reopen_task(self, board_id, task_id, *, expected_revision, reason, request_id, actor):
        before = self._task(board_id, task_id)
        self._check_revision(before, expected_revision)
        self._invalidate(board_id, task_id)
        self.connection.execute("UPDATE tasks SET status='planned',actual_verified_at=NULL,revision=revision+1 WHERE board_id=? AND task_id=?", (board_id, task_id))
        self._bump(before['project_id'])
        self._snapshot(before['project_id'], 'current', {'action': 'reopen_task', 'board_id': board_id, 'task_id': task_id, 'revision': expected_revision + 1})
        return self._task(board_id, task_id), 'task', _json([board_id, task_id]), before

    def _record_outcome(self, before, evidence_id, checked_at):
        board, task, project = before['board_id'], before['task_id'], before['project_id']
        floor = self.connection.execute(
            "SELECT coalesce(max(json_extract(after_json,'$.revision')),0) FROM audit WHERE entity_type='task' AND entity_id=? AND action NOT IN ('claim_complete','verify_artifact')",
            (_json([board, task]),)).fetchone()[0]
        scope = "project_id=? AND kind='forecast' AND json_extract(payload_json,'$.board_id')=? AND json_extract(payload_json,'$.task_id')=? AND json_extract(payload_json,'$.milestone_id') IS NULL"
        revision = "json_array_length(payload_json,'$.input_revisions.tasks')=1 AND json_extract(payload_json,'$.input_revisions.tasks[0][0]')=? AND json_extract(payload_json,'$.input_revisions.tasks[0][1]')=? AND json_extract(payload_json,'$.input_revisions.tasks[0][2]') BETWEEN ? AND ? AND json_extract(payload_json,'$.actual_verified_at') IS NULL"
        prior = "outcome_at_or_before(recorded_at,?) AND outcome_at_or_before(json_extract(payload_json,'$.input.now'),?)"
        params = (project, board, task, board, task, floor, before['revision'], checked_at, checked_at)
        row = self.connection.execute(f'SELECT snapshot_id,recorded_at,payload_json FROM snapshots WHERE {scope} AND {revision} AND {prior} ORDER BY snapshot_id DESC LIMIT 1', params).fetchone()
        data = json.loads(row['payload_json']) if row else None
        reason = None
        if row is None:
            prior_row = self.connection.execute(f'SELECT 1 FROM snapshots WHERE {scope} AND {prior} LIMIT 1', (project, board, task, checked_at, checked_at)).fetchone()
            any_row = self.connection.execute(f'SELECT 1 FROM snapshots WHERE {scope} LIMIT 1', (project, board, task)).fetchone()
            reason = 'stale' if prior_row else ('incomparable' if any_row else 'missing')
        elif data is not None and (not data.get('forecast_at') or not _at_or_before(data['input']['now'], data['forecast_at'])):
            reason = 'incomparable'
        error = on_time = None
        if data is not None:
            on_time = int(datetime.fromisoformat(checked_at) <= datetime.fromisoformat(data['target_at']))
        if reason is None and data is not None:
            error = (datetime.fromisoformat(checked_at) - datetime.fromisoformat(data['forecast_at'])).total_seconds()
        self.connection.execute('INSERT INTO outcomes(project_id,board_id,task_id,evidence_id,verified_at,snapshot_id,evaluated_at,recorded_at,input_revisions,target_at,forecast_at,completion_error_seconds,on_time,forecast_reason,duration_seconds,duration_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (project, board, task, evidence_id, checked_at, row['snapshot_id'] if row else None,
             data['input']['now'] if data else None, row['recorded_at'] if row else None,
             _json(data['input_revisions']) if data else None, data['target_at'] if data else None,
             data['forecast_at'] if data else None, error, on_time, reason, None, 'duration_unobserved'))
        self.connection.execute('DELETE FROM outcomes WHERE project_id=? AND sequence <= coalesce((SELECT sequence FROM outcomes WHERE project_id=? ORDER BY sequence DESC LIMIT 1 OFFSET ?),0)',
                                (project, project, self.outcome_retention))

    @partial(_command, prepare=True)
    def verify_artifact(self, board_id, task_id, criterion_id, artifact_relative_path, *,
                        expected_revision, reason, request_id, actor):
        _id(criterion_id, 'criterion_id')
        with self._transaction('verify_artifact'):
            captured = self._task(board_id, task_id)
            self._check_revision(captured, expected_revision)
            criterion = next((c for c in captured['criteria'] if c['criterion_id'] == criterion_id), None)
            if criterion is None:
                raise LedgerError('NOT_FOUND', 'verify_artifact', 'criterion_id')
            if criterion['expected_sha256'] is None:
                _invalid('expected_sha256')
        # No database transaction, especially no write lock, spans file I/O.
        digest = self._hash_artifact(artifact_relative_path)
        if digest != criterion['expected_sha256']:
            raise LedgerError('EVIDENCE_MISMATCH', 'verify_artifact', 'artifact_relative_path')
        checked_at = self._now()

        def accept():
            before = self._task(board_id, task_id)
            self._check_revision(before, captured['revision'])
            if criterion not in before['criteria']:
                raise LedgerError('REVISION_CONFLICT', 'verify_artifact', 'criterion_revision')
            key = (board_id, task_id, criterion_id, criterion['criterion_revision'])
            existing = self.connection.execute('SELECT * FROM evidence WHERE board_id=? AND task_id=? AND criterion_id=? AND criterion_revision=? AND invalidated_at IS NULL', key).fetchone()
            if existing:
                return dict(existing), 'task', _json([board_id, task_id]), before
            self.connection.execute('INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,NULL)',
                                    (request_id, *key, artifact_relative_path, digest, checked_at))
            missing = self.connection.execute('SELECT count(*) FROM criteria c WHERE board_id=? AND task_id=? AND active=1 AND NOT ' + _HAS_EVIDENCE, (board_id, task_id)).fetchone()[0]
            self.connection.execute('UPDATE tasks SET status=?,actual_verified_at=?,last_verified_progress_at=?,revision=revision+1 WHERE board_id=? AND task_id=?',
                                    ('verified' if not missing else before['status'], checked_at if not missing else None, checked_at, board_id, task_id))
            if not missing and before['status'] != 'verified':
                self._record_outcome(before, request_id, checked_at)
            self._bump(before['project_id'])
            return dict(self.connection.execute('SELECT * FROM evidence WHERE evidence_id=?', (request_id,)).fetchone()), 'task', _json([board_id, task_id]), before
        return accept

    def _open_artifact(self, parts):
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
        if os.name != 'nt':
            directory = os.open(self.artifact_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                return os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=directory)
            finally:
                os.close(directory)
        # Check the opened handle, not just a racy path resolution. This prevents
        # reading outside the root if an ancestor becomes a junction mid-open.
        import ctypes
        import msvcrt
        from ctypes import wintypes
        fd = os.open(self.artifact_root.joinpath(*parts), flags)
        try:
            final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            final_path.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = final_path(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
            if not 0 < length < len(buffer):
                _invalid('artifact_relative_path')
            actual = buffer.value
            if actual.startswith('\\\\?\\UNC\\'):
                actual = '\\\\' + actual[8:]
            elif actual.startswith('\\\\?\\'):
                actual = actual[4:]
            if not Path(actual).is_relative_to(self.artifact_root):
                _invalid('artifact_relative_path')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _hash_artifact(self, name):
        _text(name, 'artifact_relative_path')
        parts = name.split('/')
        if any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts) or any(c in name for c in '\\:') or name.startswith('/'):
            _invalid('artifact_relative_path')
        path = self.artifact_root
        try:
            info = path.lstat()
            identities = []
            for part in parts:
                path = path / part
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    _invalid('artifact_relative_path')
                identities.append((path, info.st_dev, info.st_ino))
            if not path.resolve(strict=True).is_relative_to(self.artifact_root) or not stat.S_ISREG(info.st_mode):
                _invalid('artifact_relative_path')
            fd = self._open_artifact(parts)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_size > 16 * 1024 * 1024:
                    _invalid('artifact_relative_path')
                digest, total = hashlib.sha256(), 0
                while total < 16 * 1024 * 1024:
                    chunk = stream.read(min(65536, 16 * 1024 * 1024 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    digest.update(chunk)
                final = os.fstat(stream.fileno())
                if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                    _invalid('artifact_relative_path')
            for component, device, inode in identities:
                current = component.lstat()
                if stat.S_ISLNK(current.st_mode) or getattr(current, 'st_file_attributes', 0) & 0x400 or (current.st_dev, current.st_ino) != (device, inode):
                    _invalid('artifact_relative_path')
            return digest.hexdigest()
        except OSError:
            raise LedgerError('INVALID_INPUT', 'verify_artifact', 'artifact_relative_path') from None

    def _milestone(self, milestone_id):
        _id(milestone_id, 'milestone_id')
        row = self.connection.execute('SELECT * FROM milestones WHERE milestone_id=?', (milestone_id,)).fetchone()
        if not row:
            raise LedgerError('NOT_FOUND', 'read', 'milestone_id')
        return {**dict(row), 'tasks': [list(r) for r in self.connection.execute('SELECT board_id,task_id FROM milestone_tasks WHERE milestone_id=? ORDER BY board_id,task_id', (milestone_id,))]}

    def get_milestone(self, milestone_id):
        with self._transaction('get_milestone'):
            return self._milestone(milestone_id)

    def list_milestones(self, project_id, limit=100, after=None):
        _integer(limit, 'limit', 1, 500)
        if after is not None:
            _id(after, 'after')
        with self._transaction('list_milestones'):
            self._project(project_id)
            keys = self.connection.execute(
                'SELECT milestone_id FROM milestones WHERE project_id=? AND milestone_id>? ORDER BY milestone_id LIMIT ?',
                (project_id, after or '', limit)).fetchall()
            return [self._milestone(row[0]) for row in keys]

    @_command
    def edit_milestone(self, milestone_id, *, title, expected_revision, reason, request_id, actor):
        before = self._milestone(milestone_id)
        self._check_revision(self._project(before['project_id']), expected_revision)
        _text(title, 'title')
        self.connection.execute('UPDATE milestones SET title=? WHERE milestone_id=?', (title, milestone_id))
        self._bump(before['project_id'])
        return self._milestone(milestone_id), 'milestone', milestone_id, before

    @_command
    def create_milestone(self, milestone_id, *, project_id, title, request_id, actor):
        _id(milestone_id, 'milestone_id')
        _text(title, 'title')
        self._project(project_id)
        if self.connection.execute('SELECT 1 FROM milestones WHERE milestone_id=?', (milestone_id,)).fetchone():
            raise LedgerError('REVISION_CONFLICT', 'create_milestone', 'milestone_id')
        self.connection.execute('INSERT INTO milestones VALUES(?,?,?)', (milestone_id, project_id, title))
        self._bump(project_id)
        return self._milestone(milestone_id), 'milestone', milestone_id, None

    @_command
    def set_milestone_tasks(self, milestone_id, tasks, *, expected_revision, reason, request_id, actor):
        before = self._milestone(milestone_id)
        self._check_revision(self._project(before['project_id']), expected_revision)
        if not isinstance(tasks, list) or len(tasks) > 10000:
            _invalid('tasks')
        members = set()
        for pair in tasks:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                _invalid('tasks')
            task = self._task(*pair)
            if task['project_id'] != before['project_id']:
                _invalid('project_id')
            members.add(tuple(pair))
        self.connection.execute('DELETE FROM milestone_tasks WHERE milestone_id=?', (milestone_id,))
        self.connection.executemany('INSERT INTO milestone_tasks VALUES(?,?,?)', [(milestone_id, *pair) for pair in sorted(members)])
        self._bump(before['project_id'])
        return self._milestone(milestone_id), 'milestone', milestone_id, before

    def _deadline(self, deadline_id):
        _id(deadline_id, 'deadline_id')
        row = self.connection.execute('SELECT * FROM deadlines WHERE deadline_id=?', (deadline_id,)).fetchone()
        if not row:
            raise LedgerError('NOT_FOUND', 'read', 'deadline_id')
        return dict(row)

    def get_deadline(self, deadline_id):
        with self._transaction('get_deadline'):
            return self._deadline(deadline_id)

    @_command
    def set_deadline(self, deadline_id, *, project_id, kind, at, timezone, owner, source,
                     request_id, actor, board_id=None, task_id=None, milestone_id=None):
        _id(deadline_id, 'deadline_id')
        self._project(project_id)
        _text(owner, 'owner')
        _text(source, 'source')
        if kind not in ('hard', 'soft'):
            _invalid('kind')
        timestamp = _date(at, timezone)
        if (board_id is None) != (task_id is None) or (milestone_id is not None and board_id is not None):
            _invalid('scope')
        if board_id is not None and self._task(board_id, task_id)['project_id'] != project_id:
            _invalid('project_id')
        if milestone_id is not None and self._milestone(milestone_id)['project_id'] != project_id:
            _invalid('project_id')
        if self.connection.execute('SELECT 1 FROM deadlines WHERE deadline_id=?', (deadline_id,)).fetchone():
            raise LedgerError('REVISION_CONFLICT', 'set_deadline', 'deadline_id')
        self.connection.execute('INSERT INTO deadlines VALUES(?,?,?,?,?,?,?,?,?,?,?,1)',
                                (deadline_id, project_id, board_id, task_id, milestone_id, kind, timestamp, timestamp, timezone, owner, source))
        self._bump(project_id)
        return self._deadline(deadline_id), 'deadline', deadline_id, None

    @_command
    def revise_deadline(self, deadline_id, *, at, timezone, intent, expected_revision, reason, request_id, actor):
        before = self._deadline(deadline_id)
        self._check_revision(before, expected_revision)
        if intent not in ('operator', 'automatic') or (before['kind'] == 'hard' and intent != 'operator'):
            _invalid('intent')
        timestamp = _date(at, timezone)
        self.connection.execute('UPDATE deadlines SET target_at=?,timezone=?,revision=revision+1 WHERE deadline_id=?', (timestamp, timezone, deadline_id))
        self._bump(before['project_id'])
        return self._deadline(deadline_id), 'deadline', deadline_id, before

    def _schedule_input(self, deadline, tasks, now):
        """Join complete canonical inventory to Ledger without exposing graph IDs."""
        from graphlib import CycleError, TopologicalSorter
        from .schedule import MAX_EDGES, MAX_NODES, schedule

        standalone = dict(schedule={'method': 'ledger_only'})
        if not callable(self.forecast_evidence):
            return standalone
        try:
            evidence = self.forecast_evidence(deadline['project_id'], now)
        except Exception:
            evidence = {'reason': 'source_unavailable'}
        if evidence is None:
            return standalone
        def unknown(reason, *, contributors=(), actual=None):
            return dict(schedule={'method': 'unknown', 'reason': reason}, contributors=contributors,
                        work=None, verification=0, deployment=0, actual=actual)
        if not isinstance(evidence, dict):
            return unknown('source_unavailable')
        reason = evidence.get('reason')
        if reason is not None:
            allowed = {'source_unavailable', 'source_unbound', 'source_incomplete', 'source_stale',
                       'capacity_unknown', 'project_scope_incomplete'}
            return unknown(reason if isinstance(reason, str) and reason in allowed else 'source_unavailable')
        items = evidence.get('items')
        if not isinstance(items, list) or len(items) > MAX_NODES:
            return unknown('source_incomplete')
        graph = {}
        edges = 0
        for row in items:
            if not isinstance(row, dict):
                return unknown('source_incomplete')
            board, task, deps = row.get('board_id'), row.get('task_id'), row.get('parents')
            if not isinstance(board, str) or not isinstance(task, str) or not isinstance(deps, list):
                return unknown('source_incomplete')
            edges += len(deps)
            if edges > MAX_EDGES or (board, task) in graph:
                return unknown('source_incomplete')
            if not all(isinstance(dep, str) for dep in deps) or len(set(deps)) != len(deps):
                return unknown('source_incomplete')
            graph[(board, task)] = [(board, parent) for parent in deps]
        project_tasks = tasks if deadline['board_id'] is None and deadline['milestone_id'] is None else [dict(row) for row in self.connection.execute(
            'SELECT * FROM tasks WHERE project_id=? ORDER BY board_id,task_id LIMIT ?', (deadline['project_id'], MAX_NODES + 1))]
        ledger_rows = {(t['board_id'], t['task_id']): t for t in project_tasks}
        if len(project_tasks) > MAX_NODES or set(graph) != set(ledger_rows):
            return unknown('project_scope_incomplete')
        if any(parent not in graph for deps in graph.values() for parent in deps):
            return unknown('dependency_outside_project')
        try:
            TopologicalSorter(graph).prepare()
        except CycleError:
            return unknown('dependency_cycle')
        members = {(t['board_id'], t['task_id']) for t in tasks}
        if not members or not members <= set(graph):
            return unknown('project_scope_incomplete')
        closure, pending = set(), list(members)
        while pending:
            key = pending.pop()
            if key not in closure:
                closure.add(key)
                pending.extend(graph[key])
        actual = (max(ledger_rows[key]['actual_verified_at'] for key in closure)
                  if all(ledger_rows[key]['actual_verified_at'] for key in closure) else None)
        contributors = [ledger_rows[key] for key in sorted(closure)]
        slots, observed = evidence.get('slots'), evidence.get('observed_at')
        try:
            observed_time = datetime.fromisoformat(observed.replace('Z', '+00:00')) if isinstance(observed, str) else None
            if observed_time is None or observed_time.utcoffset() is None:
                raise ValueError
        except (ValueError, OverflowError):
            return unknown('capacity_unknown', contributors=contributors, actual=actual)
        if type(slots) is not int or not 1 <= slots <= 64:
            return unknown('capacity_unknown', contributors=contributors, actual=actual)
        unfinished = [row for row in contributors if not row['actual_verified_at']]
        if any(not t.get('remaining_known', 1) for t in unfinished):
            return unknown('missing_estimates', contributors=contributors, actual=actual)
        durations = {key: (0 if ledger_rows[key]['actual_verified_at'] else ledger_rows[key]['remaining_seconds']) for key in closure}
        work = schedule({key: graph[key] for key in closure}, durations, slots)
        return dict(schedule={'method': 'operator_declared_capacity', 'reason': None,
                              'slots': slots, 'observed_at': observed}, contributors=contributors, actual=actual,
                    work=work, verification=math.fsum(t['verification_seconds'] for t in unfinished),
                    deployment=math.fsum(t['deployment_seconds'] for t in unfinished))

    def _explain(self, deadline_id):
        from dataclasses import asdict
        from datetime import timedelta
        from .forecast import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast
        deadline = self._deadline(deadline_id)
        parameters = [deadline['project_id']]
        condition = 'project_id=?'
        if deadline['board_id'] is not None:
            condition += ' AND board_id=? AND task_id=?'
            parameters.extend((deadline['board_id'], deadline['task_id']))
        elif deadline['milestone_id'] is not None:
            condition += ' AND EXISTS(SELECT 1 FROM milestone_tasks m WHERE m.milestone_id=? AND m.board_id=tasks.board_id AND m.task_id=tasks.task_id)'
            parameters.append(deadline['milestone_id'])
        tasks = [dict(row) for row in self.connection.execute('SELECT * FROM tasks WHERE ' + condition + ' ORDER BY board_id,task_id LIMIT 10001', parameters)]
        work, verification, deployment = [math.fsum(t[field] for t in tasks) for field in ('remaining_seconds', 'verification_seconds', 'deployment_seconds')]
        if not tasks or any(not t.get('remaining_known', 1) for t in tasks):
            work = None
        now = datetime.fromisoformat(self._now())
        schedule_input = None
        if deadline['board_id'] is None:
            now = self.forecast_now or now
            schedule_input = self._schedule_input(deadline, tasks, now)
            if 'work' in schedule_input:
                work = schedule_input['work']
                verification = schedule_input['verification']
                deployment = schedule_input['deployment']
        previous_row = self.connection.execute("SELECT payload_json FROM snapshots WHERE project_id=? AND kind='forecast' AND json_extract(payload_json,'$.deadline_id')=? ORDER BY snapshot_id DESC LIMIT 1", (deadline['project_id'], deadline_id)).fetchone()
        previous = None
        if previous_row:
            record = json.loads(previous_row[0])['forecast']
            previous = UrgencyState(UrgencyLevel(record['level']), datetime.fromisoformat(record['changed_at']))
        try:
            forecast = asdict(calculate_forecast(ForecastInput(datetime.fromisoformat(deadline['target_at']), work, verification, deployment), now=now, previous=previous))
            projected = now + timedelta(seconds=work + verification + deployment) if work is not None else None
        except (ValueError, OverflowError):
            _invalid('forecast')
        forecast['changed_at'] = forecast['changed_at'].isoformat()
        forecast['level'], forecast['raw_level'] = int(forecast['level']), int(forecast['raw_level'])
        forecast['reasons'] = list(forecast['reasons'])
        actual = max(t['actual_verified_at'] for t in tasks) if tasks and all(t['actual_verified_at'] for t in tasks) else None
        if schedule_input is not None and 'actual' in schedule_input:
            actual = schedule_input['actual']
        comparison = datetime.fromisoformat(actual) if actual else projected
        input_data = dict(remaining_seconds=work, verification_seconds=verification, deployment_seconds=deployment, now=now.isoformat())
        if schedule_input is not None:
            input_data['schedule'] = schedule_input['schedule']
        revision_tasks = tasks if schedule_input is None else schedule_input.get('contributors', tasks)
        return {**deadline, 'forecast': forecast, 'forecast_at': projected.isoformat() if projected else None,
                'actual_verified_at': actual,
                'baseline_variance_seconds': (comparison - datetime.fromisoformat(deadline['original_at'])).total_seconds() if comparison else None,
                'target_variance_seconds': (comparison - datetime.fromisoformat(deadline['target_at'])).total_seconds() if comparison else None,
                'input_revisions': {'deadline': deadline['revision'], 'project': self._project(deadline['project_id'])['revision'],
                                    'tasks': [[t['board_id'], t['task_id'], t['revision']] for t in revision_tasks]},
                'input': input_data}

    def explain_deadline(self, deadline_id):
        with self._transaction('explain_deadline'):
            return self._explain(deadline_id)

    @_command
    def snapshot_forecast(self, deadline_id, *, expected_revision, reason, request_id, actor):
        deadline = self._deadline(deadline_id)
        self._check_revision(deadline, expected_revision)
        result = self._snapshot(deadline['project_id'], 'forecast', self._explain(deadline_id))
        self._bump(deadline['project_id'])
        return result, 'deadline', deadline_id, deadline

    def progress(self, project_id):
        with self._transaction('progress'):
            project = self._project(project_id)
            baseline = self.connection.execute("SELECT payload_json FROM snapshots WHERE project_id=? AND kind='baseline'", (project_id,)).fetchone()
            totals = self.connection.execute('SELECT coalesce(sum(c.weight),0), coalesce(sum(CASE WHEN ' + _HAS_EVIDENCE + ' THEN c.weight ELSE 0 END),0), count(*), coalesce(sum(CASE WHEN ' + _HAS_EVIDENCE + ' THEN 1 ELSE 0 END),0) FROM criteria c JOIN tasks t USING(board_id,task_id) WHERE t.project_id=? AND c.active=1', (project_id,)).fetchone()
            weight, verified, count, verified_count = totals
            percent = min(99.99999999999999, 100 * (verified / weight)) if weight else None
            if count and count == verified_count:
                percent = 100.0
            unscoped = self.connection.execute('SELECT count(*) FROM tasks t WHERE project_id=? AND NOT EXISTS (SELECT 1 FROM criteria c WHERE c.board_id=t.board_id AND c.task_id=t.task_id AND c.active=1)', (project_id,)).fetchone()[0]
            if unscoped:
                percent = None
            last = self.connection.execute('SELECT max(last_verified_progress_at) FROM tasks WHERE project_id=?', (project_id,)).fetchone()[0]
            return dict(project_id=project_id, baseline_weight=json.loads(baseline[0])['weight'] if baseline else None,
                        current_weight=weight, verified_weight=verified, percent=percent,
                        state='determinate' if percent is not None else 'insufficient_evidence', unscoped_task_count=unscoped,
                        last_verified_progress_at=last, revision=project['revision'])


def open_readonly_ledger(path, *, clock, deadline=None, busy_timeout_ms=100, check_integrity=False):
    """Open existing ledger bytes only; never run the host's journal setup."""
    import stat
    import time

    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise LedgerError('STORAGE_FAILURE', 'open')
        connection = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=busy_timeout_ms / 1000)
    except FileNotFoundError:
        raise LedgerError('NOT_FOUND', 'open', next_action='Initialize the ledger with hermes urgency apply.') from None
    except (OSError, sqlite3.Error):
        raise LedgerError('STORAGE_FAILURE', 'open') from None
    if deadline is not None:
        connection.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
    return Ledger(connection, clock=clock, artifact_root=path.parent / 'artifacts',
                  busy_timeout_ms=busy_timeout_ms, read_only=True, check_integrity=check_integrity)


def open_hermes_ledger(*, clock, artifact_root, busy_timeout_ms=5000):
    """Open documented, profile-aware plugin storage; caller uses a with block."""
    try:
        from plugins.plugin_storage import plugin_db
    except ImportError:
        raise LedgerError('STORAGE_FAILURE', 'open_hermes_ledger', next_action='Use a Hermes installation with documented plugin_storage, or inject a dedicated SQLite connection for offline use.') from None
    try:
        connection = plugin_db('hermes-urgency-engine')
    except (OSError, sqlite3.Error):
        raise LedgerError('STORAGE_FAILURE', 'open_hermes_ledger', side_effects='unknown') from None
    try:
        return Ledger(connection, clock=clock, artifact_root=artifact_root, busy_timeout_ms=busy_timeout_ms)
    except BaseException:
        connection.close()
        raise


def _date(value, zone_name):
    import re
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    _text(value, 'at', 64)
    _text(zone_name, 'timezone', 128)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        _invalid('at')
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # datetime accepts offset minute overflow; RFC3339 does not.
        if value[-6:-5] in ('+', '-') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            _invalid('at')
        utc = moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _invalid('at')
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise LedgerError('INVALID_INPUT', 'validate', 'timezone', next_action='Provide a valid IANA timezone and install system tzdata (or Python tzdata) if unavailable.') from None
    try:
        local = utc.astimezone(zone)
    except (ValueError, OverflowError):
        _invalid('at')
    if local.utcoffset() != moment.utcoffset() or local.replace(tzinfo=None) != moment.replace(tzinfo=None):
        _invalid('at')
    return utc.isoformat()


def _number(value, field, *, positive=False):
    if type(value) not in (int, float):
        _invalid(field)
    try:
        result = float(value)
    except OverflowError:
        _invalid(field)
    # Bounds keep project sums and datetime forecasts representable.
    if not math.isfinite(result) or not 0 <= result <= 1e12 or (positive and result == 0):
        _invalid(field)
    return result

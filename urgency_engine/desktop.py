"""Bounded Desktop read models. The Ledger remains the authority for progress."""
from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
import json
import re

from .ledger import LedgerError, _make_dirs, _readonly_uri
from .runtime import EVIDENCE, MUTATIONS


MANAGEMENT_FIELDS = {name: fields for name, fields in MUTATIONS.items() if name in (
    'create_project', 'create_milestone', 'set_milestone_tasks', 'set_deadline', 'revise_deadline',
    'baseline_scope', 'set_criteria')}
MANAGEMENT_FIELDS.update(edit_project=('project_id title expected_revision reason request_id', ''),
                         edit_milestone=('milestone_id title expected_revision reason request_id', ''))


def scope_identity(path, *, initialize=False):
    """Opaque namespace, not authority. Only explicit management initializes it.

    A separate metadata store leaves the Ledger schema and older CLI unchanged.
    Copied stores retain their identity; the renderer also binds known gateway
    identity and refuses ambiguous anonymous-route recovery across activations.
    """
    import sqlite3
    import uuid
    if not initialize and not path.exists():
        return None
    if initialize:
        try:
            _make_dirs(path.parent)
        except OSError:
            raise LedgerError('STORAGE_FAILURE', 'desktop_identity',
                              next_action='Select an existing Hermes profile; a deleted or renamed profile is not recreated.') from None
    connection = None
    try:
        connection = sqlite3.connect(path if initialize else _readonly_uri(path),
                                     uri=not initialize, timeout=5)
        connection.execute('BEGIN IMMEDIATE' if initialize else 'BEGIN')
        if initialize:
            connection.execute('CREATE TABLE IF NOT EXISTS desktop_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO desktop_identity VALUES (1, ?)', (uuid.uuid4().hex,))
        rows = connection.execute('SELECT singleton,value FROM desktop_identity').fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or not re.fullmatch(r'[0-9a-f]{32}', rows[0][1]):
            raise LedgerError('CORRUPT_STATE', 'desktop_identity')
        connection.commit()
        return rows[0][1]
    except sqlite3.Error as exc:
        code = getattr(exc, 'sqlite_errorcode', 0) & 255
        raise LedgerError('STORE_BUSY' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                          else 'CORRUPT_STATE', 'desktop_identity') from None
    finally:
        if connection is not None:
            connection.close()


def command_arguments(payload):
    from .ledger import _id
    fields = {'operation', 'request_id', 'arguments'}
    if not isinstance(payload, dict) or set(payload) not in (fields, fields | {'scope_id'}):
        raise LedgerError('INVALID_INPUT', 'desktop', 'command')
    if 'scope_id' in payload and (not isinstance(payload['scope_id'], str)
                                or not re.fullmatch(r'[0-9a-f]{32}', payload['scope_id'])):
        raise LedgerError('INVALID_INPUT', 'desktop', 'scope_id')
    operation, arguments = payload['operation'], payload['arguments']
    if not isinstance(operation, str) or operation not in MANAGEMENT_FIELDS:
        raise LedgerError('INVALID_INPUT', 'desktop', 'operation')
    _id(payload['request_id'], 'request_id')
    required, optional = (set(fields.split()) for fields in MANAGEMENT_FIELDS[operation])
    required.remove('request_id')
    if not isinstance(arguments, dict) or set(arguments) - (required | optional) or required - set(arguments):
        raise LedgerError('INVALID_INPUT', 'desktop', 'arguments')
    return operation, {**arguments, 'request_id': payload['request_id'], 'actor': 'desktop-operator'}


def command_time(value, zone_name):
    """Resolve a datetime-local control only when its named-zone instant is unique.

    Explicit offsets still go through the Ledger's existing zone validation.
    Never pick one side of a DST fold or normalize a nonexistent civil time.
    """
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', value):
        return value
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        local = datetime.fromisoformat(value)
        zone = ZoneInfo(zone_name)
        candidates = {}
        for fold in (0, 1):
            aware = local.replace(tzinfo=zone, fold=fold)
            utc = aware.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == local:
                candidates[utc] = aware
        if len(candidates) == 1:
            return next(iter(candidates.values())).isoformat()
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
        pass
    raise LedgerError('INVALID_INPUT', 'desktop', 'at', next_action=
                      'Choose an unambiguous date and time in a valid IANA zone; use the CLI with an explicit offset for a DST fold.')


def management_command(ledger, payload):
    """Domain adapter only. The host must authorize before opening the writer."""
    operation, arguments = command_arguments(payload)
    routes = {'create_project': ledger.create_project, 'edit_project': ledger.edit_project,
              'create_milestone': ledger.create_milestone, 'edit_milestone': ledger.edit_milestone,
              'set_milestone_tasks': ledger.set_milestone_tasks, 'set_deadline': ledger.set_deadline,
              'revise_deadline': ledger.revise_deadline, 'baseline_scope': ledger.baseline_scope,
              'set_criteria': ledger.set_criteria}
    if operation in ('set_deadline', 'revise_deadline'):
        arguments['at'] = command_time(arguments['at'], arguments['timezone'])
    if operation == 'revise_deadline':
        arguments['intent'] = 'operator'
    return routes[operation](**arguments)



def range_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        raise LedgerError('INVALID_INPUT', 'timeline', 'range')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise LedgerError('INVALID_INPUT', 'timeline', 'range') from None


def blocker_observations(path, project_id, tasks, *, clock):
    """Project-scoped retained signals only; never extend a band to the read clock."""
    from .watchdog import Monitor, instant
    if not tasks:
        return [], 'not_observed'
    store = Monitor.open(path, clock=clock, readonly=True)
    if store is None:
        return [], 'not_observed'
    keys = json.dumps([(row['board_id'], row['task_id']) for row in tasks])
    with store, store.transaction(False):
        rows = list(store.db.execute('''SELECT o.board_id,o.task_id,o.occurred_at,
                json_extract(o.payload_json,'$.data.blocker_since') AS started_at
            FROM json_each(?) AS key
            JOIN observations o ON o.board_id=json_extract(key.value,'$[0]')
                AND o.task_id=json_extract(key.value,'$[1]')
            JOIN monitor_audit a ON a.request_id=o.event_id
            WHERE o.project_id=? AND o.kind='task_signal'
                AND json_extract(o.payload_json,'$.out_of_order')=0
                AND json_type(o.payload_json,'$.data.blocker_since') IS NOT NULL
            ORDER BY o.occurred_at DESC,a.sequence DESC LIMIT 501''', (keys, project_id)))
    periods, latest = [], {}
    for row in reversed(rows[:500]):
        key = row['board_id'], row['task_id']
        start, at = row['started_at'], row['occurred_at']
        previous = latest.get(key)
        if start is None:
            if previous is not None:
                previous['clear_observed_at'] = at
                del latest[key]
        elif instant(start) <= instant(at):
            if previous is not None and previous['started_at'] == start:
                previous['last_observed_at'] = at
            else:
                period = dict(board_id=key[0], task_id=key[1], started_at=start,
                              last_observed_at=at, clear_observed_at=None)
                latest[key] = period
                periods.append(period)
    return periods, 'partial' if len(rows) > 500 else 'retained_observations' if rows else 'not_observed'


class Views:
    def __init__(self, ledger, *, clock):
        self.ledger, self.clock = ledger, clock

    def dependency_bound(self, project_id, graph, page):
        """Longest recorded remaining-work chain; never a resource forecast."""
        key = lambda row: (row['board_id'], row['task_id'])
        rows = {key(row): row for row in graph['items']}
        sql = 'SELECT board_id,task_id,remaining_seconds,remaining_known FROM tasks WHERE project_id=?'
        def load(clause, argument):
            return {key(r): r['remaining_seconds'] if r['remaining_known'] else None
                    for r in self.ledger.connection.execute(sql + clause, (project_id, argument))}
        reason = graph['reason']
        estimates = {}
        with self.ledger._transaction('desktop_dependency_bound'):
            self.ledger._project(project_id)
            if not reason:
                # One extra row detects a larger Ledger scope without reading it all.
                estimates = load(' ORDER BY board_id,task_id LIMIT ?', len(rows) + 1)
                if rows.keys() != estimates.keys():
                    reason = 'project_scope_incomplete'
            if reason:
                estimates = load(" AND (board_id,task_id) IN (SELECT json_extract(value,'$[0]'),"
                                 "json_extract(value,'$[1]') FROM json_each(?))", json.dumps([key(item) for item in page])) if page else {}
        for item in page:
            item.update(remaining_seconds=estimates.get(key(item)), critical_path_step=None)
        def unknown(reason):
            return dict(reason=reason, lower_bound_seconds=None, path_tasks=0)
        if reason:
            return unknown(reason)
        if not rows:
            return unknown('no_tasks')
        parents = {task: [(task[0], p) for p in row['parents']] for task, row in rows.items()}
        if any(p not in rows for dependencies in parents.values() for p in dependencies):
            return unknown('dependency_outside_project')
        try:
            order = list(TopologicalSorter(parents).static_order())
        except CycleError:
            return unknown('dependency_cycle')
        if None in estimates.values():
            return unknown('missing_estimates')
        lengths, previous = {}, {}
        for task in order:
            parent = max(parents[task], key=lambda p: (lengths[p], p), default=None)
            previous[task] = parent
            lengths[task] = lengths.get(parent, 0) + estimates[task]
        task = max(lengths, key=lambda t: (lengths[t], t))
        bound = lengths[task]
        path = []
        while task is not None:
            path.append(task)
            task = previous[task]
        steps = {task: step for step, task in enumerate(reversed(path), 1)}
        for item in page:
            item['critical_path_step'] = steps.get(key(item))
        return dict(reason=None, lower_bound_seconds=bound, path_tasks=len(path))

    def summary(self, project_id, commitments, partial):
        # Aggregate owned Ledger rows, never native board SQL or a subprocess per
        # task. Names are bounded; counts still cover the complete recorded scope.
        db = self.ledger.connection
        states = dict(db.execute('SELECT status,count(*) FROM tasks WHERE project_id=? GROUP BY status', (project_id,)))
        def names(column, condition=''):
            return [row[0] for row in db.execute(
                f'SELECT DISTINCT {column} FROM tasks WHERE project_id=? AND {column} IS NOT NULL {condition} ORDER BY {column} LIMIT 101',
                (project_id,))]
        boards = names('board_id')
        assigned = names('assignee')
        running = names('assignee', "AND status='running'")
        active = sorted((d for d in commitments if not d['actual_verified_at']), key=lambda d: (d['target_at'], d['deadline_id']))
        whole = [d for d in commitments if d['board_id'] is None and d['milestone_id'] is None]
        forecast = whole[0]['forecast_at'] if whole and not partial else None
        status = 'unknown'
        if not partial and active:
            if any(datetime.fromisoformat(d['target_at']) < self.clock() for d in active):
                status = 'overdue'
            elif any(d['forecast']['level'] >= 3 for d in active):
                status = 'at risk'
            elif all(d['forecast_at'] is not None for d in active):
                status = 'ahead' if all(d['target_variance_seconds'] < 0 for d in active) else 'on track'
        return dict(task_states=states, boards=boards[:100], assigned_agents=assigned[:100],
                    recorded_running_agents=running[:100], names_partial=any(len(v) > 100 for v in (boards, assigned, running)),
                    next_commitment=active[0] if active and not partial else None,
                    hard_deadline=next((d for d in active if d['kind'] == 'hard'), None) if not partial else None,
                    next_milestone=next((d for d in active if d['milestone_id']), None) if not partial else None,
                    forecast_at=forecast, forecast_evidence=whole[0]['forecast']['evidence'] if whole and not partial else 'unknown',
                    schedule_status=status, coverage='recorded_ledger_state_not_live_worker_liveness')

    def project(self, project_id):
        project = self.ledger.get_project(project_id)
        progress = self.ledger.progress(project_id)
        deadlines = self.ledger.list_deadlines(project_id, 100)
        more = bool(deadlines and self.ledger.list_deadlines(project_id, 1, deadlines[-1]['deadline_id']))
        commitments = []
        for deadline in deadlines:
            explanation = self.ledger.explain_deadline(deadline['deadline_id'])
            # Input tuples can contain the entire 10,000-task graph; the detail
            # inspector pages tasks separately rather than duplicating it per marker.
            explanation.pop('input_revisions')
            commitments.append(explanation)
        summary = self.summary(project_id, commitments, more)
        if self.ledger.get_project(project_id)['revision'] != project['revision']:
            raise LedgerError('REVISION_CONFLICT', 'desktop', next_action='Reload the changed project.')
        active = [d for d in commitments if d['actual_verified_at'] is None]
        urgent = max(active, key=lambda d: d['forecast']['level'], default=None)
        return {**project, 'progress': progress, 'summary': summary, **EVIDENCE,
                'commitments': commitments, 'commitments_has_more': more,
                'commitments_next': deadlines[-1]['deadline_id'] if more else None,
                'urgency': urgent['forecast'] if urgent and not more else None,
                'risk_coverage': 'partial' if more else 'recorded_commitments',
                'observed_at': self.clock().isoformat(), 'coverage': 'local_ledger_only'}

    def commitments(self, project_id, *, limit=100, after=None):
        project = self.ledger.get_project(project_id)
        rows = self.ledger.list_deadlines(project_id, limit, after)
        more = bool(rows and self.ledger.list_deadlines(project_id, 1, rows[-1]['deadline_id']))
        items = []
        for row in rows:
            item = self.ledger.explain_deadline(row['deadline_id'])
            item.pop('input_revisions')
            items.append(item)
        if self.ledger.get_project(project_id)['revision'] != project['revision']:
            raise LedgerError('REVISION_CONFLICT', 'desktop', next_action='Reload the changed project.')
        return {'items': items, 'has_more': more, 'next': rows[-1]['deadline_id'] if more else None,
                'project_revision': project['revision'], 'observed_at': self.clock().isoformat()}

    def timeline(self, project_id, *, start, end, limit=100, after=None):
        left, right = range_time(start), range_time(end)
        if not 0 < (right - left).total_seconds() <= 366 * 86400:
            raise LedgerError('INVALID_INPUT', 'timeline', 'range')
        cursor = None
        if after is not None:
            if not isinstance(after, str) or len(after) > 1024:
                raise LedgerError('INVALID_INPUT', 'timeline', 'after')
            try:
                cursor = json.loads(after)
            except (ValueError, RecursionError):
                raise LedgerError('INVALID_INPUT', 'timeline', 'after') from None
            if not isinstance(cursor, list) or len(cursor) != 2:
                raise LedgerError('INVALID_INPUT', 'timeline', 'after')
        keys = self.ledger.list_tasks(project_id, limit, cursor)
        next_key = [keys[-1]['board_id'], keys[-1]['task_id']] if keys else None
        more = bool(keys and self.ledger.list_tasks(project_id, 1, next_key))
        tasks = []
        with self.ledger._transaction('desktop_timeline'):
            for key in keys:
                task = self.ledger._task(**key)
                if task['project_id'] != project_id:
                    raise LedgerError('REVISION_CONFLICT', 'timeline')
                evidence = [dict(row) for row in self.ledger.connection.execute(
                    'SELECT c.criterion_id,c.criterion_revision,c.description,c.weight,c.required,'
                    'c.expected_sha256,e.observed_sha256,e.checked_at,e.evidence_id,'
                    '(e.observed_sha256=c.expected_sha256) AS verified '
                    'FROM criteria c LEFT JOIN evidence e ON e.board_id=c.board_id AND e.task_id=c.task_id '
                    'AND e.criterion_id=c.criterion_id AND e.criterion_revision=c.criterion_revision AND e.invalidated_at IS NULL '
                    'WHERE c.board_id=? AND c.task_id=? AND c.active=1 ORDER BY c.criterion_id',
                    (key['board_id'], key['task_id']))]
                for criterion in evidence:
                    criterion['verified'] = bool(criterion['verified'])
                tasks.append({**task, 'criterion_evidence': evidence, 'started_at': None,
                              'dependencies': None, 'blocked_periods': None})
        return {'items': tasks, 'next': json.dumps(next_key, separators=(',', ':')) if more else None,
                'has_more': more, 'start': left.isoformat(), 'end': right.isoformat(),
                'now': self.clock().astimezone(timezone.utc).isoformat(),
                'coverage': 'local_ledger_only',
                'range_policy': 'Undated tasks remain visible; range controls the time axis, not task membership.'}

    def portfolio(self, *, limit=100, after=None, board=None, project=None, assignee=None,
                  urgency=None, deadline_type=None):
        from .ledger import _id
        for name, value in (('board', board), ('project', project), ('assignee', assignee)):
            if value is not None:
                _id(value, name)
        if urgency is not None and urgency not in tuple(str(n) for n in range(7)):
            raise LedgerError('INVALID_INPUT', 'portfolio', 'urgency')
        if deadline_type is not None and deadline_type not in ('hard', 'soft'):
            raise LedgerError('INVALID_INPUT', 'portfolio', 'deadline_type')
        rows = self.ledger.list_projects(limit, after)
        more = bool(rows and self.ledger.list_projects(1, rows[-1]['project_id']))
        items = []
        for row in rows:
            project_id = row['project_id']
            if project is not None and project_id != project:
                continue
            db = self.ledger.connection
            if board is not None and not db.execute('SELECT 1 FROM tasks WHERE project_id=? AND board_id=? LIMIT 1', (project_id, board)).fetchone():
                continue
            if assignee is not None and not db.execute('SELECT 1 FROM tasks WHERE project_id=? AND assignee=? LIMIT 1', (project_id, assignee)).fetchone():
                continue
            if deadline_type is not None and not db.execute('SELECT 1 FROM deadlines WHERE project_id=? AND kind=? LIMIT 1', (project_id, deadline_type)).fetchone():
                continue
            item = self.project(project_id)
            if item['revision'] != row['revision']:
                raise LedgerError('REVISION_CONFLICT', 'portfolio', next_action='Reload the changed portfolio.')
            if urgency is not None and str((item['urgency'] or {}).get('level', 0)) != urgency:
                continue
            items.append(item)
        agents, days = {}, {}
        for item in items:
            for agent in item['summary']['assigned_agents']:
                agents.setdefault(agent, set()).add(item['project_id'])
            for deadline in item['commitments']:
                if not deadline['actual_verified_at']:
                    days.setdefault(deadline['target_at'][:10], set()).add(item['project_id'])
        shared = [{'agent': agent, 'projects': sorted(projects)} for agent, projects in sorted(agents.items()) if len(projects) > 1]
        collisions = [{'utc_day': day, 'projects': sorted(projects)} for day, projects in sorted(days.items()) if len(projects) > 1]
        coordination = dict(shared_agents=shared[:100], same_day_targets=collisions[:100],
                            truncated=len(shared) > 100 or len(collisions) > 100,
                            coverage='loaded_project_and_commitment_pages', capacity_assessment='unknown')
        return {'items': items, 'coordination': coordination,
                'next': rows[-1]['project_id'] if more else None, 'has_more': more,
                'observed_at': self.clock().isoformat(), 'coverage': 'local_ledger_only',
                'scanned': len(rows), 'filter_policy': 'Filters select recorded Ledger data within each bounded project page; an empty page may have a next cursor.'}

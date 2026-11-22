"""Bounded Desktop read models. The Ledger remains the authority for progress."""
from datetime import datetime, timezone
import json
import re

from .ledger import LedgerError
from .runtime import EVIDENCE, MUTATIONS


MANAGEMENT_FIELDS = {name: fields for name, fields in MUTATIONS.items() if name in (
    'create_project', 'create_milestone', 'set_milestone_tasks', 'set_deadline', 'revise_deadline',
    'baseline_scope', 'set_criteria')}
MANAGEMENT_FIELDS.update(edit_project=('project_id title expected_revision reason request_id', ''),
                         edit_milestone=('milestone_id title expected_revision reason request_id', ''))


def command_arguments(payload):
    from .ledger import _id
    if not isinstance(payload, dict) or set(payload) != {'operation', 'request_id', 'arguments'}:
        raise LedgerError('INVALID_INPUT', 'desktop', 'command')
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


class Views:
    def __init__(self, ledger, *, clock):
        self.ledger, self.clock = ledger, clock

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
        if self.ledger.get_project(project_id)['revision'] != project['revision']:
            raise LedgerError('REVISION_CONFLICT', 'desktop', next_action='Reload the changed project.')
        active = [d for d in commitments if d['actual_verified_at'] is None]
        urgent = max(active, key=lambda d: d['forecast']['level'], default=None)
        return {**project, 'progress': progress, **EVIDENCE,
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
        tasks = [{**self.ledger.get_task(**key), 'started_at': None,
                  'dependencies': None, 'blocked_periods': None} for key in keys]
        return {'items': tasks, 'next': json.dumps(next_key, separators=(',', ':')) if more else None,
                'has_more': more, 'start': left.isoformat(), 'end': right.isoformat(),
                'now': self.clock().astimezone(timezone.utc).isoformat(),
                'coverage': 'local_ledger_only',
                'range_policy': 'Undated tasks remain visible; range controls the time axis, not task membership.'}

    def portfolio(self, *, limit=100, after=None):
        rows = self.ledger.list_projects(limit, after)
        more = bool(rows and self.ledger.list_projects(1, rows[-1]['project_id']))
        return {'items': [self.project(row['project_id']) for row in rows],
                'next': rows[-1]['project_id'] if more else None, 'has_more': more,
                'observed_at': self.clock().isoformat(), 'coverage': 'local_ledger_only'}

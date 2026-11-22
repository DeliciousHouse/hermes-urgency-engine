"""Scoped REST adapter; Hermes authenticates and selects profile before routing.

Synchronous handlers run in the host's worker pool, not its event loop.
No mutable active-profile state, credentials, paths or board SQL are cached here.
"""
import importlib.util
import asyncio
import io
from pathlib import Path
import sys
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# Load this package's own code without adding its directory to the process import
# path or binding the common `urgency_engine` name across different installations.
_name = __name__ + '.engine'
_spec = importlib.util.spec_from_file_location(_name, Path(__file__).resolve().parents[1] / 'urgency_engine' / '__init__.py',
                                              submodule_search_locations=[str(Path(__file__).resolve().parents[1] / 'urgency_engine')])
if _spec is None or _spec.loader is None:
    raise ImportError('Urgency package is incomplete')
_engine = importlib.util.module_from_spec(_spec)
sys.modules[_name] = _engine
_spec.loader.exec_module(_engine)
_desktop = importlib.import_module(_name + '.desktop')
_ledger = importlib.import_module(_name + '.ledger')
_runtime = importlib.import_module(_name + '.runtime')
router = APIRouter()


def clock():
    return datetime.now(timezone.utc)


def respond(action):
    try:
        return {'ok': True, 'data': action()}
    except _ledger.LedgerError as exc:
        status = {'NOT_FOUND': 404, 'REVISION_CONFLICT': 409, 'IDEMPOTENCY_CONFLICT': 409,
                  'SOURCE_DENIED': 403, 'READ_ONLY': 403, 'PAUSED': 403,
                  'CAPABILITY_UNAVAILABLE': 503, 'STORE_BUSY': 503}.get(exc.code, 400)
        return JSONResponse(_runtime.Runtime.error(exc, 'desktop'), status_code=status)
    except Exception:
        error = _runtime.Runtime.error(_ledger.LedgerError('INTERNAL_ERROR', 'desktop', side_effects='unknown'), 'desktop')
        error['error']['diagnostic_id'] = uuid.uuid4().hex
        return JSONResponse(error, status_code=500)


def read_view(action):
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / 'plugin-data' / 'hermes-urgency-engine' / 'data.db'
    try:
        storage = _ledger.open_readonly_ledger(path, clock=clock)
    except _ledger.LedgerError as exc:
        if exc.code != 'NOT_FOUND':
            raise
        import sqlite3
        storage = _ledger.Ledger(sqlite3.connect(':memory:'), clock=clock, artifact_root=path.parent)
    with storage as ledger:
        return action(_desktop.Views(ledger, clock=clock))


def page_arguments(request, extra=()):
    # No path/home/profile payload can select storage. `profile` is consumed by
    # the host's authenticated routing dependency, never by this adapter.
    if set(request.query_params) - {'limit', 'after', 'profile'} - set(extra):
        raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'query')
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'query')
    raw = request.query_params.get('limit', '100')
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 500:
        raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'limit')
    return dict(limit=int(raw), after=request.query_params.get('after'))


def identity_data(*, initialize=False):
    if initialize:
        _runtime.interventions.operator_only()
        _runtime.interventions.host_stop()
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / 'plugin-data' / 'hermes-urgency-engine' / 'desktop.db'
    return {'scope_id': _desktop.scope_identity(path, initialize=initialize)}


@router.get('/identity')
def identity():
    return respond(identity_data)


@router.post('/identity')
def initialize_identity():
    # Authentication and selected profile are supplied by the native host mount.
    # This namespace is not an authorization token and grants no new operation.
    return respond(lambda: identity_data(initialize=True))


@router.get('/portfolio')
def portfolio(request: Request):
    def read():
        filters = ('board', 'project', 'assignee', 'urgency', 'deadline_type')
        arguments = page_arguments(request, filters)
        arguments.update({key: request.query_params[key] for key in filters if key in request.query_params})
        return read_view(lambda view: view.portfolio(**arguments))
    return respond(read)


@router.get('/projects/{project_id}')
def project(project_id: str):
    return respond(lambda: read_view(lambda view: view.project(project_id)))


@router.get('/projects/{project_id}/commitments')
def commitments(project_id: str, request: Request):
    return respond(lambda: read_view(lambda view: view.commitments(project_id, **page_arguments(request))))


@router.get('/projects/{project_id}/timeline')
def timeline(project_id: str, request: Request):
    def read():
        arguments = page_arguments(request, ('start', 'end'))
        return read_view(lambda view: view.timeline(project_id, start=request.query_params.get('start'),
                                                    end=request.query_params.get('end'), **arguments))
    return respond(read)


@router.get('/projects/{project_id}/history')
def history(project_id: str, request: Request):
    def read():
        arguments = page_arguments(request)
        after = arguments['after']
        if after is not None:
            if not after.isascii() or not after.isdecimal() or len(after) > 18:
                raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'after')
            arguments['after'] = int(after)
        def records(view):
            view.ledger.get_project(project_id)
            rows = view.ledger.project_history(project_id, **arguments)
            more = bool(rows and view.ledger.project_history(project_id, 1, rows[-1]['sequence']))
            return {'items': rows, 'has_more': more, 'next': rows[-1]['sequence'] if more else None}
        return read_view(records)
    return respond(read)


def scoped_runtime():
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    ctx = PluginContext(PluginManifest(name='hermes-urgency-engine'), get_plugin_manager())
    return _runtime.Runtime(ctx)


@router.get('/projects/{project_id}/milestones')
def milestones(project_id: str, request: Request):
    def read():
        arguments = page_arguments(request)
        def records(view):
            rows = view.ledger.list_milestones(project_id, **arguments)
            more = bool(rows and view.ledger.list_milestones(project_id, 1, rows[-1]['milestone_id']))
            return {'items': rows, 'has_more': more, 'next': rows[-1]['milestone_id'] if more else None}
        return read_view(records)
    return respond(read)


@router.get('/projects/{project_id}/milestones/{milestone_id}')
def milestone(project_id: str, milestone_id: str):
    def read(view):
        project = view.ledger.get_project(project_id)
        record = view.ledger.get_milestone(milestone_id)
        if record['project_id'] != project_id:
            raise _ledger.LedgerError('NOT_FOUND', 'desktop', 'milestone_id')
        if view.ledger.get_project(project_id)['revision'] != project['revision']:
            raise _ledger.LedgerError('REVISION_CONFLICT', 'desktop')
        return {**record, 'project_revision': project['revision']}
    return respond(lambda: read_view(read))


@router.get('/projects/{project_id}/source-tasks')
def source_tasks(project_id: str, request: Request):
    def read():
        arguments = page_arguments(request)
        if arguments['after'] is None:
            del arguments['after']
        read_view(lambda view: view.ledger.get_project(project_id))
        runtime = scoped_runtime()
        data = runtime.kanban_read('tasks', {'project_id': project_id, **arguments})
        data['sync_health'] = runtime.kanban_read('health', {})
        data['sync_seconds'] = runtime.kanban_config()['kanban_sync_seconds']
        return data
    return respond(read)


@router.get('/projects/{project_id}/interventions')
def interventions(project_id: str, request: Request):
    def read():
        arguments = page_arguments(request)
        if arguments['after'] is not None:
            _ledger._id(arguments['after'], 'after')
        read_view(lambda view: view.ledger.get_project(project_id))
        runtime = scoped_runtime()
        store = _runtime.interventions.Actions.read(runtime.data_dir() / 'interventions.db', clock)
        if store is None:
            data = dict(items=[], next=None, has_more=False)
        else:
            with store, store.transaction(False):
                data = store.page(project_id, arguments['limit'], arguments['after'])
        data['recovery_creation'] = 'CAPABILITY_UNAVAILABLE'
        data['next_action'] = 'Use the supported local hermes urgency interventions commands for supervised actions.'
        return data
    return respond(read)


def execute_command(raw):
    try:
        payload = _runtime.read_json(io.BytesIO(raw))
    except (ValueError, UnicodeError, RecursionError):
        raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'body') from None
    _desktop.command_arguments(payload)
    # The normal host authentication gate has already admitted this request.
    # A worker-launched backend still cannot acquire operator privileges here.
    _runtime.interventions.operator_only()
    _runtime.interventions.host_stop()
    if 'scope_id' in payload and payload['scope_id'] != identity_data()['scope_id']:
        raise _ledger.LedgerError('SOURCE_DENIED', 'desktop', 'scope_id',
                                  next_action='Return to the original scope to reconcile the retained request.')
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    ctx = PluginContext(PluginManifest(name='hermes-urgency-engine'), get_plugin_manager())
    runtime = _runtime.Runtime(ctx)
    if any(ctx.redact_text(value, force=True) != value for value in _strings(payload)):
        raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'secret_text')
    with runtime.open(runtime.settings()) as ledger:
        return _desktop.management_command(ledger, payload)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


@router.post('/commands')
async def commands(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        # Refuse before the oversized chunk is retained, not after.
        if len(raw) + len(chunk) > _runtime.MAX_INPUT_BYTES:
            return JSONResponse(_runtime.Runtime.error(_ledger.LedgerError('INVALID_INPUT', 'desktop', 'body'), 'desktop'), status_code=400)
        raw.extend(chunk)
    return await asyncio.to_thread(respond, lambda: execute_command(bytes(raw)))


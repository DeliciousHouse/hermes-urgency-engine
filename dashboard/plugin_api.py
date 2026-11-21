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
    with _ledger.open_readonly_ledger(path, clock=clock) as ledger:
        return action(_desktop.Views(ledger, clock=clock))


def page_arguments(request):
    # No path/home/profile payload can select storage. `profile` is consumed by
    # the host's authenticated routing dependency, never by this adapter.
    if set(request.query_params) - {'limit', 'after', 'profile'}:
        raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'query')
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'query')
    raw = request.query_params.get('limit', '100')
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 500:
        raise _ledger.LedgerError('INVALID_INPUT', 'desktop', 'limit')
    return dict(limit=int(raw), after=request.query_params.get('after'))


@router.get('/portfolio')
def portfolio(request: Request):
    def read():
        arguments = page_arguments(request)
        return read_view(lambda view: view.portfolio(**arguments))
    return respond(read)


@router.get('/projects/{project_id}')
def project(project_id: str):
    return respond(lambda: read_view(lambda view: view.project(project_id)))


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
        raw.extend(chunk)
        if len(raw) > _runtime.MAX_INPUT_BYTES:
            return JSONResponse(_runtime.Runtime.error(_ledger.LedgerError('INVALID_INPUT', 'desktop', 'body'), 'desktop'), status_code=400)
    return await asyncio.to_thread(respond, lambda: execute_command(bytes(raw)))


"""Real host REST mount test (run with a provisioned Hermes interpreter).

protects=authenticated scoped read and safe errors; fails_when=auth leaks or mount fails;
why_new=new Desktop HTTP boundary; seam=ASGI transport only;
no_claim=actual Electron interaction or remote routing.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = 'hermes-urgency-engine'
PLUGIN_CONFIG = {'enabled': [PLUGIN], 'isolation': 'in_process'}


def child():
    from datetime import datetime, timedelta, timezone
    from inspect import getfile
    import sqlite3
    from fastapi.testclient import TestClient
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    assert home == Path(os.environ['HERMES_HOME']).resolve()
    package = home / 'plugins' / PLUGIN
    sys.path.insert(0, str(package))
    from urgency_engine.ledger import Ledger
    root = home / 'plugin-data' / PLUGIN
    root.mkdir(parents=True)
    with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda:datetime(2030, 1, 1, tzinfo=timezone.utc), artifact_root=root) as ledger:
        ledger.create_project('synthetic', 'Synthetic project', request_id='create', actor='operator')
    from hermes_cli import web_server as server
    from hermes_cli.web_server_dashboard import _mount_plugin_api_routes
    _mount_plugin_api_routes()
    url = '/api/plugins/' + PLUGIN
    mounted_source = None
    mounted_module = None

    async def app(scope, receive, send):
        nonlocal mounted_source, mounted_module
        await server.app(scope, receive, send)
        if scope['path'] == url + '/portfolio' and 'endpoint' in scope:
            mounted_source = Path(getfile(scope['endpoint'])).resolve()
            mounted_module = sys.modules[scope['endpoint'].__module__]

    client = TestClient(app)
    assert client.get(url + '/portfolio').status_code == 401
    headers = {'X-Hermes-Session-Token': server._SESSION_TOKEN}
    assert client.get(url + '/identity').status_code == 401
    identity = client.get(url + '/identity', headers=headers)
    assert identity.status_code == 200, (identity.status_code, identity.text)
    assert identity.json()['data']['scope_id'] is None
    assert not (root / 'desktop.db').exists()
    assert client.post(url + '/identity').status_code == 401
    refused = client.post(url + '/identity', headers=headers)
    assert refused.status_code == 403, (refused.status_code, refused.text)
    assert not (root / 'desktop.db').exists(), 'worker fence must precede initialization'
    from urgency_engine.desktop import scope_identity
    expected_identity = scope_identity(root / 'desktop.db', initialize=True)
    assert client.get(url + '/identity', headers=headers).json()['data']['scope_id'] == expected_identity
    response = client.get(url + '/portfolio', headers=headers)
    assert response.status_code == 200, (response.status_code, response.text)
    expected_source = (package / 'dashboard' / 'plugin_api.py').resolve()
    assert mounted_source == expected_source, mounted_source

    # protects=no-root read; fails_when=a GET creates a store or hides invalid root;
    # seam=native authenticated REST; no_claim=Electron or installation.
    parked = root.with_name('parked')
    root.rename(parked)
    no_root = client.get(url + '/portfolio', headers=headers)
    assert no_root.status_code == 400 and no_root.json()['error']['field'] == 'artifact_root', no_root.text
    assert not root.exists()
    root.mkdir()
    empty_root = client.get(url + '/portfolio', headers=headers)
    assert empty_root.status_code == 200 and empty_root.json()['data']['items'] == []
    assert list(root.iterdir()) == []
    root.rmdir()
    parked.rename(root)

    # protects=forced public redaction parity; fails_when=preference disables it;
    # seam=native loader context and mounted API; no_claim=all possible secrets.
    config = home / 'config.yaml'
    config.write_text(json.dumps({'security': {'redact_secrets': False}, 'plugins': {
        **PLUGIN_CONFIG, 'entries': {PLUGIN: {'settings': {'context_max_chars': 2200},
                                               'config': {'context_max_chars': 1800, 'default_project': 'legacy'}}}}}), encoding='utf-8')
    from hermes_cli.plugins import discover_plugins, get_plugin_command_handler
    discover_plugins()
    handler = get_plugin_command_handler('urgency')
    assert handler is not None
    native = handler.__self__.ctx
    public = mounted_module._RequestServices()
    for key, default in [('context_max_chars', 123), ('default_project', 'none'), ('missing_key', 'fallback')]:
        assert public.get_config(key, default) == native.get_config(key, default)
    samples = ('password=' + 'synth' + 'etic-only',
               'Bearer ' + 'sk-' + 'a' * 40,
               'https://user:synthetic-pass@example.invalid/?token=synthetic-token')
    for value in samples:
        actual = public.redact_text(value)
        assert actual == native.redact_text(value, force=True)
        assert actual != value, 'synthetic credential was not redacted'

    data = response.json()['data']
    assert data['items'][0]['title'] == 'Synthetic project'
    assert data['items'][0]['progress']['percent'] is None
    detail = client.get(url + '/projects/synthetic', headers=headers)
    assert detail.status_code == 200 and detail.json()['data']['project_id'] == 'synthetic'
    timeline = client.get(url + '/projects/synthetic/timeline?start=2030-01-01T00:00:00Z&end=2030-01-02T00:00:00Z', headers=headers)
    assert timeline.status_code == 200, (timeline.status_code, timeline.text)
    assert timeline.json()['data']['items'] == []
    history = client.get(url + '/projects/synthetic/history', headers=headers)
    assert history.status_code == 200, (history.status_code, history.text)
    assert history.json()['data']['items'][0]['action'] == 'create_project'
    for suffix in ('commitments', 'milestones', 'source-tasks', 'interventions'):
        response = client.get(url + '/projects/synthetic/' + suffix, headers=headers)
        assert response.status_code == 200, (suffix, response.status_code, response.text)
        assert response.json()['data']['items'] == []
        assert client.get(url + '/projects/foreign/' + suffix, headers=headers).status_code == 404
        if suffix == 'source-tasks':
            assert response.json()['data']['blocker_coverage'] == 'not_observed'
    # Read an existing milestone and its membership at the owning revision;
    # a milestone ID from another project must never populate this editor.
    with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda:datetime(2030, 1, 1, tzinfo=timezone.utc), artifact_root=root) as ledger:
        ledger.upsert_task('board', 'task', project_id='synthetic', goal='Deliver', expected_revision=0,
                          reason='Plan', request_id='task', actor='operator')
        ledger.create_milestone('m', project_id='synthetic', title='Review', request_id='m', actor='operator')
        ledger.set_milestone_tasks('m', [['board', 'task']], expected_revision=3, reason='Scope', request_id='members', actor='operator')
        ledger.create_project('other', 'Other', request_id='other', actor='operator')
    milestone = client.get(url + '/projects/synthetic/milestones/m', headers=headers)
    assert milestone.status_code == 200, (milestone.status_code, milestone.text)
    assert milestone.json()['data']['tasks'] == [['board', 'task']]
    assert milestone.json()['data']['project_revision'] == 4
    audit = client.get(url + '/projects/synthetic/history', headers=headers).json()['data']['items']
    assert any(row['action'] == 'set_milestone_tasks' for row in audit), audit
    assert not any(row['entity_id'] == 'other' for row in audit)
    assert client.get(url + '/projects/other/milestones/m', headers=headers).status_code == 404
    assert client.get(url + '/projects/synthetic/milestones/m').status_code == 401
    missing = client.get(url + '/projects/foreign', headers=headers)
    assert missing.status_code == 404 and missing.json()['error']['code'] == 'NOT_FOUND'
    invalid = client.get(url + '/portfolio?limit=501', headers=headers)
    assert invalid.status_code == 400 and invalid.json()['error']['code'] == 'INVALID_INPUT'
    filtered = client.get(url + '/portfolio?project=synthetic&board=board&urgency=0', headers=headers)
    assert filtered.status_code == 200, (filtered.status_code, filtered.text)
    assert [row['project_id'] for row in filtered.json()['data']['items']] == ['synthetic']
    assert client.get(url + '/portfolio?assignee=absent', headers=headers).json()['data']['items'] == []
    assert client.get(url + '/portfolio?deadline_type=automatic', headers=headers).status_code == 400
    assert client.get(url + '/portfolio?project=synthetic&project=other', headers=headers).status_code == 400
    denied_command = client.post(url + '/commands', json={'request_id': 'rename', 'operation': 'edit_project',
        'arguments': {'project_id': 'synthetic', 'title': 'Changed', 'expected_revision': 1, 'reason': 'Edit'}}, headers=headers)
    assert denied_command.status_code == 403, (denied_command.status_code, denied_command.text)
    assert client.get(url + '/projects/synthetic', headers=headers).json()['data']['title'] == 'Synthetic project'
    # Canonical source is synthetic; the persisted Sync/Ledger and mounted read
    # route are real. A page of one must still explain a two-task dependency path.
    from urgency_engine.kanban import Sync, settings
    binding = dict(board='board', tenant='synthetic', project_id='synthetic')
    source_settings = dict(kanban_sync_enabled=True, kanban_bindings=[binding], forecast_capacity={
        'synthetic': dict(slots=2, observed_at=datetime.now(timezone.utc).isoformat())})
    config = get_hermes_home() / 'config.yaml'
    # JSON is valid YAML; let the pinned host load it with its own parser.
    config.write_text(json.dumps({'plugins': {
        **PLUGIN_CONFIG, 'entries': {PLUGIN: {'settings': source_settings}},
    }}), encoding='utf-8')

    class SyntheticSource:
        edge = False

        def inventory(self, binding):
            return [dict(id=task, tenant=binding['tenant']) for task in ('prepare', 'task')]

        def detail(self, binding, task_id):
            return dict(task=dict(id=task_id, tenant=binding['tenant'], title=task_id, status='todo'),
                        parents=['prepare'] if task_id == 'task' and self.edge else [])

    def now():
        return datetime.now(timezone.utc)

    source = SyntheticSource()
    with Ledger(sqlite3.connect(root / 'data.db'), clock=now, artifact_root=root) as ledger, \
            Sync(sqlite3.connect(root / 'kanban.db'), clock=now, redact=str) as sync:
        assert sync.scan(settings(source_settings), ledger, None, root / 'data.db', source,
                         request_id='source-scan', reason='Synthetic graph')['state'] == 'complete'
        for task, duration in [('prepare', 1800), ('task', 3600)]:
            ledger.upsert_task('board', task, project_id='synthetic', goal=task, remaining_seconds=duration,
                               expected_revision=ledger.get_task('board', task)['revision'],
                               reason='Estimate', request_id='estimate-' + task, actor='operator')
        target = (now() + timedelta(seconds=4500)).isoformat()
        ledger.set_deadline('delivery', project_id='synthetic', kind='soft', at=target, timezone='UTC',
                            owner='Synthetic', source='Fixture', request_id='delivery', actor='operator')

        # protects=authenticated REST risk; fails_when=only the bound changes;
        # no_claim=Electron rendering or actual execution capacity.
        def commitment(work):
            response = client.get(url + '/projects/synthetic/commitments', headers=headers)
            assert response.status_code == 200, (response.status_code, response.text)
            item, = response.json()['data']['items']
            assert (item['original_at'], item['target_at']) == (target, target)
            assert item['input']['remaining_seconds'] == work, item['input']
            assert item['input']['schedule']['method'] == 'operator_declared_capacity'
            return item['forecast']

        before = commitment(3600)
        source.edge = True
        assert sync.scan(settings(source_settings), ledger, None, root / 'data.db', source,
                         request_id='source-edge', reason='Synthetic edge')['state'] == 'complete'
        after = commitment(5400)
        assert before['effective_slack_seconds'] > 0 > after['effective_slack_seconds']
        assert before['raw_level'] < after['raw_level']
    source_url = url + '/projects/synthetic/source-tasks'
    assert client.get(source_url).status_code == 401
    source_page = client.get(source_url, params={'limit': 1}, headers=headers)
    assert source_page.status_code == 200, (source_page.status_code, source_page.text)
    source_data = source_page.json()['data']
    assert 'graph' not in source_data and source_data['has_more']
    assert source_data['dependency_bound'] == dict(reason=None, lower_bound_seconds=5400, path_tasks=2)
    assert len(source_data['items']) == 1 and source_data['items'][0]['critical_path_step'] == 1
    tail = client.get(source_url, params={'limit': 1, 'after': source_data['next']}, headers=headers).json()['data']
    assert tail['dependency_bound'] == source_data['dependency_bound']
    assert tail['items'][0]['critical_path_step'] == 2 and tail['items'][0]['parents'] == ['prepare']
    malformed = client.post(url + '/commands', json={'actor': 'operator'}, headers=headers)
    assert malformed.status_code == 400, (malformed.status_code, malformed.text)
    for raw in ('{', '{"operation":"edit_project","operation":"set_deadline"}'):
        malformed = client.post(url + '/commands', content=raw, headers={**headers, 'Content-Type': 'application/json'})
        assert malformed.status_code == 400, (malformed.status_code, malformed.text)
        assert malformed.json()['error']['field'] != 'content_type', malformed.text
    plain = client.post(url + '/commands', content='{}', headers={**headers, 'Content-Type': 'text/plain'})
    assert plain.status_code == 400 and plain.json()['error']['field'] == 'content_type', plain.text
    assert client.post(url + '/commands', json={}).status_code == 401
    (root / 'data.db').rename(root / 'saved.db')
    empty = client.get(url + '/portfolio', headers=headers)
    assert empty.status_code == 200, (empty.status_code, empty.text)
    assert empty.json()['data']['items'] == []
    assert not (root / 'data.db').exists(), 'first read must not create durable state'
    (root / 'saved.db').rename(root / 'data.db')

    # protects=host runtime opt-out and config validation; fails_when=disabled
    # mounted routes remain callable or invalid values fall back; seam=native REST;
    # no_claim=secondary profile routing or Electron.
    saved_config = config.read_text(encoding='utf-8')
    config.write_text(json.dumps({'plugins': {**PLUGIN_CONFIG, 'entries': {PLUGIN: {
        'settings': {'kanban_sync_enabled': 'invalid'}}}}}), encoding='utf-8')
    invalid_config = client.get(url + '/projects/synthetic/source-tasks', headers=headers)
    assert invalid_config.status_code == 400 and invalid_config.json()['error']['code'] == 'INVALID_CONFIG', invalid_config.text
    config.write_text(json.dumps({'plugins': {**PLUGIN_CONFIG, 'disabled': [PLUGIN]}}), encoding='utf-8')
    assert client.get(url + '/portfolio', headers=headers).status_code == 404
    assert client.get(url + '/portfolio').status_code == 401
    config.write_text(saved_config, encoding='utf-8')
    assert client.get(url + '/portfolio', headers=headers).status_code == 200
    print(json.dumps({'native_rest': 'passed', 'unauthenticated': 401, 'read': 200,
                      'missing': 404, 'invalid_limit': 400, 'data': 'synthetic only',
                      'mounted_from': expected_source.relative_to(package).as_posix()}))


def secondary_child():
    # protects=profile-scoped REST and command replay; fails_when=the secondary
    # host reads or writes launch state/config; seam=real gateway + PluginHost;
    # no_claim=Electron, real boards, installed application, or release.
    from types import SimpleNamespace
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from gateway.runtime_bootstrap import TicketStore
    from gateway.session_authorities import SessionAuthorities
    from hermes_cli import plugins, profiles, web_server as server
    import tui_gateway.launch_profile_policy as launch_policy

    launch = Path(os.environ['HERMES_HOME']).resolve()
    beta = launch / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    roots = [home / 'plugin-data' / PLUGIN for home in (launch, beta)]
    for root in roots:
        root.mkdir(parents=True)

    def config(home, seconds, *, disabled=False):
        document = {'security': {'redact_secrets': False}, 'plugins': {
            **PLUGIN_CONFIG, 'isolation': 'host', 'disabled': [PLUGIN] if disabled else [],
            'entries': {PLUGIN: {'config': {'kanban_sync_seconds': 100},
                                  'settings': {'kanban_sync_seconds': seconds}}}}}
        (home / 'config.yaml').write_text(json.dumps(document), encoding='utf-8')

    config(launch, 120)
    config(beta, 180)
    authorities = SessionAuthorities(launch, multiplexed=True)
    for home in (launch, beta):
        authorities.add(home, SimpleNamespace(profile_id=str(home), instance_id='daemon'))
    tickets = TicketStore('daemon', authorities.profile_ids())
    runner = SimpleNamespace(session_authorities=authorities, session_ticket_store=tickets, _draining=False,
        session_runtime_descriptor={'state': 'ready', 'instance_id': 'daemon',
                                    'served_profiles': authorities.served_profiles()})
    routes, modules, multiplexed = list(server.app.router.routes), set(sys.modules), is_multiplex_active()
    server._dashboard_plugins_cache = None

    async def app(scope, receive, send):
        scope['hermes.gateway_socket_peer'] = ('127.0.0.1', 50000)
        await server.app(scope, receive, send)

    def files(root):
        return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}

    url = '/api/plugins/' + PLUGIN
    owner = {'X-Hermes-Session-Token': server._SESSION_TOKEN}
    ticket = lambda: {'X-Hermes-Gateway-Ticket': tickets.mint(
        profile_id=str(beta), subject='desktop', purpose='native-http')}
    try:
        with patch.object(server.app.state, 'gateway_runner', runner, create=True), \
             patch.object(launch_policy, '_snapshot', None), \
             patch.object(profiles, '_get_default_hermes_home', lambda: launch), \
             patch.object(profiles, '_get_profiles_root', lambda: launch / 'profiles'), TestClient(app) as client:
            # Empty reads must not create any durable plugin stores in either home.
            assert client.get(url + '/portfolio').status_code == 401
            for make_headers in (lambda: owner, ticket):
                response = client.get(url + '/portfolio', headers=make_headers())
                assert response.status_code == 200 and response.json()['data']['items'] == [], response.text
            assert all(files(root) == {} for root in roots)

            # Each request writes only its own home; identical request IDs are independent.
            for home, make_headers, project in ((launch, lambda: owner, 'launch-project'), (beta, ticket, 'beta-project')):
                other = roots[1 if home == launch else 0]
                unchanged = files(other)
                payload = {'request_id': 'same-request', 'operation': 'create_project',
                           'arguments': {'project_id': project, 'title': project}}
                first = client.post(url + '/commands', json=payload, headers=make_headers())
                assert first.status_code == 200, first.text
                replay = client.post(url + '/commands', json=payload, headers=make_headers())
                assert replay.status_code == 200 and replay.json() == first.json(), replay.text
                assert files(other) == unchanged
                rows = client.get(url + '/projects/' + project + '/history', headers=make_headers()).json()['data']['items']
                assert len(rows) == 1 and rows[0]['action'] == 'create_project', rows
                conflict = {**payload, 'arguments': {**payload['arguments'], 'title': 'different'}}
                refused = client.post(url + '/commands', json=conflict, headers=make_headers())
                assert refused.status_code == 409 and refused.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT', refused.text
                rows = client.get(url + '/projects/' + project + '/history', headers=make_headers()).json()['data']['items']
                assert len(rows) == 1 and rows[0]['action'] == 'create_project', rows

            scopes = []
            for make_headers in (lambda: owner, ticket):
                identity = client.post(url + '/identity', headers=make_headers())
                assert identity.status_code == 200, identity.text
                scopes.append(identity.json()['data']['scope_id'])
            assert scopes[0] != scopes[1]
            before = [files(root) for root in roots]
            cross_scope = {'request_id': 'cross-scope', 'operation': 'create_project', 'scope_id': scopes[0],
                           'arguments': {'project_id': 'cross-scope', 'title': 'Cross scope'}}
            denied = client.post(url + '/commands', json=cross_scope, headers=ticket())
            assert denied.status_code == 403 and denied.json()['error']['field'] == 'scope_id', denied.text
            assert [files(root) for root in roots] == before

            for make_headers, own, foreign, seconds in ((lambda: owner, 'launch-project', 'beta-project', 120),
                                                        (ticket, 'beta-project', 'launch-project', 180)):
                response = client.get(url + '/portfolio', headers=make_headers())
                assert [item['project_id'] for item in response.json()['data']['items']] == [own], response.text
                assert client.get(url + '/projects/' + foreign, headers=make_headers()).status_code == 404
                tasks = client.get(url + '/projects/' + own + '/source-tasks', headers=make_headers())
                assert tasks.status_code == 200 and tasks.json()['data']['sync_seconds'] == seconds, tasks.text
                assert client.get(url + '/projects/' + own + '/interventions', headers=make_headers()).status_code == 200
            selected = client.get(url + '/portfolio?profile=beta', headers=owner)
            assert [item['project_id'] for item in selected.json()['data']['items']] == ['beta-project'], selected.text

            before = [files(root) for root in roots]
            secret = {'request_id': 'secret', 'operation': 'create_project',
                      'arguments': {'project_id': 'secret', 'title': 'password=' + 'synthetic-only'}}
            refused = client.post(url + '/commands', json=secret, headers=ticket())
            assert refused.status_code == 400 and refused.json()['error']['field'] == 'secret_text', refused.text
            assert [files(root) for root in roots] == before
            config(beta, 'invalid')
            invalid = client.get(url + '/projects/beta-project/source-tasks', headers=ticket())
            assert invalid.status_code == 400 and invalid.json()['error']['code'] == 'INVALID_CONFIG', invalid.text
            assert client.get(url + '/projects/launch-project/source-tasks', headers=owner).status_code == 200
            assert [files(root) for root in roots] == before
            config(beta, 180, disabled=True)
            assert client.get(url + '/portfolio', headers=ticket()).status_code == 404
            assert client.get(url + '/portfolio?profile=beta', headers=owner).status_code == 404
            assert client.get(url + '/portfolio', headers=owner).status_code == 200
            assert [files(root) for root in roots] == before
            hosts = [getattr(manager, '_plugin_host_instance', None)
                     for manager in plugins._plugin_managers_by_home.values()]
            active = {host.home.resolve(): host for host in hosts if host is not None and host.alive}
            assert {launch, beta} <= set(active), 'both real hosts must run'
            assert len({active[launch].pid, active[beta].pid, os.getpid()}) == 3, 'hosts must be distinct children'
            print(json.dumps({'native_secondary': 'passed', 'launch_sync_seconds': 120,
                              'secondary_sync_seconds': 180, 'replay_audit_rows_per_profile': 1}))
    finally:
        for manager in list(plugins._plugin_managers_by_home.values()):
            host = getattr(manager, '_plugin_host_instance', None)
            if host is not None:
                proc = host._proc
                host.shutdown()
                assert not host.alive and (proc is None or proc.poll() is not None)
        server.app.router.routes[:] = routes
        server._dashboard_plugins_cache = None
        for name in set(sys.modules) - modules:
            if name.startswith('hermes_dashboard_plugin_'):
                del sys.modules[name]
        set_multiplex_active(multiplexed)


def operator_child():
    # protects=operator command safety and reconciliation; fails_when=secrets reach
    # writer, scope changes or replay duplicates audit; seam=native REST plus a
    # writer trap for the refusal; no_claim=Electron or real credentials.
    from inspect import getmodule
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from hermes_cli import web_server as server
    from hermes_cli.web_server_dashboard import _mount_plugin_api_routes
    _mount_plugin_api_routes()
    url = '/api/plugins/' + PLUGIN
    client = TestClient(server.app)
    headers = {'X-Hermes-Session-Token': server._SESSION_TOKEN}
    api = next(getmodule(route.endpoint) for route in server.app.routes
               if getattr(route, 'path', None) == url + '/portfolio')
    assert api is not None
    home = Path(os.environ['HERMES_HOME'])
    config = home / 'config.yaml'
    document = json.loads(config.read_text(encoding='utf-8'))
    document['security'] = {'redact_secrets': False}
    config.write_text(json.dumps(document), encoding='utf-8')
    payload = {'request_id': 'operator-safe', 'operation': 'create_project',
               'arguments': {'project_id': 'operator-project', 'title': 'Operator project'}}
    bad = {**payload, 'request_id': 'operator-secret',
           'arguments': {'project_id': 'operator-secret', 'title': 'password=' + 'synthetic-only'}}
    with patch.object(api._runtime.Runtime, 'open', side_effect=AssertionError('writer reached')):
        refused = client.post(url + '/commands', json=bad, headers=headers)
    assert refused.status_code == 400 and refused.json()['error']['field'] == 'secret_text', refused.text
    wrong = client.post(url + '/commands', json={**payload, 'scope_id': '0' * 32}, headers=headers)
    assert wrong.status_code == 403 and wrong.json()['error']['field'] == 'scope_id', wrong.text
    # protects=host-stop precedence; fails_when=paused host opens a writer;
    # seam=real disposable ESTOP sentinel and native REST; no_claim=fleet state.
    stop = home / 'ESTOP'
    assert not stop.exists()
    stop.touch()
    try:
        with patch.object(api._runtime.Runtime, 'open', side_effect=AssertionError('writer reached')):
            paused = client.post(url + '/commands', json=payload, headers=headers)
        assert paused.status_code == 403 and paused.json()['error']['code'] == 'PAUSED', paused.text
    finally:
        stop.unlink()
    scope = client.get(url + '/identity', headers=headers).json()['data']['scope_id']
    payload['scope_id'] = scope
    first = client.post(url + '/commands', json=payload, headers=headers)
    assert first.status_code == 200, first.text
    repeat = client.post(url + '/commands', json=payload, headers=headers)
    assert repeat.status_code == 200 and repeat.json() == first.json(), repeat.text
    conflict = {**payload, 'arguments': {**payload['arguments'], 'title': 'Different project'}}
    refused_replay = client.post(url + '/commands', json=conflict, headers=headers)
    assert refused_replay.status_code == 409 and refused_replay.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT', refused_replay.text
    history = client.get(url + '/projects/operator-project/history', headers=headers)
    assert history.status_code == 200, history.text
    rows = history.json()['data']['items']
    assert len(rows) == 1 and rows[0]['action'] == 'create_project', rows
    print(json.dumps({'native_operator': 'passed', 'secret_refused_before_writer': True,
                      'scope_refused': True, 'replayed_audit_rows': len(rows)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--operator-child', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--secondary-child', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--installed', action='store_true',
                      help='run from the enabled package in a fresh disposable HERMES_HOME')
    args = parser.parse_args()
    if args.child:
        child()
    elif args.operator_child:
        operator_child()
    elif args.secondary_child:
        secondary_child()
    else:
        with tempfile.TemporaryDirectory(prefix='urgency-rest-') as directory:
            if args.installed and not os.environ.get('HERMES_HOME'):
                parser.error('--installed requires a fresh disposable HERMES_HOME')
            home = Path(os.environ['HERMES_HOME'] if args.installed else directory).resolve()
            target = home / 'plugins' / PLUGIN
            if args.installed:
                if (ROOT != target or not (target / '.git').is_dir()
                        or (home / 'plugin-data' / PLUGIN).exists()):
                    parser.error('--installed requires this script in a fresh installed Git checkout')
            else:
                target.mkdir(parents=True)
                for name in ('__init__.py', 'plugin.yaml'):
                    shutil.copy2(ROOT / name, target / name)
                for name in ('urgency_engine', 'dashboard'):
                    if (ROOT / name).exists():
                        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
                (home / 'config.yaml').write_text(json.dumps({'plugins': PLUGIN_CONFIG}), encoding='utf-8')
            env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_')}
            # Preserve worker fences; this test does not gain operator privileges.
            env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home / 'boards'), HERMES_DISABLE_LAZY_INSTALLS='1',
                       HERMES_RUNTIME_DIR=str(home / 'runtime'), HERMES_KANBAN_TASK='synthetic-worker')
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child'], env=env,
                                    cwd=home, capture_output=True, text=True, timeout=90)
            print(result.stdout)
            if result.returncode:
                print(result.stderr)
                raise SystemExit(result.returncode)
            # Only this disposable child is an operator; never unset the parent fence.
            operator_env = {k: v for k, v in env.items() if k not in (
                'HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB', 'HERMES_DELEGATED_CHILD_CONTEXT')}
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--operator-child'],
                                    env=operator_env, cwd=home, capture_output=True, text=True, timeout=90)
            print(result.stdout)
            if result.returncode:
                print(result.stderr)
                raise SystemExit(result.returncode)
            # A separate disposable launch home keeps the installed fixture untouched.
            launch = Path(directory) / 'multiplex'
            launch.mkdir()
            shutil.copytree(target, launch / 'plugins' / PLUGIN,
                            ignore=shutil.ignore_patterns('__pycache__', '.git'))
            secondary_env = {**operator_env, 'HERMES_HOME': str(launch),
                             'HERMES_KANBAN_HOME': str(launch / 'boards'),
                             'HERMES_RUNTIME_DIR': str(launch / 'runtime')}
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--secondary-child'],
                                    env=secondary_env, cwd=launch, capture_output=True, text=True, timeout=120)
            print(result.stdout)
            if result.returncode:
                print(result.stderr)
            raise SystemExit(result.returncode)

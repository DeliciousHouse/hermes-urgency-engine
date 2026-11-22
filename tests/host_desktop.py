"""Real host REST mount test (run with a provisioned Hermes interpreter).

protects=authenticated scoped read and safe errors; fails_when=auth leaks or mount fails;
why_new=new Desktop HTTP boundary; seam=ASGI transport only;
no_claim=actual Electron interaction, remote routing or public installation.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = 'hermes-urgency-engine'


def child():
    from datetime import datetime, timezone
    import sqlite3
    from fastapi.testclient import TestClient
    from hermes_constants import get_hermes_home
    sys.path.insert(0, str(ROOT))
    from urgency_engine.ledger import Ledger
    root = get_hermes_home() / 'plugin-data' / PLUGIN
    root.mkdir(parents=True)
    with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda:datetime(2030, 1, 1, tzinfo=timezone.utc), artifact_root=root) as ledger:
        ledger.create_project('synthetic', 'Synthetic project', request_id='create', actor='operator')
    from hermes_cli import web_server as server
    from hermes_cli.web_server_dashboard import _mount_plugin_api_routes
    _mount_plugin_api_routes()
    client = TestClient(server.app)
    url = '/api/plugins/' + PLUGIN
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
    from yaml import safe_dump, safe_load
    binding = dict(board='board', tenant='synthetic', project_id='synthetic')
    source_settings = dict(kanban_sync_enabled=True, kanban_bindings=[binding])
    config = get_hermes_home() / 'config.yaml'
    values = safe_load(config.read_text(encoding='utf-8'))
    values['plugins']['entries'] = {PLUGIN: {'settings': source_settings}}
    config.write_text(safe_dump(values), encoding='utf-8')

    class SyntheticSource:
        def inventory(self, binding):
            return [dict(id=task, tenant=binding['tenant']) for task in ('prepare', 'task')]

        def detail(self, binding, task_id):
            return dict(task=dict(id=task_id, tenant=binding['tenant'], title=task_id, status='todo'),
                        parents=['prepare'] if task_id == 'task' else [])

    def now():
        return datetime.now(timezone.utc)

    with Ledger(sqlite3.connect(root / 'data.db'), clock=now, artifact_root=root) as ledger, \
            Sync(sqlite3.connect(root / 'kanban.db'), clock=now, redact=str) as sync:
        assert sync.scan(settings(source_settings), ledger, None, root / 'data.db', SyntheticSource(),
                         request_id='source-scan', reason='Synthetic graph')['state'] == 'complete'
        for task, duration in [('prepare', 1800), ('task', 3600)]:
            ledger.upsert_task('board', task, project_id='synthetic', goal=task, remaining_seconds=duration,
                               expected_revision=ledger.get_task('board', task)['revision'],
                               reason='Estimate', request_id='estimate-' + task, actor='operator')
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
        malformed = client.post(url + '/commands', content=raw, headers=headers)
        assert malformed.status_code == 400, (malformed.status_code, malformed.text)
    assert client.post(url + '/commands', json={}).status_code == 401
    (root / 'data.db').rename(root / 'saved.db')
    empty = client.get(url + '/portfolio', headers=headers)
    assert empty.status_code == 200, (empty.status_code, empty.text)
    assert empty.json()['data']['items'] == []
    assert not (root / 'data.db').exists(), 'first read must not create durable state'
    (root / 'saved.db').rename(root / 'data.db')
    print(json.dumps({'native_rest': 'passed', 'unauthenticated': 401, 'read': 200,
                      'missing': 404, 'invalid_limit': 400, 'data': 'synthetic only'}))


if __name__ == '__main__':
    if '--child' in sys.argv:
        child()
    else:
        with tempfile.TemporaryDirectory(prefix='urgency-rest-') as directory:
            home = Path(directory)
            target = home / 'plugins' / PLUGIN
            target.mkdir(parents=True)
            for name in ('__init__.py', 'plugin.yaml'):
                shutil.copy2(ROOT / name, target / name)
            for name in ('urgency_engine', 'dashboard'):
                if (ROOT / name).exists():
                    shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
            (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-urgency-engine]\n  isolation: in_process\n', encoding='utf-8')
            env = dict(os.environ)
            # Preserve worker fences; this test does not gain operator privileges.
            env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home / 'boards'), HERMES_DISABLE_LAZY_INSTALLS='1',
                       HERMES_KANBAN_TASK='synthetic-worker')
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child'], env=env,
                                    capture_output=True, text=True, timeout=90)
            print(result.stdout)
            if result.returncode:
                print(result.stderr)
            raise SystemExit(result.returncode)

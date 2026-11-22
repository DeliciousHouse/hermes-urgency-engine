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
    denied_command = client.post(url + '/commands', json={'request_id': 'rename', 'operation': 'edit_project',
        'arguments': {'project_id': 'synthetic', 'title': 'Changed', 'expected_revision': 1, 'reason': 'Edit'}}, headers=headers)
    assert denied_command.status_code == 403, (denied_command.status_code, denied_command.text)
    assert client.get(url + '/projects/synthetic', headers=headers).json()['data']['title'] == 'Synthetic project'
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

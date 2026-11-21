"""Native canonical sync journey on a disposable installed-plugin home.

protects=external native edits reach the existing no-agent cron and scoped reads;
fails_when=only synthetic transport works; why_new=canonical integration;
seam=disposable home, real CLI; no_claim=Desktop, workers, remote delivery or recovery.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = 'hermes-urgency-engine'


def journey(home, host=None):
    assert (home / 'config.yaml').is_file()
    assert not (home / 'plugin-data' / PLUGIN).exists(), 'use a clean disposable installation'
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('HERMES_', 'GIT_')) and k not in ('PYTHONPATH', 'VIRTUAL_ENV')}
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home / 'kanban'),
               HERMES_RUNTIME_DIR=os.environ.get('URGENCY_TEST_RUNTIME', str(home / 'runtime')),
               HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONUTF8='1')
    if host is not None:
        # Match host_runtime's explicit source-checkout selection for local proof.
        env['PYTHONPATH'] = str(host)
    cli = [sys.executable, '-m', 'hermes_cli.main']

    def run(*args):
        result = subprocess.run([*cli, *args], cwd=home, env=env,
                                capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
        return result.stdout

    def urgency(*args):
        reply = json.loads(run('urgency', *args))
        assert reply['ok'], reply
        return reply['data']

    # The public CLI bootstraps host dependencies; the driver stays stdlib-only.
    # Set only synthetic settings, retaining the installer's enable state.
    config_path = home / 'config.yaml'
    prefix = f'plugins.entries.{PLUGIN}.settings'
    run('config', 'set', f'{prefix}.kanban_sync_enabled', 'true')
    run('config', 'set', f'{prefix}.kanban_bindings', json.dumps([
        dict(board='synthetic', tenant='demo', project_id='demo')]))
    assert urgency('kanban', 'health')['storage'] == 'uninitialized'
    assert urgency('kanban', 'tasks', '--project', 'demo')['items'] == []
    assert not (home / 'plugin-data' / PLUGIN).exists()

    tutorial, = re.findall(r'^```python\n(.*?)^```', (ROOT / 'docs/kanban.md').read_text(), re.M | re.S)
    tutorial = tutorial.replace("['hermes', *args]", "[sys.executable, '-m', 'hermes_cli.main', *args]")
    result = subprocess.run([sys.executable, '-c', 'import sys\n' + tutorial], cwd=home,
                            env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout, result.stderr)
    task, = urgency('kanban', 'tasks', '--project', 'demo')['items']
    task_id = task['task_id']
    assert task['title'] == 'Externally edited' and task['worker']['heartbeat_at'] is None
    run('kanban', 'boards', 'create', 'foreign')
    foreign = json.loads(run('kanban', '--board', 'foreign', 'create',
                            'Foreign task', '--tenant', 'demo', '--json'))
    assert foreign['id'] not in {t['task_id'] for t in urgency('kanban', 'tasks', '--project', 'demo')['items']}
    # Preserve native worker board fencing; no worker process is dispatched.
    denied = subprocess.run([*cli, 'kanban', '--board', 'foreign', 'show', foreign['id'], '--json'],
        cwd=home, env={**env, 'HERMES_KANBAN_TASK': task_id, 'HERMES_KANBAN_BOARD': 'synthetic',
             'HERMES_KANBAN_DB': str(home / 'kanban' / 'boards' / 'synthetic' / 'kanban.db')},
        capture_output=True, text=True, timeout=120)
    assert denied.returncode != 0 and 'Foreign task' not in denied.stdout, (denied.returncode, denied.stdout, denied.stderr)

    parent = json.loads(run('kanban', '--board', 'synthetic', 'create',
                            'Synthetic parent', '--tenant', 'demo', '--json'))
    run('kanban', '--board', 'synthetic', 'link', parent['id'], task_id)
    receipt = urgency('watchdog', 'install')
    assert receipt['no_agent'] is True
    assert urgency('watchdog', 'install')['job_id'] == receipt['job_id']
    initial = urgency('kanban', 'health')['last_success']
    run('kanban', '--board', 'synthetic', 'edit', task_id, '--title', 'External cron update')
    # Claim a disposable assigned task via the native CLI, without dispatching.
    board = ('kanban', '--board', 'synthetic')
    worker_id = json.loads(run(*board, 'create', 'Synthetic assigned task',
                               '--tenant', 'demo', '--assignee', 'fixture', '--json'))['id']
    run(*board, 'list', '--tenant', 'demo', '--json')
    run(*board, 'claim', worker_id)
    run(*board, 'heartbeat', worker_id)
    native = json.loads(run(*board, 'show', worker_id, '--json'))
    run_id = native['task'].get('current_run_id')
    claim_at = heartbeat_at = None
    if run_id is not None:
        assert type(run_id) is int and run_id > 0
        claimed, = [r for r in native['runs'] if r['id'] == run_id]
        heartbeat, = [e for e in native['events'] if e['kind'] == 'heartbeat' and e['run_id'] == run_id]
        assert claimed['started_at'] == native['task']['started_at']
        claim_at = datetime.fromtimestamp(claimed['started_at'], timezone.utc).isoformat()
        heartbeat_at = datetime.fromtimestamp(heartbeat['created_at'], timezone.utc).isoformat()
        assert claimed['profile'] == 'fixture' and claimed['status'] == 'running'
        expected_worker = dict(run_id=run_id, state='running', started_at=claim_at,
                               heartbeat_at=heartbeat_at, ended_at=None)
    else:
        # Historical runs/heartbeats cannot substitute for the current-run pointer.
        expected_worker = dict(run_id=None, state=None, started_at=None, heartbeat_at=None, ended_at=None)
    # Naturally due public scheduler, not force or a replaced clock.
    time.sleep(61)
    run('cron', 'tick')
    health = urgency('kanban', 'health')
    assert health['last_success'] > initial, health
    assert urgency('task', 'synthetic', task_id)['goal'] == 'External cron update'
    assert urgency('watchdog', 'health')['last_success']
    projections = {t['task_id']: t for t in urgency('kanban', 'tasks', '--project', 'demo')['items']}
    assert projections[task_id]['parents'] == [parent['id']]
    assert projections[worker_id]['assignee'] == 'fixture'
    assert projections[worker_id]['worker'] == expected_worker
    worker_task = urgency('task', 'synthetic', worker_id)
    assert worker_task['assignee'] == 'fixture' and worker_task['status'] == 'running'
    facts = [o for o in urgency('watchdog', 'export', '--table', 'observations', '--project', 'demo')['items']
             if o['board_id'] == 'synthetic' and o['task_id'] == worker_id and o['source'] == 'kanban-sync']
    if run_id is not None:
        signal, = [o for o in facts if o['kind'] == 'task_signal' and 'heartbeat_at' in o['payload']['data']]
        assert signal['payload']['data'] == dict(claim_at=claim_at, heartbeat_at=heartbeat_at, agent_available=True)
        outcome, = [o for o in facts if o['kind'] == 'worker_outcome']
        assert outcome['occurred_at'] == heartbeat_at and outcome['payload']['data'] == dict(state='running')
    else:
        assert not [o for o in facts if o['kind'] == 'worker_outcome']
        assert all(all(o['payload']['data'].get(key) is None
                       for key in ('claim_at', 'heartbeat_at', 'agent_available'))
                   for o in facts if o['kind'] == 'task_signal')
    run('kanban', '--board', 'synthetic', 'complete', parent['id'], '--summary', 'Synthetic parent claim')
    run('kanban', '--board', 'synthetic', 'complete', task_id, '--summary', 'Synthetic claim only')
    urgency('kanban', 'sync', '--request-id', 'claimed', '--reason', 'fixture')
    assert urgency('task', 'synthetic', task_id)['status'] == 'claimed_complete'
    assert urgency('status', '--project', 'demo')['items'][0]['progress']['percent'] is None

    probe = '''
import json
from hermes_cli.plugins import discover_plugins, get_plugin_command_handler, invoke_hook
from hermes_cli.plugin_command_dispatch import invoke_plugin_command
from tools.registry import registry
discover_plugins()
handler = get_plugin_command_handler('urgency')
def slash(text):
    return json.loads(invoke_plugin_command(handler, text, session_id='synthetic', source='cli'))
assert slash('kanban tasks --project demo')['data']['items']
assert not slash('kanban sync --request-id forbidden --reason forbidden')['ok']
assert not json.loads(registry.dispatch('urgency_query', {'operation': 'kanban_sync', 'arguments': {}}))['ok']
assert slash('focus demo')['ok']
text = '\\n'.join(r.get('context', '') for r in invoke_hook('pre_llm_call', session_id='synthetic', user_message='', is_first_turn=False) if isinstance(r, dict))
assert 'Canonical task:' in text, text
assert 'Foreign task' not in text
'''
    before = {p.name: p.read_bytes() for p in (home / 'plugin-data' / PLUGIN).glob('*.db')}
    result = subprocess.run([sys.executable, '-c', probe], cwd=home, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert before == {p.name: p.read_bytes() for p in (home / 'plugin-data' / PLUGIN).glob('*.db')}
    assert urgency('kanban', 'pause', '--request-id', 'pause', '--reason', 'fixture')['paused']
    run('kanban', '--board', 'synthetic', 'edit', task_id, '--title', 'Paused update')
    assert urgency('kanban', 'sync', '--request-id', 'paused', '--reason', 'fixture')['state'] == 'paused'
    assert urgency('task', 'synthetic', task_id)['goal'] == 'External cron update'
    assert not urgency('kanban', 'resume', '--request-id', 'resume', '--reason', 'fixture')['paused']
    urgency('kanban', 'sync', '--request-id', 'resumed', '--reason', 'fixture')
    assert urgency('task', 'synthetic', task_id)['goal'] == 'Paused update'

    # A fresh second profile has the plugin/settings but no projected records.
    with tempfile.TemporaryDirectory(prefix='urgency-kanban-other-') as folder:
        other = Path(folder)
        shutil.copytree(home / 'plugins', other / 'plugins')
        shutil.copy2(config_path, other / 'config.yaml')
        result = subprocess.run([*cli, 'urgency', 'kanban', 'tasks', '--project', 'demo'], cwd=other,
            env={**env, 'HERMES_HOME': str(other), 'HERMES_KANBAN_HOME': str(other / 'kanban')},
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['data']['items'] == []
        assert not (other / 'plugin-data' / PLUGIN).exists()
    assert urgency('watchdog', 'uninstall')['retained_data']
    assert receipt['job_id'] not in run('cron', 'list')
    assert (home / 'plugin-data' / PLUGIN / 'kanban.db').is_file()
    print(json.dumps(dict(native_kanban='passed', scheduler='naturally due public cron tick',
                         tutorial='executed', isolation='two homes and boards',
                         last_success=health['last_success'], assigned_run_admitted=run_id is not None,
                         worker_capability='current_run' if run_id is not None else 'unknown_missing_current_run_id',
                         current_run_pointer_present='current_run_id' in native['task'], worker_dispatch=False)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed-home', type=Path, required=True)
    parser.add_argument('--hermes-root', type=Path)
    args = parser.parse_args()
    journey(args.installed_home.resolve(), args.hermes_root.resolve() if args.hermes_root else None)

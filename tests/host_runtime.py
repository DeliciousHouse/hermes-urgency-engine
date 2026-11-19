"""Real host integration (not unit discovery): run with a provisioned Hermes Python.

protects=native loading, transports, lifecycle and profile boundaries;
fails_when=registration is cosmetic or commands/hooks drift; seam=none;
no_claim=Desktop, LLM compliance, semantic acceptance or external control.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

PLUGIN = 'hermes-urgency-engine'
ROOT = Path(__file__).resolve().parents[1]


def probe():
    from hermes_cli.plugins import discover_plugins, get_plugin_command_handler, invoke_hook
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    from tools.registry import registry
    discover_plugins()
    handler = get_plugin_command_handler('urgency')
    if os.environ.get('URGENCY_EXPECT_DISABLED') == '1':
        assert handler is None
        assert not any(isinstance(r, dict) and 'Urgency planning' in r.get('context', '')
                       for r in invoke_hook('pre_llm_call', session_id='a', user_message='', is_first_turn=False))
        print('disabled contributions absent')
        return
    assert handler is not None
    def slash(text, session='a'):
        return json.loads(invoke_plugin_command(handler, text, session_id=session, source='cli'))
    assert slash('focus demo')['ok']
    assert slash('focus demo', None)['error']['code'] == 'SESSION_REQUIRED'
    def context(session):
        return '\n'.join(r['context'] for r in invoke_hook('pre_llm_call', session_id=session,
                         user_message='untrusted prompt', is_first_turn=False)
                         if isinstance(r, dict) and 'context' in r)
    assert 'Synthetic objective' in context('a')
    assert 'Synthetic objective' not in context('b')
    assert 'artifact_byte_identity' in context('a')
    assert not slash('apply create_project')['ok']
    read = json.loads(registry.dispatch('urgency_query', {'operation': 'status', 'arguments': {'project_id': 'demo'}}))
    assert read['ok'], read
    denied = json.loads(registry.dispatch('urgency_query', {'operation': 'create_project', 'arguments': {}}))
    assert denied['error']['code'] == 'READ_ONLY', denied
    invoke_hook('on_session_reset', old_session_id='a', session_id='new')
    assert 'Synthetic objective' not in context('a')
    assert slash('focus demo')['ok']
    invoke_hook('on_session_end', session_id='a')
    assert 'Synthetic objective' not in context('a')
    assert slash('focus demo')['ok']
    from plugins.plugin_storage import plugin_data_dir
    db = plugin_data_dir(PLUGIN) / 'data.db'
    lock = sqlite3.connect(db)
    lock.execute('BEGIN EXCLUSIVE')
    started = time.monotonic()
    result = context('a')
    elapsed = time.monotonic() - started
    lock.rollback()
    lock.close()
    # gstack-timing-ok: explicit acceptance budget under a real held SQLite write lock.
    assert elapsed < 1, elapsed
    assert 'unavailable' in result, result
    print(json.dumps({'native_dispatch': 'passed', 'lock_seconds': elapsed}))


def journey(host, home, installed=False):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('HERMES_', 'GIT_')) or key in ('VIRTUAL_ENV', 'PYTHONPATH'):
            env.pop(key)
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(host), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    # Retain only an explicitly supplied disposable package-manager store.
    if os.environ.get('URGENCY_TEST_RUNTIME'):
        env['HERMES_RUNTIME_DIR'] = os.environ['URGENCY_TEST_RUNTIME']
    def run(args, code=0, probe_mode=False, extra=None):
        command = [sys.executable, str(Path(__file__).resolve()), '--probe'] if probe_mode else [sys.executable, str(host / 'hermes'), *args]
        result = subprocess.run(command, env={**env, **(extra or {})}, cwd=home,
                                capture_output=True, text=True, timeout=600)
        assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
        return result
    if not installed:
        target = home / 'plugins' / PLUGIN
        target.mkdir(parents=True)
        for name in ('__init__.py', 'plugin.yaml', 'pyproject.toml', 'README.md', 'LICENSE'):
            shutil.copy2(ROOT / name, target / name)
        shutil.copytree(ROOT / 'urgency_engine', target / 'urgency_engine', ignore=shutil.ignore_patterns('__pycache__'))
        # Synthetic test config only; installed-public mode uses installer-produced config.
        (home / 'config.yaml').write_text('plugins:\n  enabled:\n    - hermes-urgency-engine\n', encoding='utf-8')
    for args in (['urgency', '--help'], ['urgency', 'apply', '--help']):
        assert 'apply' in run(args).stdout
    assert 'apply' in run(['urgency'], 2).stdout
    bad = run(['urgency', 'status', '--limit', 'PRIVATE_MARKER'], 2)
    assert not bad.stdout
    assert bad.stderr.lstrip().startswith('{'), bad.stderr
    assert json.loads(bad.stderr)['error']['code'] == 'INVALID_INPUT'
    assert 'PRIVATE_MARKER' not in bad.stderr
    def apply(operation, data, code=0):
        payload = home / 'input.json'
        payload.write_text(json.dumps(data), encoding='utf-8')
        result = run(['urgency', 'apply', operation, '--json-file', str(payload)], code)
        return json.loads(result.stdout if code == 0 else result.stderr)
    project = dict(project_id='demo', title='Synthetic objective', request_id='create')
    created = apply('create_project', project)
    assert created == apply('create_project', project)
    assert apply('create_project', {**project, 'title': 'Changed'}, 1)['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert apply('create_project', {**project, 'actor': 'forged'}, 1)['error']['code'] == 'INVALID_INPUT'
    task = dict(project_id='demo', board_id='board', task_id='task', goal='Synthetic task', expected_revision=0, reason='Plan',
                remaining_seconds=600, verification_seconds=60, deployment_seconds=30, request_id='task')
    apply('upsert_task', task)
    apply('set_criteria', dict(board_id='board', task_id='task', criteria=[{'criterion_id': 'proof', 'description': 'Byte check', 'weight': 1, 'required': True, 'expected_sha256': hashlib.sha256(b'synthetic proof\n').hexdigest()}], expected_revision=1, reason='Scope', request_id='criteria'))
    revision = json.loads(run(['urgency', 'status', '--project', 'demo']).stdout)['data']['items'][0]['revision']
    apply('baseline_scope', dict(project_id='demo', expected_revision=revision, reason='Baseline', request_id='baseline'))
    for kind in ('hard', 'soft'):
        apply('set_deadline', dict(deadline_id=kind, project_id='demo', kind=kind, at='2035-01-01T00:00:00Z', timezone='UTC', owner='operator', source='fixture', request_id=kind))
    before = json.loads(run(['urgency', 'explain', 'hard']).stdout)['data']
    apply('claim_complete', dict(board_id='board', task_id='task', expected_revision=2, reason='Claim', request_id='claim'))
    status = json.loads(run(['urgency', 'status', '--project', 'demo']).stdout)['data']['items'][0]
    assert status['progress']['percent'] == 0
    assert status['evidence_kind'] == 'artifact_byte_identity'
    apply('upsert_task', {**task, 'remaining_seconds': 5, 'expected_revision': 1, 'request_id': 'stale'}, 1)
    artifact = home / 'plugin-data' / PLUGIN / 'artifacts' / 'proof.txt'
    artifact.write_bytes(b'synthetic proof\n')
    apply('verify_artifact', dict(board_id='board', task_id='task', criterion_id='proof', artifact_relative_path='proof.txt',
          expected_revision=3, reason='Check bytes', request_id='verify'))
    after = json.loads(run(['urgency', 'explain', 'hard']).stdout)['data']
    assert before['original_at'] == after['original_at'] and before['target_at'] == after['target_at']
    for payload in (None, [], {'unknown': True}, {'objective': float('nan')}, {'objective': '\ud800'}, {'objective': 'x' * 262145}):
        assert not apply('create_project', payload, 1)['ok']
    print(run([], probe_mode=True).stdout.strip())
    # A fresh process still observes data; a different profile does not.
    assert json.loads(run(['urgency', 'status']).stdout)['data']['items']
    other = home / 'other-home'
    other.mkdir()
    shutil.copytree(home / 'plugins', other / 'plugins')
    shutil.copy2(home / 'config.yaml', other / 'config.yaml')
    isolated = run(['urgency', 'status'], extra={'HERMES_HOME': str(other)})
    assert json.loads(isolated.stdout)['data']['items'] == []
    run(['plugins', 'disable', PLUGIN])
    print(run([], probe_mode=True, extra={'URGENCY_EXPECT_DISABLED': '1'}).stdout.strip())
    run(['plugins', 'enable', PLUGIN])
    assert json.loads(run(['urgency', 'status']).stdout)['data']['items'][0]['progress']['percent'] == 100
    print('native CLI journey, fresh process, two homes, disable/re-enable: passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-root', type=Path)
    parser.add_argument('--installed-home', type=Path)
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    if args.probe:
        probe()
    else:
        assert args.hermes_root and (args.hermes_root / 'hermes').is_file()
        if args.installed_home:
            journey(args.hermes_root.resolve(), args.installed_home.resolve(), installed=True)
        else:
            with tempfile.TemporaryDirectory(prefix='urgency-host-') as directory:
                journey(args.hermes_root.resolve(), Path(directory))

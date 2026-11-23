"""Released-host watchdog journey, using only the installed public CLI.

protects=independent scheduling and profile confinement; fails_when=no durable tick;
why_new=no-agent monitor; seam=disposable home; no_claim=remote delivery or Desktop.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time


def external_liveness(host, installed, scratch):
    probe = installed / 'scripts' / 'gateway_liveness.py'
    home = scratch / 'home'
    home.mkdir()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = str(listener.getsockname()[1])
    (home / 'config.yaml').write_text(json.dumps({
        'kanban': {'dispatch_in_gateway': False},
        'gateway': {'multiplex_profiles': False},
    }), encoding='utf-8')
    (home / '.env').write_text(
        'API_SERVER_ENABLED=true\nAPI_SERVER_HOST=127.0.0.1\n'
        f'API_SERVER_PORT={port}\nAPI_SERVER_KEY={secrets.token_hex(32)}\n', encoding='utf-8')
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'SYSTEMROOT', 'WINDIR') if k in os.environ}
    env.update(HOME=str(scratch), USERPROFILE=str(scratch), XDG_CONFIG_HOME=str(scratch / 'xdg'),
               HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(scratch / 'runtime'),
               HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONUTF8='1')

    def sample():
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-I', str(probe), port], env=env, cwd=scratch,
                                capture_output=True, text=True, timeout=5)
        elapsed = time.monotonic() - started
        assert result.returncode in (0, 1), result.returncode
        assert result.stdout == '' and result.stderr == (
            '' if result.returncode == 0 else 'Gateway health check failed.\n')
        return {'exit': result.returncode, 'seconds': round(elapsed, 3)}

    def while_running(label):
        with (scratch / f'{label}.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-m', 'hermes_cli.main', 'gateway', 'run'],
                                       cwd=host, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 45
                while process.poll() is None and time.monotonic() < deadline:
                    receipt = sample()
                    if receipt['exit'] == 0:
                        return receipt
                    time.sleep(0.25)
                raise AssertionError(f'Gateway did not become healthy: {label}, exit={process.poll()}')
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        raise

    up = while_running('up')
    stopped = sample()
    assert stopped['exit'] == 1, stopped
    restored = while_running('restored')
    print(json.dumps({'gateway_liveness': {'up': up, 'stopped': stopped, 'restored': restored},
                      'systemd_timer': 'not run by native host journey'}), flush=True)


def journey(host, home):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('HERMES_', 'GIT_')) and k not in ('PYTHONPATH', 'VIRTUAL_ENV')}
    env.update(HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONUTF8='1')
    if os.environ.get('URGENCY_TEST_RUNTIME'):
        env['HERMES_RUNTIME_DIR'] = os.environ['URGENCY_TEST_RUNTIME']
    # The supplied interpreter belongs to the released host environment. Its normal
    # module entry point is exercised without adding the plugin to sys.path.
    cli = [sys.executable, '-m', 'hermes_cli.main']

    def run(*args, code=0):
        result = subprocess.run([*cli, *args], cwd=host, env=env, capture_output=True,
                                text=True, timeout=120)
        assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
        return result

    def command(*args):
        result = run('urgency', 'watchdog', *args)
        data = json.loads(result.stdout)
        assert data['ok'], data
        return data['data']

    def apply(operation, data):
        payload = home / 'watchdog-fixture.json'
        payload.write_text(json.dumps(data), encoding='utf-8')
        result = run('urgency', 'apply', operation, '--json-file', str(payload))
        assert json.loads(result.stdout)['ok']

    store = home / 'plugin-data' / 'hermes-urgency-engine'
    assert command('health')['storage'] == 'uninitialized'
    assert command('incidents')['items'] == []
    assert command('plans')['items'] == []
    assert not (store / 'watchdog.db').exists()
    assert not (home / 'scripts' / 'hermes-urgency-engine-watchdog.py').exists()
    assert 'hermes-urgency-engine-watchdog' not in run('cron', 'list').stdout
    assert not list(Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
                    .glob('systemd/user/urgency-gateway-liveness.*'))
    with tempfile.TemporaryDirectory(dir=home.parent, prefix='liveness-') as scratch:
        external_liveness(host, home / 'plugins' / 'hermes-urgency-engine', Path(scratch))
    apply('create_project', dict(project_id='watchdog-demo', title='Synthetic watchdog objective', request_id='watchdog-project'))
    apply('upsert_task', dict(board_id='watchdog-board', task_id='task', project_id='watchdog-demo',
                             goal='Deliver proof', status='running', remaining_seconds=600,
                             expected_revision=0, reason='Synthetic fixture', request_id='watchdog-task'))
    unrelated = home / 'scripts' / 'unrelated.py'
    unrelated.parent.mkdir(exist_ok=True)
    unrelated.write_text('print("unrelated fixture")\n', encoding='utf-8')
    run('cron', 'create', '--help')
    run('cron', 'create', 'every 1d', '--name', 'unrelated-fixture',
                        '--script', 'unrelated.py', '--no-agent', '--deliver', 'local')
    assert 'unrelated-fixture' in run('cron', 'list').stdout
    receipt = command('install')
    assert receipt['state'] == 'installed' and receipt['no_agent'] is True
    assert command('install')['job_id'] == receipt['job_id']
    probe = '''
import json
from hermes_cli.plugins import discover_plugins, get_plugin_command_handler
from hermes_cli.plugin_command_dispatch import invoke_plugin_command
from model_tools import handle_function_call
discover_plugins()
handler = get_plugin_command_handler('urgency')
assert json.loads(invoke_plugin_command(handler, 'focus watchdog-demo', session_id='synthetic-session', source='cli'))['ok']
result = handle_function_call('urgency_query', {'operation': 'status', 'arguments': {}},
                              task_id='not-a-board-task', session_id='synthetic-session',
                              tool_call_id='synthetic-tool-call', enabled_tools=['urgency_query'])
assert json.loads(result)['ok'], result
'''
    executed = subprocess.run([sys.executable, '-c', probe], cwd=host, env=env,
                             capture_output=True, text=True, timeout=120)
    assert executed.returncode == 0, (executed.stdout, executed.stderr)
    observed = command('export', '--table', 'observations')['items']
    assert any(x['source'] == 'host-post-tool-call' and x['task_id'] is None and
               x['payload']['data']['outcome'] == 'success' for x in observed), observed
    launcher = Path(receipt['script'])
    original = launcher.read_bytes()
    event = dict(request_id='watchdog-signal', reason='Synthetic unavailable worker',
                 project_id='watchdog-demo', board_id='watchdog-board', task_id='task',
                 kind='worker_outcome', occurred_at=datetime.now(timezone.utc).isoformat(),
                 data={'state': 'unavailable'})
    payload = home / 'watchdog-observation.json'
    payload.write_text(json.dumps(event), encoding='utf-8')
    command('observe', '--json-file', str(payload))
    # Wait until naturally due, then execute the actual scheduler scan. This is
    # not a direct tick call, mocked scheduler, resident gateway or provider run.
    time.sleep(61)
    scan = run('cron', 'tick')
    health = command('health')
    assert health['last_success'], (scan.stdout, scan.stderr, health)
    incidents = command('incidents')['items']
    assert any(i['detector'] == 'delegated_work_unavailable' for i in incidents), incidents
    plans = command('plans')['items']
    assert plans and all(p['state'] == 'recommended' for p in plans)
    assert run('urgency', 'watchdog', 'tick').stdout == ''
    command('pause', '--request-id', 'watchdog-pause', '--reason', 'Lifecycle test')
    paused_at = command('health')['last_success']
    run('cron', 'run', receipt['job_id'])
    assert command('health')['last_success'] == paused_at
    command('resume', '--request-id', 'watchdog-resume', '--reason', 'Lifecycle test')
    run('cron', 'run', receipt['job_id'])
    assert command('health')['last_success'] != paused_at
    run('plugins', 'disable', 'hermes-urgency-engine')
    before = (store / 'watchdog.db').read_bytes()
    run('cron', 'run', receipt['job_id'])
    assert (store / 'watchdog.db').read_bytes() == before
    run('plugins', 'enable', 'hermes-urgency-engine')
    assert command('health')['installed']['job_id'] == receipt['job_id']
    launcher.write_bytes(original + b'\n# operator edit\n')
    conflict = run('urgency', 'watchdog', 'uninstall', code=1)
    assert 'INSTALL_CONFLICT' in conflict.stderr and launcher.exists()
    launcher.write_bytes(original)
    result = command('uninstall')
    assert result['retained_data'] and not launcher.exists()
    assert (store / 'watchdog.db').is_file() and (store / 'data.db').is_file()
    assert unrelated.read_text(encoding='utf-8') == 'print("unrelated fixture")\n'
    listing = run('cron', 'list').stdout
    assert receipt['job_id'] not in listing and 'unrelated-fixture' in listing
    print(json.dumps({'watchdog_native': 'passed', 'scheduler': 'naturally due public cron tick',
                      'last_success': health['last_success'], 'incidents': len(incidents),
                      'plans': len(plans), 'delivery': 'not verified'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-root', type=Path, required=True)
    parser.add_argument('--installed-home', type=Path, required=True)
    args = parser.parse_args()
    journey(args.hermes_root.resolve(), args.installed_home.resolve())

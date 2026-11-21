"""protects=installed CLI effects and readback; fails_when=only checkout works;
why_new=supervised commands; seam=disposable native host; no_claim=worker dispatch.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = 'hermes-urgency-engine'


def journey(home, host=None):
    assert (home / 'plugins' / PLUGIN / 'plugin.yaml').is_file()
    assert (home / 'config.yaml').is_file()
    assert not (home / 'plugin-data' / PLUGIN).exists()
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT', 'OS'}}
    env.update({key: str(home) for key in ('HOME', 'HERMES_HOME', 'USERPROFILE', 'APPDATA',
        'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'TMPDIR', 'TMP', 'TEMP')})
    env.update(HERMES_RUNTIME_DIR=os.environ.get('URGENCY_TEST_RUNTIME', str(home / 'runtime')),
               HERMES_KANBAN_HOME=str(home / 'kanban'), HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONUTF8='1')
    env['PYTHONPATH'] = str(host) if host else os.environ['PYTHONPATH']
    cli = [sys.executable, '-m', 'hermes_cli.main']

    def run(*args):
        result = subprocess.run([*cli, *args], env=env, cwd=home, capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, (args, result.returncode, result.stdout, result.stderr)
        return result.stdout

    prefix = f'plugins.entries.{PLUGIN}.settings.'
    run('config', 'set', prefix + 'kanban_sync_enabled', 'true')
    run('config', 'set', prefix + 'kanban_bindings', json.dumps([
        dict(board='synthetic', tenant='demo', project_id='demo')]))
    for verb in ('list', 'show', 'apply', 'reconcile', 'pause', 'resume'):
        assert verb in run('urgency', 'interventions', verb, '--help')
    for name in ('kanban', 'interventions'):
        tutorial, = re.findall(r'^```python\n(.*?)^```', (ROOT / 'docs' / f'{name}.md').read_text(encoding='utf-8'), re.M | re.S)
        tutorial = tutorial.replace("['hermes',", "[sys.executable, '-m', 'hermes_cli.main',")
        result = subprocess.run([sys.executable, '-c', 'import sys\n' + tutorial], cwd=home, env=env,
                                capture_output=True, text=True, timeout=360)
        assert result.returncode == 0, (name, result.returncode, result.stdout, result.stderr)
    after = json.loads(run('kanban', '--board', 'synthetic', 'list', '--tenant', 'demo', '--archived', '--json'))
    assert len(after) == 1 and after[0]['priority'] == 7
    print(json.dumps(dict(interventions='passed', native_cards=len(after), effects=['priority', 'estimate', 'soft_target'],
                          recovery='refused', workers_launched=0)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed-home', type=Path, required=True)
    parser.add_argument('--hermes-root', type=Path)
    args = parser.parse_args()
    journey(args.installed_home.resolve(), args.hermes_root.resolve() if args.hermes_root else None)

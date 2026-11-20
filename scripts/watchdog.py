"""Owned no-agent cron launcher. No plugin imports or absolute source paths."""
import json
import subprocess
import sys


def main():
    command = [sys.executable, '-m', 'hermes_cli.main']
    try:
        result = subprocess.run(command + ['plugins', 'list', '--json'], capture_output=True,
                                text=True, timeout=20, check=False)
        if result.returncode:
            raise ValueError('listing failed')
        # The host prints a friendly message rather than [] with zero plugins.
        if result.stdout.strip().startswith('No plugins installed.'):
            return 0
        plugins = json.loads(result.stdout)
        if not isinstance(plugins, list):
            raise ValueError('invalid listing')
        if not any(p.get('name') == 'hermes-urgency-engine' and p.get('status') == 'enabled' for p in plugins):
            return 0
        return subprocess.run(command + ['urgency', 'watchdog', 'tick'], timeout=35, check=False).returncode
    except (subprocess.TimeoutExpired, OSError, ValueError):
        print('Urgency watchdog launcher failed: inspect plugin/cron health; no recovery action executed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

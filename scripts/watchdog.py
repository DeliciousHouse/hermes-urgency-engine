"""Owned no-agent cron launcher. No plugin imports or absolute source paths."""
import json
import os
import subprocess
import sys
import time

# Each Hermes CLI launch can take 20 s to start on Windows. The tick's source
# sync stops at the passed deadline, leaving the reserve for its flush, deadline
# evaluation and child cleanup before the timeout kills it.
LIST_TIMEOUT = 60
TICK_TIMEOUT = 180
TICK_RESERVE = 40


def main():
    command = [sys.executable, '-m', 'hermes_cli.main']
    try:
        result = subprocess.run(command + ['plugins', 'list', '--json'], capture_output=True,
                                text=True, timeout=LIST_TIMEOUT, check=False)
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
        deadline = time.time() + TICK_TIMEOUT - TICK_RESERVE
        env = dict(os.environ, URGENCY_ENGINE_TICK_DEADLINE=repr(deadline))
        return subprocess.run(command + ['urgency', 'watchdog', 'tick'], timeout=TICK_TIMEOUT,
                              check=False, env=env).returncode
    except (subprocess.TimeoutExpired, OSError, ValueError):
        print('Urgency watchdog launcher failed: inspect plugin/cron health; no recovery action executed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

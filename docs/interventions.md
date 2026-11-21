# Supervised interventions

Use the local Hermes CLI to act on an active watchdog plan. The watchdog still only detects, recommends and hands off notices; neither cron nor a conversational tool runs these actions.

## Command and request reference

Set `plugins.entries.hermes-urgency-engine.settings.intervention_mode` in Hermes settings. The default, `advisory`, refuses writes through this surface. `supervised` enables explicit local CLI requests; `autonomous` is recognized but does not authorize any write. Worker/delegated contexts cannot apply or control interventions. Set up an exact board, tenant and project [Kanban binding](kanban.md) and enable the [watchdog](watchdog.md) before applying an action.

| `hermes urgency interventions …` | Result |
| --- | --- |
| `list --project ID [--limit N] [--after KEY]` | Project history, ordered by action ID; default 100, range 1–500; pass the returned `next` as `--after`. An uninitialized store returns an empty page. |
| `show ACTION_ID` | A stored action, including attempts and attribution. A successful read does not mean the action applied. |
| `apply --json-file FILE` | Apply a typed request. Use `-` for stdin. Repeat the identical request to inspect its existing result, not to resend a native write. |
| `reconcile ACTION_ID` | Inspect current state without resending. It may update the local action history; it does not mutate Kanban or the Ledger. |
| `pause --request-id ID --reason TEXT` | Stop admitting new effects in this profile. Already issued effects are not rolled back. |
| `resume --request-id ID --reason TEXT` | Reopen admission; never replays an action or starts a job. |

An apply file is one UTF-8 JSON object, at most 256 KiB. Required fields: integer `schema_version` (exactly `1`); string `request_id`, `incident_id`, `plan_id`, `project_id`, `board_id`, and `task_id`; nonnegative integer `expected_revision` (the **local** task revision); nonempty string `reason` (1–4096 characters); string `kind`; and a `parameters` object. IDs are 1–128 characters with no separators `/` or `\` or control characters. Canonical scope must match current configuration. Duplicate/extra keys, non-finite numbers and values flagged by host redaction are refused.

| Kind | Exact parameters | Effect |
| --- | --- | --- |
| `set_priority` | `priority`: integer in `[-9223372036854775807, 9223372036854775807]` | Absolute native Kanban assignment. |
| `reestimate` | `remaining_seconds`: finite nonnegative number | Replace only remaining work; preserve buffers, actual work and verification criteria. |
| `replan_soft_deadline` | `deadline_id`: ID; `expected_deadline_revision`: nonnegative integer; `at`: offset-bearing ISO timestamp; `timezone`: matching IANA zone | Move a bound soft target and retain original target and revision history. Never moves a hard deadline. |
| `create_recovery_task` | `title` and `body`: nonempty strings up to 4096 characters each; `assignee`: ID; `priority`: same as above; `parents`: at most 10,000 distinct IDs | Always refuses with `CAPABILITY_UNAVAILABLE` and no create, link or intervention-store write. |

The JSON envelope has `ok` and `data`, or `ok: false` and an `error` with `code`, `operation`, `retryable`, `side_effects` and `next_action`. A recorded failed or uncertain effect also returns `data`. Commands with failed or uncertain outcomes exit nonzero and report on stderr. `data` includes the action ID, kind, state, requested/observed effect, attribution, attempt count, last error and next action. `attempt_count` counts durable effect admissions; a process may stop before the external call begins.

## Why receipts and readback are separate

`reserved` means no effect was admitted. `in_flight` means the intent was durably admitted before the write. `applied` requires a conclusive receipt and matching readback; `observed` means the desired value matches but no receipt attributes it to the request. `refused` and `conflict` distinguish blocked input/scope from a competing revision; `unknown` means the current evidence cannot settle the outcome. None of these states resolves an incident or verifies delivery.

Priority is last-writer-wins. The plugin checks current scope, status and retained graph coverage, then runs a fixed same-interpreter `kanban --board … edit … --priority …` command. Checks and readback do not make that native command atomic with other writers. The scoped snapshot uses the existing public `kanban_show` tool via a local same-interpreter CLI child, including its board lookup; there is no inventory scan or readiness recomputation in apply/reconcile. Native calls are capped at 20 seconds and 16 MiB of stdout each. Local changes use Ledger revision checks. Taken together, the local stores do not offer an atomic transaction with native Kanban, so do not execute against a board whose operators expect exclusive modification.

Recovery creation needs more than a unique-looking title or a same-key retry. On the supported hosts, native public create/show and archive-inclusive inventory do not expose an exact key lookup. The same native key can return an active task with different input and can create another task after archive. A safe future interface needs board-scoped archive-inclusive exact key lookup with complete/not-found/denied results, plus atomic canonical-payload create-or-reuse with created/reused/conflict results. This is a capability requirement, not an upstream submission or an implemented fallback.

## How to pause, reconcile and roll back

1. Stop new effects with `hermes urgency interventions pause --request-id stop-1 --reason 'Inspect unknown priority'`. Switch mode back to `advisory` to stop applying through the surface independently of the pause flag.
2. Run `hermes urgency interventions show ACTION_ID`. For an in-flight or unknown action, run `hermes urgency interventions reconcile ACTION_ID`, then inspect the result again. If it remains unknown, compare canonical state before deciding what action is still needed. Do not change the request ID to work around an uncertain write.
3. Resume only by explicit operator choice: `hermes urgency interventions resume --request-id resume-1 --reason 'Inspection finished'`. Resume does not modify prior actions.

On `REVISION_CONFLICT`, reload the task and commitment before deciding whether to submit a fresh request. On `SOURCE_UNAVAILABLE` for stale coverage, check [Kanban health](kanban.md) and refresh the configured scan. On a storage error, preserve `interventions.db` in the plugin data directory; do not remove it to clear pending requests. The store retains at most 10,000 actions and 50,000 events. Source rollback must preserve those records and must not perform compensating native edits or deletions.

## Change a synthetic task's priority

Use an already enabled plugin in a disposable Hermes profile. Run the [Kanban setup tutorial](kanban.md), leaving the `synthetic` board and `demo` project configured. The example assumes a non-running task and current Kanban coverage. It creates a synthetic fault observation and uses the real resulting plan, not a guessed incident ID.

```python
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess


def run(*args, expected=0):
    p = subprocess.run(['hermes', *args], text=True, capture_output=True, check=False)
    assert p.returncode == expected, p.stderr
    if args[0] == 'kanban':
        return json.loads(p.stdout)
    return json.loads((p.stdout if expected == 0 else p.stderr).splitlines()[-1])


prefix = 'plugins.entries.hermes-urgency-engine.settings.'
for setting, value in (('intervention_mode', 'supervised'), ('watchdog_enabled', 'true')):
    subprocess.run(['hermes', 'config', 'set', prefix + setting, value], check=True)
urgency = ('urgency',)
board = ('kanban', '--board', 'synthetic')
actions = (*urgency, 'interventions')
task, = run(*urgency, 'kanban', 'tasks', '--project', 'demo')['data']['items']
key = dict(project_id='demo', board_id='synthetic', task_id=task['task_id'])
file = Path('synthetic-intervention.json')
file.write_text(json.dumps(dict(**key, request_id='tutorial-fault', reason='Synthetic unavailable worker',
    kind='task_signal', occurred_at=datetime.now(timezone.utc).isoformat(), data={'agent_available': False})), encoding='utf-8')
run(*urgency, 'watchdog', 'observe', '--json-file', str(file))
subprocess.run(['hermes', *urgency, 'watchdog', 'tick', '--force'], check=True, capture_output=True)
incident = next(i for i in run(*urgency, 'watchdog', 'incidents', '--project', 'demo')['data']['items']
                if i['task_id'] == key['task_id'] and i['state'] == 'active')
plan = next(p for p in run(*urgency, 'watchdog', 'plans', '--project', 'demo')['data']['items']
            if p['incident_id'] == incident['incident_id'])
current = run(*urgency, 'task', 'synthetic', key['task_id'])['data']
request = dict(**key, schema_version=1, request_id='tutorial-priority', incident_id=incident['incident_id'],
               plan_id=plan['plan_id'], expected_revision=current['revision'], kind='set_priority',
               reason='Prioritize the synthetic task', parameters={'priority': 7})
file.write_text(json.dumps(request), encoding='utf-8')
action = run(*actions, 'apply', '--json-file', str(file))['data']
assert run(*actions, 'show', action['action_id'])['data']['state'] == 'applied'
assert run(*board, 'show', key['task_id'], '--json')['task']['priority'] == 7
run(*urgency, 'kanban', 'sync', '--request-id', 'tutorial-round2', '--reason', 'Refresh after native edit')
current = run(*urgency, 'task', 'synthetic', key['task_id'])['data']
request['expected_revision'] = current['revision']

def submit(request_id, kind, parameters, expected=0):
    file.write_text(json.dumps(dict(request, request_id=request_id, kind=kind, parameters=parameters)), encoding='utf-8')
    return run(*actions, 'apply', '--json-file', str(file), expected=expected)

assert submit('tutorial-estimate', 'reestimate', {'remaining_seconds': 60})['data']['state'] == 'applied'
estimated = run(*urgency, 'task', 'synthetic', key['task_id'])['data']
assert estimated['remaining_seconds'] == 60
for field in ('verification_seconds', 'deployment_seconds', 'actual_seconds', 'criteria'):
    assert estimated[field] == current[field]
request['expected_revision'] = estimated['revision']
file.write_text(json.dumps(dict(**key, request_id='tutorial-target', deadline_id='soft-target', kind='soft',
    at='2099-01-01T00:00:00+00:00', timezone='UTC', owner='operator', source='synthetic')), encoding='utf-8')
run(*urgency, 'apply', 'set_deadline', '--json-file', str(file))
assert submit('tutorial-replan', 'replan_soft_deadline', dict(deadline_id='soft-target', expected_deadline_revision=1,
    at='2099-01-02T00:00:00+00:00', timezone='UTC'))['data']['state'] == 'applied'
target = run(*urgency, 'explain', 'soft-target')['data']
assert target['original_at'] == '2099-01-01T00:00:00+00:00'
assert target['target_at'] == '2099-01-02T00:00:00+00:00'
recovery = dict(title='Synthetic recovery', body='Check the fixture', assignee='synthetic', priority=1, parents=[])
refusal = submit('tutorial-recovery', 'create_recovery_task', recovery, expected=1)['error']
assert (refusal['code'], refusal['side_effects']) == ('CAPABILITY_UNAVAILABLE', 'none')
run(*actions, 'pause', '--request-id', 'tutorial-pause', '--reason', 'Stop fixture effects')
file.write_text(json.dumps(dict(request, request_id='tutorial-paused')), encoding='utf-8')
assert run(*actions, 'apply', '--json-file', str(file), expected=1)['error']['code'] == 'PAUSED'
assert run(*actions, 'reconcile', action['action_id'])['data']['attribution'] == 'receipt'
```

The fresh native `show` reads the actual priority. The watchdog incident remains separate. A valid `create_recovery_task` request instead reports `CAPABILITY_UNAVAILABLE`; it is not an example of successful recovery.

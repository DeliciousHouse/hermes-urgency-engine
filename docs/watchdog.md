# Independent watchdog

The watchdog evaluates local ledger work without an agent turn. It records incidents
and **recommended**, not executed, recovery plans. Hermes's no-agent cron runs one
small Python launcher; the plugin starts no daemon, thread, model call or OS service.
Use [native installation](runtime.md#compatibility-and-installation) first.

## Reference

### Commands

All commands below start with `hermes urgency watchdog`.

| Command | Arguments and effect |
| --- | --- |
| `install` | `--deliver local` by default, or an existing Hermes delivery target. Explicitly writes one owned launcher and registers one no-agent job every minute. Existing owned installation is reconciled. |
| `tick` | `--force` skips next-due throttling, not pause, deduplication or notice limits. Exit 0 with empty stdout when quiet; fresh notices are one JSON summary; failures exit 1 on stderr. |
| `health` | Read-only schema version, install receipt, pause, last successful evaluation, last complete scan, cursor, lag, next due, coverage and limitations. A stored install receipt is not a live scheduler-health probe. |
| `pause`, `resume` | Require `--request-id ID --reason TEXT`. Persist audited control; resume makes the next tick due. Same request and payload replay; different payload conflicts. |
| `incidents`, `plans` | Read-only pages, `--project ID` optional, `--limit N` default 100 (1–500), `--after ID` continuation. Includes historical resolved records. |
| `export` | `--table incidents` by default; also `plans`, `observations`, `monitor_audit`. Same paging flags; audit continuation is an integer and does not support project filtering. Writes JSON to stdout, never an arbitrary destination path. |
| `observe` | `--json-file FILE`: explicit typed local observation, at most 16 KiB after JSON parsing. No conversational mutation equivalent. |
| `uninstall` | Removes only the owned cron and unchanged launcher. Retains ledger, monitor and audit data. Edited or ambiguous assets return `INSTALL_CONFLICT`. |

Reads return `{"ok":true,"data":...}`; pages include `schema_version: 1`,
`items`, `has_more`, `next`. Pass `next` as `--after` while `has_more` is true.
Before initialization, reads return uninitialized health or empty pages without
creating storage. String exports use Hermes redaction. Do not put secrets in IDs.
Read-only `/urgency watchdog health|incidents|plans` and `urgency_query` operations
`watchdog_health`, `watchdog_incidents`, `watchdog_plans` share these envelopes.
Tool filters use `project_id`, `limit`, `after`. Pre-turn guidance includes up to
three active plans for the selected project within the existing guidance budget.
Nonquiet `tick` output includes `notices` (at most 40), `omitted` (eligible notices
that did not fit), and `more` (true when `omitted` is nonzero).

### Settings

Under `plugins.entries.hermes-urgency-engine.settings` in the active profile:

| Key | Type, default | Range or meaning |
| --- | --- | --- |
| `watchdog_enabled` | bool, false | Explicit ticks/telemetry enabled; installed state also enables monitoring |
| `watchdog_normal_seconds` | int, 300 | 60–3600 |
| `watchdog_risk_seconds` | int, 60 | 60–300, no greater than normal |
| `watchdog_stall_seconds` | int, 1800 | 60–86400 |
| `watchdog_claim_seconds` | int, 900 | 60–86400 |
| `watchdog_notice_limit` | int, 3 | 1–10, total notices per episode |
| `watchdog_retention_days` | int, 30 | 1–365, old observations and resolved episodes |
| `watchdog_max_rows` | int, 10000 | 100–100000, retained monitor rows across all tables |

One-minute cron dispatch is not a one-minute latency guarantee. A running Hermes
scheduler is required. Normal evaluations throttle to 300 seconds; incident or
high-risk evaluations use the risk interval. Missed intervals coalesce, never
replay every missed tick. Force does not override safety controls.

### Observations

Each object has exactly `request_id`, `reason`, `project_id`, `board_id`, `task_id`,
`kind`, `occurred_at`, `data`. IDs follow the [ledger constraints](ledger.md), reason
is at most 1024 characters, timestamp is UTC ISO 8601 and no more than 60 seconds
in the future. Project/task and dependency references must exist in this profile's
ledger. Unknown fields, nonfinite durations and malformed IDs are rejected.
Sparse task signals preserve omitted values; explicit null clears nullable fields.
Out-of-order events remain in history but do not replace current signals.

| Kind | Data fields |
| --- | --- |
| `task_signal` | Optional nullable `checkpoint_at`, `claim_at`, `heartbeat_at`, `blocker_since` UTC timestamps; nullable boolean `agent_available`; `dependency_state` null/available/unreachable/unknown; integer `tool_failures`, `recovery_failures` 0–1000000; `dependencies` up to 1000 `[board_id, task_id]` pairs |
| `tool_outcome` | All required: `tool_name`, `outcome` success/failure/unknown, nonnegative finite `duration_seconds` |
| `worker_outcome` | Required `state`: running/failed/exhausted/unavailable |
| `recovery_outcome` | Required `incident_id` of an active incident on the task; `outcome`: improved/no_improvement/failed |

Only `tool_outcome` permits both board/task IDs null, for project-focused telemetry
without a board correlation. Such records never cause task failure incidents.
The native `post_tool_call` observer recognizes structured `ok`/`success` booleans
and errors; ambiguous output is unknown. It never stores tool arguments, results,
prompts or exception text. Host session identity is not a Kanban task ID.
This slice has no automatic Kanban lifecycle adapter: use explicit observations;
missing worker/dependency telemetry stays unknown. Explicit availability/dependency
facts retain their own observation time; unrelated telemetry does not refresh it.
If retention prunes that source event, freshness is `unknown_source_pruned`, not
current. A recovery counter alone does
not prove failed recovery: the detector requires outcomes tied to an incident.

### Deterministic detectors

| Detector | Positive evidence |
| --- | --- |
| `stalled` | Running without verified progress for the configured threshold; first observed running time is the labelled fallback |
| `missed_checkpoint` | Explicit checkpoint passed while task is not verified |
| `expired_deadline` | Hard/soft target passed without verified completion; commitments remain unchanged |
| `dependency_cycle` | Strongly connected graph component or self-edge, not simply blocked status |
| `repeated_tool_failure` | At least three explicitly failed correlated outcomes in 900 seconds since the last explicit success; an explicit failure-count signal is also accepted |
| `delegated_work_unavailable` | Explicit unavailable/failed/exhausted worker or unavailable agent |
| `stale_claim` | Explicit heartbeat, otherwise claim, at least the configured age |
| `dependency_unreachable` | Explicit unreachable state, or missing known graph target when graph collection is complete |
| `forecast_miss` | Previously retained completion estimate elapsed with required work unverified |
| `ineffective_recovery` | At least three failed/no-improvement outcomes for the same still-active incident; improvement resets that count |

A verified task closes task incidents. Verified progress closes its current episode.
For `dependency_cycle`, an incomplete graph leaves the existing incident and plan
unchanged without sending or consuming a reminder. Once the graph is complete,
the same episode resumes its reminder schedule if the cycle remains. A complete
scan with no cycle for the task closes the episode. A later recurrence starts a
new episode.

For delegated-work unavailability, unknown availability leaves the existing episode
active without a reminder. If unavailability is observed again, it is still the
same episode. Only observed recovery (such as explicit availability or verified
progress) closes it; a later detection of unavailability then opens a new episode.

For `dependency_unreachable`, an explicit `unreachable` signal or a missing target
in a complete graph is positive evidence. Without either, `unknown`, null, or
absent dependency state leaves the incident and its plan unchanged. No reminder
is sent or consumed while coverage is unknown, and renewed evidence uses the same
episode. An explicit `available` signal closes the episode if no missing target is
detected. Verified progress or completion still closes task episodes.

A new incident creates one plan with facts, unknowns,
objective, task IDs, required verification and stable ordered recommendations.
Deadline plans refer to their ledger deadline; consult `hermes urgency explain ID`
for complete commitment scope and evidence. Plans never execute commands, change
models, delegate, reclaim tasks, extend deadlines, remove criteria or spend money.

### Storage and bounds

`plugin_db('hermes-urgency-engine', 'watchdog.db')` owns monitor schema 1 separately
from unchanged ledger `data.db` schema 1. Tables: `monitor_meta` (cursor, generation,
install/scan/forecast state), `observations` (typed events), `task_signals`
(current sparse metadata), `dependencies` (edges), `incidents` (episodes), `plans`
(recommendations), `monitor_audit` (control/event replay hashes and results).
Registration does no I/O. Explicit mutations open write storage; reads open existing
storage read-only. SQLite can maintain WAL coordination files for an existing DB.

Ticks process at most 500 tasks and 500 deadlines per page, 100 incident transitions,
and a 10000-node/50000-edge graph. Exceeding either graph cap reports
`coverage.graph_complete: false` in health. An incomplete graph cannot establish
that a cycle has cleared or that a dependency is missing. Cursor and coverage
expose incomplete scans. Evaluation has cooperative 25/28-second checkpoints
and a 100 ms SQLite busy wait; hardware/filesystem stalls
are not a hard real-time guarantee. Selection is capped at 40 before notice
accounting, inside the tick transaction. Incidents beyond that cap keep their next
reminder due instead of consuming it.
A 10000-task board needs a larger row cap than the default because each task also
has retained monitor metadata. Exceeding capacity rolls back the transaction.
Old observations and resolved episodes expire by retention time; active records,
control replay and ledger audit are not silently evicted. Disk quota is not guaranteed.

## Why detection and delivery are separate

Without persisted state, every restart could alert again, erase an old forecast miss,
or treat an absent heartbeat as proof of failure. SQLite transactions retain episode
identity, cooldown and retry state across fresh processes. Ledger snapshots are read
before the monitor write lock; source revisions detect intervening changes. This is
not a transaction spanning both databases: later ledger changes are seen next tick.

A new incident is eligible immediately; reminders wait 300, 600, 1200 seconds, capped at
3600, and never exceed the configured episode total. Rising severity can advance a
pending notice, not reset the cap. The forecast core retains urgency state/cooldown.
Plans preserve verification and permissions even when the target is impossible.

Notice generation is recorded **before** stdout. A crash afterward can lose output;
`emission: unknown_after_commit` requires reconciliation, not blind retransmission.
The CLI can hand output to cron but cannot prove remote receipt. Local delivery adds
no network dependency; configured remote targets use Hermes's existing channel and
credentials. No delivery acknowledgement or exactly-once guarantee is claimed.

## How to operate, pause and remove the monitor

1. With the plugin enabled, run `hermes urgency watchdog install`.
2. Leave the normal Hermes scheduler running. Read `hermes urgency watchdog health`
   after a due evaluation; check advancing `last_success`, not just `installed`.
3. Read `hermes urgency watchdog incidents` and `hermes urgency watchdog plans`.
   Inspect recommendations before choosing any action through ordinary host controls.
4. Pause with `hermes urgency watchdog pause --request-id pause-1 --reason maintenance`.
   Resume with `hermes urgency watchdog resume --request-id resume-1 --reason ready`.
5. Run `hermes urgency watchdog uninstall` before disabling/removing the plugin.
   An already-installed launcher also exits quietly if the plugin is disabled/removed.

Troubleshooting: `STORE_BUSY` permits a bounded retry after the other writer ends.
`CAPACITY_EXCEEDED` requires reviewing retained data and a deliberate larger row cap,
not deletion of active incidents. `CLOCK_ROLLBACK` waits for clock correction.
`UNSUPPORTED_SCHEMA`/`CORRUPT_STATE` require backup and diagnosis, not automatic repair.
`INSTALL_CONFLICT` requires inspecting `hermes cron list` and the owned launcher:
preserve edited/unrelated assets. An interrupted create may have succeeded; read
public cron state before retrying. Never blindly add a duplicate job.

Rollback: pause, uninstall the owned job, stop writes, and use SQLite's backup API for
both databases. Revert plugin source through the normal reviewed workflow. Older code
ignores `watchdog.db`; do not downgrade/delete it or assume uninstall purges history.
No OS supervisor is installed by this plugin.

## Tutorial: produce one independent incident

Use a disposable Hermes home with the plugin installed, Python from that Hermes
environment and `hermes` on PATH. This creates synthetic ledger data, not board tasks.

### 1. Create the example input

Save as `watchdog-demo.py` and run `python watchdog-demo.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess

def command(*args):
    result = subprocess.run(['hermes', 'urgency', *args], check=True,
                            capture_output=True, text=True)
    print(result.stdout.strip())
    return json.loads(result.stdout)['data']

def apply(operation, data):
    Path('watchdog-input.json').write_text(json.dumps(data), encoding='utf-8')
    return command('apply', operation, '--json-file', 'watchdog-input.json')

apply('create_project', dict(project_id='watchdog-demo', title='Synthetic objective', request_id='wd-project'))
apply('upsert_task', dict(project_id='watchdog-demo', board_id='demo', task_id='task',
      goal='Deliver proof', status='running', remaining_seconds=600,
      expected_revision=0, reason='Example', request_id='wd-task'))
command('watchdog', 'install')
event = dict(request_id='wd-worker', reason='Synthetic unavailable worker',
             project_id='watchdog-demo', board_id='demo', task_id='task',
             kind='worker_outcome', occurred_at=datetime.now(timezone.utc).isoformat(),
             data={'state': 'unavailable'})
Path('watchdog-input.json').write_text(json.dumps(event), encoding='utf-8')
command('watchdog', 'observe', '--json-file', 'watchdog-input.json')
```

### 2. Run a due scheduler evaluation

Wait at least a minute, then run `hermes cron tick` if you do not already have a
running Hermes scheduler. This invokes the host scheduler, not the plugin directly.
Run `hermes urgency watchdog health` and `hermes urgency watchdog incidents`.
Expect advancing `last_success` and `delegated_work_unavailable`; exact timestamps
and IDs depend on the run. Empty health means no successful evaluation, not success.

### 3. Inspect the plan and clean up

Run `hermes urgency watchdog plans`, then `hermes urgency watchdog uninstall`.
Verify `hermes cron list` no longer contains the owned watchdog job. Data remains.

### What you built

A persistent incident and recommendation produced without a model call. Nothing
reclaimed the task or claimed verified delivery. See the [command reference](#commands)
and [operating guide](#how-to-operate-pause-and-remove-the-monitor) for real use.

## Contributor verification

Run `python -m unittest discover -s tests -v`. The limits tests use independent
processes, crash boundaries and a 10000-task synthetic scan, reporting elapsed time
and Python traced peak allocation (not total RSS). With a clean installed plugin:

```sh
python tests/host_watchdog.py --hermes-root /path/to/hermes-agent --installed-home /path/to/disposable-home
```

This real-host journey observes an executed read tool, waits for a naturally due
cron scan, tests quiet/pause/restart/disable/uninstall behavior and preserves data.
It is not evidence of remote delivery, continuous gateway availability, model
compliance, native Desktop functionality or autonomous recovery.

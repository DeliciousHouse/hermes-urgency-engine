# Independent watchdog

The watchdog evaluates local ledger work without an agent turn. It records incidents
and **recommended**, not executed, recovery plans. Hermes's no-agent cron runs one
small Python launcher; the plugin starts no monitoring daemon, background evaluator,
model call or OS service. Canonical collection uses a bounded child-output reader
thread only for the duration of each source call.
Use [native installation](runtime.md#compatibility-and-installation) first.
To observe the gateway when its own scheduler can't run, use the separate,
opt-in [external liveness check](#external-gateway-liveness).

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

`install`, `uninstall`, `pause`, `resume` and `observe` return `SOURCE_DENIED`
(field `context`) in a worker context, as [operator writes](runtime.md#operator-writes)
do, so a monitored worker cannot pause, remove or feed its own watchdog. `tick` and
the reads stay available; the owned no-agent cron job runs `tick`.

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
Sparse task signals preserve omitted values; explicit null removes current coverage
for nullable fields. It does not resolve a previously detected incident.
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
Optional [Kanban synchronization](kanban.md) admits canonical worker/dependency
facts on the existing tick. Lifecycle hooks only mark dirty bindings; missing
worker/dependency telemetry stays unknown. Explicit availability/dependency
facts retain their own observation time; unrelated telemetry does not refresh it.
Compact admitted evidence watermarks survive raw-event pruning. Retention expiry
makes current source coverage unknown; historical incident facts remain unchanged.
Health reports current evaluation counts separately from incident state. A recovery counter alone does
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
| `stale_claim` | Admitted heartbeat, otherwise admitted claim, at least the configured age |
| `dependency_unreachable` | Explicit unreachable state, or missing known graph target when graph collection is complete |
| `forecast_miss` | Previously retained completion estimate elapsed with required work unverified |
| `ineffective_recovery` | At least three failed/no-improvement outcomes for the same still-active incident; improvement resets that count |

### Recovery and unknown coverage

Each source evaluates to `active`, `clear` with positive recovery evidence, or
`unknown`. An omitted evaluation is unknown, never clear. The reconciler alone
updates lifecycle rows. Any active source wins; otherwise every implicated source
must have recovery evidence at least as recent as its fault evidence before the
incident and plan resolve together.

| Detector | Positive recovery | Unknown (does not resolve) |
| --- | --- | --- |
| `stalled` | Newer verified progress or authoritative non-running state; verified completion | Lost/null progress, missing task/page, older revision, unverified completion claim |
| `missed_checkpoint` | Explicit checkpoint replan into the future, or verified completion | Null/omitted/pruned checkpoint |
| `expired_deadline` | Audited committed-target revision into the future, or verified deadline completion | Missing deadline, automatic forecast changes, unrelated progress |
| `dependency_cycle` | Complete graph proves no cycle for the task, or verified completion | Node/edge cap, missing task/page, incomplete graph |
| `repeated_tool_failure` | New explicit tool success or explicit failure-count reset below three, for each implicated source | Elapsed 900-second window, pruned history, unknown outcome, unrelated tool traffic |
| `delegated_work_unavailable` | Explicit available/running signal for the affected task, or verified completion | Null availability, missing worker, expired evidence, older healthy signal |
| `stale_claim` | Strictly newer fresh heartbeat/claim; newer authoritative non-running state ends the claim; verified completion | Null or expired coverage, equal/older timestamp replay after loss, absent worker, unrelated progress |
| `dependency_unreachable` | Explicit available for signal-origin faults; complete graph with targets present or edges removed for graph-origin faults; verified completion | Cached healthy signal cannot clear graph evidence; null/unknown signal or graph cap |
| `forecast_miss` | Audited superseding commitment replan, or verified deadline completion | Moving forecast, lost prediction, missing deadline |
| `ineffective_recovery` | New improved outcome for the same episode, or actual recovery of its underlying incident | Pruned outcomes, missing underlying incident, unrelated improvement |

A checkpoint replan uses the existing explicit `observe` command and its request,
reason and audit record. Deadline replans use the existing ledger `revise_deadline`
operation and its revision/reason rules. There is no new authorization channel.
A partial criterion verification clears only the stalled predicate, not unrelated
incidents. An agent completion claim earns no verification credit. Actual verified
terminal completion closes task incidents; reopening does not replay pre-completion
signals as fresh failures.

Unknown coverage preserves incident/plan identity, episode, last-seen time,
pending sources, notice count and next reminder time, and emits no repeated
reminder. Returning fault evidence resumes the same episode and notice budget.
A new episode follows actual recovery and a later fault, not missing telemetry.
Its episode number advances and its incident and plan IDs are distinct, even if
retention has removed the previous resolved records.
Positive partial recovery can update source evidence without sending a notice;
other pending sources still keep the incident active. Graph and explicit signals
are independent sources. Explicit failure counts and correlated tool outcomes
share a failure watermark; an authoritative reset clears that source.

`incidents` exposes `explanation.sources` with source identity, verdict, timestamp,
revision and facts, plus `pending_sources`. These are the last persisted lifecycle
evidence, not a promise of current coverage. `health.coverage.evaluations` counts
active/clear/unknown evaluations on the last bounded page;
`unknown_policy: retain_episode_no_reminder` describes their effect. Missing entities
and unscanned pages are not included in these counts: consult the cursor and scan
completeness as well. An active historical incident with unknown current coverage
is neither a new fault observation nor evidence of recovery.

Delivery and sample identity are separate. The request ID identifies a delivery;
an older observation envelope is retained for inspection but does not replace
current evidence. For each task and each of `heartbeat_at` and `claim_at`, the
normalized UTC timestamp in the payload is the sample identity and watermark.
Equal/older samples never replace the watermark or refresh coverage. A known,
uninterrupted sample still ages at the configured threshold, without new traffic.
The monitor evaluates both fields together in each observation and persists which
sample, if any, remains admitted. It prefers heartbeat when both are known. Null,
retention expiry, or verified scope end can remove coverage without erasing the
watermark. Sparse updates preserve omitted fields but do not renew coverage.
When a source is lost, its retained sample and older cached fallback evidence
set a freshness bound. An equal or older alternate cannot bridge the gap, even
if it arrived shortly before the null or in the same observation. A strictly
newer alternate can qualify in that same observation. Repeated null neither
makes a replay eligible nor interrupts a newer admitted alternate that is still
known. These decisions survive monitor reopen and raw observation pruning.

Task progress and scope use the authoritative task revision; commitments use the
deadline revision and audited replan when needed. Tool and recovery outcomes use
their admitted event IDs and observation order: separate genuine failures count
even when their values match. Explicit scalar signals without upstream revisions
use observation time and audit order; this is not a claim of upstream identity.
Graph recovery requires a complete current graph snapshot, not a later tick or
an unrelated signal alone. Older monitor explanations remain readable without a
schema change; retained claim timestamps without provable continuity supply a
watermark only. Absence never supplies recovery. Protected evidence is retained
rather than discarded to satisfy a quota.

A new incident creates one plan with facts, unknowns,
objective, task IDs, required verification and stable ordered recommendations.
Deadline plans refer to their ledger deadline; consult `hermes urgency explain ID`
for complete commitment scope and evidence. Plans never execute commands, change
models, delegate, reclaim tasks, extend deadlines, remove criteria or spend money.

### Storage and bounds

`plugin_db('hermes-urgency-engine', 'watchdog.db')` owns monitor schema 1 separately
from ledger `data.db` schema 1/2 and optional source `kanban.db` schema 1. Tables: `monitor_meta` (cursor, generation,
install/scan/forecast state, compact admitted source watermarks and pruned episode counters), `observations` (typed events), `task_signals`
(current sparse metadata), `dependencies` (edges), `incidents` (episodes), `plans`
(recommendations), `monitor_audit` (control/event replay hashes and results).
Registration does no I/O. Explicit mutations open write storage; reads open existing
storage read-only. SQLite can maintain WAL coordination files for an existing DB.

Ticks process at most 500 tasks and 500 deadlines per page, 100 incident transitions,
and a 10000-node/50000-edge graph. Exceeding either graph cap reports
`coverage.graph_complete: false` in health. An incomplete graph cannot establish
that a cycle or graph-proven missing target has cleared, or that a dependency is
newly missing. Explicit-signal evidence can still be detected or recovered without
complete graph coverage. Cursor and coverage
expose incomplete scans. Evaluation has cooperative 25/28-second checkpoints
and a 100 ms SQLite busy wait; hardware/filesystem stalls
are not a hard real-time guarantee. Selection is capped at 40 before notice
accounting, inside the tick transaction. Incidents beyond that cap keep their next
reminder due instead of consuming it.
A 10000-task board needs a larger row cap than the default because each task also
has retained monitor metadata. Exceeding capacity rolls back the transaction.
Old observations and resolved episodes expire by retention time. One counter per
entity and detector with pruned history counts toward the row cap and prevents
ID reuse. It cannot recover episode numbers already pruned by earlier code.
Active records, control replay and ledger audit are not silently evicted.
Disk quota is not guaranteed.

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
   Inspect pending sources and compare them with health coverage. Unknown is not healthy.
   Supply genuine recovery through the existing observation/ledger commands; do not
   clear an incident by deleting telemetry. Inspect recommendations before choosing
   any action through ordinary host controls. For lost claim coverage, submit a
   genuinely newer heartbeat or claim through `observe`, not the old sample with
   a new `request_id` or `occurred_at`.
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
ledger and monitor databases, plus `kanban.db` when canonical sync is configured.
Revert plugin source through the normal reviewed workflow. Older code
ignores `watchdog.db`; do not downgrade/delete it or assume uninstall purges history.
No OS supervisor is installed by this plugin.

## External gateway liveness

### Probe reference

Run `python3 scripts/gateway_liveness.py PORT` from the plugin source directory
with Python 3.11 or later. Supply one decimal TCP port (`1`–`65535`). The probe
sends one unauthenticated `GET /health` to `127.0.0.1` on that port. It does not
follow redirects, use proxy settings, retry, or read Hermes config. The total
network deadline is 3 seconds, including connect, headers and body. It accepts
at most 8 KiB of HTTP headers and 4 KiB of UTF-8 JSON, with one `Content-Length`
and no transfer encoding.

| Exit | Meaning |
| --- | --- |
| `0` | HTTP 200 with the documented JSON `{"status":"ok"}`, or the pinned native form with exactly `status: "ok"`, `platform: "hermes-agent"`, and a nonempty string `version`. Silent success. |
| `1` | Unreachable, timed out, or not an accepted response. A fixed error goes to stderr. |
| `2` | Invalid arguments. A fixed usage line goes to stderr. |

The probe does not print headers, response bodies or caller input.

### Why this is separate from watchdog health

`hermes urgency watchdog health` reads the last recorded evaluation; it can
remain unchanged when the gateway is down. An OS timer can run without the
gateway and tell you whether its [API Server health endpoint][api-health]
answered this time. It does not prove scheduler freshness, provider readiness,
Desktop or dashboard availability, or the identity of a process answering on the
port. Keep checking the watchdog's `last_success` for evaluation freshness.
Neither plugin installation nor `hermes urgency watchdog install` enables the
external check. It does not restart the gateway, launch agents or change the queue.

### How to enable or disable a Linux systemd user timer

Use a Linux systemd user manager with a working user journal, and a Hermes API
Server already enabled on `127.0.0.1`. Use its **API Server port**, not the
dashboard port. Configure the API Server securely through Hermes if needed;
do not put its key in the unit. Pick a stable absolute path to the installed
probe, run it manually, and proceed only if it exits `0`.

Create `~/.config/systemd/user/urgency-gateway-liveness.service` and paste the
following. Replace the quoted path and the sample port with yours, and change
`/usr/bin/python3` if it is not your absolute Python 3.11+ path.

```ini
[Unit]
Description=Urgency Engine gateway liveness check

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 "/absolute/path/to/scripts/gateway_liveness.py" 18765
TimeoutStartSec=5s
StandardOutput=journal
StandardError=journal
```

Create `~/.config/systemd/user/urgency-gateway-liveness.timer`:

```ini
[Unit]
Description=Check Hermes gateway liveness every minute

[Timer]
OnActiveSec=5s
OnUnitInactiveSec=60s
AccuracySec=1s

[Install]
WantedBy=timers.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now urgency-gateway-liveness.timer
systemctl --user list-timers urgency-gateway-liveness.timer
journalctl --user -u urgency-gateway-liveness.service -n 20
```

A healthy run logs no probe output. A failure records a nonzero exit in the
unit journal; no restart or external notification is configured. If it fails,
check the loopback listener and configured port, then run the probe manually to
separate an endpoint failure from a Python or unit-path problem. The interval
is approximate. The user manager must stay running; without that, the check
does not run after logout. No recipe for other operating systems is provided.

To opt out, run `systemctl --user disable --now urgency-gateway-liveness.timer`,
remove the two unit files you created, then run `systemctl --user daemon-reload`.
Verify the timer no longer appears in `systemctl --user list-timers`. Disable the
timer before removing or moving the plugin checkout. No database or cron cleanup
is needed for the external probe.

### Tutorial: see an endpoint come and go

From the repository root, run this synthetic journey. It binds a temporary
loopback port and runs the same probe command as the timer. It installs no unit
and uses no Hermes process or credentials.

```sh
python3 - <<'PY'
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

with tempfile.TemporaryDirectory() as root:
    Path(root, 'health').write_text('{"status":"ok"}', encoding='utf-8')
    handler = partial(SimpleHTTPRequestHandler, directory=root)
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        cmd = [sys.executable, 'scripts/gateway_liveness.py', str(server.server_port)]
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            up = subprocess.run(cmd, timeout=5).returncode
        finally:
            server.shutdown()
            thread.join()
    stopped = subprocess.run(cmd, timeout=5).returncode
    print(f'up={up}, stopped={stopped}')
    assert (up, stopped) == (0, 1)
PY
```

Expect `up=0, stopped=1` and one probe failure line. You built a disposable
example that distinguishes a responding endpoint from a stopped listener, not
a native gateway or scheduler test. See the [probe reference](#probe-reference)
and [timer recipe](#how-to-enable-or-disable-a-linux-systemd-user-timer) for real use.

[api-health]: https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server#get-health

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
It also verifies the installed probe sees a disposable native API Server up,
stopped and restored. The journey does not install or execute the systemd timer.
It is not evidence of remote delivery, continuous gateway availability, model
compliance, native Desktop functionality or autonomous recovery.

# Canonical Kanban synchronization

Import tasks from explicitly selected native Hermes boards into this profile's
urgency ledger. The board remains authoritative for task metadata; the ledger
remains authoritative for estimates, criteria, commitments and byte evidence.
Sync is disabled by default. It does not dispatch workers or execute recovery.

## Reference

### Configuration

Under `plugins.entries.hermes-urgency-engine.settings` in the active profile:

| Key | Type/default | Constraint |
| --- | --- | --- |
| `kanban_sync_enabled` | boolean, false | Enables explicit and existing watchdog-tick collection |
| `kanban_bindings` | array, empty | At most 32 objects with exactly `board`, `tenant`, `project_id`; no wildcards or duplicate board/tenant pairs |
| `kanban_sync_seconds` | integer, 60 | 60–3600; minimum interval between due source attempts and cached-read stale threshold |
| `kanban_max_tasks` | integer, 10000 | 1–10000 across selected inventories; excess is refused |

Board slugs match `[a-z0-9][a-z0-9_-]{0,63}`. Tenant and local project IDs match
`[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`. The local project must already exist. Binding
is explicit: no selection by working directory, prompt, assignee or board title.

### Commands and surfaces

All CLI rows begin with `hermes urgency kanban`.

| Command | Arguments/effect |
| --- | --- |
| `health` | Read cached attempt/success times, age, pause, cursor, dirty bindings, incomplete request and last error/result |
| `tasks` | Required `--project ID`; optional `--limit N` (1–500, default 100), `--after CURSOR`; reads only currently bound cached rows |
| `snapshot` | Required `--board SLUG --task ID`; read minimal public-tool detail within one unambiguous configured binding; no plugin store initialization |
| `sync` | Required `--request-id ID --reason TEXT`; one bounded scan, even before watchdog installation |
| `pause`, `resume` | Required `--request-id ID --reason TEXT`; persisted source control, not watchdog control |

`sync`, `pause` and `resume` return `SOURCE_DENIED` (field `context`) in a worker
context, as [operator writes](runtime.md#operator-writes) do. The due scan inside
`watchdog tick` is not affected.

Request IDs use the [ledger identifier rules](ledger.md#mutation-signatures).
Reasons are nonempty and at most 1024 characters. Exact request replay returns
its saved result; changed arguments with that ID conflict. A new scan after a
completed partial page uses a new request ID. An interrupted scan retries the
original ID and arguments.

Reads and successful writes return `{"ok":true,"data":...}` on stdout. A partial
scan returns `ok:false` with `data.state:partial` on stderr and exit 1; inspect
its cursor rather than calling that full coverage. Named operation errors also
exit 1. Invalid CLI usage exits 2. Disabled/unbound sync returns `state:disabled`
without initializing source storage. When source storage already exists, enabled
sync with no bindings reconciles withdrawn graph coverage without calling the
native source. Task pages return `items`, `has_more`,
`next`; pass `next` unchanged as `--after`.

`/urgency kanban health` and `/urgency kanban tasks --project ID` are read-only.
The `urgency_query` tool accepts `kanban_health` with no arguments, or
`kanban_tasks` with `project_id` and optional `limit`, `after`. Neither surface
can snapshot, sync, pause or resume. Pre-turn guidance reads at most five cached canonical
tasks for the selected project within the existing guidance budget. It labels
stale/unknown coverage and does not collect, initialize storage or grant credit.

### Projection and freshness

A cached task contains board/task/project/tenant identity, redacted title,
canonical status, assignee, priority, parents, source digest, optional source
update time, collection time, worker summary and evidence label. Worker summary
is current run ID, recognized state, start/end and matching heartbeat times.
Bodies, comments, workspace paths, full run history, prompts and artifacts are
not persisted by this adapter. The snapshot child calls public `kanban_show`
through the registered plugin context, discards private fields, validates the
minimal document and redacts text before rendering. It never prints the raw
response. Snapshot does not open plugin stores or invoke sync/tick.

`observed_at` is collection time, **not a heartbeat or source revision**.
`source_updated_at` remains null when the host does not expose it. The last
known source revision is retained separately for ordering: a later null does
not allow a delayed, older known revision to overwrite the projection. Worker facts
require the exact current-run pointer selecting one run with valid timestamps
and the assigned profile where supplied. A resumed run can start after the task
lifetime start. Heartbeats must match that run and lie within its lifetime, not
in the future. Unrelated events and polling cannot renew a worker sample.
If native task output has no authoritative `current_run_id`, worker projection
and Monitor heartbeat remain null even if a run-history entry says `running`.
The adapter does not guess current ownership from that history.

Projection coverage is `partial` even after a successful scan: canonical status
is not verification. Failed/absent/stale source coverage is `unknown`. Cached
reads add `age_seconds` and `stale`; negative age or age greater than the configured
interval downgrades coverage. Health's aggregate coverage remains `unknown`, not
a promise that the whole project is healthy. `last_success` means a completed
source traversal, not accepted quality or a running scheduler.

`done` calls the ledger's completion-claim path and earns no credit. A transition
from canonical done/archived back to active work invalidates prior evidence via
reopen. New imports have unknown remaining work and no criteria; project percent
and forecast cannot invent completion. See [unknown work](ledger.md#unknown-remaining-work-and-incomplete-scope).
Local estimates, buffers, actual durations, criteria/weights, baseline and deadline
targets are preserved. Missing source tasks retain local scope rather than being
deleted. Cross-project conflicts are refused rather than silently moving tasks.

### Storage and bounds

The documented plugin storage API owns separate `kanban.db` schema 1:
`sources` contains the minimal projection; `sync_requests` contains durable
application/control/scan replay records; `sync_events` contains control/source
error events; `sync_meta` contains cursors, pause, dirty hints and graph admission
state. Source sync never reads the raw canonical board database.

One process owns a scan at a time through an OS file lock. After validating the
selected inventories, the scanner starts a 20-second collection window. It
allows the first detail read even if its ledger lookup uses the window. For
later reads, it checks the deadline after the ledger lookup, just before
starting the detail read. At or past the deadline, it flushes collected work
and returns a partial result without starting that read. A durable ordered
cursor continues on a later request and resets at the tail. Each native
subprocess has a 20-second timeout and 16 MiB stdout cap. These are per-call
bounds, not a 20-second scan guarantee. Inventories cap at 10000 tasks, each task
at 1000 parents, and admitted graphs at 50000 edges. A partial traversal revokes
previous complete graph coverage until the tail is read and every selected
projection is within the configured freshness interval. A slow multi-page scan
can complete its traversal while graph coverage remains unknown; future-dated
observations also cannot establish coverage. Unmapped parents do not become
fabricated local tasks or proof of a healthy graph.

At most 20000 distinct source rows are retained across bindings; new rows beyond
that are refused before ledger mutation. Event writes prune entries older than
30 days and keep at most the newest 10000. Replay identities are never silently
pruned; writes exceeding 50000 request records fail with `SOURCE_LIMIT`. Ledger
and monitor retain their own capacity rules. These are row/input safeguards,
not a disk quota or native CLI throughput guarantee.

## Why collection, application and verification are separate

A board status can change while a local operator updates an estimate. Applying
an entire stale task would lose that estimate; treating done as verified would
invent evidence. Sync records the intended update before applying it through
ledger revision checks, then admits monitor facts and records application.

```text
explicit bindings -> CLI boards/list -> bounded snapshot child
                                    -> public kanban_show -> minimal projection
                                             -> revision-checked ledger update
                                             -> monitor fact/graph admission
existing no-agent tick ----------------------> source scan, then evaluation
conversation --------------------------------> cached reads only
```

This is not a transaction spanning three databases. Persisted original request
arguments reconcile a crash after a ledger or monitor commit without duplicate
credit. Each flush sizes its intent transactions from the consecutive changes
ready to write. Unchanged tasks and previously acknowledged tasks do not count.

| Changes ready to write | Maximum changes per transaction |
| --- | ---: |
| 1–30 | 10 |
| 31–60 | 60 |
| 61–100 | 100 |
| 101–200 | 200 |
| 201–400 | 400 |
| Above 400 | Double the ceiling until it covers the ready changes |

A fast scan with 27 changed cards writes batches of 10, 10 and 7 in the same
request. The scanner flushes a short tail immediately. It also flushes before
adding a plan that would take the buffered intent data above 16 MiB.
Each task retains its own original arguments and replay identity before
any Ledger or Monitor effect. Each accepted task then gets its own source-store
commit publishing projection, acknowledgment and cursor together. An unchanged
row flushes earlier collected changes before advancing the cursor. Pending page
intents replay in cursor order after restart, not request-hash order. A source
failure before the batch commits leaves those collected changes unapplied and
its cursor unchanged, so they can be collected again.

No source transaction spans native I/O or other stores' calls; journal and
synchronous durability settings are unchanged. A failed final commit leaves the
intent replayable; older pending records without a cursor still replay.
After a binding change, sync cancels an old pending intent only when no Ledger
operation for it committed. It retains the cancelled receipt and reports
`SCOPE_CONFLICT`; a subsequent scan can collect under the new binding. An
intent with any committed Ledger effect stays pending for reconciliation.
Concurrent local edits produce conflicts instead of last-writer-wins.
Native inventory and detail calls are not an atomic board snapshot either;
missing or inconsistent data stays unknown.

The adapter uses public native CLI inventory and its registered snapshot CLI
for detail. The child initializes built-in tools and calls the documented
`ctx.dispatch_tool` API for `kanban_show`; it never reads board SQL or invokes
a private handler. Parent sync/tick retains the same interpreter, profile,
20-second owned-child timeout and 16 MiB output bound. There is no weaker CLI
show fallback on denial or missing capability. **Native `kanban list` recomputes
dependency readiness**; opting in accepts that normal native effect. Collection
is therefore not described as side-effect-free, even though this adapter never
writes board rows directly or bypasses worker fences. Cached conversational
reads do not invoke that CLI. A worker/delegate or database-pinned `snapshot`, or
a due scan inside `watchdog tick` in such a process, must explicitly identify the same board in `HERMES_KANBAN_BOARD`; mismatched or
unidentified bindings return `SOURCE_DENIED`. The adapter retains native pins
and never relabels redirected board rows as a different configured board.

Lifecycle callbacks only coalesce dirty-binding hints. They never establish
source facts. Periodic scans still reconcile external edits and missed hooks.
There is no new scheduler: use the existing watchdog no-agent job. Partial
source pages, named source transport failures, and scope or ledger revision
conflicts during a scan revoke graph coverage. Before collecting, enabled sync also
withdraws coverage owned by persisted rows whose binding was removed or changed.
It admits `unknown` to Monitor under the original project identity, never moves
the ledger task to the replacement project, and leaves unrelated facts alone.
The desired graph transition is committed before any pending Monitor event is
replayed. Original event arguments are retained until acknowledged; a crash
before or after Monitor commit retries the same event. Interrupted known coverage
is drained to the durable unknown transition before any new source I/O. This is
eventual crash reconciliation, not a transaction across the two stores.
Independent monitor evaluation still runs in that tick. Source health retains
its incomplete traversal/error; a successful watchdog tick is not a successful
source refresh. Explicit `kanban sync` still reports its partial/error result.
Pausing or disabling source sync leaves the standalone watchdog usable.

## How to enable and inspect a binding

1. Install the [native plugin](runtime.md#compatibility-and-installation) in the
   intended profile. Create the local project with `apply create_project`.
2. Set `kanban_sync_enabled: true` and an explicit binding in that profile's
   plugin settings. Verify the board and tenant are the intended source.
3. Run `hermes urgency kanban sync --request-id import-1 --reason initial`.
4. Read `hermes urgency kanban tasks --project demo` and
   `hermes urgency kanban health`. Check source identity, ages and cursor.
5. For periodic collection, run `hermes urgency watchdog install` once and keep
   the normal Hermes scheduler running. Inspect advancing source success times;
   an install receipt alone does not prove execution.

Troubleshooting: `SOURCE_DENIED` includes nonzero native CLI exit or a missing
board; verify public CLI access in the same profile. Snapshot also refuses
disabled, unbound, ambiguous or cross-tenant requests before returning detail.
`SOURCE_UNAVAILABLE` means no callable public dispatch API; worker facts stay
unknown. A CLI serializer without a pointer does not imply that the public tool
lacks it. Inspect `hermes urgency kanban snapshot --board synthetic --task ID`
for the selected tool capability, not historical run ordering. `SOURCE_TIMEOUT` and
`SOURCE_LIMIT` do not establish complete coverage. `SOURCE_MALFORMED` or
`SOURCE_UNSUPPORTED` require a compatible native response, not guessed fields.
After changing a binding, retry once on `SCOPE_CONFLICT` to allow a scan after
unapplied intent cancellation. Persistent `SCOPE_CONFLICT`, `REVISION_CONFLICT`
and `STATE_CONFLICT` require reconciling local state and configured bindings.
Preserve pending records; do not clear a
conflict by deleting storage. Read current task/audit state before retrying an
uncertain write. `STORE_BUSY` permits a bounded retry after the owner exits.

## How to pause or roll back source integration

Run `hermes urgency kanban pause --request-id pause-1 --reason maintenance`.
Verify `health.paused` is true. Resume with
`hermes urgency kanban resume --request-id resume-1 --reason ready`; the next due
attempt still respects the configured interval. Neither command pauses the
watchdog. Setting `kanban_sync_enabled: false` disables collection and hints.
Removed bindings disappear from scoped cached reads, not from retained history.

Stop writers before backing up all plugin stores with SQLite's backup API.
Preserve canonical board data and plugin history. Restore reviewed compatible
source, not a destructive database downgrade. Schema-2 ledger compatibility is
covered in the [ledger rollback guide](ledger.md#schema-versions-1-and-2).

## Tutorial: import and refresh one synthetic task

Use an empty **disposable** Hermes home with this plugin installed and enabled,
Python from that Hermes environment, and `hermes` on PATH. Do not use a production
home or start a dispatcher. This example does not run a worker or model.

### 1. Configure an explicit synthetic binding

In the disposable home's `config.yaml`, merge these settings under the existing
plugin entry; keep its existing enabled/install configuration:

```yaml
plugins:
  entries:
    hermes-urgency-engine:
      settings:
        kanban_sync_enabled: true
        kanban_bindings:
          - board: synthetic
            tenant: demo
            project_id: demo
```

Run `hermes urgency kanban health`. Before a sync, expect uninitialized storage,
not a fabricated healthy board.

### 2. Import and refresh through native commands

Save the following as `kanban-demo.py` and run `python kanban-demo.py` in that
same disposable environment. It creates a board and unassigned synthetic task,
imports it, makes an external native title edit, and reads the refreshed ledger.

```python
import json
from pathlib import Path
import subprocess

def command(*args):
    result = subprocess.run(['hermes', *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout

def urgency(*args):
    return json.loads(command('urgency', *args))['data']

command('kanban', 'boards', 'create', 'synthetic')
Path('project.json').write_text(json.dumps(dict(
    project_id='demo', title='Synthetic objective', request_id='demo-project')),
    encoding='utf-8')
urgency('apply', 'create_project', '--json-file', 'project.json')
task = json.loads(command('kanban', '--board', 'synthetic', 'create',
                         'Synthetic task', '--tenant', 'demo', '--json'))
urgency('kanban', 'sync', '--request-id', 'demo-first', '--reason', 'tutorial')
record = urgency('task', 'synthetic', task['id'])
assert record['remaining_seconds'] is None
assert record['remaining_known'] is False
assert record['criteria'] == []
command('kanban', '--board', 'synthetic', 'edit', task['id'],
        '--title', 'Externally edited')
urgency('kanban', 'sync', '--request-id', 'demo-second', '--reason', 'tutorial')
assert urgency('task', 'synthetic', task['id'])['goal'] == 'Externally edited'
print(json.dumps(urgency('kanban', 'tasks', '--project', 'demo'), indent=2))
```

The printed page contains the externally edited title, canonical status, actual
collection time and partial coverage. Unknown work remains null. If the CLI is
missing, check the documented in-process plugin mode and the current interpreter;
do not switch to a raw database reader.

### What you built

A scoped persisted source projection refreshed from a real native edit, with no
invented estimate or verification. Continue with the operating how-to above for
periodic collection, and the [ledger tutorial](ledger.md#build-a-temporary-evidence-ledger)
for deliberate criteria and byte evidence. Neither tutorial proves semantic
quality, remote delivery, Desktop operation or executed recovery.

## Tested native capabilities

Both pinned CI hosts were exercised with a clean exact-public plugin installation,
not only a source copy. These results describe those immutable host revisions:

| Hermes host | Host revision | CLI `show` current-run pointer | Public-tool snapshot | Installed initial/resumed claim and heartbeat |
| --- | --- | --- | --- | --- |
| Released 0.23.2 | `ae2a81ccc00e9b7c06301523e5bb09608397149c` | Absent | Present | Admitted to Ledger/Monitor |
| Stock development | `152fe18976002e9a987422b7c6102c805d61264b` | Absent | Present | Admitted to Ledger/Monitor |

The journeys used native claim, heartbeat, schedule and reclaim operations without
starting a worker or model. Both exercised the tutorial, naturally due cron and
profile/board fences. Missing-pointer projection is separately regression-tested
with synthetic inputs; these two hosts do **not** supply native missing-tool proof.
Other host revisions must expose the same public contract or remain unknown.

## Contributor verification

Run `python -m unittest discover -s tests -v` for deterministic source, replay,
capacity and unknown-work checks. On a clean disposable public installation,
using that Hermes interpreter, run:

```sh
python tests/host_kanban.py --installed-home /path/to/disposable-home
```

The journey executes the tutorial and a naturally due public cron tick after an
external title edit. For a native claim and heartbeat on a synthetic assigned
task, it checks current-run identity, canonical Ledger status and Monitor heartbeat
when the public tool exposes authoritative current identity. It also schedules,
unblocks and reclaims that task using native operations, proves the earlier
heartbeat cannot renew the new attempt, then admits a new heartbeat. CLI
pointer presence is reported separately. Otherwise it asserts null
worker projection and no known worker signal. This is a
capability assertion, not a skipped host or a weaker identity rule.
It also checks cached slash/tool/pre-turn reads, pause/resume, two-home and board isolation, and
retained-data uninstall. It does not start a worker or model.
The supplied home must contain only the enabled installed plugin and no prior
plugin data. CI runs this against its exact public revision on both pinned native
hosts; source-copy execution alone is not public-install evidence.

### How to measure disk synchronization cost

From a checkout with Python 3.11–3.14, run:

```sh
python tests/diagnose_sync_cost.py --source . --rows 1000
python tests/diagnose_sync_cost.py --source . --rows 100 --monitor
```

`--source` selects the checkout whose implementation and synthetic source fixture
are imported. Use a fresh process per revision. `--rows` accepts 100 or 1000;
`--monitor` exercises real Monitor admission too. The diagnostic creates temporary
disk stores, reports Python/SQLite/runner-image versions and effective durability
settings, then prints one JSON record per page for fresh import, unchanged rescan
and reopened source storage. Stage timings are exclusive of nested instrumented
calls; SQL cursor-fetch time remains in its caller. It emits no task contents.
A final `event:complete` and exit 0 mean the diagnostic assertions passed; missing
completion or nonzero exit is incomplete evidence, not a passing benchmark.

Hosted Windows probes identified commit time as the dominant measured cost,
with substantial variation even for identical commit counts. In the earlier
fixed-100 diagnostic, intent batching reduced source-store commits for a fresh
1000-row traversal from 2021 to 1031. With adaptive batching the commit count
depends on remaining inventory, elapsed time, payload size and unchanged rows.
Without a Monitor consumer, withdrawal still updates cached coverage but avoids
unused per-task graph-metadata reads. The small probes are diagnostics, not a
throughput promise or a replacement for the full 10000-row restart/churn test.
For errors, preserve the last page record and the nonzero exit, check the source
checkout/import path, and rerun only after identifying the failing boundary.

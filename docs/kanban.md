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
| `sync` | Required `--request-id ID --reason TEXT`; one bounded scan, even before watchdog installation |
| `pause`, `resume` | Required `--request-id ID --reason TEXT`; persisted source control, not watchdog control |

Request IDs use the [ledger identifier rules](ledger.md#mutation-signatures).
Reasons are nonempty and at most 1024 characters. Exact request replay returns
its saved result; changed arguments with that ID conflict. A new scan after a
completed partial page uses a new request ID. An interrupted scan retries the
original ID and arguments.

Reads and successful writes return `{"ok":true,"data":...}` on stdout. A partial
scan returns `ok:false` with `data.state:partial` on stderr and exit 1; inspect
its cursor rather than calling that full coverage. Named operation errors also
exit 1. Invalid CLI usage exits 2. Disabled/unbound sync returns `state:disabled`
without initializing source storage. Task pages return `items`, `has_more`,
`next`; pass `next` unchanged as `--after`.

`/urgency kanban health` and `/urgency kanban tasks --project ID` are read-only.
The `urgency_query` tool accepts `kanban_health` with no arguments, or
`kanban_tasks` with `project_id` and optional `limit`, `after`. Neither surface
can sync, pause or resume. Pre-turn guidance reads at most five cached canonical
tasks for the selected project within the existing guidance budget. It labels
stale/unknown coverage and does not collect, initialize storage or grant credit.

### Projection and freshness

A cached task contains board/task/project/tenant identity, redacted title,
canonical status, assignee, priority, parents, source digest, optional source
update time, collection time, worker summary and evidence label. Worker summary
is current run ID, recognized state, start/end and matching heartbeat times.
Bodies, comments, workspace paths, full run history, prompts and artifacts are
not persisted by this adapter. Native `show` may return them in its bounded
response; the adapter discards them rather than logging them.

`observed_at` is collection time, **not a heartbeat or source revision**.
`source_updated_at` remains null when the host does not expose it. Worker facts
require a matching current run and start identity; heartbeat events must belong
to that run. Unrelated events and repeated polling cannot renew a worker sample.

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

One process owns a scan at a time through an OS file lock. Each scan processes
at most 100 task details after validating all selected inventories; a durable
ordered cursor continues on a later request and resets at the tail. Each native
subprocess has a 20-second timeout and 16 MiB stdout cap. These are per-call
bounds, not a 20-second scan guarantee. Inventories cap at 10000 tasks, each task
at 1000 parents, and admitted graphs at 50000 edges. A partial traversal revokes
previous complete graph coverage until the tail is read. Unmapped parents do not
become fabricated local tasks or proof of a healthy graph.

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
explicit bindings -> native boards/list/show -> minimal source projection
                                             -> revision-checked ledger update
                                             -> monitor fact/graph admission
existing no-agent tick ----------------------> source scan, then evaluation
conversation --------------------------------> cached reads only
```

This is not a transaction spanning three databases. Persisted original request
arguments reconcile a crash after a ledger or monitor commit without duplicate
credit. Concurrent local edits produce conflicts instead of last-writer-wins.
Native inventory and detail calls are not an atomic board snapshot either;
missing or inconsistent data stays unknown.

The adapter invokes the public native CLI. **Native `kanban list` recomputes
dependency readiness**; opting in accepts that normal native effect. Collection
is therefore not described as side-effect-free, even though this adapter never
writes board rows directly or bypasses worker fences. Cached conversational
reads do not invoke that CLI.

Lifecycle callbacks only coalesce dirty-binding hints. They never establish
source facts. Periodic scans still reconcile external edits and missed hooks.
There is no new scheduler: use the existing watchdog no-agent job. A partial
source page defers that tick's monitor evaluation; a source failure exits with
an error rather than reporting a successful complete refresh. Pausing or
disabling source sync leaves the standalone watchdog usable.

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
board; verify public CLI access in the same profile. `SOURCE_TIMEOUT` and
`SOURCE_LIMIT` do not establish complete coverage. `SOURCE_MALFORMED` or
`SOURCE_UNSUPPORTED` require a compatible native response, not guessed fields.
`REVISION_CONFLICT`, `SCOPE_CONFLICT` and `STATE_CONFLICT` require reconciling
local state and configured bindings. Preserve pending records; do not clear a
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

## Contributor verification

Run `python -m unittest discover -s tests -v` for deterministic source, replay,
capacity and unknown-work checks. On a clean disposable public installation,
using that Hermes interpreter, run:

```sh
python tests/host_kanban.py --installed-home /path/to/disposable-home
```

The journey executes the tutorial, a naturally due public cron tick after an
external title edit, cached slash/tool/pre-turn reads, pause/resume, two-home and
board isolation, and retained-data uninstall. It does not start a worker or model.
The supplied home must contain only the enabled installed plugin and no prior
plugin data. CI runs this against its exact public revision on the pinned
released host; source-copy execution alone is not public-install evidence.

# Native runtime

This development plugin exposes the ledger through `hermes urgency`, `/urgency`
and the read-only `urgency_query` tool. It adds advisory context before a model
turn. It does not interrupt inference, run a watchdog, coordinate workers,
synchronize a board, control external services, or provide a Desktop dashboard.
Byte identity is **not independent semantic quality acceptance**.

## Reference

### Compatibility and installation

Requires Python 3.11–3.14 and Hermes >=0.23.2. Hermes 0.23.1 lacks the
`PluginContext.redact_text` API required for exports and guidance. Native CI
pins both the 0.23.2 release source and a stock upstream development checkout.
Date entry needs system IANA timezone data or `tzdata` (normally included with
Hermes).

Install a reviewed immutable revision, replacing `COMMIT_SHA` with its full
40-character Git commit ID:

```sh
hermes plugins install DeliciousHouse/hermes-urgency-engine --ref COMMIT_SHA --yes-deps --enable
hermes urgency --help
hermes urgency diagnose
```

The plugin has no additional Python dependencies. The native plugin is installed
from the Git repository or source archive, not by `pip install`. The wheel supplies
the Python library. The source archive includes the manifest and entry point.
The local CLI requires the default `plugins.isolation: in_process` mode;
Hermes skips CLI registrations in its separate plugin-host process mode.

### Commands and responses

| Surface | Operations |
| --- | --- |
| `hermes urgency` | All reads below and `apply OPERATION --json-file PATH` |
| `/urgency` | All reads below; `focus PROJECT_ID`, `focus clear` |
| `urgency_query` | Reads only; JSON object with `operation` and `arguments` |

| Read | Arguments |
| --- | --- |
| `status` | `--project ID` optional; `--limit N` default 100, range 1–500; `--after ID` for project pages only |
| `task` | `BOARD TASK` |
| `explain` | `DEADLINE` |
| `history` | `TYPE ID`, optional `--limit N` (1–500), `--after N` sequence |
| `diagnose` | None; opens/checks storage, does not repair it |

Tool argument names are `project_id`, `board_id`, `task_id`, `deadline_id`,
`entity_type`, `entity_id`, `limit` and `after`, as applicable. `after` is a string
for project pages and an integer for history. Unknown fields and operations fail.
Status and history return `items`, `has_more` and `next`; pass `next` as `after`.
`/urgency` uses shell-like quoting, never shell execution. `focus clear` reserves
`clear`; use `default_project` for a project with that ID.

Success is `{"ok":true,"data":...}`. Failure is `{"ok":false,"error":{...}}` with
`code`, `operation`, `field`, `retryable`, `side_effects` (`none` or `unknown`) and
`next_action`. CLI success goes to stdout (exit 0); operation failure goes to
stderr (exit 1); usage errors are JSON on stderr (exit 2). Hermes itself can emit
startup diagnostics before the plugin's final JSON error line. Help is text; a missing
subcommand prints help and exits 2. Slash and tool return a JSON string. String
values are redacted through Hermes before export; redaction can therefore alter
an identifier containing a credential-like value. Do not put secrets in IDs.

### Operator writes

`hermes urgency apply OPERATION --json-file PATH` accepts one UTF-8 JSON object,
at most 256 KiB. `--json-file -` reads stdin with the same limit. Duplicate keys,
NaN/infinity, invalid Unicode, unknown fields and non-objects fail. The CLI stamps
`actor="local-operator"` and, for deadline revisions, `intent="operator"`. Callers
cannot override actor, intent, surface, profile, database or artifact root.

The following fields are required; optional fields are listed separately. See
[ledger contracts](ledger.md) for field types, length limits, revision rules,
criteria schema, timezones, and stable domain errors.

| Operation | Required JSON fields | Optional fields |
| --- | --- | --- |
| `create_project` | `project_id`, `title`, `request_id` | — |
| `upsert_task` | `board_id`, `task_id`, `project_id`, `goal`, `expected_revision`, `reason`, `request_id` | `assignee`, `status`, `remaining_seconds`, `verification_seconds`, `deployment_seconds`, `actual_seconds` |
| `set_criteria` | `board_id`, `task_id`, `criteria`, `expected_revision`, `reason`, `request_id` | — |
| `baseline_scope` | `project_id`, `expected_revision`, `reason`, `request_id` | — |
| `claim_complete`, `reopen_task` | `board_id`, `task_id`, `expected_revision`, `reason`, `request_id` | — |
| `verify_artifact` | `board_id`, `task_id`, `criterion_id`, `artifact_relative_path`, `expected_revision`, `reason`, `request_id` | — |
| `create_milestone` | `milestone_id`, `project_id`, `title`, `request_id` | — |
| `set_milestone_tasks` | `milestone_id`, `tasks`, `expected_revision`, `reason`, `request_id` | — |
| `set_deadline` | `deadline_id`, `project_id`, `kind`, `at`, `timezone`, `owner`, `source`, `request_id` | `board_id`, `task_id`, `milestone_id` |
| `revise_deadline` | `deadline_id`, `at`, `timezone`, `expected_revision`, `reason`, `request_id` | — |
| `snapshot_forecast` | `deadline_id`, `expected_revision`, `reason`, `request_id` | — |

Creation uses revision 0 for a task; updates use the revision returned by the
latest read. Replaying identical JSON with the same request ID returns the saved
result; changed payload with that ID fails. Never change the request ID merely
to retry an uncertain write. No conversational surface performs these writes.
This is a transport boundary, **not a sandbox**: local executable code has the
OS user's authority. The provenance label is not authentication.

### Configuration, scope and storage

Settings live under `plugins.entries.hermes-urgency-engine.settings` in the active
Hermes profile's config. They are read each operation, not cached at registration.

| Setting | Type/default | Bounds and behavior |
| --- | --- | --- |
| `context_enabled` | bool, `true` | `false` disables guidance, not queries |
| `context_max_chars` | int, `4000` | 512–8000; truncation is labelled |
| `default_project` | string, empty | Explicit fallback for this profile only |
| `artifact_root` | string, empty | Existing absolute directory; empty uses plugin-owned `artifacts/` |
| `diagnostic_log_bytes` | int, `1048576` | 65536–4194304; current log plus one rotated backup |

The default artifact directory is created under
`<active Hermes home>/plugin-data/hermes-urgency-engine/artifacts/`. Configured
artifact directories are never created automatically. Artifact paths in requests
are relative; the ledger rejects escapes and symlinks. SQLite `data.db`, focus
`state.json`, and diagnostic logs are plugin-owned persistent data, not files in
the plugin install directory. The existing schema stays at version 1. Audit
retention is not bounded by diagnostic-log rotation.

Focus requires a session ID. It stores a separate full-hash session key, expires
after 24 hours, and is cleared by session end or reset (including the old session
ID). The host's 10 MiB state quota bounds retained entries; its public get/set API
has no delete or atomic eviction operation, so expired keys remain inert and a
full quota rejects new focus writes rather than overwriting another session.
Expired or invalid focus gives no project
guidance. There is no prompt/cwd/board-name inference and no arbitrary profile
selector. Without focus, only an explicit `default_project` is used. With neither,
the hook returns no context.

## Why guidance is advisory

All transports reuse the existing ledger; storage opens in the host's active
profile per operation rather than sharing a process-global SQLite connection.
The pre-turn hook opens only an existing ledger in SQLite read-only mode. It
does not initialize the schema, create the artifact directory, or run journal
setup. SQLite can still maintain WAL coordination files. The hook never snapshots
forecasts, moves targets, changes criteria or grants credit.

The hook labels objective/task text as untrusted data. It includes up to five
active deadlines ordered by urgency descending, target ascending and ID; up to
five tasks with criteria; and the recorded byte-evidence limitation. Missing
blocker information stays unknown. Long guidance is explicitly truncated. The
hook reads one size-limited snapshot of the atomically replaced focus file
without taking the writer lock. It applies a 750 ms cooperative work budget and
a 100 ms SQLite busy timeout. Large projects can return an unavailable message
rather than stale or partially ranked guidance. Ordinary commands still use the
host's storage opener and retain its safe journaling policy. `diagnose` runs
the full integrity checks; the hook checks the schema without a full scan.

These bounds trade detail for low latency. They are not a hard OS execution
budget: filesystem or hardware failures can still delay a call. Redaction runs
before injection. An unavailable ledger produces a short diagnostic and permits
other work to continue. Hermes inserts the context into the current user turn;
this plugin does not rewrite the system prompt. Deterministic tests verify text
and dispatch, not whether a model follows the advice or delivers better work.

## How to select a project and inspect a deadline

1. Create a project with the tutorial below or your existing ledger workflow.
2. In the desired conversation, run `/urgency focus demo`.
3. Run `/urgency explain hard` or `hermes urgency explain hard` to inspect the
   commitment, forecast inputs and evidence. Run `/urgency focus clear` to clear
   that conversation's selection (an explicit configured default still applies).

Verification: the next model turn receives guidance for that project. Another
session does not inherit focus. If `SESSION_REQUIRED` appears, use a conversation
surface that supplies a session ID, or configure a deliberate profile default.

## How to disable, upgrade or recover

1. Run `hermes plugins disable hermes-urgency-engine`. Fresh dispatch no longer
   contributes the plugin's commands, tool or hook; persistent data remains.
2. Back up plugin-owned data while writes are stopped, following the
   [ledger backup guidance](ledger.md). Do not copy a live SQLite file alone.
3. Install a reviewed revision, then run `hermes plugins enable hermes-urgency-engine`
   and `hermes urgency diagnose`. Read status/history to reconcile uncertain writes.

Rollback changes plugin code only; this slice adds no schema migration. Do not
remove plugin-data to fix a load error. For a source checkout, validate only the
plugin source directory, not a parent containing dependency caches or host copies:

```sh
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

Troubleshooting:

- `INVALID_INPUT`: inspect `hermes urgency apply --help` and the field table;
  correct JSON rather than retrying unchanged malformed input.
- `REVISION_CONFLICT`: read the current entity, reconcile the change, then submit
  an intentional update with its current revision.
- `IDEMPOTENCY_CONFLICT`: compare history; do not repurpose the original ID.
- `INVALID_CONFIG`: use an existing absolute artifact directory or the empty default;
  use the documented setting types/ranges.
- `INTERNAL_ERROR`: record `diagnostic_id`, inspect local `diagnostics.log`, and
  reconcile history before retrying. Side effects may be unknown. Logs contain
  redacted frame names/line numbers, not input values or exception messages.
- Guidance unavailable: check `diagnose`, configuration and storage contention.
  A missing or corrupt ledger is not evidence of healthy progress.
- CLI missing in plugin-host isolation mode: use the documented in-process CLI
  environment; changing mode is a host configuration decision, not an automatic
  action performed by this plugin.

## Tutorial: from a commitment to byte evidence

Use an empty **disposable** Hermes home with the plugin installed. The commands
below create synthetic projects/tasks, not live board tasks. Use Python from your
Hermes environment and ensure its `hermes` command is on PATH.

### 1. Confirm the installed commands

Run `hermes urgency diagnose`, then save this JSON as `project.json`:

```json
{"project_id":"demo","title":"Synthetic objective","request_id":"demo-create"}
```

### 2. Create and inspect the project

```sh
hermes urgency apply create_project --json-file project.json
hermes urgency status --project demo
```

Both return `ok: true`. Progress is initially insufficient evidence, not 100%.

### 3. Add scope, targets, a changed estimate and proof

Save the following as `demo.py` and run `python demo.py` in the same environment.
Every command runs in a fresh process, also exercising persisted reads. The code
prints real command responses; it does not manufacture sample output.

```python
import hashlib
import json
from pathlib import Path
import subprocess
from plugins.plugin_storage import plugin_data_dir

def command(*args):
    result = subprocess.run(['hermes', 'urgency', *args], check=True,
                            capture_output=True, text=True)
    print(result.stdout.strip())
    return json.loads(result.stdout)['data']

def apply(operation, **data):
    Path('input.json').write_text(json.dumps(data), encoding='utf-8')
    return command('apply', operation, '--json-file', 'input.json')

task = apply('upsert_task', board_id='board', task_id='task', project_id='demo',
             goal='Deliver synthetic proof', remaining_seconds=600,
             verification_seconds=60, deployment_seconds=30,
             expected_revision=0, reason='Plan', request_id='demo-task')
proof = b'synthetic proof\n'
task = apply('set_criteria', board_id='board', task_id='task',
             criteria=[dict(criterion_id='proof', description='Expected bytes',
                            weight=1, required=True,
                            expected_sha256=hashlib.sha256(proof).hexdigest())],
             expected_revision=task['revision'], reason='Scope', request_id='demo-criteria')
project = command('status', '--project', 'demo')['items'][0]
apply('baseline_scope', project_id='demo', expected_revision=project['revision'],
      reason='Baseline', request_id='demo-baseline')
for kind in ('hard', 'soft'):
    apply('set_deadline', deadline_id=kind, project_id='demo', kind=kind,
          at='2035-01-01T00:00:00Z', timezone='UTC', owner='operator',
          source='Tutorial', request_id='demo-' + kind)
command('explain', 'hard')
task = apply('upsert_task', board_id='board', task_id='task', project_id='demo',
             goal='Deliver synthetic proof', remaining_seconds=1200,
             expected_revision=task['revision'], reason='Updated estimate', request_id='demo-estimate')
command('explain', 'hard')  # Target unchanged; remaining-work estimate changed.
task = apply('claim_complete', board_id='board', task_id='task',
             expected_revision=task['revision'], reason='Claim', request_id='demo-claim')
assert command('status', '--project', 'demo')['items'][0]['progress']['percent'] == 0
artifact = plugin_data_dir('hermes-urgency-engine') / 'artifacts' / 'proof.txt'
artifact.write_bytes(proof)
apply('verify_artifact', board_id='board', task_id='task', criterion_id='proof',
      artifact_relative_path='proof.txt', expected_revision=task['revision'],
      reason='Compare bytes', request_id='demo-verify')
assert command('status', '--project', 'demo')['items'][0]['progress']['percent'] == 100
command('history', 'task', '["board","task"]')
```

### What you built

A persisted synthetic project with explicit scope, hard/soft targets, changing
estimates and byte evidence. Claimed completion alone earned no credit; the final
100% means all recorded required bytes matched, not that an independent reviewer
accepted quality. Start a new process and run `hermes urgency status --project demo`
to confirm persistence. Use `/urgency focus demo` in a conversation to select it.
For more details see the [ledger](ledger.md) and [forecast policy](forecast.md).

## Contributor verification

Run `python -m unittest discover -s tests -v` for the stdlib suite. The separately
invoked `tests/host_runtime.py` requires a provisioned real Hermes interpreter:

```sh
python tests/host_runtime.py --hermes-root /path/to/hermes-agent
```

It creates disposable homes, copies the source plugin, and exercises native CLI,
slash/tool/hook dispatch, profile separation and disable/re-enable. For testing an
exact public installation, pass `--installed-home /path/to/disposable-home` after
installing there. That home must be empty of tutorial data. Tests write only
synthetic fixture state. Add `--tutorial` to run the tutorial in a separate home
using the same installed plugin and installer-generated configuration. CI runs
both checks after a clean public install on the pinned Hermes release. No model
or external control action is tested.

# Durable deadline and progress ledger

Keep original commitments, revised targets and verified progress in one local SQLite database. This page describes the Python library; the [native plugin adapter](runtime.md) supplies CLI, slash/tool queries and advisory runtime hooks. The native adapter also offers opt-in [Kanban synchronization](kanban.md); neither supplies a scheduler, Desktop page or release integration.

- [Reference](#reference)
- [Why commitments and evidence are separate](#why-commitments-and-evidence-are-separate)
- [How to revise a deadline safely](#how-to-revise-a-deadline-safely)
- [How to reconcile a failed command](#how-to-reconcile-a-failed-command)
- [How to back up and restore](#how-to-back-up-and-restore)
- [Build a temporary evidence ledger](#build-a-temporary-evidence-ledger)
- [How to run the checks](#how-to-run-the-checks)

## Reference

Import `Ledger`, `LedgerError` and `open_hermes_ledger` from `urgency_engine`. Python 3.11–3.14 is supported. Date entry needs IANA timezone data supplied by the OS or Python's `tzdata` package. Missing data produces `INVALID_INPUT`, field `timezone`, with installation guidance; it never guesses an offset. Offline tests use synthetic TZif files, not the machine's timezone database.

### Connections and ownership

```text
Ledger(connection, *, clock, artifact_root, busy_timeout_ms=5000)
open_hermes_ledger(*, clock, artifact_root, busy_timeout_ms=5000)
```

Both return a context manager. `connection` is a dedicated `sqlite3.Connection`, with no active transaction. The ledger closes it on context exit or initialization failure; a connection rejected for an existing transaction remains the caller's responsibility. Never share a ledger between threads or nest its operations inside an external transaction. Returned records are independent, JSON-compatible dictionaries/lists, not live ORM objects.

`clock` is a callable returning an aware `datetime`. `artifact_root` is an existing directory configured by trusted application code, never supplied by an agent's command payload. Keep that root and its ancestors under operator control. Do not point it at a filesystem writable by an adversary.

`open_hermes_ledger` lazily calls the documented `plugins.plugin_storage.plugin_db('hermes-urgency-engine')` on **every open**, following the active Hermes profile without caching a connection. Mutations use this opener. Native read-only queries and the pre-turn hook use `open_readonly_ledger` to read an existing database without initializing one. Offline callers inject their own temporary connection. No Hermes modules are needed merely to import the library. See the [Hermes plugin documentation](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/).

`busy_timeout_ms` is an integer from 0 to 5000 (not bool). The default retains the library wait budget; the native hook uses 100 ms to fail open promptly under a held write lock.

### Reads

```text
get_project(project_id)
get_task(board_id, task_id)
list_projects(limit=100, after=None)
list_tasks(project_id, limit=100, after=None)
list_deadlines(project_id, limit=100, after=None)
history(entity_type, entity_id, limit=100, after=None)
progress(project_id)
explain_deadline(deadline_id)
```

All reads use a consistent transaction and write nothing.

- `get_project`: project columns from the schema below.
- `get_task`: task columns plus `criteria`, the active criterion records ordered by ID.
- `list_projects`: projects ordered by ID, strictly after the optional ID cursor. Limit is 1–500. Continue with the last `project_id`; an empty page ends enumeration. No omission/duplication is promised across concurrent changes; use a stable database/backup for a stable enumeration.
- `list_tasks`: scoped task key dictionaries (`board_id`, `task_id`), ordered by that pair. The cursor is the last pair as a two-item list/tuple. Use `get_task` for details.
- `list_deadlines`: scoped deadline records ordered by ID; the cursor is the last `deadline_id`. Both scoped lists require an existing project, accept limits 1–500, and have the same concurrent-enumeration limits as `list_projects`.
- `history`: audit rows ordered by `sequence`, strictly after the optional integer cursor. Limit is 1–500. `entity_type` is `project`, `task`, `milestone` or `deadline`; task entity IDs are compact JSON arrays such as `["demo-board","demo-task"]`. Other entity IDs are their plain IDs. `before_json` and `after_json` are JSON strings. History includes successful commands only.
- `progress`: `{project_id, baseline_weight, current_weight, verified_weight, percent, state, last_verified_progress_at, revision}`. No baseline yields `baseline_weight=null`. No active criteria yields `percent=null`, `state='insufficient_evidence'`; otherwise state is `determinate`. Progress is the weighted fraction of active criterion revisions with accepted, uninvalidated matching evidence. A missing required criterion prevents 100%, even if floating-point rounding would conceal its weight. `last_verified_progress_at` is historical: reopening invalidates credit without erasing the last accepted-evidence time.
- `explain_deadline`: deadline columns plus `forecast`, `forecast_at`, nullable `actual_verified_at`, `baseline_variance_seconds`, `target_variance_seconds`, `input`, and `input_revisions`. Variance is actual verified completion minus the respective date when all scoped tasks have verified completion; otherwise it is projected completion minus that date. Positive means late. `forecast_at` is clock + summed stored work/buffers, not an empirical percentile. Work/buffers include every scoped task exactly once; callers update estimates explicitly. An empty scope has no actual completion. `input_revisions` records deadline/project revisions and `[board_id,task_id,revision]` tuples; `input` records aggregate seconds and evaluation time.

Forecasts reuse the unchanged [forecast policy](forecast.md#forecast-api), with the stored target and default policy. The latest explicitly stored forecast supplies cooldown state; read-only explanations do not advance it. No completion samples are invented: P50/P90 and on-time probability remain null. Unrepresentable projected dates return a named `INVALID_INPUT` rather than overflowing or inventing a date.

### Mutation signatures

Every command requires a nonempty `request_id` and `actor`. Update commands require `expected_revision` and nonempty `reason`. Arguments following `*` are keyword-only. Exact replay of a successful request returns its original response, even after later state changes, without another audit row or revision. A request ID reused with different arguments or operation yields `IDEMPOTENCY_CONFLICT`.

```text
create_project(project_id, title, *, request_id, actor)
upsert_task(board_id, task_id, *, project_id, goal, expected_revision,
            reason, request_id, actor, assignee=None, status='planned',
            remaining_seconds=0, verification_seconds=0,
            deployment_seconds=0, actual_seconds=0)
set_criteria(board_id, task_id, criteria, *, expected_revision,
             reason, request_id, actor)
baseline_scope(project_id, *, expected_revision, reason, request_id, actor)
set_deadline(deadline_id, *, project_id, kind, at, timezone, owner,
             source, request_id, actor, board_id=None, task_id=None,
             milestone_id=None)
revise_deadline(deadline_id, *, at, timezone, intent, expected_revision,
                reason, request_id, actor)
create_milestone(milestone_id, *, project_id, title, request_id, actor)
set_milestone_tasks(milestone_id, tasks, *, expected_revision,
                    reason, request_id, actor)
claim_complete(board_id, task_id, *, expected_revision, reason, request_id, actor)
reopen_task(board_id, task_id, *, expected_revision, reason, request_id, actor)
verify_artifact(board_id, task_id, criterion_id, artifact_relative_path, *,
                expected_revision, reason, request_id, actor)
snapshot_forecast(deadline_id, *, expected_revision, reason, request_id, actor)
```

| Command | Revision and result |
| --- | --- |
| `create_project` | New ID; returns project at revision 1. |
| `upsert_task` | Task revision, or 0 to create. Returns task and active criteria. Full metadata replacement: omitted optional fields reset to their defaults. Project cannot change. Accepts only planned/running/blocked; existing verified/claimed status is preserved. |
| `set_criteria` | Task revision. Replaces the entire active set; returns task. Invalidates accepted evidence, clears actual completion and inserts immutable criterion revisions. |
| `baseline_scope` | Project revision. One baseline per project; returns snapshot ID, weight, criterion count and audit sequence watermark. The immutable audit prefix through that watermark retains the exact original scope; the summary avoids copying every description into progress reads. |
| `set_deadline` | New ID; returns deadline at revision 1. Scope is project, a board/task pair, or a milestone, never both task and milestone. Every reference must belong to the same project. |
| `revise_deadline` | Deadline revision. Returns revised deadline; original date/owner/source remain unchanged and audit retains actor/reason and all targets. |
| `create_milestone` | New ID; returns milestone with empty tasks. |
| `set_milestone_tasks` | **Project** revision. Replaces membership with a list of board/task pairs; duplicates collapse. Returns milestone plus sorted `tasks`. |
| `claim_complete` | Task revision. Returns task; adds no evidence and never sets actual completion. Already verified tasks stay verified. |
| `reopen_task` | Task revision. Returns task; invalidates all its evidence, clears actual completion and records a current-scope event. |
| `verify_artifact` | Task revision captured before hashing and rechecked at commit. Returns an evidence record, including criterion revision and observed digest. First acceptance increments task/project revisions. Another request for already accepted evidence returns that evidence without additional credit or a new progress timestamp (the successful request is still audited). |
| `snapshot_forecast` | Deadline revision. Returns snapshot ID plus the full explanation and input revisions. Advances project revision, not the deadline target or revision. |

Task updates increment their project revision. Milestone/deadline/scope/forecast mutations also increment project revision. Creation conflicts do not overwrite existing IDs. Criterion replacement increments each reused criterion ID's revision; removed criteria remain historical.

Criteria are lists of **exactly** these keys:

```text
{criterion_id: str, description: str, weight: number,
 required: bool, expected_sha256: lowercase 64-character hex string | None}
```

Weights must be positive; numeric durations must be nonnegative. Numbers are finite int/float, not bool, and at most `1e12`. Identifiers are 1–128 characters, with no path separators or control characters. Ordinary text is nonempty, at most 4096 characters, with no NUL or unpaired Unicode surrogate. Assignee uses identifier limits. Commands, after canonical JSON serialization, are capped at 256 KiB. Supported capacity is 10,000 tasks per project and 100 active criteria per task; excess produces `INVALID_INPUT` (task capacity field `tasks_capacity`). Queries use project, active-criterion, evidence and audit indexes. The ledger imposes no automatic audit retention; disk use grows with history.

### Unknown remaining work and incomplete scope

`upsert_task(..., remaining_seconds=None)` explicitly records unknown work. JSON
operator requests use `null`. Reads return `remaining_seconds: null` and
`remaining_known: false`; numeric zero remains known zero. Omitting the argument
still defaults to known zero for legacy callers. Verification, deployment and
actual durations still require finite nonnegative numbers. Canonical imports
explicitly supply unknown work and preserve any later local estimate.

If any scoped task has unknown work, or deadline scope is empty, forecast work,
slack, projected completion and probability are null. Confidence is
`insufficient_evidence`. Urgency is at least elevated, imminent near the target,
and overdue after it; an existing higher level is not downgraded. Numeric samples
cannot replace the absent estimate. Variance remains null unless actual verified
completion supplies a comparison. Historical numeric snapshots remain historical
evidence, not a current estimate.

A project containing a task with no active criteria returns `percent: null`,
`state: insufficient_evidence`, and an `unscoped_task_count`. It never treats an
imported unscoped task as zero-weight completed work. Recorded baseline, current
weight and verified weight remain visible and are not rewritten.

### Dates and evidence

`kind` is `hard` or `soft`. `at` is RFC3339 with seconds and an explicit `Z` or `±HH:MM` offset, optionally 1–6 fractional digits. `timezone` is a valid IANA name. Its offset and wall time must agree with the timestamp at that instant. Spring-forward gaps and naive dates are rejected; a fall-back fold must specify one of the two valid offsets. Storage is normalized to UTC; named display timezone is retained.

`intent` is `operator` or `automatic`. A hard deadline rejects `automatic`; a soft target allows either. **This is an auditable policy distinction, not authentication.** A host adapter must enforce its own authorization before calling these APIs. No tokens or approval gates are implemented here.

An artifact name is a forward-slash relative path below the configured root. Absolute paths, traversal, empty/dot components, backslashes, colons, trailing dots/spaces, symlinks/reparse points and nonregular files are rejected. The opener checks POSIX directory descriptors or the Windows opened-handle path before reading. Hashing streams at most 16 MiB, with size/identity/change checks. No database transaction spans file I/O. A missing expected digest or mismatched bytes earns no credit. Concurrent task edits, reopen or scope replacement produce `REVISION_CONFLICT` at acceptance, not stale credit.

Evidence proves **byte identity at verification time only**. It does not prove tests passed, code is correct, or a deployment is live. It does not monitor later file edits/deletions; accepted historical evidence remains until explicit reopen/scope replacement. The ledger never executes artifacts or fetches URLs. Files are not required to stay present for exact request replay. All active criteria must have evidence for task `actual_verified_at`; clock time alone never sets it.

### Schema versions 1 and 2

Schema 2 adds `tasks.remaining_known INTEGER NOT NULL DEFAULT 1` constrained to
0 or 1. Existing schema-1 durations retain their known meaning. Unknown work is
stored as numeric zero plus flag 0, but public reads, new audit payloads and new
forecasts project it as null. Never read the numeric compatibility column alone.
Read-only opens accept schema 1 without migration. A write-open validates the
schema and integrity before atomically adding the column and advancing the
version; failure rolls back both. Historical audit and replay responses are not
rewritten. Schema-1-only code refuses schema 2: rollback needs compatible code
or a consistent pre-upgrade backup, not a forced version-number downgrade.


```text
projects --< tasks --< criteria(revisions) --< evidence
    |           |
    |           +--< milestone_tasks >-- milestones
    |                                       |
    +--< deadlines (project / task / milestone scope)
    +--< snapshots (baseline / current / forecast)

Every successful mutation --> audit + operations (same transaction)
```

| Table | Key and stored columns |
| --- | --- |
| `projects` | `project_id` PK; title, revision, created_at |
| `tasks` | `(board_id,task_id)` PK; project_id FK, assignee, goal, status, remaining_seconds, verification_seconds, deployment_seconds, actual_seconds, actual_verified_at, last_verified_progress_at, revision |
| `criteria` | `(board_id,task_id,criterion_id,criterion_revision)` PK; task FK, description, weight, required, active, expected_sha256 |
| `deadlines` | `deadline_id` PK; project_id FK, nullable task FK pair / milestone_id FK, kind, original_at, target_at, timezone, owner, source, revision |
| `milestones` | `milestone_id` PK; project_id FK, title |
| `milestone_tasks` | `(milestone_id,board_id,task_id)` PK; milestone and task FKs |
| `evidence` | `evidence_id` PK (first acceptance request ID); criterion-revision FK, artifact_relative_path, observed_sha256, checked_at, invalidated_at |
| `snapshots` | `snapshot_id` PK; project_id FK, kind, recorded_at, payload_json |
| `operations` | `request_id` PK; payload_sha256, response_json |
| `audit` | `sequence` PK; request_id, entity_type, entity_id, action, actor, reason, occurred_at, before_json, after_json |

New storage uses `PRAGMA user_version=2`, foreign keys and atomic `BEGIN IMMEDIATE`; mutation also uses `BEGIN IMMEDIATE` with a configurable busy timeout (5000 ms by default) and rollback on failure. Reads use `BEGIN`. A nonempty unknown database, future version, incomplete layout or failed integrity check is refused without repair/deletion. One active criterion per ID, one accepted evidence record per criterion revision, and one baseline per project are enforced by unique indexes. Internal bounded snapshots contain summaries, up to 100 criteria for scope changes, or at most 10,000 task revision tuples for forecasts. Retained audit JSON records and historical criteria allow reconstruction; do not delete history independently.

### Errors

`LedgerError` exposes `code`, `operation`, optional `field`, `retryable`, `side_effects` and safe `next_action`. Raw SQL errors and absolute artifact paths are not included. Caller-authored text is retained in audit: never place credentials, private paths or secrets in titles, actors, reasons, goals or other text fields; the ledger is not a general-purpose redactor.

| Code | Meaning / response |
| --- | --- |
| `INVALID_INPUT` | Correct the named field, timezone data or capacity. |
| `NOT_FOUND` | Resolve the missing ID. |
| `REVISION_CONFLICT` | Reload state; reconcile the change rather than overwrite. |
| `IDEMPOTENCY_CONFLICT` | Use the original arguments, or a new ID for a genuinely new command. |
| `EVIDENCE_MISMATCH` | Bytes differ from the declared expected digest; no credit. |
| `UNSUPPORTED_SCHEMA` | Preserve DB; use a compatible reader. |
| `CORRUPT_STATE` | Stop writers; preserve DB and restore a verified backup. |
| `STORE_BUSY` | Retry the identical request after the writer finishes; the only retryable code. |
| `STORAGE_FAILURE` | Preserve state and reconcile request ID before retrying. |

Validation and known rolled-back transactions report `side_effects='none'`. Failure during commit or failed rollback reports `unknown`, even if a subsequent read can reconcile it. Never treat an uncertain I/O result as a fresh command.

## Why commitments and evidence are separate

A target is a promise; a forecast is an estimate. Moving a target whenever work slips erases the difference. The ledger keeps original dates immutable and makes every target revision explicit. Forecast snapshots retain their input revisions and cooldown state without rewriting those promises.

Likewise, a task status is a claim, not proof. Weighted criteria define the denominator; accepted evidence defines the numerator. Reopening removes credit while keeping history. Adding scope can lower current progress without rewriting the baseline. Milestones are views of membership, so sharing a task across milestones never multiplies project progress.

SQLite trades concurrent writer throughput for atomic local changes. Each command commits entity changes, audit and replay response together. Hashing happens outside transactions, followed by a revision comparison inside the acceptance transaction. This permits another writer to replan during a slow hash without accepting obsolete proof. Whole-project JSON rewrites would not provide this protection. This library deliberately does not add a server, scheduler or learned calibration; those require separate integration and evidence.

## How to revise a deadline safely

1. Read `explain_deadline(deadline_id)` and retain its `revision`.
2. Call `revise_deadline` with that revision, a new request ID, actor, reason, explicit-offset timestamp and IANA zone. Use `intent='operator'` for a deliberate hard-target change; automatic replanning may change only soft targets.
3. Read the explanation and `history('deadline', deadline_id)` to verify original/target separation and who changed it.

For `REVISION_CONFLICT`, reload and reconcile. For a timezone error, install IANA data or correct the explicit offset; do not strip the offset. See [date rules](#dates-and-evidence).

## How to reconcile a failed command

1. Retain the command's request ID and exact original arguments before sending it.
2. For `STORE_BUSY`, wait for the competing writer and make a bounded retry of the identical request. The library does not loop indefinitely.
3. For uncertain storage outcomes, stop new writes, reopen the preserved DB and inspect the relevant history. Reissue the **identical** request to recover its saved response if it committed. Never use a fresh ID to blindly repeat an uncertain command.
4. For a revision conflict, read the current entity and decide whether the intended change still applies. A revised command needs a new request ID and current revision.

Verify there is one audit row for the successful request. An exact replay returns historical response data, not necessarily today's state; call a getter for current state. See [errors](#errors).

## How to back up and restore

Stop callers before source rollback or restoration. Preserve the original database and any WAL files; do not downgrade `user_version`, drop tables, delete state or copy only a live WAL database's main file. Use SQLite's backup API to create a consistent backup. Revert source through your normal review process, and open only a schema supported by that source.

This complete demonstration touches only temporary fixtures:

```python
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from urgency_engine import Ledger

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    with Ledger(sqlite3.connect(root / 'original.db'),
                clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
                artifact_root=root) as ledger:
        ledger.create_project('demo', 'Demo', request_id='create', actor='operator')
        with closing(sqlite3.connect(root / 'backup.db')) as backup:
            ledger.connection.backup(backup)
            assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    with closing(sqlite3.connect(root / 'backup.db')) as backup:
        with closing(sqlite3.connect(root / 'restored.db')) as restored:
            backup.backup(restored)
    with Ledger(sqlite3.connect(root / 'restored.db'),
                clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
                artifact_root=root) as ledger:
        assert ledger.get_project('demo')['title'] == 'Demo'
    print('backup and restore verified')
```

If integrity checks fail, keep the original and backup for investigation. Do not repair by recreating an empty ledger. Disk growth, export and automatic retention are not managed by this version.

## Build a temporary evidence ledger

### Step 1: Install the library

From the repository root, use a virtual environment with Python 3.11–3.14:

```sh
python -m pip install .
```

If your platform lacks IANA timezone data, install it in that environment with `python -m pip install tzdata`. This is library installation, not Hermes plugin installation.

### Step 2: Save and run this example

Save the following as `ledger_demo.py`, then run `python ledger_demo.py`. All database and artifact writes are temporary; no Hermes profile is touched.

```python
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import sqlite3
import tempfile
from urgency_engine import Ledger

now = datetime(2030, 1, 1, tzinfo=timezone.utc)
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    artifact = root / 'result.txt'
    artifact.write_bytes(b'demo result\n')
    # Declare expected bytes before verification; no caller-supplied verified flag.
    expected = hashlib.sha256(b'demo result\n').hexdigest()
    with Ledger(sqlite3.connect(root / 'ledger.db'), clock=lambda: now,
                artifact_root=root) as ledger:
        ledger.create_project('demo', 'Demo', request_id='project', actor='operator')
        task = ledger.upsert_task('demo-board', 'demo-task', project_id='demo',
            goal='Produce the agreed result', remaining_seconds=600,
            verification_seconds=60, deployment_seconds=30, expected_revision=0,
            reason='Initial plan', request_id='task', actor='operator')
        task = ledger.set_criteria('demo-board', 'demo-task', [dict(
            criterion_id='bytes', description='Agreed result bytes', weight=1,
            required=True, expected_sha256=expected)], expected_revision=task['revision'],
            reason='Acceptance scope', request_id='scope', actor='operator')
        ledger.baseline_scope('demo', expected_revision=ledger.get_project('demo')['revision'],
            reason='Record original scope', request_id='baseline', actor='operator')
        ledger.set_deadline('demo-date', project_id='demo', kind='hard',
            at='2030-01-02T00:00:00Z', timezone='UTC', owner='operator', source='Demo plan',
            request_id='deadline', actor='operator')
        task = ledger.claim_complete('demo-board', 'demo-task', expected_revision=task['revision'],
            reason='Work reported', request_id='claim', actor='worker')
        assert ledger.progress('demo')['percent'] == 0
        ledger.verify_artifact('demo-board', 'demo-task', 'bytes', 'result.txt',
            expected_revision=task['revision'], reason='Independent byte check',
            request_id='verify', actor='verifier')
        assert ledger.progress('demo')['percent'] == 100
        assert ledger.explain_deadline('demo-date')['actual_verified_at'] == now.isoformat()
        print('claim: 0%; verified bytes: 100%')
```

Expected output: `claim: 0%; verified bytes: 100%`. A missing file, digest mismatch or stale task revision raises `LedgerError` instead of crediting progress. See [reconciliation](#how-to-reconcile-a-failed-command).

### What you built

A durable local ledger where a claim earns no progress and matching bytes earn criterion credit. It does not establish semantic correctness of the result. Continue with the [API reference](#reference) or [backup how-to](#how-to-back-up-and-restore).

## How to run the checks

Run the offline suite from the repository root:

```sh
python -m unittest discover -s tests -v
```

The forecast tests remain unchanged. Ledger tests use real temporary SQLite databases, spawned competing processes, deterministic timezone fixtures, injected rollback/uncertain-commit failures, and a 10,000-task/20,000-criterion read fixture. Its printed measurement includes elapsed progress-query time, `tracemalloc` peak Python allocation (not process RSS or SQLite native memory), and SQL statement count. It is not a live UI responsiveness or write-throughput claim. Symlink coverage requires host privileges; the CI gate rejects skipped offline tests.

Separately, in a Python environment with the documented Hermes plugin storage installed, run the actual storage-opener integration check:

```sh
python -c "import sys, unittest; sys.path.insert(0, 'tests'); result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName('test_ledger.LedgerTests.check_documented_plugin_storage_isolates_homes')); raise SystemExit(not (result.wasSuccessful() and not result.skipped))"
```

This switches only temporary `HERMES_HOME` directories, checks that the real documented storage API selects each home, and verifies closure and isolation. It is explicitly separate from offline discovery; an unavailable Hermes dependency is reported, not counted as integration success. For plugin doctor/validate and native dispatch checks, use the [runtime testing guide](runtime.md#contributor-verification). Live Desktop/gateway acceptance remains separate and necessary before a complete plugin release.

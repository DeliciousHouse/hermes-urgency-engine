from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
import hashlib
import multiprocessing
import time
from unittest.mock import patch
from struct import pack
from zoneinfo import ZoneInfo, reset_tzpath, TZPATH


def fixture_zones(root):
    """Small deterministic TZif files, no downloaded platform tz database."""
    folder = root / 'zones'
    folder.mkdir(exist_ok=True)
    header = b'TZif\0' + b'\0' * 15
    (folder / 'UTC').write_bytes(header + pack('>6l', 0, 0, 0, 0, 1, 4) + pack('>lbb', 0, 0, 0) + b'UTC\0')
    # DST entry and exit, with ordinary -05:00 / summer -04:00.
    transitions = [int(datetime(2030, 3, 10, 7, tzinfo=timezone.utc).timestamp()),
                   int(datetime(2030, 11, 3, 6, tzinfo=timezone.utc).timestamp())]
    (folder / 'Synthetic').write_bytes(header + pack('>6l', 0, 0, 0, 2, 2, 8)
        + pack('>2l', *transitions) + bytes([1, 0])
        + pack('>lbb', -18000, 0, 0) + pack('>lbb', -14400, 1, 4) + b'STD\0DST\0')
    reset_tzpath([str(folder)])
    ZoneInfo.clear_cache()


def process_command(db, root, action, ready, go, output):
    from urgency_engine.ledger import Ledger, LedgerError
    class PausedLedger(Ledger):
        def _hash_artifact(self, name):
            result = super()._hash_artifact(name)
            ready.set()
            if not go.wait(20):
                raise RuntimeError('fixture timed out')
            return result
    try:
        if action == 'startup':
            ready.set()
            if not go.wait(20):
                raise RuntimeError('fixture timed out')
        cls = PausedLedger if action == 'verify' else Ledger
        with cls(sqlite3.connect(db), clock=lambda: NOW, artifact_root=root) as ledger:
            if action not in ('verify', 'startup'):
                ready.set()
                if not go.wait(20):
                    raise RuntimeError('fixture timed out')
            if action in ('replay', 'startup'):
                result = ledger.create_project('shared', 'Shared', request_id='shared', actor='operator')
            elif action == 'persist':
                fixture_zones(Path(root))
                ledger.create_project('p', 'Project', request_id='p', actor='operator')
                ledger.upsert_task('b', 't', project_id='p', goal='Deliver', expected_revision=0,
                    request_id='t', actor='operator', reason='Plan')
                ledger.set_criteria('b', 't', [dict(criterion_id='c', description='Check', weight=2, required=True, expected_sha256=None)],
                    expected_revision=1, request_id='scope', actor='operator', reason='Scope')
                result = ledger.set_deadline('d', project_id='p', kind='hard', at='2030-01-02T00:00:00Z', timezone='UTC',
                    owner='operator', source='Plan', request_id='d', actor='operator')
            elif action == 'verify':
                result = ledger.verify_artifact('b', 't', 'c', 'artifact', expected_revision=2,
                    request_id='verify', actor='verifier', reason='Check')
            else:
                result = ledger.upsert_task('b', 't', project_id='p', goal=action, expected_revision=2,
                    request_id=action, actor='operator', reason='Edit')
            output.put(('ok', result))
    except LedgerError as exc:
        output.put(('error', exc.code))

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'ledger.db'
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))

    def prepare_task(self):
        with self.open() as ledger:
            ledger.create_project('p', 'Project', request_id='p', actor='operator')
            ledger.upsert_task('b', 't', project_id='p', goal='Deliver', expected_revision=0,
                request_id='t', actor='operator', reason='Plan')
            (self.root / 'artifact').write_bytes(b'proof')
            ledger.set_criteria('b', 't', [dict(criterion_id='c', description='Check', weight=1, required=True,
                expected_sha256=hashlib.sha256(b'proof').hexdigest())], expected_revision=1, reason='Scope', request_id='scope', actor='operator')

    def spawn(self, action, go):
        ctx = multiprocessing.get_context('spawn')
        ready, output = ctx.Event(), ctx.Queue()
        process = ctx.Process(target=process_command, args=(str(self.db), str(self.root), action, ready, go, output))
        process.start()
        def cleanup():
            go.set()
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(5)
            output.close()
        self.addCleanup(cleanup)
        self.assertTrue(ready.wait(20), 'child failed to become ready')
        return process, output

    def open(self):
        from urgency_engine.ledger import Ledger
        return Ledger(sqlite3.connect(self.db), clock=lambda: NOW, artifact_root=self.root)

    def test_project_persists_and_replay_is_exact(self):
        """protects=durability; fails_when=replay writes twice; why_new=storage;
        seam=none; no_claim=gateway restart integration.
        """
        with self.open() as ledger:
            original = ledger.create_project('demo', 'Demo', request_id='create', actor='operator')
            self.assertEqual(original['revision'], 1)
        with self.open() as ledger:
            self.assertEqual(ledger.get_project('demo'), original)
            self.assertEqual(ledger.create_project('demo', 'Demo', request_id='create', actor='operator'), original)
            self.assertEqual(len(ledger.history('project', 'demo')), 1)
            from urgency_engine.ledger import LedgerError
            with self.assertRaises(LedgerError) as caught:
                ledger.create_project('demo', 'Changed', request_id='create', actor='operator')
            self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')
            self.assertEqual(caught.exception.side_effects, 'none')

    def test_task_scope_baseline_and_claim_have_no_credit(self):
        """protects=scope and claims; fails_when=claims count; why_new=ledger;
        seam=none; no_claim=Desktop display or artifact semantics.
        """
        with self.open() as ledger:
            ledger.create_project('demo', 'Demo', request_id='p', actor='operator')
            task = ledger.upsert_task('board', 'task', project_id='demo', goal='Deliver',
                                      expected_revision=0, reason='Plan', request_id='t', actor='operator')
            self.assertEqual(task['revision'], 1)
            self.assertIsNone(ledger.progress('demo')['percent'])
            task = ledger.set_criteria('board', 'task', [
                dict(criterion_id='one', description='First', weight=1, required=True, expected_sha256='a'*64),
                dict(criterion_id='two', description='Second', weight=3, required=True, expected_sha256='b'*64),
            ], expected_revision=1, reason='Scope', request_id='c', actor='operator')
            baseline = ledger.baseline_scope('demo', expected_revision=ledger.get_project('demo')['revision'],
                                            reason='Baseline', request_id='b', actor='operator')
            self.assertEqual(baseline['weight'], 4)
            claimed = ledger.claim_complete('board', 'task', expected_revision=task['revision'],
                                            reason='Claim', request_id='claim', actor='agent')
            self.assertIsNone(claimed['actual_verified_at'])
            self.assertEqual(ledger.progress('demo')['percent'], 0)
            ledger.set_criteria('board', 'task', [], expected_revision=claimed['revision'],
                                reason='Removed', request_id='remove', actor='operator')
            result = ledger.progress('demo')
            self.assertEqual(result['baseline_weight'], 4)
            self.assertEqual(result['current_weight'], 0)
            self.assertIsNone(result['percent'])
            self.assertEqual(ledger.get_task('board', 'task')['criteria'], [])

    def test_artifact_credit_reopen_and_replacement(self):
        """protects=weighted evidence; fails_when=claim or stale bytes credit;
        why_new=verification; seam=none; no_claim=semantic correctness.
        """
        import hashlib
        from urgency_engine.ledger import LedgerError
        (self.root / 'one').write_bytes(b'one')
        (self.root / 'two').write_bytes(b'two')
        with self.open() as ledger:
            ledger.create_project('demo', 'Demo', request_id='p', actor='operator')
            ledger.upsert_task('board', 'task', project_id='demo', goal='Deliver', expected_revision=0,
                               reason='Plan', request_id='t', actor='operator')
            criteria = [dict(criterion_id=name, description=name, weight=weight, required=True,
                            expected_sha256=hashlib.sha256(name.encode()).hexdigest())
                        for name, weight in [('one', 1), ('two', 3)]]
            task = ledger.set_criteria('board', 'task', criteria, expected_revision=1,
                                       reason='Scope', request_id='c', actor='operator')
            for path in ('missing', 'two', '../one', '/one', 'C:\\one', 'one:stream'):
                with self.subTest(path=path), self.assertRaises(LedgerError):
                    ledger.verify_artifact('board', 'task', 'one', path, expected_revision=task['revision'],
                                           reason='Verify', request_id='bad', actor='verifier')
                self.assertEqual(ledger.progress('demo')['verified_weight'], 0)
            result = ledger.verify_artifact('board', 'task', 'one', 'one', expected_revision=task['revision'],
                                            reason='Verify', request_id='v1', actor='verifier')
            self.assertEqual(result['observed_sha256'], criteria[0]['expected_sha256'])
            self.assertEqual(ledger.progress('demo')['percent'], 25)
            # Replay does not re-read deleted bytes, nor add another evidence row.
            (self.root / 'one').unlink()
            self.assertEqual(ledger.verify_artifact('board', 'task', 'one', 'one', expected_revision=task['revision'],
                                                    reason='Verify', request_id='v1', actor='verifier'), result)
            task = ledger.get_task('board', 'task')
            task = ledger.claim_complete('board', 'task', expected_revision=task['revision'],
                                         reason='Claim', request_id='claim', actor='agent')
            self.assertEqual(ledger.progress('demo')['percent'], 25)
            ledger.verify_artifact('board', 'task', 'two', 'two', expected_revision=task['revision'],
                                   reason='Verify', request_id='v2', actor='verifier')
            task = ledger.get_task('board', 'task')
            self.assertEqual(task['status'], 'verified')
            self.assertEqual(task['actual_verified_at'], NOW.isoformat())
            self.assertEqual(ledger.progress('demo')['percent'], 100)
            ledger.verify_artifact('board', 'task', 'two', 'two', expected_revision=task['revision'],
                                   reason='Duplicate', request_id='duplicate', actor='verifier')
            self.assertEqual(ledger.progress('demo')['verified_weight'], 4)
            task = ledger.get_task('board', 'task')
            reopened = ledger.reopen_task('board', 'task', expected_revision=task['revision'],
                                          reason='Rework', request_id='reopen', actor='operator')
            self.assertEqual(ledger.progress('demo')['percent'], 0)
            self.assertIsNone(reopened['actual_verified_at'])
            ledger.verify_artifact('board', 'task', 'two', 'two', expected_revision=reopened['revision'],
                                   reason='Verify', request_id='again', actor='verifier')
            task = ledger.get_task('board', 'task')
            ledger.set_criteria('board', 'task', criteria, expected_revision=task['revision'],
                                reason='New scope', request_id='replace', actor='operator')
            self.assertEqual(ledger.progress('demo')['percent'], 0)
            self.assertEqual(ledger.connection.execute('SELECT count(*) FROM evidence WHERE invalidated_at IS NULL').fetchone()[0], 0)

    def test_deadline_targets_and_independent_forecast(self):
        """protects=commitments; fails_when=auto moves hard target; why_new=dates;
        seam=none; no_claim=host authorization or learned calibration.
        """
        from urgency_engine.ledger import LedgerError
        from urgency_engine import ForecastInput, calculate_forecast
        with self.open() as ledger:
            ledger.create_project('demo', 'Demo', request_id='p', actor='operator')
            ledger.upsert_task('b', 't', project_id='demo', goal='Deliver', remaining_seconds=100,
                               verification_seconds=20, deployment_seconds=30, expected_revision=0,
                               reason='Plan', request_id='t', actor='operator')
            for kind in ('hard', 'soft'):
                deadline = ledger.set_deadline(kind, project_id='demo', kind=kind,
                    at='2030-01-02T00:00:00+00:00', timezone='UTC', owner='operator', source='plan',
                    board_id='b', task_id='t', request_id=kind, actor='operator')
                if kind == 'hard':
                    count = ledger.connection.total_changes
                    with self.assertRaises(LedgerError) as caught:
                        ledger.revise_deadline(kind, at='2030-01-03T00:00:00Z', timezone='UTC',
                            intent='automatic', expected_revision=1, reason='Replan', request_id='bad', actor='agent')
                    self.assertEqual(caught.exception.code, 'INVALID_INPUT')
                    self.assertEqual(ledger.connection.total_changes, count)
                revised = ledger.revise_deadline(kind, at='2030-01-03T00:00:00Z', timezone='UTC',
                    intent='operator' if kind == 'hard' else 'automatic', expected_revision=1,
                    reason='Replan', request_id='rev-' + kind, actor='operator')
                self.assertEqual(revised['original_at'], deadline['original_at'])
                self.assertNotEqual(revised['target_at'], revised['original_at'])
                explained = ledger.explain_deadline(kind)
                expected = calculate_forecast(ForecastInput(datetime(2030, 1, 3, tzinfo=timezone.utc), 100, 20, 30), now=NOW)
                self.assertEqual(explained['forecast']['effective_slack_seconds'], expected.effective_slack_seconds)
                self.assertIsNone(explained['actual_verified_at'])
                self.assertEqual(explained['target_variance_seconds'], 150 - 172800)
                snapshot = ledger.snapshot_forecast(kind, expected_revision=2, reason='Measure', request_id='f-'+kind, actor='agent')
                self.assertEqual(snapshot['forecast'], explained['forecast'])
                self.assertEqual(ledger.explain_deadline(kind)['target_at'], revised['target_at'])
            self.assertEqual(len(ledger.history('deadline', 'hard')), 3)

    def test_milestone_membership_scope_and_project_isolation(self):
        """protects=scope accounting; fails_when=membership multiplies progress;
        why_new=milestones; seam=none; no_claim=UI portfolio routing.
        """
        from urgency_engine.ledger import LedgerError
        with self.open() as ledger:
            for p in ('p', 'q'):
                ledger.create_project(p, p, request_id=p, actor='operator')
                ledger.upsert_task(p, 'same', project_id=p, goal='Deliver', expected_revision=0,
                                   reason='Plan', request_id='task-'+p, actor='operator', remaining_seconds=10)
                ledger.set_criteria(p, 'same', [dict(criterion_id='c', description='Check', weight=1, required=True, expected_sha256=None)],
                                    expected_revision=1, reason='Scope', request_id='scope-'+p, actor='operator')
            for m in ('m', 'n'):
                ledger.create_milestone(m, project_id='p', title=m, request_id=m, actor='operator')
                result = ledger.set_milestone_tasks(m, [('p', 'same'), ('p', 'same')],
                    expected_revision=ledger.get_project('p')['revision'], reason='Group', request_id='group-'+m, actor='operator')
                self.assertEqual(result['tasks'], [['p', 'same']])
            self.assertEqual(ledger.progress('p')['current_weight'], 1)
            self.assertEqual(ledger.progress('q')['current_weight'], 1)
            with self.assertRaises(LedgerError):
                ledger.set_milestone_tasks('m', [('q', 'same')], expected_revision=ledger.get_project('p')['revision'],
                    reason='Wrong project', request_id='wrong', actor='operator')
            ledger.set_deadline('d', project_id='p', milestone_id='m', kind='soft', at='2030-01-02T00:00:00Z',
                                timezone='UTC', owner='operator', source='Plan', request_id='d', actor='operator')
            self.assertEqual(ledger.explain_deadline('d')['input']['remaining_seconds'], 10)
            ledger.set_milestone_tasks('m', [], expected_revision=ledger.get_project('p')['revision'],
                reason='Ungroup', request_id='ungroup', actor='operator')
            self.assertIsNone(ledger.explain_deadline('d')['input']['remaining_seconds'])
            self.assertEqual(len(ledger.history('milestone', 'm')), 3)

    def test_process_replay_and_competing_revisions(self):
        """protects=atomicity; fails_when=two writers both win; why_new=IPC;
        seam=process event gates; no_claim=distributed or network filesystems.
        """
        self.prepare_task()
        ctx = multiprocessing.get_context('spawn')
        for actions in [('replay', 'replay'), ('edit-a', 'edit-b')]:
            go = ctx.Event()
            workers = [self.spawn(action, go) for action in actions]
            go.set()
            outcomes = [queue.get(timeout=20) for _, queue in workers]
            for process, _ in workers:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            if actions[0] == 'replay':
                self.assertEqual(outcomes[0], outcomes[1])
                with self.open() as ledger:
                    self.assertEqual(len(ledger.history('project', 'shared')), 1)
            else:
                self.assertEqual(sorted(x[0] for x in outcomes), ['error', 'ok'])
                self.assertIn(('error', 'REVISION_CONFLICT'), outcomes)
                with self.open() as ledger:
                    self.assertEqual(ledger.get_task('b', 't')['revision'], 3)
                    self.assertEqual(len(ledger.history('task', '["b","t"]')), 3)

    def test_inflight_verification_replays_after_initial_lookup_misses(self):
        """Use two real connections; commit the winner before the retry captures the task."""
        from contextlib import contextmanager
        from urgency_engine.ledger import LedgerError

        for actor in ('verifier', 'other-verifier'):
            with self.subTest(actor=actor):
                self.db = self.root / (actor + '.db')
                self.prepare_task()
                args = ('b', 't', 'c', 'artifact')
                kwargs = dict(expected_revision=2, request_id='verify', reason='Check')
                with self.open() as retry, self.open() as winner:
                    transaction = retry._transaction
                    accepted = []

                    @contextmanager
                    def interleave(*tx_args, **tx_kwargs):
                        with transaction(*tx_args, **tx_kwargs):
                            yield
                        if not accepted:
                            accepted.append(winner.verify_artifact(*args, **kwargs, actor='verifier'))
                            (self.root / 'artifact').unlink()

                    with patch.object(retry, '_transaction', interleave):
                        if actor == 'verifier':
                            result = retry.verify_artifact(*args, **kwargs, actor=actor)
                            self.assertEqual(result, accepted[0])
                        else:
                            with self.assertRaises(LedgerError) as caught:
                                retry.verify_artifact(*args, **kwargs, actor=actor)
                            self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')
                            self.assertEqual(caught.exception.side_effects, 'none')
                    self.assertEqual(retry.get_task('b', 't')['revision'], 3)
                    self.assertEqual(retry.get_project('p')['revision'], 4)
                    for table in ('evidence', 'audit', 'operations'):
                        self.assertEqual(retry.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0],
                                         1 if table == 'evidence' else 4)

    def test_process_hash_compare_and_set_has_no_write_lock(self):
        """protects=evidence revision; fails_when=hash blocks or stale credits;
        why_new=split transaction; seam=post-hash gate; no_claim=file immutability.
        """
        self.prepare_task()
        go = multiprocessing.get_context('spawn').Event()
        process, output = self.spawn('verify', go)
        with self.open() as ledger:
            # Must succeed while child is paused outside its read transaction.
            ledger.set_criteria('b', 't', [], expected_revision=2, request_id='replace', reason='Rescope', actor='operator')
        go.set()
        self.assertEqual(output.get(timeout=20), ('error', 'REVISION_CONFLICT'))
        process.join(20)
        self.assertEqual(process.exitcode, 0)
        with self.open() as ledger:
            self.assertEqual(ledger.connection.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
            self.assertEqual(ledger.connection.execute("SELECT count(*) FROM audit WHERE request_id='verify'").fetchone()[0], 0)

    def test_process_reopen_invalidates_inflight_and_accepted_evidence(self):
        """protects=reopen ordering; fails_when=stale acceptance resurrects credit;
        why_new=both process orderings; seam=post-hash event; no_claim=network filesystems.
        """
        self.prepare_task()
        go = multiprocessing.get_context('spawn').Event()
        process, output = self.spawn('verify', go)
        with self.open() as ledger:
            ledger.reopen_task('b', 't', expected_revision=2, request_id='reopen',
                actor='operator', reason='Rework')
        go.set()
        self.assertEqual(output.get(timeout=20), ('error', 'REVISION_CONFLICT'))
        process.join(20)
        self.assertEqual(process.exitcode, 0)
        with self.open() as ledger:
            self.assertEqual(ledger.progress('p')['verified_weight'], 0)
            self.assertEqual(ledger.connection.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
            ledger.verify_artifact('b', 't', 'c', 'artifact', expected_revision=3,
                request_id='fresh', actor='verifier', reason='Check')
        with self.open() as ledger:
            self.assertEqual(ledger.progress('p')['percent'], 100)
            ledger.reopen_task('b', 't', expected_revision=4, request_id='reopen-again',
                actor='operator', reason='Rework')
            self.assertEqual(ledger.progress('p')['percent'], 0)
            self.assertIsNone(ledger.get_task('b', 't')['actual_verified_at'])
            self.assertEqual(ledger.connection.execute('SELECT count(*) FROM evidence WHERE invalidated_at IS NOT NULL').fetchone()[0], 1)

    def test_absent_digest_and_tiny_required_weight_cannot_complete(self):
        """protects=predeclared proof and completion; fails_when=rounding hides missing criterion;
        why_new=numeric boundary; seam=none; no_claim=artifact semantic correctness.
        """
        from urgency_engine.ledger import LedgerError
        self.prepare_task()
        with self.open() as ledger:
            criteria = ledger.get_task('b', 't')['criteria']
            items = [dict(criterion_id='c', description='Main', weight=1e12, required=True,
                          expected_sha256=criteria[0]['expected_sha256']),
                     dict(criterion_id='tiny', description='Required', weight=5e-324,
                          required=True, expected_sha256=None)]
            ledger.set_criteria('b', 't', items, expected_revision=2, request_id='weights',
                actor='operator', reason='Scope')
            ledger.verify_artifact('b', 't', 'c', 'artifact', expected_revision=3,
                request_id='main', actor='verifier', reason='Check')
            with self.assertRaises(LedgerError) as caught:
                ledger.verify_artifact('b', 't', 'tiny', 'artifact', expected_revision=4,
                    request_id='absent', actor='verifier', reason='Check')
            self.assertEqual(caught.exception.field, 'expected_sha256')
            self.assertLess(ledger.progress('p')['percent'], 100)
            self.assertIsNone(ledger.get_task('b', 't')['actual_verified_at'])

    def test_busy_rollback_read_only_and_unsupported_schema(self):
        """protects=storage failure safety; fails_when=partial audit commits;
        why_new=real SQLite failures; seam=abort trigger; no_claim=power-loss recovery.
        """
        from urgency_engine.ledger import Ledger, LedgerError
        self.prepare_task()
        with self.open() as ledger:
            locker = sqlite3.connect(self.db)
            self.addCleanup(locker.close)
            locker.execute('BEGIN IMMEDIATE')
            start = time.monotonic()
            try:
                with self.assertRaises(LedgerError) as caught:
                    ledger.create_project('busy', 'Busy', request_id='busy', actor='operator')
                self.assertEqual(caught.exception.code, 'STORE_BUSY')
                # gstack-timing-ok: bounds the documented 5 s busy timeout, not throughput.
                self.assertLess(time.monotonic() - start, 12)
            finally:
                locker.rollback()
            ledger.connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'synthetic private path'); END")
            with self.assertRaises(LedgerError) as caught:
                ledger.create_project('rollback', 'Rollback', request_id='rollback', actor='operator')
            self.assertNotIn('private', str(caught.exception))
            self.assertIsNone(ledger.connection.execute("SELECT 1 FROM projects WHERE project_id='rollback'").fetchone())
            self.assertIsNone(ledger.connection.execute("SELECT 1 FROM operations WHERE request_id='rollback'").fetchone())
            ledger.connection.execute('DROP TRIGGER fail_audit')
            before = ledger.connection.execute('SELECT count(*) FROM audit').fetchone()[0]
        read_only = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(read_only.close)
        with Ledger(read_only, clock=lambda: NOW, artifact_root=self.root) as ledger:
            with self.assertRaises(LedgerError) as caught:
                ledger.create_project('readonly', 'Read only', request_id='readonly', actor='operator')
        self.assertEqual(caught.exception.code, 'STORAGE_FAILURE')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA user_version=999')
        with self.assertRaises(LedgerError) as caught:
            self.open()
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_SCHEMA')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM audit').fetchone()[0], before)

    def test_datetime_validation_and_dst_fold(self):
        """protects=unambiguous commitments; fails_when=naive/gap accepted;
        why_new=IANA validation; seam=synthetic TZif; no_claim=system tzdata install.
        """
        from urgency_engine.ledger import LedgerError
        with self.open() as ledger:
            ledger.create_project('p', 'Project', request_id='p', actor='operator')
            for index, at in enumerate(['2030-01-01T00:00:00', '2030-03-10T02:30:00-05:00',
                '2030-01-01T00:00:00Z', '2030-11-03T01:30:00-03:00', '2030-01-01T00:00:00+00:60']):
                with self.subTest(at=at), self.assertRaises(LedgerError):
                    ledger.set_deadline(str(index), project_id='p', kind='hard', at=at, timezone='Synthetic',
                        owner='operator', source='Plan', request_id=str(index), actor='operator')
            for index, offset in enumerate(['-04:00', '-05:00']):
                row = ledger.set_deadline('fold-'+str(index), project_id='p', kind='hard',
                    at='2030-11-03T01:30:00'+offset, timezone='Synthetic', owner='operator', source='Plan',
                    request_id='fold-'+str(index), actor='operator')
                self.assertEqual(row['original_at'], ['2030-11-03T05:30:00+00:00', '2030-11-03T06:30:00+00:00'][index])
            with patch('zoneinfo.ZoneInfo', side_effect=__import__('zoneinfo').ZoneInfoNotFoundError):
                with self.assertRaises(LedgerError) as caught:
                    ledger.set_deadline('missing', project_id='p', kind='hard', at='2030-01-01T00:00:00Z',
                        timezone='UTC', owner='operator', source='Plan', request_id='missing', actor='operator')
                self.assertIn('tzdata', caught.exception.next_action)
            self.assertEqual(ledger.connection.execute('SELECT count(*) FROM deadlines').fetchone()[0], 2)

    def test_dot_only_project_and_milestone_ids_are_refused_at_creation(self):
        """protects=Desktop REST routing; fails_when='.' or '..' becomes a path segment;
        why_new=URL dot-segment normalization; seam=Ledger API; no_claim=existing dot IDs repaired.
        """
        from urgency_engine.ledger import LedgerError
        with self.open() as ledger:
            for value in ('.', '..', '...'):
                with self.subTest(project=value), self.assertRaises(LedgerError) as caught:
                    ledger.create_project(value, 'Project', request_id='p' + str(len(value)), actor='operator')
                self.assertEqual((caught.exception.code, caught.exception.field), ('INVALID_INPUT', 'project_id'))
            ledger.create_project('p.1', 'Project', request_id='p', actor='operator')
            for value in ('.', '..'):
                with self.subTest(milestone=value), self.assertRaises(LedgerError) as caught:
                    ledger.create_milestone(value, project_id='p.1', title='M', request_id='m' + str(len(value)), actor='operator')
                self.assertEqual((caught.exception.code, caught.exception.field), ('INVALID_INPUT', 'milestone_id'))
            ledger.create_milestone('.m', project_id='p.1', title='M', request_id='m', actor='operator')

    def test_readonly_uri_keeps_unc_server_and_escapes_names(self):
        """protects=reads on a network-share home; fails_when=UNC server parsed as URI authority;
        why_new=as_uri() form; seam=URI text plus a real read-only open; no_claim=a live SMB share.
        """
        import os
        from urgency_engine.ledger import _readonly_uri, open_readonly_ledger
        if os.name == 'nt':
            self.assertEqual(_readonly_uri(Path('\\\\server\\share\\a b#%.db')), 'file:////server/share/a%20b%23%25.db?mode=ro')
            self.assertEqual(_readonly_uri(Path('C:\\x\\y.db')), 'file:///C:/x/y.db?mode=ro')
        else:
            self.assertEqual(_readonly_uri(Path('//server/share/a b#%.db')), 'file:////server/share/a%20b%23%25.db?mode=ro')
            self.assertEqual(_readonly_uri(Path('/x/y.db')), 'file:///x/y.db?mode=ro')
        folder = self.root / 'odd #%? name' if os.name != 'nt' else self.root / 'odd #% name'
        folder.mkdir()
        self.db = folder / 'ledger.db'
        with self.open() as ledger:
            ledger.create_project('p', 'Project', request_id='p', actor='operator')
        with open_readonly_ledger(self.db, clock=lambda: NOW) as ledger:
            self.assertEqual(ledger.get_project('p')['project_id'], 'p')
        # Where the local administrative share exists, read the same file through a real UNC path.
        unc = Path('\\\\localhost\\' + str(self.db)[0] + '$' + str(self.db)[2:]) if os.name == 'nt' else None
        if unc is not None and unc.exists():
            with open_readonly_ledger(unc, clock=lambda: NOW) as ledger:
                self.assertEqual(ledger.get_project('p')['project_id'], 'p')

    def test_concurrent_initialization_and_corrupt_preservation(self):
        """protects=non-destructive schema; fails_when=init races/drops state;
        why_new=migrations; seam=process gate; no_claim=future schema migration.
        """
        from urgency_engine.ledger import LedgerError
        go = multiprocessing.get_context('spawn').Event()
        workers = [self.spawn('startup', go) for _ in range(2)]
        go.set()
        results = [queue.get(timeout=20) for _, queue in workers]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 'ok')
        for process, _ in workers:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        with self.open() as ledger:
            self.assertEqual(len(ledger.history('project', 'shared')), 1)
        for mode, expected in [('future', 'UNSUPPORTED_SCHEMA'), ('incomplete', 'CORRUPT_STATE'), ('bytes', 'CORRUPT_STATE')]:
            self.db = self.root / (mode + '.db')
            if mode == 'bytes':
                self.db.write_bytes(b'not a database at all')
            else:
                with closing(sqlite3.connect(self.db)) as connection:
                    connection.execute('CREATE TABLE keep_me(value TEXT)')
                    connection.execute('PRAGMA user_version=' + ('999' if mode == 'future' else '1'))
            before = self.db.read_bytes()
            with self.subTest(mode=mode), self.assertRaises(LedgerError) as caught:
                self.open()
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(self.db.read_bytes(), before)

    def test_input_bounds_and_safe_errors(self):
        """protects=validated commands; fails_when=bad data writes or leaks;
        why_new=public boundary; seam=none; no_claim=GUI or host authorization.
        """
        from urgency_engine.ledger import LedgerError
        self.prepare_task()
        with self.open() as ledger:
            baseline = ledger.connection.total_changes
            invalid = [True, -1, float('nan'), float('inf'), 10**400, '1']
            for value in invalid:
                with self.subTest(value=type(value).__name__), self.assertRaises(LedgerError) as caught:
                    ledger.upsert_task('b', 't', project_id='p', goal='Deliver', remaining_seconds=value,
                        expected_revision=2, reason='Edit', request_id='bad-number', actor='operator')
                self.assertEqual(caught.exception.code, 'INVALID_INPUT')
            for payload in [dict(goal=''), dict(status='verified'), dict(goal='x'*4097), dict(assignee='\ud800'), dict(expected_revision=True)]:
                args = dict(project_id='p', goal='Deliver', expected_revision=2, reason='Edit', request_id='bad-text', actor='operator')
                args.update(payload)
                with self.subTest(payload=list(payload)), self.assertRaises(LedgerError):
                    ledger.upsert_task('b', 't', **args)
            with self.assertRaises(LedgerError):
                ledger.set_criteria('b', 't', [{'huge': 'x'*262144}], expected_revision=2, reason='Scope', request_id='big', actor='operator')
            for limit in [0, 501, True, '100']:
                with self.assertRaises(LedgerError):
                    ledger.list_projects(limit)
            with self.assertRaises(LedgerError) as caught:
                ledger.get_task('b', 'missing')
            self.assertEqual(caught.exception.code, 'NOT_FOUND')
            self.assertEqual(ledger.connection.total_changes, baseline)

    def check_documented_plugin_storage_isolates_homes(self):
        """protects=profile isolation; fails_when=factory caches a connection;
        why_new=host boundary; seam=temporary HERMES_HOME; no_claim=live gateway routing.
        """
        import os
        try:
            from plugins.plugin_storage import plugin_data_dir
        except ImportError:
            self.skipTest('optional Hermes plugin_storage is not installed')
        from urgency_engine.ledger import open_hermes_ledger
        for name in ('one', 'two', 'one'):
            home = self.root / name
            with patch.dict(os.environ, {'HERMES_HOME': str(home)}):
                self.assertEqual(plugin_data_dir('hermes-urgency-engine').parent.parent, home)
                with open_hermes_ledger(clock=lambda: NOW, artifact_root=self.root) as ledger:
                    if name == 'two':
                        self.assertEqual(ledger.list_projects(), [])
                    result = ledger.create_project(name, name, request_id='same-key', actor='operator')
                    self.assertEqual(result['project_id'], name)
                    self.assertEqual(len(ledger.history('project', name)), 1)
                    connection = ledger.connection
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')

    def test_large_board_progress_and_pagination(self):
        """protects=bounded reads; fails_when=double count/unbounded pages;
        why_new=capacity; seam=SQL fixture population; no_claim=UI latency or write throughput.
        """
        import tracemalloc
        from urgency_engine.ledger import LedgerError
        with self.open() as ledger:
            ledger.create_project('p', 'Project', request_id='p', actor='operator')
            conn = ledger.connection
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany("INSERT INTO tasks VALUES('b',?,'p',NULL,'Goal','planned',0,0,0,0,NULL,NULL,1,1)", [(str(i),) for i in range(10000)])
            conn.executemany("INSERT INTO criteria VALUES('b',?,?,1,'Check',?,1,1,?)", [(str(i), c, weight, 'a'*64) for i in range(10000) for c, weight in [('a', 1), ('b', 3)]])
            conn.executemany("INSERT INTO evidence VALUES(?,'b',?,'a',1,'artifact',?,?,NULL)", [(str(i), str(i), 'a'*64, NOW.isoformat()) for i in range(10000)])
            conn.executemany('INSERT INTO projects VALUES(?,?,1,?)', [('page-'+str(i).zfill(4), 'Page', NOW.isoformat()) for i in range(503)])
            conn.commit()
            queries = []
            conn.set_trace_callback(queries.append)
            tracemalloc.start()
            start = time.perf_counter()
            progress = ledger.progress('p')
            elapsed = time.perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            conn.set_trace_callback(None)
            self.assertEqual((progress['current_weight'], progress['verified_weight'], progress['percent']), (40000, 10000, 25))
            self.assertLessEqual(len(queries), 10)
            self.assertEqual(len(ledger.get_task('b', '9999')['criteria']), 2)
            print(f'\nSynthetic 10000 tasks/20000 criteria: progress_seconds={elapsed:.6f}; traced_python_peak_bytes={peak}; sql_statements={len(queries)}')
            seen, after = [], None
            while page := ledger.list_projects(limit=127, after=after):
                self.assertLessEqual(len(page), 127)
                seen.extend(row['project_id'] for row in page)
                after = page[-1]['project_id']
            self.assertEqual(len(seen), 504)
            self.assertEqual(seen, sorted(set(seen)))
            with self.assertRaises(LedgerError) as caught:
                ledger.upsert_task('b', 'overflow', project_id='p', goal='Extra', expected_revision=0,
                    reason='Capacity', request_id='overflow', actor='operator')
            self.assertEqual(caught.exception.field, 'tasks_capacity')
            criteria = [dict(criterion_id=str(i), description='Check', weight=1, required=True, expected_sha256=None) for i in range(100)]
            ledger.set_criteria('b', '0', criteria, expected_revision=1, reason='Capacity', request_id='100', actor='operator')
            with self.assertRaises(LedgerError):
                ledger.set_criteria('b', '0', criteria + [dict(criteria[0], criterion_id='extra')], expected_revision=2,
                    reason='Capacity', request_id='101', actor='operator')

    def test_artifact_containment_limits_and_duplicate_credit(self):
        """protects=local byte verification; fails_when=outside/nonregular credits;
        why_new=file boundary; seam=none; no_claim=semantic artifact correctness.
        """
        from urgency_engine.ledger import LedgerError
        self.prepare_task()
        (self.root / 'directory').mkdir()
        with (self.root / 'large').open('wb') as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        with self.open() as ledger:
            for name in ['missing', 'directory', 'large', '../artifact', '/artifact', 'C:/artifact', 'dir\\artifact', './artifact', 'https://example.test/file']:
                with self.subTest(name=name), self.assertRaises(LedgerError) as caught:
                    ledger.verify_artifact('b', 't', 'c', name, expected_revision=2, request_id='bad', actor='verifier', reason='Check')
                self.assertEqual(caught.exception.side_effects, 'none')
                self.assertNotIn(name, str(caught.exception))
            result = ledger.verify_artifact('b', 't', 'c', 'artifact', expected_revision=2, request_id='v', actor='verifier', reason='Check')
            before = ledger.get_task('b', 't')
            duplicate = ledger.verify_artifact('b', 't', 'c', 'artifact', expected_revision=3, request_id='v2', actor='verifier', reason='Check again')
            self.assertEqual(result, duplicate)
            self.assertEqual(ledger.get_task('b', 't'), before)
            (self.root / 'artifact').unlink()
            self.assertEqual(ledger.verify_artifact('b', 't', 'c', 'artifact', expected_revision=2, request_id='v', actor='verifier', reason='Check'), result)
            self.assertEqual(ledger.progress('p')['percent'], 100)

    def test_symlink_rejection(self):
        """protects=artifact containment; fails_when=symlink accepted;
        why_new=filesystem links; seam=none; no_claim=unavailable OS privileges.
        """
        from urgency_engine.ledger import LedgerError
        self.prepare_task()
        link = self.root / 'link'
        try:
            link.symlink_to(self.root / 'artifact')
        except OSError:
            self.skipTest('host does not permit symlink creation; POSIX CI exercises it')
        with self.open() as ledger:
            with self.assertRaises(LedgerError):
                ledger.verify_artifact('b', 't', 'c', 'link', expected_revision=2, request_id='link', actor='verifier', reason='Check')
            self.assertEqual(ledger.progress('p')['percent'], 0)

    def test_forecast_cooldown_survives_reopen_and_reads_do_not_write(self):
        """protects=hysteresis history; fails_when=reopen discards cooldown;
        why_new=forecast persistence; seam=fixed clock; no_claim=empirical calibration.
        """
        from datetime import timedelta
        self.prepare_task()
        with self.open() as ledger:
            ledger.set_deadline('d', project_id='p', kind='soft', at='2030-01-01T00:01:00Z', timezone='UTC',
                owner='operator', source='Plan', request_id='d', actor='operator')
            first = ledger.snapshot_forecast('d', expected_revision=1, request_id='f', actor='agent', reason='Measure')
            self.assertEqual(first['forecast']['level'], 5)
            ledger.revise_deadline('d', at='2030-01-02T00:00:00Z', timezone='UTC', intent='automatic',
                expected_revision=1, request_id='extend', actor='agent', reason='Replan')
        with self.open() as ledger:
            baseline = ledger.connection.total_changes
            ledger.clock = lambda: NOW + timedelta(seconds=60)
            self.assertEqual(ledger.explain_deadline('d')['forecast']['level'], 5)
            ledger.clock = lambda: NOW + timedelta(seconds=300)
            current = ledger.explain_deadline('d')
            self.assertEqual(current['forecast']['level'], 1)
            self.assertIsNone(current['actual_verified_at'])
            self.assertEqual(current['original_at'], '2030-01-01T00:01:00+00:00')
            self.assertEqual(current['target_at'], '2030-01-02T00:00:00+00:00')
            self.assertEqual(ledger.connection.total_changes, baseline)

    def test_durable_records_after_process_exit_and_uncertain_commit(self):
        """protects=restart/reconciliation; fails_when=success lost/duplicated;
        why_new=durable writes; seam=post-commit exception; no_claim=power-loss durability.
        """
        from urgency_engine.ledger import Ledger, LedgerError
        go = multiprocessing.get_context('spawn').Event()
        process, output = self.spawn('persist', go)
        go.set()
        state, original = output.get(timeout=20)
        self.assertEqual(state, 'ok')
        process.join(20)
        self.assertEqual(process.exitcode, 0)
        with self.open() as ledger:
            self.assertEqual(ledger.set_deadline('d', project_id='p', kind='hard', at='2030-01-02T00:00:00Z', timezone='UTC',
                owner='operator', source='Plan', request_id='d', actor='operator'), original)
            self.assertEqual(ledger.explain_deadline('d')['original_at'], original['original_at'])
            self.assertEqual(ledger.get_task('b', 't')['criteria'][0]['weight'], 2)
            self.assertEqual(len(ledger.history('deadline', 'd')), 1)
        class UncertainConnection(sqlite3.Connection):
            fail = False
            def commit(self):
                super().commit()
                if self.fail:
                    self.fail = False
                    raise sqlite3.OperationalError('synthetic private error')
        connection = sqlite3.connect(self.db, factory=UncertainConnection)
        with Ledger(connection, clock=lambda: NOW, artifact_root=self.root) as ledger:
            connection.fail = True
            with self.assertRaises(LedgerError) as caught:
                ledger.create_project('uncertain', 'Uncertain', request_id='uncertain', actor='operator')
            self.assertEqual(caught.exception.side_effects, 'unknown')
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(ledger.create_project('uncertain', 'Uncertain', request_id='uncertain', actor='operator')['revision'], 1)
            self.assertEqual(len(ledger.history('project', 'uncertain')), 1)

    def test_scope_addition_preserves_baseline_and_history_pages(self):
        """protects=honest scope; fails_when=added scope hides baseline/credit;
        why_new=denominator changes; seam=none; no_claim=Desktop rendering.
        """
        import json
        self.prepare_task()
        with self.open() as ledger:
            baseline = ledger.baseline_scope('p', expected_revision=ledger.get_project('p')['revision'],
                request_id='baseline', actor='operator', reason='Baseline')
            ledger.verify_artifact('b', 't', 'c', 'artifact', expected_revision=2, request_id='v', actor='verifier', reason='Check')
            self.assertEqual(ledger.progress('p')['percent'], 100)
            ledger.upsert_task('b', 'extra', project_id='p', goal='More work', expected_revision=0,
                request_id='extra', actor='operator', reason='Added scope')
            ledger.set_criteria('b', 'extra', [dict(criterion_id='c', description='Check', weight=3, required=True, expected_sha256=None)],
                expected_revision=1, request_id='extra-scope', actor='operator', reason='Added scope')
            self.assertEqual(ledger.progress('p')['percent'], 25)
            self.assertEqual(ledger.progress('p')['baseline_weight'], 1)
            rows, after = [], None
            while page := ledger.history('task', '["b","t"]', limit=1, after=after):
                rows.extend(page)
                after = page[-1]['sequence']
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({r['sequence'] for r in rows}), 3)
            original_scope = [r for r in rows if r['sequence'] <= baseline['audit_sequence'] and r['action'] == 'set_criteria']
            self.assertEqual(json.loads(original_scope[0]['after_json'])['criteria'][0]['weight'], 1)
            ledger.set_criteria('b', 'extra', [], expected_revision=2, request_id='remove', actor='operator', reason='Remove added scope')
            self.assertIsNone(ledger.progress('p')['percent'])
            self.assertEqual(ledger.progress('p')['unscoped_task_count'], 1)
            self.assertEqual(ledger.progress('p')['baseline_weight'], 1)


if __name__ == '__main__':
    unittest.main()

from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import TZPATH, ZoneInfo, reset_tzpath

import test_ledger as fixtures
from test_ledger import NOW, fixture_zones
from urgency_engine.ledger import Ledger, LedgerError


class OutcomeTests(unittest.TestCase):
    prepare_task = fixtures.LedgerTests.prepare_task

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'ledger.db'
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        self.prepare_task()

    def open(self, **kwargs):
        return Ledger(sqlite3.connect(self.db), clock=lambda: NOW, artifact_root=self.root, **kwargs)

    def deadline(self, ledger):
        ledger.set_deadline('d', project_id='p', board_id='b', task_id='t', kind='soft',
            at='2030-01-02T00:00:00Z', timezone='UTC', owner='operator', source='plan',
            request_id='d', actor='operator')

    def verify(self, ledger, request='v'):
        return ledger.verify_artifact('b', 't', 'c', 'artifact', expected_revision=ledger.get_task('b', 't')['revision'],
            reason='Check', request_id=request, actor='verifier')

    def test_final_verification_captures_saved_forecast(self):
        with self.open() as ledger:
            self.deadline(ledger)
            snapshot = ledger.snapshot_forecast('d', expected_revision=1, reason='Measure', request_id='f', actor='agent')
            ledger.clock = lambda: NOW + timedelta(seconds=60)
            self.verify(ledger)
            outcome = ledger.export_outcomes('p')[0]
            self.assertEqual((outcome['project_id'], outcome['board_id'], outcome['task_id'], outcome['evidence_id']), ('p', 'b', 't', 'v'))
            self.assertEqual(outcome['snapshot_id'], snapshot['snapshot_id'])
            self.assertEqual(outcome['forecast_at'], snapshot['forecast_at'])
            self.assertEqual(outcome['target_at'], snapshot['target_at'])
            self.assertEqual(outcome['input_revisions'], snapshot['input_revisions'])
            self.assertEqual(outcome['evaluated_at'], snapshot['input']['now'])
            self.assertEqual(outcome['verified_at'], (NOW + timedelta(seconds=60)).isoformat())
            self.assertEqual(outcome['completion_error_seconds'], 60)
            self.assertTrue(outcome['on_time'])
            self.assertIsNone(outcome['duration_seconds'])
            self.assertEqual(outcome['duration_reason'], 'duration_unobserved')

    def test_claim_partial_failed_replay_and_reopen(self):
        import hashlib
        with self.open() as ledger:
            criteria = [dict(criterion_id=c, description=c, weight=1, required=True,
                             expected_sha256=hashlib.sha256(b'proof').hexdigest()) for c in ('c', 'other')]
            ledger.set_criteria('b', 't', criteria, expected_revision=2, reason='scope', request_id='two', actor='operator')
            self.deadline(ledger)
            snap = ledger.snapshot_forecast('d', expected_revision=1, reason='Measure', request_id='f', actor='agent')
            ledger.claim_complete('b', 't', expected_revision=3, reason='claim', request_id='claim', actor='agent')
            with self.assertRaises(LedgerError):
                ledger.verify_artifact('b', 't', 'c', 'missing', expected_revision=4, reason='check', request_id='bad', actor='verifier')
            self.assertEqual(ledger.export_outcomes('p'), [])
            self.verify(ledger, 'partial')
            self.assertEqual(ledger.export_outcomes('p'), [])
            args = ('b', 't', 'other', 'artifact')
            kwargs = dict(expected_revision=5, reason='check', request_id='final', actor='verifier')
            first = ledger.verify_artifact(*args, **kwargs)
            self.assertEqual(ledger.verify_artifact(*args, **kwargs), first)
            self.assertEqual(ledger.export_outcomes('p')[0]['snapshot_id'], snap['snapshot_id'])
            ledger.reopen_task('b', 't', expected_revision=6, reason='rework', request_id='reopen', actor='operator')
            self.verify(ledger, 'partial2')
            ledger.verify_artifact(*args, expected_revision=8, reason='check', request_id='final2', actor='verifier')
            rows = ledger.export_outcomes('p')
            self.assertEqual([r['evidence_id'] for r in rows], ['final', 'final2'])
            self.assertEqual(rows[1]['forecast_reason'], 'stale')

    def test_missing_stale_future_unknown_and_target_revision(self):
        for mode in ('missing', 'stale', 'future', 'unknown', 'target'):
            with self.subTest(mode=mode):
                self.db = self.root / (mode + '.db')
                self.prepare_task()
                with self.open() as ledger:
                    self.deadline(ledger)
                    if mode == 'unknown':
                        ledger.upsert_task('b', 't', project_id='p', goal='unknown', remaining_seconds=None,
                            expected_revision=2, reason='edit', request_id='unknown', actor='operator')
                    if mode != 'missing':
                        if mode == 'future':
                            ledger.clock = lambda: NOW + timedelta(seconds=30)
                        snap = ledger.snapshot_forecast('d', expected_revision=1, reason='Measure', request_id='f', actor='agent')
                        ledger.clock = lambda: NOW
                    if mode == 'stale':
                        ledger.upsert_task('b', 't', project_id='p', goal='changed', expected_revision=2,
                            reason='edit', request_id='edit', actor='operator')
                    if mode == 'target':
                        ledger.revise_deadline('d', at='2029-12-31T00:00:00Z', timezone='UTC', intent='automatic',
                            expected_revision=1, reason='replan', request_id='revise', actor='operator')
                    self.verify(ledger)
                    outcome = ledger.export_outcomes('p')[0]
                    self.assertEqual(outcome['forecast_reason'], {'missing': 'missing', 'stale': 'stale', 'future': 'incomparable', 'unknown': 'incomparable', 'target': None}[mode])
                    if mode == 'target':
                        self.assertEqual(outcome['target_at'], snap['target_at'])
                        self.assertEqual(outcome['input_revisions']['deadline'], 1)
                        self.assertTrue(outcome['on_time'])
                    elif mode == 'unknown':
                        self.assertTrue(outcome['on_time'])
                        self.assertIsNone(outcome['completion_error_seconds'])
                    elif mode in ('missing', 'stale', 'future'):
                        self.assertIsNone(outcome['snapshot_id'])

    def test_later_incompatible_snapshot_does_not_mask_prior_or_reconstruct(self):
        with self.open() as ledger:
            self.deadline(ledger)
            snap = ledger.snapshot_forecast('d', expected_revision=1, reason='Measure', request_id='f', actor='agent')
            ledger.clock = lambda: NOW + timedelta(days=1)
            ledger.snapshot_forecast('d', expected_revision=1, reason='Measure', request_id='future', actor='agent')
            ledger.clock = lambda: NOW
            self.verify(ledger)
            row = ledger.export_outcomes('p')[0]
            self.assertEqual(row['snapshot_id'], snap['snapshot_id'])
            ledger.clock = lambda: NOW + timedelta(days=2)
            ledger.snapshot_forecast('d', expected_revision=1, reason='Measure', request_id='after', actor='agent')
            self.assertEqual(ledger.export_outcomes('p')[0], row)

    def test_old_forecast_has_no_ttl_and_error_is_signed_against_saved_target(self):
        with self.open() as ledger:
            ledger.upsert_task('b', 't', project_id='p', goal='work', remaining_seconds=172800,
                actual_seconds=1000, expected_revision=2, reason='estimate', request_id='edit', actor='operator')
            self.deadline(ledger)
            snap = ledger.snapshot_forecast('d', expected_revision=1, reason='measure', request_id='f', actor='agent')
            ledger.clock = lambda: NOW + timedelta(days=1, seconds=1)
            self.verify(ledger)
            row = ledger.export_outcomes('p')[0]
            self.assertEqual(row['snapshot_id'], snap['snapshot_id'])
            self.assertEqual(row['completion_error_seconds'], -86399)
            self.assertFalse(row['on_time'])
            self.assertIsNone(row['duration_seconds'])

    def test_outcome_identity_is_unique_in_storage(self):
        with self.open() as ledger:
            self.verify(ledger)
            row = ledger.export_outcomes('p')[0]
            with self.assertRaises(sqlite3.IntegrityError):
                ledger.connection.execute(
                    'INSERT INTO outcomes(project_id,board_id,task_id,evidence_id,verified_at,duration_reason) VALUES(?,?,?,?,?,?)',
                    ('p', 'b', 't', 'v', row['verified_at'], 'duration_unobserved'))
            ledger.connection.rollback()

    def test_atomic_outcome_failure_rolls_back_evidence_and_retry(self):
        with self.open() as ledger:
            ledger.connection.execute("CREATE TRIGGER fail_outcome BEFORE INSERT ON outcomes BEGIN SELECT RAISE(ABORT, 'failure'); END")
            with self.assertRaises(LedgerError):
                self.verify(ledger)
            self.assertEqual(ledger.get_task('b', 't')['revision'], 2)
            self.assertEqual(ledger.connection.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
            self.assertFalse(ledger.has_committed_operation('v'))
            ledger.connection.execute('DROP TRIGGER fail_outcome')
            self.verify(ledger)
            self.assertEqual(len(ledger.export_outcomes('p')), 1)
        with self.open() as ledger:
            self.assertEqual(ledger.export_outcomes('p')[0]['evidence_id'], 'v')

    def test_legacy_v1_v2_read_and_upgrade(self):
        from urgency_engine.ledger import _SCHEMA, _SCHEMA_V2
        for version, statements in ((1, _SCHEMA), (2, _SCHEMA_V2)):
            path = self.root / f'legacy{version}.db'
            db = sqlite3.connect(path)
            for sql in statements:
                db.execute(sql)
            db.execute(f'PRAGMA user_version={version}')
            db.execute("INSERT INTO projects VALUES('p','legacy',1,?)", (NOW.isoformat(),))
            db.commit()
            db.close()
            before = path.read_bytes()
            with Ledger(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True), clock=lambda: NOW, artifact_root=self.root, read_only=True) as ledger:
                self.assertEqual(ledger.schema_version, version)
                self.assertEqual(ledger.export_outcomes('p'), [])
            self.assertEqual(path.read_bytes(), before)
            with Ledger(sqlite3.connect(path), clock=lambda: NOW, artifact_root=self.root) as ledger:
                self.assertEqual(ledger.schema_version, 3)
                self.assertEqual(ledger.get_project('p')['title'], 'legacy')
            with Ledger(sqlite3.connect(path), clock=lambda: NOW, artifact_root=self.root) as ledger:
                self.assertEqual(ledger.schema_version, 3)

    def test_failed_upgrade_and_post_outcome_audit_are_atomic(self):
        from urgency_engine.ledger import _SCHEMA_V2
        path = self.root / 'upgrade.db'
        with sqlite3.connect(path) as db:
            for sql in _SCHEMA_V2:
                db.execute(sql)
            db.execute('PRAGMA user_version=2')
        db.close()
        conn = sqlite3.connect(path)
        conn.set_authorizer(lambda action, name, *rest: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_CREATE_INDEX and name == 'outcomes_project'
                            else sqlite3.SQLITE_OK)
        with self.assertRaises(LedgerError):
            Ledger(conn, clock=lambda: NOW, artifact_root=self.root)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='outcomes'").fetchone())
        db.close()
        with self.open() as ledger:
            ledger.connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'fixture'); END")
            with self.assertRaises(LedgerError):
                self.verify(ledger)
            self.assertEqual(ledger.export_outcomes('p'), [])
            self.assertEqual(ledger.get_task('b', 't')['status'], 'planned')
            ledger.connection.execute('DROP TRIGGER fail_audit')
            self.verify(ledger)
            self.assertEqual(len(ledger.export_outcomes('p')), 1)

    def test_default_retention_under_stress_and_bounded_export(self):
        with self.open() as ledger:
            self.verify(ledger)
            ledger.connection.executemany(
                "INSERT INTO evidence VALUES(?,'b','t','c',1,'artifact',?,?,?)",
                [(f'old-{i}', 'a'*64, NOW.isoformat(), NOW.isoformat()) for i in range(10000)])
            ledger.connection.executemany(
                "INSERT INTO outcomes(project_id,board_id,task_id,evidence_id,verified_at,duration_reason) VALUES('p','b','t',?,?,'duration_unobserved')",
                [(f'old-{i}', NOW.isoformat()) for i in range(10000)])
            ledger.connection.commit()
            ledger.reopen_task('b', 't', expected_revision=3, reason='again', request_id='r', actor='operator')
            self.verify(ledger, 'new')
            self.assertEqual(ledger.connection.execute('SELECT count(*) FROM outcomes').fetchone()[0], 1000)
            queries = []
            ledger.connection.set_trace_callback(queries.append)
            page = ledger.export_outcomes('p', limit=127)
            ledger.connection.set_trace_callback(None)
            self.assertEqual(len(page), 127)
            self.assertLessEqual(len(queries), 4)
            plan = ledger.connection.execute('EXPLAIN QUERY PLAN SELECT * FROM outcomes WHERE project_id=? AND sequence>? ORDER BY sequence LIMIT ?', ('p', 0, 127)).fetchall()
            self.assertIn('outcomes_project', str([tuple(row) for row in plan]))
            seen = list(page)
            while page := ledger.export_outcomes('p', limit=127, after=seen[-1]['sequence']):
                seen.extend(page)
            self.assertEqual(len({row['sequence'] for row in seen}), 1000)
            self.assertEqual(seen[-1]['evidence_id'], 'new')

    def test_retention_sequence_pagination_and_project_isolation(self):
        with self.open(outcome_retention=2) as ledger:
            self.verify(ledger)
            first = ledger.export_outcomes('p')[0]['sequence']
            for i in range(3):
                ledger.reopen_task('b', 't', expected_revision=ledger.get_task('b', 't')['revision'],
                    reason='again', request_id=f'r{i}', actor='operator')
                self.verify(ledger, f'v{i}')
            rows = ledger.export_outcomes('p', limit=1)
            next_page = ledger.export_outcomes('p', limit=1, after=rows[0]['sequence'])
            self.assertEqual(len(next_page), 1)
            self.assertGreater(rows[0]['sequence'], first)
            self.assertGreater(next_page[0]['sequence'], rows[0]['sequence'])
            self.assertEqual(ledger.export_outcomes('p', after=next_page[0]['sequence']), [])
            ledger.create_project('q', 'Other', request_id='q', actor='operator')
            self.assertEqual(ledger.export_outcomes('q'), [])
            for limit in (0, 501, True):
                with self.assertRaises(LedgerError):
                    ledger.export_outcomes('p', limit=limit)
        for value in (0, 10001, True):
            connection = sqlite3.connect(self.db)
            try:
                with self.assertRaises(LedgerError):
                    Ledger(connection, clock=lambda: NOW, artifact_root=self.root, outcome_retention=value)
            finally:
                connection.close()

    def test_indexed_lookup_filters_many_newer_incompatible_snapshots(self):
        import json
        with self.open() as ledger:
            self.deadline(ledger)
            original = ledger.snapshot_forecast('d', expected_revision=1, reason='Measure', request_id='f', actor='agent')
            bogus = dict(original)
            bogus['input_revisions'] = dict(bogus['input_revisions'], tasks=[['b', 't', 9999]])
            payload = json.dumps(bogus)
            ledger.connection.executemany("INSERT INTO snapshots(project_id,kind,recorded_at,payload_json) VALUES('p','forecast',?,?)", [(NOW.isoformat(), payload)] * 1500)
            ledger.connection.commit()
            plan = ledger.connection.execute("EXPLAIN QUERY PLAN SELECT snapshot_id FROM snapshots WHERE project_id=? AND kind='forecast' AND json_extract(payload_json,'$.board_id')=? AND json_extract(payload_json,'$.task_id')=? ORDER BY snapshot_id DESC LIMIT 1", ('p', 'b', 't')).fetchall()
            self.assertIn('snapshot_task_forecast', str([tuple(row) for row in plan]))
            self.verify(ledger)
            self.assertEqual(ledger.export_outcomes('p')[0]['snapshot_id'], original['snapshot_id'])

    def test_same_task_id_other_board_and_database_are_isolated(self):
        with self.open() as ledger:
            self.deadline(ledger)
            snap = ledger.snapshot_forecast('d', expected_revision=1, reason='measure', request_id='f', actor='agent')
            ledger.create_project('q', 'Other', request_id='q', actor='operator')
            ledger.upsert_task('other', 't', project_id='q', goal='other', expected_revision=0,
                reason='plan', request_id='other', actor='operator')
            criteria = [dict(criterion_id='c', description='check', weight=1, required=True,
                expected_sha256=ledger.get_task('b', 't')['criteria'][0]['expected_sha256'])]
            ledger.set_criteria('other', 't', criteria, expected_revision=1, reason='scope', request_id='other-scope', actor='operator')
            ledger.verify_artifact('other', 't', 'c', 'artifact', expected_revision=2, reason='check', request_id='other-v', actor='verifier')
            self.assertEqual(ledger.export_outcomes('q')[0]['forecast_reason'], 'missing')
            self.verify(ledger)
            self.assertEqual(ledger.export_outcomes('p')[0]['snapshot_id'], snap['snapshot_id'])
            self.assertEqual(ledger.export_outcomes('q')[0]['board_id'], 'other')
        self.db = self.root / 'separate.db'
        self.prepare_task()
        with self.open() as ledger:
            self.assertEqual(ledger.export_outcomes('p'), [])

"""Explicit missing work must survive the shared forecast boundary."""
from datetime import datetime, timedelta, timezone
import unittest

from urgency_engine.forecast import ForecastInput, UrgencyLevel, UrgencyState, calculate_forecast


class UnknownWorkTests(unittest.TestCase):
    def test_unknown_work_retains_risk_without_inventing_a_distribution(self):
        """protects=unknown work; fails_when=null becomes zero; why_new=nullable input;
        seam=real calculator; no_claim=calibrated prediction.
        """
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        old = UrgencyState(UrgencyLevel.CRITICAL, now - timedelta(hours=1))
        for seconds, level in ((3600, UrgencyLevel.CRITICAL), (60, UrgencyLevel.DEADLINE_IMMINENT),
                               (0, UrgencyLevel.OVERDUE_RECOVERY_REQUIRED)):
            with self.subTest(seconds=seconds):
                result = calculate_forecast(ForecastInput(now + timedelta(seconds=seconds), None,
                    completion_samples_seconds=(1, 2, 3, 4, 5)), now=now, previous=old)
                self.assertEqual(result.level, level)
                self.assertIsNone(result.deadline_slack_seconds)
                self.assertIsNone(result.effective_slack_seconds)
                self.assertIsNone(result.p50_seconds)
                self.assertIsNone(result.p90_seconds)
                self.assertIsNone(result.on_time_probability)
                self.assertEqual(result.evidence, 'insufficient_evidence')
                if seconds == 3600:
                    self.assertEqual(result.changed_at, old.changed_at)
        fresh = calculate_forecast(ForecastInput(now + timedelta(days=1), None), now=now)
        self.assertEqual(fresh.level, UrgencyLevel.ELEVATED)
        recovered = calculate_forecast(ForecastInput(now + timedelta(days=1), 0), now=now,
                                      previous=old)
        self.assertEqual(recovered.level, UrgencyLevel.NORMAL)


class UnknownLedgerTests(unittest.TestCase):
    def test_monitor_snapshot_projects_null_instead_of_storage_payload(self):
        """protects=consumer uncertainty; fails_when=snapshot leaks compatibility zero;
        why_new=shared consumer; seam=real Ledger/Monitor; no_claim=host lifecycle.
        """
        import sqlite3
        import tempfile
        from pathlib import Path
        from urgency_engine.ledger import Ledger
        from urgency_engine.watchdog import snapshot
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'data.db'
            with Ledger(sqlite3.connect(path), clock=lambda: now, artifact_root=root) as ledger:
                ledger.create_project('p', 'Project', request_id='p', actor='test')
                ledger.upsert_task('b', 't', project_id='p', goal='Unknown', expected_revision=0,
                    reason='Import', request_id='t', actor='test', remaining_seconds=None)
            task = snapshot(path, lambda: now)['tasks'][0]
            self.assertIsNone(task['remaining_seconds'])
            self.assertIs(task['remaining_known'], False)

    def test_legacy_read_is_nonmutating_and_write_migrates_exact_schema(self):
        """protects=atomic upgrade; fails_when=read migrates or wrong schema accepted;
        why_new=schema2; seam=real legacy SQLite; no_claim=production migration.
        """
        import sqlite3
        import tempfile
        from pathlib import Path
        from urgency_engine.ledger import Ledger, _SCHEMA, open_readonly_ledger
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'legacy.db'
            db = sqlite3.connect(path)
            for sql in _SCHEMA:
                db.execute(sql)
            db.execute('PRAGMA user_version=1')
            db.execute("INSERT INTO projects VALUES('p','Legacy',1,?)", (now.isoformat(),))
            db.execute("INSERT INTO tasks VALUES('b','t','p',NULL,'Goal','planned',0,0,0,0,NULL,NULL,1)")
            db.commit()
            db.close()
            before = path.read_bytes()
            with open_readonly_ledger(path, clock=lambda: now, check_integrity=True) as ledger:
                self.assertEqual(ledger.schema_version, 1)
                self.assertIs(ledger.get_task('b', 't')['remaining_known'], True)
                self.assertEqual(ledger.get_task('b', 't')['remaining_seconds'], 0)
            self.assertEqual(path.read_bytes(), before)
            with Ledger(sqlite3.connect(path), clock=lambda: now, artifact_root=root) as ledger:
                self.assertEqual(ledger.schema_version, 2)
                self.assertEqual(ledger.get_task('b', 't')['remaining_seconds'], 0)
            with open_readonly_ledger(path, clock=lambda: now) as ledger:
                self.assertEqual(ledger.schema_version, 2)


    def test_explicit_null_survives_storage_forecast_and_progress(self):
        """protects=shared uncertainty; fails_when=null coerced; why_new=schema2;
        seam=real SQLite; no_claim=canonical transport or semantic acceptance.
        """
        import sqlite3
        import tempfile
        from pathlib import Path
        from urgency_engine.ledger import Ledger
        from test_ledger import fixture_zones, TZPATH, reset_tzpath, ZoneInfo
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture_zones(root)
            self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
            now = datetime(2030, 1, 1, tzinfo=timezone.utc)
            with Ledger(sqlite3.connect(root / 'ledger.db'), clock=lambda: now, artifact_root=root) as ledger:
                ledger.create_project('p', 'Project', request_id='p', actor='test')
                ledger.set_deadline('d', project_id='p', kind='hard', at='2030-01-02T00:00:00Z',
                    timezone='UTC', owner='test', source='fixture', request_id='d', actor='test')
                self.assertIsNone(ledger.explain_deadline('d')['forecast_at'])
                args = dict(project_id='p', goal='Unknown', expected_revision=0, reason='Import',
                            request_id='t', actor='test', remaining_seconds=None)
                task = ledger.upsert_task('b', 't', **args)
                self.assertIsNone(task['remaining_seconds'])
                self.assertIs(task['remaining_known'], False)
                self.assertEqual(ledger.upsert_task('b', 't', **args), task)
                forecast = ledger.explain_deadline('d')
                self.assertIsNone(forecast['input']['remaining_seconds'])
                self.assertIsNone(forecast['forecast_at'])
                self.assertIsNone(forecast['target_variance_seconds'])
                self.assertEqual(ledger.progress('p')['unscoped_task_count'], 1)
                self.assertIsNone(ledger.progress('p')['percent'])
                known = ledger.upsert_task('b', 't', **dict(args, remaining_seconds=0,
                                         expected_revision=1, request_id='known'))
                self.assertIs(known['remaining_known'], True)
                self.assertEqual(ledger.explain_deadline('d')['forecast_at'], now.isoformat())
                self.assertEqual(ledger.connection.execute('PRAGMA user_version').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()

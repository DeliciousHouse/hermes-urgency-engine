"""protects=truthful paged Desktop projections; fails_when=claims earn credit;
why_new=operator REST views; seam=real temporary ledger, injected canonical inventory;
no_claim=host authentication, semantic artifact acceptance or rendered UI.
"""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from urgency_engine.ledger import Ledger


class DesktopViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.ledger = Ledger(sqlite3.connect(self.root / 'data.db'), clock=lambda:self.now, artifact_root=self.root)
        self.addCleanup(self.ledger.connection.close)
        for project in ('a', 'b'):
            self.ledger.create_project(project, project.upper(), request_id=project, actor='operator')
        self.ledger.upsert_task('board', 'task', project_id='a', goal='Deliver', expected_revision=0,
                                reason='Plan', request_id='task', actor='operator')
        self.ledger.set_criteria('board', 'task', [dict(criterion_id='bytes', description='Result', weight=2,
            required=True, expected_sha256=hashlib.sha256(b'proof').hexdigest())], expected_revision=1,
            reason='Scope', request_id='criteria', actor='operator')
        (self.root / 'proof').write_bytes(b'proof')

    def test_paged_portfolio_reads_real_progress_without_crediting_claims(self):
        from urgency_engine.desktop import Views
        view = Views(self.ledger, clock=lambda:self.now)
        page = view.portfolio(limit=1)
        self.assertEqual(page['items'][0]['progress']['percent'], 0)
        self.assertTrue(page['has_more'])
        self.assertEqual(page['next'], 'a')
        self.assertEqual(view.portfolio(after='a')['items'][0]['progress']['percent'], None)
        self.ledger.claim_complete('board', 'task', expected_revision=2, reason='Claim', request_id='claim', actor='worker')
        self.assertEqual(view.project('a')['progress']['percent'], 0)
        self.ledger.verify_artifact('board', 'task', 'bytes', 'proof', expected_revision=3,
                                    reason='Verify bytes', request_id='verify', actor='operator')
        self.assertEqual(view.project('a')['progress']['percent'], 100)
        self.assertIsNone(view.project('b')['progress']['percent'])
        task = self.ledger.get_task('board', 'task')
        self.ledger.reopen_task('board', 'task', expected_revision=task['revision'], reason='Reopen', request_id='reopen', actor='operator')
        self.assertEqual(view.project('a')['progress']['percent'], 0)
        self.assertEqual(view.project('a')['evidence_kind'], 'artifact_byte_identity')
        self.assertEqual(page['coverage'], 'local_ledger_only')

    def test_commitments_expose_independent_forecasts_and_preserved_targets(self):
        from urgency_engine.desktop import Views
        from test_ledger import fixture_zones, TZPATH, reset_tzpath, ZoneInfo
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        self.ledger.set_deadline('d', project_id='a', kind='soft', at='2030-01-02T00:00:00Z', timezone='UTC',
                                owner='operator', source='Plan', request_id='date', actor='operator')
        view = Views(self.ledger, clock=lambda:self.now)
        before = view.project('a')
        self.assertEqual(before['commitments'][0]['original_at'], '2030-01-02T00:00:00+00:00')
        self.assertEqual(before['urgency']['level'], before['commitments'][0]['forecast']['level'])
        self.ledger.revise_deadline('d', at='2030-01-03T00:00:00Z', timezone='UTC', intent='operator',
                                    expected_revision=1, reason='Replan', request_id='replan', actor='operator')
        later = view.project('a')
        self.assertEqual(later['commitments'][0]['original_at'], before['commitments'][0]['original_at'])
        self.assertNotEqual(later['commitments'][0]['target_at'], before['commitments'][0]['target_at'])
        self.assertEqual(later['commitments'][0]['forecast_at'], before['commitments'][0]['forecast_at'])
        self.assertNotIn('input_revisions', later['commitments'][0])
        self.assertEqual(len(self.ledger.history('deadline', 'd')), 2, 'views must not persist forecasts')

    def test_timeline_pages_tasks_without_inventing_execution_dates(self):
        from urgency_engine.desktop import Views
        from urgency_engine.ledger import LedgerError
        self.ledger.upsert_task('board', 'second', project_id='a', goal='Second task', expected_revision=0,
                                reason='Plan', request_id='second', actor='operator')
        view = Views(self.ledger, clock=lambda:self.now)
        args = dict(start='2029-12-31T00:00:00+00:00', end='2030-01-03T00:00:00+00:00', limit=1)
        first = view.timeline('a', **args)
        self.assertEqual(len(first['items']), 1)
        self.assertTrue(first['has_more'])
        self.assertIsNone(first['items'][0]['started_at'])
        self.assertIsNone(first['items'][0]['actual_verified_at'])
        second = view.timeline('a', **args, after=first['next'])
        self.assertNotEqual(first['items'][0]['task_id'], second['items'][0]['task_id'])
        self.assertFalse(second['has_more'])
        for changes in ({'end': args['start']}, {'start': '2030-01-01'}, {'after': 'not-json'}, {'limit': 501}):
            with self.subTest(changes=changes), self.assertRaises(LedgerError):
                view.timeline('a', **{**args, **changes})
        self.assertEqual(view.timeline('b', **args)['items'], [])


if __name__ == '__main__':
    unittest.main()

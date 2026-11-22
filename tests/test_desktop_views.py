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

    def test_portfolio_summary_selects_next_by_time_and_keeps_execution_provenance(self):
        """protects=scanable factual portfolio; fails_when=ID order picks next date;
        why_new=portfolio summary; seam=real Ledger; no_claim=live worker liveness.
        """
        from urgency_engine.desktop import Views
        from test_ledger import fixture_zones, TZPATH, reset_tzpath, ZoneInfo
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        task = self.ledger.get_task('board', 'task')
        self.ledger.upsert_task('board', 'task', project_id='a', goal='Deliver', status='running',
            assignee='builder', remaining_seconds=3600, expected_revision=task['revision'],
            reason='Start', request_id='start', actor='operator')
        for name, at, kind in [('a-later', '2030-02-01T00:00:00Z', 'hard'),
                               ('z-next', '2030-01-02T00:00:00Z', 'soft')]:
            self.ledger.set_deadline(name, project_id='a', kind=kind, at=at, timezone='UTC',
                owner='operator', source='Plan', request_id=name, actor='operator')
        data = Views(self.ledger, clock=lambda:self.now).project('a')
        self.assertEqual(data['summary']['next_commitment']['deadline_id'], 'z-next')
        self.assertEqual(data['summary']['hard_deadline']['deadline_id'], 'a-later')
        self.assertEqual(data['summary']['assigned_agents'], ['builder'])
        self.assertEqual(data['summary']['recorded_running_agents'], ['builder'])
        self.assertEqual(data['summary']['task_states'], {'running': 1})
        self.assertEqual(data['summary']['boards'], ['board'])
        self.assertEqual(data['summary']['schedule_status'], 'ahead')
        self.assertEqual(data['summary']['forecast_at'], '2030-01-01T01:00:00+00:00')
        empty = Views(self.ledger, clock=lambda:self.now).project('b')['summary']
        self.assertIsNone(empty['forecast_at'])
        self.assertEqual(empty['schedule_status'], 'unknown')

    def test_portfolio_filters_keep_bounded_cursor_even_when_a_page_has_no_matches(self):
        """protects=honest filter traversal; fails_when=empty filtered page ends inventory;
        why_new=portfolio filters; seam=real Ledger; no_claim=canonical host permissions.
        """
        from urgency_engine.desktop import Views
        from urgency_engine.ledger import LedgerError
        view = Views(self.ledger, clock=lambda:self.now)
        first = view.portfolio(limit=1, project='b')
        self.assertEqual(first['items'], [])
        self.assertEqual(first['next'], 'a')
        self.assertTrue(first['has_more'])
        self.assertEqual(view.portfolio(after=first['next'], project='b')['items'][0]['project_id'], 'b')
        self.assertEqual([x['project_id'] for x in view.portfolio(board='board')['items']], ['a'])
        self.assertEqual(view.portfolio(assignee='absent')['items'], [])
        self.assertEqual(view.portfolio(urgency='0')['items'][0]['project_id'], 'a')
        self.assertEqual(view.portfolio(deadline_type='hard')['items'], [])
        for bad in ({'urgency': '7'}, {'deadline_type': 'automatic'}, {'board': []}):
            with self.subTest(bad=bad), self.assertRaises(LedgerError):
                view.portfolio(**bad)

    def test_portfolio_identifies_shared_assignments_and_same_day_targets_without_claiming_overload(self):
        """protects=visible contention; fails_when=shared agents disappear;
        why_new=portfolio coordination; seam=real Ledger; no_claim=capacity violation.
        """
        from urgency_engine.desktop import Views
        from test_ledger import fixture_zones, TZPATH, reset_tzpath, ZoneInfo
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        for project in ('a', 'b'):
            self.ledger.upsert_task('shared', project, project_id=project, goal='Deliver',
                assignee='builder', expected_revision=0, reason='Plan', request_id='shared-' + project, actor='operator')
            self.ledger.set_deadline('deadline-' + project, project_id=project, kind='hard',
                at='2030-01-02T12:00:00Z', timezone='UTC', owner='operator', source='Plan',
                request_id='deadline-' + project, actor='operator')
        result = Views(self.ledger, clock=lambda:self.now).portfolio()
        self.assertEqual(result['coordination']['shared_agents'], [{'agent': 'builder', 'projects': ['a', 'b']}])
        self.assertEqual(result['coordination']['same_day_targets'][0]['projects'], ['a', 'b'])
        self.assertEqual(result['coordination']['same_day_targets'][0]['utc_day'], '2030-01-02')
        self.assertEqual(result['coordination']['capacity_assessment'], 'unknown')
        self.assertEqual(Views(self.ledger, clock=lambda:self.now).portfolio(project='a')['coordination']['shared_agents'], [])

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

    def test_capped_commitments_do_not_claim_complete_project_risk(self):
        from urgency_engine.desktop import Views
        from test_ledger import fixture_zones, TZPATH, reset_tzpath, ZoneInfo
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        for index in range(101):
            self.ledger.set_deadline(f'd{index:03}', project_id='a', kind='hard',
                at='2030-01-02T00:00:00Z', timezone='UTC', owner='operator', source='Plan',
                request_id=f'date{index}', actor='operator')
        data = Views(self.ledger, clock=lambda:self.now).project('a')
        self.assertTrue(data['commitments_has_more'])
        self.assertIsNone(data['urgency'])
        self.assertEqual(data['risk_coverage'], 'partial')

    def test_commitment_pages_preserve_each_explanation_and_refuse_foreign_project(self):
        """protects=all commitments accessible; fails_when=only first100 reachable;
        seam=real Ledger; no_claim=native navigation or complete aggregate risk.
        """
        from urgency_engine.desktop import Views
        from urgency_engine.ledger import LedgerError
        from test_ledger import fixture_zones, TZPATH, reset_tzpath, ZoneInfo
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))
        for name in ('a-first', 'b-second'):
            self.ledger.set_deadline(name, project_id='a', kind='hard', at='2030-01-02T00:00:00Z',
                timezone='UTC', owner='operator', source='Plan', request_id=name, actor='operator')
        view = Views(self.ledger, clock=lambda:self.now)
        first = view.commitments('a', limit=1)
        self.assertTrue(first['has_more'])
        second = view.commitments('a', limit=1, after=first['next'])
        self.assertEqual(second['items'][0]['deadline_id'], 'b-second')
        self.assertEqual(second['items'][0]['original_at'], '2030-01-02T00:00:00+00:00')
        self.assertNotIn('input_revisions', second['items'][0])
        self.assertFalse(second['has_more'])
        self.assertEqual(view.commitments('b')['items'], [])
        with self.assertRaises(LedgerError):
            view.commitments('missing')

    def test_project_refuses_a_mixed_revision_measurement(self):
        from urgency_engine.desktop import Views
        from urgency_engine.ledger import LedgerError
        from unittest.mock import patch
        original = self.ledger.progress
        def changed(project_id):
            progress = original(project_id)
            self.ledger.edit_project(project_id, title='Concurrent title', expected_revision=progress['revision'],
                reason='Concurrent edit', request_id='concurrent', actor='operator')
            return progress
        with patch.object(self.ledger, 'progress', side_effect=changed):
            with self.assertRaises(LedgerError) as caught:
                Views(self.ledger, clock=lambda:self.now).project('a')
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')

    def test_project_history_includes_member_changes_without_foreign_records(self):
        """protects=inspectable schedule revisions; fails_when=history only shows
        project names; seam=real Ledger; no_claim=native history presentation.
        """
        self.ledger.create_milestone('m', project_id='a', title='Review', request_id='m', actor='operator')
        self.ledger.edit_milestone('m', title='Final review', expected_revision=self.ledger.get_project('a')['revision'],
            reason='Rename review', request_id='rename-m', actor='operator')
        self.ledger.edit_project('b', title='Private other', expected_revision=1,
            reason='Foreign', request_id='foreign', actor='operator')
        rows = self.ledger.project_history('a', limit=2)
        self.assertEqual([r['action'] for r in rows], ['create_project', 'upsert_task'])
        rest = self.ledger.project_history('a', after=rows[-1]['sequence'])
        self.assertEqual([r['action'] for r in rest], ['set_criteria', 'create_milestone', 'edit_milestone'])
        self.assertEqual(rest[-1]['reason'], 'Rename review')
        self.assertNotIn('Foreign', str(rows + rest))

    def test_task_inspector_exposes_current_criterion_evidence_and_removes_reopened_credit(self):
        """protects=inspectable earned credit; fails_when=claims appear verified;
        why_new=criterion inspector; seam=real Ledger; no_claim=semantic quality.
        """
        from urgency_engine.desktop import Views
        view = Views(self.ledger, clock=lambda:self.now)
        args = dict(start='2030-01-01T00:00:00Z', end='2030-01-02T00:00:00Z')
        before = view.timeline('a', **args)['items'][0]
        self.assertEqual(before['criterion_evidence'][0]['verified'], False)
        self.assertIsNone(before['criterion_evidence'][0]['checked_at'])
        self.ledger.verify_artifact('board', 'task', 'bytes', 'proof', expected_revision=2,
            reason='Verify bytes', request_id='evidence-view', actor='operator')
        after = view.timeline('a', **args)['items'][0]
        self.assertTrue(after['criterion_evidence'][0]['verified'])
        self.assertEqual(after['criterion_evidence'][0]['observed_sha256'], hashlib.sha256(b'proof').hexdigest())
        self.assertEqual(after['criterion_evidence'][0]['checked_at'], self.now.isoformat())
        self.assertNotIn('artifact_relative_path', after['criterion_evidence'][0])
        self.ledger.reopen_task('board', 'task', expected_revision=after['revision'],
            reason='Reopen', request_id='reopen-evidence', actor='operator')
        self.assertFalse(view.timeline('a', **args)['items'][0]['criterion_evidence'][0]['verified'])

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

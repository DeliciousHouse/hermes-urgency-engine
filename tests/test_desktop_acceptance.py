"""Numbered Desktop amendment acceptance cases, asserted on the surface the UI renders.

protects=the amendment's required Desktop behaviour; fails_when=the view layer
reorders, invents or silently rewrites a commitment, a forecast or a risk level;
why_new=the Desktop read model is the new release gate, so each numbered case
needs its own named proof; seam=real temporary Ledger with an injected clock;
no_claim=these are author tests on a synthetic store. They do not establish
native rendering, installed-package behaviour, real gateway routing or future
forecast accuracy, and they are not the independent Desktop QA the release needs.

Case numbers refer to the amendment's "Additional Required Acceptance Tests".
Cases proved elsewhere are mapped in docs/desktop.md rather than duplicated here.
"""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from urgency_engine.desktop import Views
from urgency_engine.kanban import Sync, settings
from urgency_engine.ledger import Ledger, LedgerError


class DesktopAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.ledger = Ledger(sqlite3.connect(self.root / 'data.db'), clock=lambda: self.now,
                             artifact_root=self.root)
        self.addCleanup(self.ledger.connection.close)
        self.ledger.create_project('a', 'Alpha', request_id='a', actor='operator')
        from test_ledger import fixture_zones, TZPATH, reset_tzpath, ZoneInfo
        fixture_zones(self.root)
        self.addCleanup(lambda: (reset_tzpath(TZPATH), ZoneInfo.clear_cache()))

    def view(self):
        return Views(self.ledger, clock=lambda: self.now)

    def task(self, task_id, *, remaining=None, weight=2, project='a'):
        self.ledger.upsert_task('board', task_id, project_id=project, goal='Deliver',
                                expected_revision=0, reason='Plan', request_id='t' + task_id,
                                actor='operator', remaining_seconds=remaining)
        revision = self.ledger.get_task('board', task_id)['revision']
        self.ledger.set_criteria('board', task_id, [dict(criterion_id='bytes', description='Result',
            weight=weight, required=True, expected_sha256=hashlib.sha256(b'proof').hexdigest())],
            expected_revision=revision, reason='Scope', request_id='c' + task_id, actor='operator')
        (self.root / 'proof').write_bytes(b'proof')

    def deadline(self, deadline_id, *, kind, at, project='a', milestone_id=None):
        self.ledger.set_deadline(deadline_id, project_id=project, kind=kind, at=at, timezone='UTC',
                                 owner='Client', source='Contract', request_id='d' + deadline_id,
                                 actor='operator', milestone_id=milestone_id)

    def test_case_6_hard_deadline_never_moves_automatically(self):
        self.deadline('hard', kind='hard', at='2030-02-01T00:00:00Z')
        before = self.view().project('a')['commitments'][0]
        with self.assertRaises(LedgerError) as caught:
            self.ledger.revise_deadline('hard', at='2030-03-01T00:00:00Z', timezone='UTC',
                                        intent='automatic', expected_revision=1,
                                        reason='Forecast slipped', request_id='auto', actor='engine')
        self.assertEqual(caught.exception.code, 'INVALID_INPUT')
        self.assertEqual(self.view().project('a')['commitments'][0]['target_at'], before['target_at'])
        # An explicitly authorized operator revision is permitted and audited.
        self.ledger.revise_deadline('hard', at='2030-03-01T00:00:00Z', timezone='UTC',
                                    intent='operator', expected_revision=1,
                                    reason='Client agreed a new date', request_id='manual',
                                    actor='operator')
        after = self.view().project('a')['commitments'][0]
        self.assertEqual(after['original_at'], before['original_at'])
        self.assertNotEqual(after['target_at'], before['target_at'])
        self.assertEqual([record['reason'] for record in self.ledger.history('deadline', 'hard')][-1],
                         'Client agreed a new date')

    def test_milestone_membership_change_recalculates_risk(self):
        self.ledger.create_milestone('m', project_id='a', title='Beta ready', request_id='m',
                                     actor='operator')
        self.task('slow', remaining=30 * 86400)
        self.deadline('soft', kind='soft', at='2030-01-10T00:00:00Z', milestone_id='m')
        empty = self.view().project('a')['commitments'][0]['forecast']['level']
        # A milestone has no revision of its own; membership is checked against
        # the owning project's revision.
        self.assertNotIn('revision', self.ledger.get_milestone('m'))
        revision = self.ledger.get_project('a')['revision']
        self.ledger.set_milestone_tasks('m', [['board', 'slow']], expected_revision=revision,
                                        reason='Bind the long task', request_id='bind',
                                        actor='operator')
        bound = self.view().project('a')['commitments'][0]
        self.assertGreater(bound['forecast']['level'], empty,
                           'adding a task that cannot finish in time must raise measured risk')
        self.assertEqual(bound['target_at'], '2030-01-10T00:00:00+00:00',
                         'recalculated risk must not move the committed target')

    def test_case_9_canonical_edge_changes_bound_not_authoritative_forecast(self):
        """Change only a parent edge at a fixed clock, through real Sync and Ledger stores."""
        parent_ids = []

        class Source:
            def inventory(self, binding):
                return [dict(id=key, tenant=binding['tenant']) for key in ('prepare', 'task')]

            def detail(self, binding, task_id):
                return dict(task=dict(id=task_id, tenant=binding['tenant'], title=task_id, status='todo'),
                            parents=list(parent_ids) if task_id == 'task' else [])

        cfg = settings(dict(kanban_sync_enabled=True, kanban_bindings=[
            dict(board='board', tenant='synthetic', project_id='a')]))
        with Sync(sqlite3.connect(self.root / 'kanban.db'), clock=lambda: self.now, redact=str) as sync:
            source = Source()

            def scan(request_id):
                result = sync.scan(cfg, self.ledger, None, self.root / 'data.db', source,
                                   request_id=request_id, reason='Synthetic canonical graph')
                self.assertEqual(result['state'], 'complete')

            def schedule():
                page = sync.page('a', limit=2, cfg=cfg, with_graph=True)
                view = self.view()
                bound = view.dependency_bound('a', page['graph'], page['items'])
                return bound, page['items'], view.project('a')

            scan('without-edge')
            for task_id, duration in (('prepare', 1800), ('task', 3600)):
                self.ledger.upsert_task('board', task_id, project_id='a', goal=task_id,
                                        expected_revision=self.ledger.get_task('board', task_id)['revision'],
                                        reason='Estimate', request_id='estimate-' + task_id,
                                        actor='operator', remaining_seconds=duration)
            self.deadline('soft', kind='soft', at='2030-01-01T01:15:00Z')
            before, before_rows, before_project = schedule()
            parent_ids.append('prepare')
            scan('with-edge')
            after, after_rows, after_project = schedule()

        self.assertEqual(before, dict(reason=None, lower_bound_seconds=3600, path_tasks=1))
        self.assertEqual(after, dict(reason=None, lower_bound_seconds=5400, path_tasks=2))
        self.assertEqual([(r['task_id'], r['remaining_seconds'], r['parents'], r['critical_path_step'])
                          for r in before_rows], [('prepare', 1800, [], None), ('task', 3600, [], 1)])
        self.assertEqual([(r['task_id'], r['remaining_seconds'], r['parents'], r['critical_path_step'])
                          for r in after_rows], [('prepare', 1800, [], 1), ('task', 3600, ['prepare'], 2)])
        commitment, = before_project['commitments']
        self.assertEqual((before_project['urgency']['level'], commitment['forecast_at'], commitment['target_at']),
                         (4, '2030-01-01T01:30:00+00:00', '2030-01-01T01:15:00+00:00'))
        self.assertEqual(after_project['urgency'], before_project['urgency'])
        self.assertEqual(after_project['commitments'], before_project['commitments'])

    def test_case_10_escalation_changes_the_displayed_urgency_level(self):
        self.task('work', remaining=5 * 86400)
        self.deadline('soft', kind='soft', at='2030-03-01T00:00:00Z')
        levels = []
        for offset in (0, 50, 59, 70):
            self.now = datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(days=offset)
            levels.append(self.view().project('a')['urgency']['level'])
        self.assertEqual(levels, sorted(levels), f'urgency must not fall as the date nears: {levels}')
        self.assertGreater(levels[-1], levels[0])
        self.assertLessEqual(max(levels), 6)

    def test_case_11_long_running_and_unestimated_work_is_not_invented(self):
        self.task('unknown', remaining=None)
        self.deadline('soft', kind='soft', at='2030-02-01T00:00:00Z')
        unknown = self.view().project('a')['commitments'][0]
        self.assertIsNone(unknown['forecast_at'], 'an unestimated task has no forecast endpoint')
        self.assertIsNone(unknown['baseline_variance_seconds'])
        self.task('long', remaining=400 * 86400)
        measured = self.view().project('a')['commitments'][0]
        self.assertIsNone(measured['forecast_at'],
                          'one unestimated task keeps the whole commitment unestimated')
        known = self.ledger.get_task('board', 'unknown')
        self.ledger.upsert_task('board', 'unknown', project_id='a', goal='Deliver',
                                expected_revision=known['revision'], reason='Estimate',
                                request_id='estimate', actor='operator', remaining_seconds=86400)
        now_measured = self.view().project('a')['commitments'][0]
        self.assertIsNotNone(now_measured['forecast_at'])
        self.assertGreater(now_measured['forecast_at'], now_measured['target_at'],
                           '401 days of work cannot be forecast inside a one month target')

    def test_case_14_large_board_detail_stays_bounded_and_paged(self):
        connection = self.ledger.connection
        connection.execute('BEGIN IMMEDIATE')
        connection.executemany(
            "INSERT INTO tasks VALUES('big',?,'a',NULL,'Goal','planned',0,0,0,0,NULL,NULL,1,1)",
            [(str(index),) for index in range(10000)])
        connection.commit()
        start = time.perf_counter()
        page = self.view().timeline('a', start='2029-12-31T00:00:00+00:00',
                                    end='2030-03-01T00:00:00+00:00', limit=100)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(page['items']), 100, 'the detail timeline must page, never stream a board')
        self.assertTrue(page['has_more'])
        seen = len(page['items'])
        while page['has_more'] and seen < 10000:
            page = self.view().timeline('a', start='2029-12-31T00:00:00+00:00',
                                        end='2030-03-01T00:00:00+00:00', limit=500,
                                        after=page['next'])
            self.assertLessEqual(len(page['items']), 500)
            seen += len(page['items'])
        self.assertEqual(seen, 10000, 'paging must reach every task exactly once')
        print(f'\nSynthetic 10000-task detail timeline: first_page_seconds={elapsed:.6f}; '
              f'page_limit=100. This is one machine and is not a latency guarantee.')


if __name__ == '__main__':
    unittest.main()

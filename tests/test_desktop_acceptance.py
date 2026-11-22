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

    def test_case_9_milestone_membership_change_recalculates_risk(self):
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

"""Desktop management's real storage contract.

protects=audited title edits with optimistic concurrency and exact replay;
fails_when=an edit loses history or overwrites concurrent scope;
why_new=Desktop needs project and milestone editing, not just creation;
seam=temporary real SQLite; no_claim=HTTP authorization or native UI acceptance.
"""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from urgency_engine.ledger import Ledger, LedgerError


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = Ledger(sqlite3.connect(self.root / 'data.db'),
                             clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc), artifact_root=self.root)
        self.addCleanup(self.ledger.connection.close)
        self.ledger.create_project('p', 'Original', request_id='create', actor='operator')

    def test_project_edit_is_revision_checked_audited_and_replayable(self):
        command = dict(title='Revised', expected_revision=1, reason='Clarify objective',
                       request_id='edit', actor='operator')
        result = self.ledger.edit_project('p', **command)
        self.assertEqual(result['title'], 'Revised')
        self.assertEqual(result['revision'], 2)
        self.assertEqual(self.ledger.edit_project('p', **command), result)
        history = self.ledger.history('project', 'p')
        self.assertEqual(len(history), 2)
        self.assertIn('Original', history[-1]['before_json'])
        self.assertEqual(history[-1]['reason'], 'Clarify objective')
        with self.assertRaises(LedgerError) as caught:
            self.ledger.edit_project('p', **{**command, 'request_id': 'stale'})
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertEqual(self.ledger.get_project('p'), result)

    def test_management_envelope_rejects_forged_actor_and_replays_exact_edit(self):
        from urgency_engine.desktop import management_command
        payload = dict(operation='edit_project', request_id='edit', arguments=dict(
            project_id='p', title='Changed', expected_revision=1, reason='Rename'))
        result = management_command(self.ledger, payload)
        self.assertEqual(result['title'], 'Changed')
        self.assertEqual(management_command(self.ledger, payload), result)
        self.assertEqual(self.ledger.history('project', 'p')[-1]['actor'], 'desktop-operator')
        for bad in ({**payload, 'actor': 'operator'}, {**payload, 'operation': 'verify_artifact'},
                    {**payload, 'arguments': {**payload['arguments'], 'actor': 'operator'}}):
            with self.subTest(bad=bad), self.assertRaises(LedgerError):
                management_command(self.ledger, bad)
        self.assertEqual(len(self.ledger.history('project', 'p')), 2)

    def test_milestone_edit_preserves_membership_and_uses_project_revision(self):
        self.ledger.upsert_task('b', 't', project_id='p', goal='Deliver', expected_revision=0,
                                reason='Plan', request_id='task', actor='operator')
        self.ledger.create_milestone('m', project_id='p', title='Original milestone', request_id='m', actor='operator')
        self.ledger.set_milestone_tasks('m', [['b', 't']], expected_revision=3,
                                        reason='Scope', request_id='membership', actor='operator')
        command = dict(title='Renamed milestone', expected_revision=4, reason='Clarify milestone',
                       request_id='rename', actor='operator')
        result = self.ledger.edit_milestone('m', **command)
        self.assertEqual(result['title'], 'Renamed milestone')
        self.assertEqual(result['tasks'], [['b', 't']])
        self.assertEqual(self.ledger.get_project('p')['revision'], 5)
        self.assertEqual(self.ledger.edit_milestone('m', **command), result)
        self.assertEqual(self.ledger.get_milestone('m'), result)
        self.assertEqual(self.ledger.list_milestones('p', limit=1), [result])
        self.assertEqual(self.ledger.list_milestones('p', after='m'), [])
        self.ledger.create_project('other', 'Other', request_id='other', actor='operator')
        self.assertEqual(self.ledger.list_milestones('other'), [])
        with self.assertRaises(LedgerError) as caught:
            self.ledger.edit_milestone('m', **{**command, 'request_id': 'stale'})
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertEqual(len(self.ledger.history('milestone', 'm')), 3)


if __name__ == '__main__':
    unittest.main()

"""An unrelated sync binding must not erase an unbound project's forecast."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from urgency_engine.kanban import Sync
from urgency_engine.ledger import Ledger
from urgency_engine.runtime import Runtime


class UnboundCommitmentTest(unittest.TestCase):
    def test_unrelated_binding_preserves_standalone_forecast(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        target = (now + timedelta(seconds=4500)).isoformat()
        config = dict(kanban_sync_enabled=True, kanban_bindings=[
            dict(board='source-board', tenant='demo', project_id='other')])

        class Context:
            def get_config(self, key, default):
                return config.get(key, default)

            def redact_text(self, text, *, force):
                return text

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rt = Runtime(Context(), clock=lambda: now, data_dir=lambda: root)
            with Ledger(sqlite3.connect(root / 'data.db'), clock=lambda: now, artifact_root=root) as ledger:
                ledger.create_project('local', 'Local', request_id='local', actor='operator')
                ledger.upsert_task('manual', 'item', project_id='local', goal='Work', remaining_seconds=3600,
                                         expected_revision=0, reason='Estimate', request_id='item', actor='operator')
                # Store the same valid UTC row on hosts with or without zoneinfo data.
                ledger.connection.execute("INSERT INTO deadlines VALUES('due','local',NULL,NULL,NULL,'hard',?,?,?,?,?,1)",
                                          (target, target, 'UTC', 'Operator', 'Fixture'))
                ledger.connection.commit()
            db = sqlite3.connect(root / 'kanban.db')
            try:
                Sync(db, clock=lambda: now, redact=str)
            finally:
                db.close()
            explained = rt.run_request('explain', {'deadline_id': 'due'}, surface='cli')
            self.assertTrue(explained['ok'], explained)
            result = explained['data']
            self.assertEqual((result['original_at'], result['target_at']), (target, target))
            self.assertEqual((result['forecast_at'], result['input']['remaining_seconds']),
                             ((now + timedelta(seconds=3600)).isoformat(), 3600), result['input'])

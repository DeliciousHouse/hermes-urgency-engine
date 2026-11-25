"""Worker health does not depend on readable session focus metadata."""
import json
import os
import unittest
from unittest.mock import patch

from test_worker_fences import CLEAR, WORKERS, WorkerFenceTests


def load_tests(loader, tests, pattern):
    class FocusIndependence(WorkerFenceTests):
        def test_health_stays_available_when_focus_state_is_invalid(self):
            self.ctx.state.path.write_text('{', encoding='utf-8')
            request = {'operation': 'watchdog_health', 'arguments': {}}
            with patch.dict(os.environ, CLEAR):
                operator = json.loads(self.runtime.tool(request, session_id='synthetic'))
            self.assertTrue(operator['ok'], operator)
            with patch.dict(os.environ, dict(CLEAR, **WORKERS[0])):
                worker = json.loads(self.runtime.tool(request, session_id='synthetic'))
            self.assertTrue(worker['ok'], worker)
            self.assertEqual(worker, operator)

    return unittest.TestSuite([
        FocusIndependence('test_health_stays_available_when_focus_state_is_invalid')
    ])

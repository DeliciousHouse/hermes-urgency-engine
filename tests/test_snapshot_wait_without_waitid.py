"""Synthetic snapshot regression for POSIX Python without os.waitid."""
import os
import sys
import unittest
from unittest.mock import patch

from urgency_engine import _snapshot_process, kanban


class WithoutWaitid:
    name = 'posix'

    def __getattr__(self, name):
        if name == 'waitid':
            raise AttributeError("module 'os' has no attribute 'waitid'")
        return getattr(os, name)


class SnapshotWithoutWaitidTests(unittest.TestCase):
    def test_successful_snapshot_exits_without_os_waitid(self):
        # Use the actual native process owner. Model only the missing POSIX
        # capability at the exit check; on macOS the owner is also POSIX.
        original_exit = kanban.snapshot_exit_status

        def exit_without_waitid(child, timeout):
            with patch.object(_snapshot_process, 'os', WithoutWaitid()):
                return original_exit(child, timeout)

        script = ('print("@urgency:board", flush=True); '
                  'print("@urgency:detail", flush=True); print("[]")')
        with patch.object(kanban, 'snapshot_exit_status', exit_without_waitid):
            self.assertEqual(kanban.read_process([sys.executable, '-c', script],
                                               timeout=3, snapshot_phases=True), [])


if __name__ == '__main__':
    unittest.main()

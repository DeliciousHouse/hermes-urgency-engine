"""Synthetic snapshot regression for POSIX Python without os.waitid."""
import os
from pathlib import Path
import sys
import tempfile
import time
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

    def test_success_cleans_up_descendant_with_no_inherited_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'escaped'
            descendant = ('import time; from pathlib import Path; time.sleep(2.5); '
                          f'Path({str(marker)!r}).touch()')
            script = ('import subprocess, sys; '
                      f'subprocess.Popen([sys.executable, "-c", {descendant!r}], '
                      'stdout=subprocess.DEVNULL); '
                      'print("@urgency:board\\n@urgency:detail\\n[]")')
            original_reap = _snapshot_process._reap

            def reap(child):
                if os.name == 'posix':
                    self.assertIsNone(child.returncode, 'POSIX leader reaped before group cleanup')
                original_reap(child)

            start = time.monotonic()
            with patch.object(_snapshot_process, '_reap', reap):
                self.assertEqual(kanban.read_process([sys.executable, '-c', script],
                                                    timeout=3, snapshot_phases=True), [])
            # gstack-timing-ok: allow a leaked child time to write its sentinel.
            time.sleep(max(0, start + 3 - time.monotonic()))
            self.assertFalse(marker.exists())

    def test_stdout_eof_does_not_replace_exit_validation(self):
        markers = 'print("@urgency:board\\n@urgency:detail\\n[]", flush=True); '
        for tail, expected in (
            ('raise SystemExit(7)', ('SOURCE_DENIED', 'transport')),
            ('import os, time; os.close(1); time.sleep(10)', ('SOURCE_TIMEOUT', 'detail')),
        ):
            with self.subTest(tail=tail), self.assertRaises(kanban.LedgerError) as caught:
                kanban.read_process([sys.executable, '-c', markers + tail],
                                    timeout=1, snapshot_phases=True)
            self.assertEqual((caught.exception.code, caught.exception.field), expected)


if __name__ == '__main__':
    unittest.main()

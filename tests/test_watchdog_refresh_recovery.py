import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runtime


class RefreshRecoveryTest(test_runtime.WatchdogInstallTests):
    def test_owned_stale_launcher_stays_removable_after_replace_failure(self):
        receipt = self.install()
        launcher = Path(receipt['script'])
        old = b'# previous release\n'
        launcher.write_bytes(old)
        self.receipt({**receipt, 'sha256': hashlib.sha256(old).hexdigest()})
        self.jobs.clear()  # The scheduler no longer has the job, but the receipt owns the file.

        with patch('urgency_engine.runtime.os.replace', side_effect=PermissionError('transient file sharing refusal')):
            with self.assertRaises(PermissionError):
                self.install()
        self.assertEqual(launcher.read_bytes(), old)

        self.assertEqual(self.install('uninstall')['state'], 'uninstalled')
        self.assertFalse(launcher.exists())

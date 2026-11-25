"""An owned launcher refresh must not delete unrelated sibling files."""
import hashlib
import unittest
from pathlib import Path

import test_runtime


class WatchdogRefreshPreservesSiblingTests(unittest.TestCase):
    def test_refresh_preserves_unowned_sibling(self):
        fixture = test_runtime.WatchdogInstallTests(
            'test_launcher_from_installed_receipt_is_owned_and_replaced')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        receipt = fixture.install()
        launcher = Path(receipt['script'])
        old = b'# previous release\n'
        launcher.write_bytes(old)
        fixture.receipt({**receipt, 'sha256': hashlib.sha256(old).hexdigest()})
        sibling = launcher.with_name(launcher.name + '.tmp')
        sibling.write_bytes(b'operator backup\n')

        fixture.install()

        self.assertTrue(sibling.exists(), 'refresh deleted an unrelated sibling file')
        self.assertEqual(sibling.read_bytes(), b'operator backup\n')
        self.assertEqual(launcher.read_bytes(), fixture.content)

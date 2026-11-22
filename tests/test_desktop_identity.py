"""protects=durable profile namespace without read-side writes;
fails_when=restart changes identity or concurrent init diverges;
why_new=retained Desktop command recovery; seam=real SQLite in disposable homes;
no_claim=identity is not authentication or proof against copied anonymous homes.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from urgency_engine import desktop
from urgency_engine.ledger import LedgerError


class IdentityTests(unittest.TestCase):
    def test_identity_is_explicit_atomic_and_stable_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile' / 'desktop.db'
            self.assertIsNone(desktop.scope_identity(path))
            self.assertFalse(path.parent.exists())
            with ThreadPoolExecutor(max_workers=4) as pool:
                identities = list(pool.map(lambda _: desktop.scope_identity(path, initialize=True), range(8)))
            self.assertEqual(len(set(identities)), 1)
            self.assertRegex(identities[0], r'^[0-9a-f]{32}$')
            self.assertEqual(desktop.scope_identity(path), identities[0])
            other = Path(directory) / 'other' / 'desktop.db'
            self.assertNotEqual(desktop.scope_identity(other, initialize=True), identities[0])

    def test_invalid_identity_is_not_silently_replaced(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.db'
            desktop.scope_identity(path, initialize=True)
            db = sqlite3.connect(path)
            try:
                with db:
                    db.execute("UPDATE desktop_identity SET value='broken'")
            finally:
                db.close()
            for initialize in (False, True):
                with self.assertRaises(LedgerError) as caught:
                    desktop.scope_identity(path, initialize=initialize)
                self.assertEqual(caught.exception.code, 'CORRUPT_STATE')


if __name__ == '__main__':
    unittest.main()

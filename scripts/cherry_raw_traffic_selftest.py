"""La ventana de tráfico crudo sobrevive a exceder la cuota; solo un fallo real la bloquea."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl.raw_traffic import RawTrafficWindow


class RawTrafficWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.window = RawTrafficWindow(Path(self.tmp.name) / "t.db", 3600, 100)
        self.window.initialize(1000.0, [])

    def test_exceeding_quota_does_not_latch_failure(self):
        token = self.window.reserve(80, 1000.0)
        with self.assertRaises(ValueError):
            self.window.reserve(50, 1000.0)
        self.assertFalse(self.window.failed)
        self.window.receive(token, 80, 1000.0)
        self.window.release(token)
        self.window.reserve(10, 1001.0)

    def test_unknown_token_does_not_latch_failure(self):
        with self.assertRaises(ValueError):
            self.window.receive("nope", 1, 1000.0)
        self.assertFalse(self.window.failed)

    def test_real_persistence_failure_latches(self):
        with self.assertRaises(sqlite3.Error):
            with self.window._connection(write=True) as db:
                db.execute("SELECT * FROM tabla_inexistente")
        self.assertTrue(self.window.failed)
        with self.assertRaises(OSError):
            self.window.reserve(1, 1000.0)


if __name__ == "__main__":
    unittest.main()

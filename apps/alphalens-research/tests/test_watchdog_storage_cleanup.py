import tempfile
import unittest
from pathlib import Path


class TestSeenEventStoreContextManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "seen.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_manager_closes_connection(self):
        from alphalens_pipeline.watchdog.storage import SeenEventStore

        with SeenEventStore(self.db_path) as store:
            store.mark_seen("ACC-1")
            self.assertTrue(store.has_seen("ACC-1"))

        # After exit, further queries should fail because connection is closed
        with self.assertRaises(Exception):
            store.has_seen("ACC-1")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


def _mk_event(accession: str, ticker: str = "AAPL"):
    from alphalens_pipeline.edgar_detector.types import Event, FormType

    return Event(
        ticker=ticker,
        form_type=FormType.FORM_8K,
        accession_number=accession,
        filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=UTC),
        url=f"https://sec.gov/{accession}",
        raw_data={},
    )


class TestSeenEventStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "seen.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_mark_seen_then_has_seen_returns_true(self):
        from alphalens_pipeline.edgar_detector.storage import SeenEventStore

        store = SeenEventStore(self.db_path)
        self.assertFalse(store.has_seen("ACC-123"))
        store.mark_seen("ACC-123")
        self.assertTrue(store.has_seen("ACC-123"))

    def test_store_persists_across_instances(self):
        from alphalens_pipeline.edgar_detector.storage import SeenEventStore

        store1 = SeenEventStore(self.db_path)
        store1.mark_seen("ACC-PERSIST")
        store1.close()

        store2 = SeenEventStore(self.db_path)
        self.assertTrue(store2.has_seen("ACC-PERSIST"))

    def test_filter_unseen_returns_only_new_events(self):
        from alphalens_pipeline.edgar_detector.storage import SeenEventStore

        store = SeenEventStore(self.db_path)
        store.mark_seen("ACC-1")
        store.mark_seen("ACC-3")

        events = [
            _mk_event("ACC-1"),
            _mk_event("ACC-2"),
            _mk_event("ACC-3"),
            _mk_event("ACC-4"),
        ]
        unseen = store.filter_unseen(events)

        self.assertEqual([e.accession_number for e in unseen], ["ACC-2", "ACC-4"])

    def test_default_path_is_in_alphalens_home(self):
        from alphalens_pipeline.edgar_detector.storage import default_db_path

        expected = Path.home() / ".alphalens" / "edgar-detect" / "seen_events.db"
        self.assertEqual(default_db_path(), expected)

    def test_mark_seen_is_idempotent(self):
        from alphalens_pipeline.edgar_detector.storage import SeenEventStore

        store = SeenEventStore(self.db_path)
        store.mark_seen("ACC-X")
        store.mark_seen("ACC-X")
        self.assertTrue(store.has_seen("ACC-X"))


if __name__ == "__main__":
    unittest.main()

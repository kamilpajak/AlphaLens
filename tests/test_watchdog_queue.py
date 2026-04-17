import tempfile
import unittest
from pathlib import Path


class TestAutoTriggerQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "queue.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_enqueue_inserts_pending_row(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue

        queue = AutoTriggerQueue(self.db_path)
        queue.enqueue(ticker="AAPL", accession="ACC-001", trigger_url="https://sec.gov/x")

        rows = queue.list_by_status("pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertEqual(rows[0]["accession_number"], "ACC-001")

    def test_claim_next_returns_oldest_pending_and_marks_in_progress(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue

        queue = AutoTriggerQueue(self.db_path)
        queue.enqueue(ticker="AAPL", accession="ACC-1", trigger_url="x")
        queue.enqueue(ticker="MSFT", accession="ACC-2", trigger_url="x")

        claimed = queue.claim_next()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["ticker"], "AAPL")  # oldest first

        in_progress = queue.list_by_status("in_progress")
        self.assertEqual(len(in_progress), 1)
        self.assertEqual(in_progress[0]["id"], claimed["id"])

    def test_claim_next_returns_none_when_no_pending(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue

        queue = AutoTriggerQueue(self.db_path)
        self.assertIsNone(queue.claim_next())

    def test_mark_done_stores_decision(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue

        queue = AutoTriggerQueue(self.db_path)
        queue.enqueue(ticker="AAPL", accession="ACC-1", trigger_url="x")
        claimed = queue.claim_next()

        queue.mark_done(claimed["id"], decision="BUY")

        done = queue.list_by_status("done")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["decision"], "BUY")
        self.assertIsNotNone(done[0]["finished_at"])

    def test_mark_failed_stores_error(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue

        queue = AutoTriggerQueue(self.db_path)
        queue.enqueue(ticker="AAPL", accession="ACC-1", trigger_url="x")
        claimed = queue.claim_next()

        queue.mark_failed(claimed["id"], error="LLM rate limit")

        failed = queue.list_by_status("failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("rate limit", failed[0]["error"])

    def test_count_done_today(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue

        queue = AutoTriggerQueue(self.db_path)
        for acc in ["A1", "A2"]:
            queue.enqueue(ticker="AAPL", accession=acc, trigger_url="x")
            c = queue.claim_next()
            queue.mark_done(c["id"], decision="BUY")

        self.assertEqual(queue.count_done_today(), 2)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class TestAutoTriggerWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tmp.name) / "queue.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _queue_with(self, *jobs):
        from tradingagents.watchdog.queue import AutoTriggerQueue

        q = AutoTriggerQueue(self.queue_path)
        for ticker, acc in jobs:
            q.enqueue(ticker=ticker, accession=acc, trigger_url=f"https://sec.gov/{acc}")
        q.close()

    def test_process_one_returns_false_when_queue_empty(self):
        from tradingagents.watchdog.worker import AutoTriggerWorker

        worker = AutoTriggerWorker(
            ta_graph=MagicMock(),
            notifier=MagicMock(),
            queue_path=self.queue_path,
        )
        self.assertFalse(worker.process_one())

    def test_process_one_calls_ta_propagate_and_notifies(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue
        from tradingagents.watchdog.worker import AutoTriggerWorker

        self._queue_with(("AAPL", "ACC-1"))
        ta_graph = MagicMock()
        ta_graph.propagate.return_value = ({}, "OVERWEIGHT")
        notifier = MagicMock()

        worker = AutoTriggerWorker(
            ta_graph=ta_graph,
            notifier=notifier,
            queue_path=self.queue_path,
        )
        self.assertTrue(worker.process_one())

        ta_graph.propagate.assert_called_once()
        self.assertEqual(ta_graph.propagate.call_args.args[0], "AAPL")
        notifier.send_message.assert_called_once()
        msg = notifier.send_message.call_args.args[0]
        self.assertIn("AAPL", msg)
        self.assertIn("OVERWEIGHT", msg)

        with AutoTriggerQueue(self.queue_path) as q:
            done = q.list_by_status("done")
            self.assertEqual(len(done), 1)
            self.assertEqual(done[0]["decision"], "OVERWEIGHT")

    def test_process_one_marks_failed_on_exception(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue
        from tradingagents.watchdog.worker import AutoTriggerWorker

        self._queue_with(("MSFT", "ACC-X"))
        ta_graph = MagicMock()
        ta_graph.propagate.side_effect = RuntimeError("LLM rate limit")

        worker = AutoTriggerWorker(
            ta_graph=ta_graph,
            notifier=MagicMock(),
            queue_path=self.queue_path,
        )
        self.assertTrue(worker.process_one())  # still "did work", just failed

        with AutoTriggerQueue(self.queue_path) as q:
            failed = q.list_by_status("failed")
            self.assertEqual(len(failed), 1)
            self.assertIn("rate limit", failed[0]["error"])

    def test_process_one_skips_when_daily_budget_exhausted(self):
        from tradingagents.watchdog.queue import AutoTriggerQueue
        from tradingagents.watchdog.worker import AutoTriggerWorker

        # Pre-populate 2 done-today
        q = AutoTriggerQueue(self.queue_path)
        for acc in ["A1", "A2"]:
            q.enqueue(ticker="AAPL", accession=acc, trigger_url="x")
            job = q.claim_next()
            q.mark_done(job["id"], decision="BUY")
        # Add a 3rd pending
        q.enqueue(ticker="NVDA", accession="A3", trigger_url="x")
        q.close()

        ta_graph = MagicMock()
        notifier = MagicMock()

        worker = AutoTriggerWorker(
            ta_graph=ta_graph,
            notifier=notifier,
            queue_path=self.queue_path,
            budget_per_day=2,
        )
        worker.process_one()

        ta_graph.propagate.assert_not_called()
        # Pending job stays pending (not consumed when budget exhausted)
        with AutoTriggerQueue(self.queue_path) as q2:
            pending = q2.list_by_status("pending")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["ticker"], "NVDA")

    def test_process_all_drains_queue(self):
        from tradingagents.watchdog.worker import AutoTriggerWorker

        self._queue_with(("AAPL", "A1"), ("MSFT", "A2"))
        ta_graph = MagicMock()
        ta_graph.propagate.return_value = ({}, "HOLD")

        worker = AutoTriggerWorker(
            ta_graph=ta_graph,
            notifier=MagicMock(),
            queue_path=self.queue_path,
        )
        processed = worker.process_all()

        self.assertEqual(processed, 2)
        self.assertEqual(ta_graph.propagate.call_count, 2)


if __name__ == "__main__":
    unittest.main()

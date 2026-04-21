import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock


def _cand(ticker="AAPL", source="momentum", priority=10, payload=None, discriminator="D"):
    from alphalens.candidates import Candidate

    return Candidate.from_screener(
        ticker=ticker,
        source=source,
        priority=priority,
        payload=payload or {},
        discriminator=discriminator,
    )


def _fake_result(candidate_id, ticker, source, rating="BUY"):
    from alphalens.candidates import AnalysisResult

    return AnalysisResult(
        candidate_id=candidate_id,
        ticker=ticker,
        source=source,
        rating=rating,
        duration_sec=1.0,
        cost_usd=None,
        model_used="gemini-3-pro-preview",
        completed_at=datetime.now(timezone.utc),
        final_state={},
    )


class TestAnalysisWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _make_worker(self, runner, notifier=None, budget_per_day=5):
        from alphalens.queue import CandidateQueue
        from alphalens.worker import AnalysisWorker

        queue = CandidateQueue(self.db, max_attempts=3, base_retry_s=1)
        return AnalysisWorker(
            queue=queue,
            runner=runner,
            notifier=notifier or MagicMock(),
            budget_per_day=budget_per_day,
        )

    def test_process_one_returns_false_when_queue_empty(self):
        runner = MagicMock()
        worker = self._make_worker(runner)
        self.assertFalse(worker.process_one())
        runner.run.assert_not_called()

    def test_process_one_calls_runner_and_marks_success(self):
        from alphalens.queue import CandidateQueue

        runner = MagicMock()
        notifier = MagicMock()
        worker = self._make_worker(runner, notifier)

        worker.queue.submit([_cand(ticker="AAPL")])
        runner.run.return_value = _fake_result(1, "AAPL", "momentum", rating="OVERWEIGHT")

        self.assertTrue(worker.process_one())
        runner.run.assert_called_once()
        # Notifier got the decision
        notifier.send_message.assert_called_once()
        msg = notifier.send_message.call_args.args[0]
        self.assertIn("AAPL", msg)
        self.assertIn("OVERWEIGHT", msg)

        with CandidateQueue(self.db) as q:
            done = q.list_by_status("done")
            self.assertEqual(len(done), 1)
            self.assertEqual(done[0]["decision"], "OVERWEIGHT")

    def test_process_one_marks_failure_on_exception(self):
        from alphalens.queue import CandidateQueue

        runner = MagicMock()
        runner.run.side_effect = RuntimeError("LLM rate limit")
        worker = self._make_worker(runner)

        worker.queue.submit([_cand(ticker="MSFT")])
        self.assertTrue(worker.process_one())

        with CandidateQueue(self.db) as q:
            pending = q.list_by_status("pending")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["attempts"], 1)
            self.assertIsNotNone(pending[0]["next_retry_at"])
            self.assertIn("rate limit", pending[0]["error"])

    def test_process_one_moves_to_dead_after_max_attempts(self):
        from alphalens.queue import CandidateQueue

        runner = MagicMock()
        runner.run.side_effect = RuntimeError("boom")
        worker = self._make_worker(runner)

        worker.queue.submit([_cand(ticker="NVDA")])
        # max_attempts=3 set on the queue via _make_worker
        for _ in range(3):
            # claim_next_ignoring_retry_window so backoff doesn't block sequential calls
            job = worker.queue.claim_next_ignoring_retry_window()
            if job is None:
                break
            try:
                runner.run(_cand(ticker="NVDA"), candidate_id=job["id"])
            except Exception as exc:
                worker.queue.mark_failure(job["id"], error=str(exc))

        with CandidateQueue(self.db) as q:
            self.assertEqual(len(q.list_by_status("dead")), 1)
            self.assertEqual(len(q.list_by_status("pending")), 0)

    def test_process_one_respects_daily_budget(self):
        from alphalens.queue import CandidateQueue

        runner = MagicMock()
        notifier = MagicMock()
        worker = self._make_worker(runner, notifier, budget_per_day=2)

        # Populate 2 done
        with CandidateQueue(self.db) as q:
            for i in range(2):
                q.submit([_cand(ticker=f"T{i}", discriminator=f"d{i}")])
                job = q.claim_next()
                q.mark_success(
                    job["id"],
                    decision="HOLD",
                    duration_sec=1.0,
                    cost_usd=None,
                    model_used="x",
                )
            q.submit([_cand(ticker="NEW", discriminator="dnew")])

        # Budget exhausted → should not call runner, pending stays
        worker.process_one()
        runner.run.assert_not_called()
        with CandidateQueue(self.db) as q:
            self.assertEqual(len(q.list_by_status("pending")), 1)

    def test_process_one_does_not_notify_on_budget_exhaustion(self):
        """Budget-exhausted path must NOT spam Telegram — logs locally only.

        Worker plist fires every 5 min. Notifying on every exhausted tick spams
        12 messages/hour until midnight. Success messages already intrinsically
        signal the budget state. Fix: log, don't notify.
        """
        from alphalens.queue import CandidateQueue

        runner = MagicMock()
        notifier = MagicMock()
        worker = self._make_worker(runner, notifier, budget_per_day=1)

        with CandidateQueue(self.db) as q:
            q.submit([_cand(ticker="DONE", discriminator="d1")])
            job = q.claim_next()
            q.mark_success(
                job["id"],
                decision="HOLD",
                duration_sec=1.0,
                cost_usd=None,
                model_used="x",
            )

        notifier.send_message.reset_mock()

        with self.assertLogs("alphalens.worker", level="INFO") as captured:
            worker.process_one()

        notifier.send_message.assert_not_called()
        self.assertTrue(
            any("Budget exhausted" in line for line in captured.output),
            msg=f"Expected budget-exhausted log line, got: {captured.output}",
        )

    def test_process_one_takes_higher_priority_first(self):
        from alphalens.queue import CandidateQueue

        runner = MagicMock()
        runner.run.side_effect = lambda c, candidate_id: _fake_result(
            candidate_id, c.ticker, c.source
        )
        worker = self._make_worker(runner)

        with CandidateQueue(self.db) as q:
            q.submit([_cand(ticker="LO", priority=20, discriminator="dL")])
            q.submit([
                _cand(
                    ticker="HI", source="watchdog_sec", priority=0, discriminator="dH"
                )
            ])

        worker.process_one()
        first_ticker = runner.run.call_args.args[0].ticker
        self.assertEqual(first_ticker, "HI")

    def test_process_all_drains_queue(self):
        runner = MagicMock()
        runner.run.side_effect = lambda c, candidate_id: _fake_result(
            candidate_id, c.ticker, c.source
        )
        worker = self._make_worker(runner)

        worker.queue.submit([
            _cand(ticker="A", discriminator="d1"),
            _cand(ticker="B", discriminator="d2"),
        ])

        processed = worker.process_all()
        self.assertEqual(processed, 2)
        self.assertEqual(runner.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _cand(ticker="AAPL", source="momentum", priority=10, payload=None, discriminator="D1"):
    from alphalens.candidates import Candidate

    return Candidate.from_screener(
        ticker=ticker,
        source=source,
        priority=priority,
        payload=payload or {},
        discriminator=discriminator,
    )


class TestCandidateQueueSubmit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_submit_inserts_candidate_returns_count_of_new(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        n = q.submit([_cand()])
        self.assertEqual(n, 1)
        self.assertEqual(len(q.list_by_status("pending")), 1)

    def test_submit_dedups_by_key(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        c = _cand()
        q.submit([c])
        n = q.submit([c])
        self.assertEqual(n, 0)
        self.assertEqual(len(q.list_by_status("pending")), 1)

    def test_submit_mixed_new_and_dup(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        c1 = _cand(ticker="AAPL", discriminator="D1")
        c2 = _cand(ticker="MSFT", discriminator="D2")
        q.submit([c1])
        n = q.submit([c1, c2])
        self.assertEqual(n, 1)
        self.assertEqual(len(q.list_by_status("pending")), 2)


class TestCandidateQueueClaim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_next_returns_highest_priority_first(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        q.submit([_cand(ticker="LO", priority=20, discriminator="d1")])
        q.submit([_cand(ticker="HI", priority=0, source="watchdog_sec", discriminator="d2")])

        job = q.claim_next()
        self.assertEqual(job["ticker"], "HI")
        self.assertEqual(job["priority"], 0)
        self.assertEqual(job["status"], "in_progress")

    def test_claim_next_fifo_within_priority(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        q.submit([_cand(ticker="FIRST", discriminator="d1")])
        time.sleep(0.01)
        q.submit([_cand(ticker="SECOND", discriminator="d2")])

        job = q.claim_next()
        self.assertEqual(job["ticker"], "FIRST")

    def test_claim_next_returns_none_when_empty(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        self.assertIsNone(q.claim_next())

    def test_claim_next_skips_items_awaiting_retry(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        q.submit([_cand(ticker="LATER", discriminator="dL")])
        job = q.claim_next()
        q.mark_failure(job["id"], error="transient")  # schedules next_retry_at in future

        # Now another candidate; claim_next should return the new one, not the retry one
        q.submit([_cand(ticker="NOW", discriminator="dN")])
        job2 = q.claim_next()
        self.assertIsNotNone(job2)
        self.assertEqual(job2["ticker"], "NOW")


class TestCandidateQueueFailureAndRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _now(self):
        return datetime.now(timezone.utc)

    def test_mark_failure_increments_attempts_and_schedules_retry(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db, base_retry_s=60, max_attempts=5)
        q.submit([_cand()])
        job = q.claim_next()
        before = self._now()
        q.mark_failure(job["id"], error="oops")

        rows = q.list_by_status("pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 1)
        nxt = datetime.fromisoformat(rows[0]["next_retry_at"])
        # expected delay ≈ 60s * 2^(1-1) = 60s after the failure
        expected_min = before + timedelta(seconds=55)
        expected_max = before + timedelta(seconds=70)
        self.assertGreaterEqual(nxt, expected_min)
        self.assertLessEqual(nxt, expected_max)

    def test_mark_failure_exponential_backoff(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db, base_retry_s=60, max_attempts=5)
        q.submit([_cand()])
        delays = []
        for _ in range(3):
            job = q.claim_next_ignoring_retry_window()  # test helper for sequential runs
            before = self._now()
            q.mark_failure(job["id"], error="x")
            row = q.list_by_status("pending")[0]
            nxt = datetime.fromisoformat(row["next_retry_at"])
            delays.append((nxt - before).total_seconds())
        # 60s, 120s, 240s (± 5s tolerance)
        self.assertAlmostEqual(delays[0], 60, delta=5)
        self.assertAlmostEqual(delays[1], 120, delta=5)
        self.assertAlmostEqual(delays[2], 240, delta=5)

    def test_mark_failure_moves_to_dead_after_max_attempts(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db, base_retry_s=1, max_attempts=3)
        q.submit([_cand()])
        for _ in range(3):
            job = q.claim_next_ignoring_retry_window()
            q.mark_failure(job["id"], error="again")

        self.assertEqual(len(q.list_by_status("pending")), 0)
        self.assertEqual(len(q.list_by_status("dead")), 1)


class TestCandidateQueueSuccess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "q.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_mark_success_stores_decision_and_metrics(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        q.submit([_cand()])
        job = q.claim_next()
        q.mark_success(
            job["id"],
            decision="BUY",
            duration_sec=42.0,
            cost_usd=None,
            model_used="gemini-3-pro-preview",
        )

        done = q.list_by_status("done")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["decision"], "BUY")
        self.assertAlmostEqual(done[0]["duration_sec"], 42.0)
        self.assertIsNone(done[0]["cost_usd"])
        self.assertEqual(done[0]["model_used"], "gemini-3-pro-preview")
        self.assertIsNotNone(done[0]["finished_at"])

    def test_count_done_today(self):
        from alphalens.queue import CandidateQueue

        q = CandidateQueue(self.db)
        for i in range(3):
            q.submit([_cand(ticker=f"T{i}", discriminator=f"d{i}")])
            job = q.claim_next()
            q.mark_success(
                job["id"],
                decision="HOLD",
                duration_sec=1.0,
                cost_usd=None,
                model_used="x",
            )
        self.assertEqual(q.count_done_today(), 3)


if __name__ == "__main__":
    unittest.main()

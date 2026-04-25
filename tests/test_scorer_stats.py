"""Tests for scorer-stats analytics: acceptance rates per scorer source."""

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _seed_candidate(conn, ticker, source, decision, status="done", finished_days_ago=0):
    now = datetime.now(UTC)
    enq = now - timedelta(days=finished_days_ago + 1)
    fin = now - timedelta(days=finished_days_ago)
    conn.execute(
        """INSERT INTO candidates
           (ticker, source, priority, payload, dedup_key, status,
            enqueued_at, started_at, finished_at, decision, duration_sec, cost_usd, model_used)
           VALUES (?, ?, 10, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ticker,
            source,
            "{}",
            f"{ticker}-{source}-{fin.isoformat()}",
            status,
            enq.isoformat(),
            enq.isoformat(),
            fin.isoformat(),
            decision,
            30.0,
            None,
            "gemini-2.5-flash",
        ),
    )
    conn.commit()


class TestScorerStats(unittest.TestCase):
    def _make_queue(self) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = Path(tmp.name)
        # Initialize schema via CandidateQueue
        from alphalens.queue import CandidateQueue

        CandidateQueue(db_path).close() if False else CandidateQueue(db_path)
        return db_path

    def test_groups_by_source_and_counts_decisions(self):
        from alphalens.scorer_stats import compute_scorer_stats

        db = self._make_queue()
        conn = sqlite3.connect(str(db))
        # 4 momentum: 1 BUY, 1 OVERWEIGHT, 2 HOLD
        _seed_candidate(conn, "AAA", "momentum", "BUY")
        _seed_candidate(conn, "BBB", "momentum", "OVERWEIGHT")
        _seed_candidate(conn, "CCC", "momentum", "HOLD")
        _seed_candidate(conn, "DDD", "momentum", "HOLD")
        # 3 early-stage: 2 BUY, 1 SELL
        _seed_candidate(conn, "EEE", "early-stage", "BUY")
        _seed_candidate(conn, "FFF", "early-stage", "BUY")
        _seed_candidate(conn, "GGG", "early-stage", "SELL")
        conn.close()

        stats = compute_scorer_stats(db, since_days=30)

        mom = [s for s in stats if s["source"] == "momentum"][0]
        early = [s for s in stats if s["source"] == "early-stage"][0]

        self.assertEqual(mom["total"], 4)
        self.assertEqual(mom["buy_count"], 1)
        self.assertEqual(mom["overweight_count"], 1)
        self.assertEqual(mom["hold_count"], 2)
        self.assertAlmostEqual(mom["accept_rate"], 0.5)  # BUY+OW / total

        self.assertEqual(early["total"], 3)
        self.assertEqual(early["buy_count"], 2)
        self.assertAlmostEqual(early["accept_rate"], 2 / 3)

    def test_since_days_filter(self):
        from alphalens.scorer_stats import compute_scorer_stats

        db = self._make_queue()
        conn = sqlite3.connect(str(db))
        _seed_candidate(conn, "RECENT", "momentum", "BUY", finished_days_ago=5)
        _seed_candidate(conn, "OLD", "momentum", "BUY", finished_days_ago=60)
        conn.close()

        stats_7 = compute_scorer_stats(db, since_days=7)
        stats_90 = compute_scorer_stats(db, since_days=90)

        self.assertEqual(stats_7[0]["total"], 1)
        self.assertEqual(stats_90[0]["total"], 2)

    def test_pending_and_failed_excluded(self):
        from alphalens.scorer_stats import compute_scorer_stats

        db = self._make_queue()
        conn = sqlite3.connect(str(db))
        _seed_candidate(conn, "DONE", "momentum", "BUY", status="done")
        _seed_candidate(conn, "PEND", "momentum", None, status="pending")
        _seed_candidate(conn, "DEAD", "momentum", None, status="dead")
        conn.close()

        stats = compute_scorer_stats(db, since_days=30)
        self.assertEqual(stats[0]["total"], 1)  # only DONE counts


if __name__ == "__main__":
    unittest.main()

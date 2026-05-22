import dataclasses
import unittest
from datetime import UTC, datetime


class TestCandidate(unittest.TestCase):
    def test_candidate_is_frozen(self):
        from alphalens_research.core.candidates import Candidate

        c = Candidate(
            ticker="AAPL",
            source="momentum",
            detected_at=datetime(2026, 4, 17, tzinfo=UTC),
            priority=10,
            payload={"momentum_score": 0.5},
            dedup_key="k1",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            c.ticker = "MSFT"  # type: ignore[misc]

    def test_from_screener_builds_stable_dedup_key(self):
        from alphalens_research.core.candidates import Candidate

        c1 = Candidate.from_screener(
            ticker="AAPL",
            source="momentum",
            priority=10,
            payload={"x": 1},
            discriminator="2026-04-17",
        )
        c2 = Candidate.from_screener(
            ticker="AAPL",
            source="momentum",
            priority=10,
            payload={"x": 999},  # payload must NOT affect dedup
            discriminator="2026-04-17",
        )
        self.assertEqual(c1.dedup_key, c2.dedup_key)

    def test_dedup_key_differs_for_different_source(self):
        from alphalens_research.core.candidates import Candidate

        c1 = Candidate.from_screener(
            ticker="AAPL",
            source="momentum",
            priority=10,
            payload={},
            discriminator="2026-04-17",
        )
        c2 = Candidate.from_screener(
            ticker="AAPL",
            source="prescreener",
            priority=20,
            payload={},
            discriminator="2026-04-17",
        )
        self.assertNotEqual(c1.dedup_key, c2.dedup_key)

    def test_dedup_key_differs_for_different_discriminator(self):
        from alphalens_research.core.candidates import Candidate

        c1 = Candidate.from_screener(
            ticker="AAPL",
            source="watchdog_sec",
            priority=0,
            payload={},
            discriminator="ACC-1",
        )
        c2 = Candidate.from_screener(
            ticker="AAPL",
            source="watchdog_sec",
            priority=0,
            payload={},
            discriminator="ACC-2",
        )
        self.assertNotEqual(c1.dedup_key, c2.dedup_key)

    def test_detected_at_defaults_to_now_utc(self):
        from alphalens_research.core.candidates import Candidate

        c = Candidate.from_screener(
            ticker="AAPL", source="momentum", priority=10, payload={}, discriminator="d"
        )
        self.assertIsNotNone(c.detected_at.tzinfo)
        # within a few seconds of now
        delta = datetime.now(UTC) - c.detected_at
        self.assertLess(delta.total_seconds(), 5)


class TestAnalysisResult(unittest.TestCase):
    def test_analysis_result_fields(self):
        from alphalens_research.core.candidates import AnalysisResult

        r = AnalysisResult(
            candidate_id=7,
            ticker="AAPL",
            source="momentum",
            rating="BUY",
            duration_sec=12.5,
            cost_usd=None,
            model_used="gemini-3-pro-preview",
            completed_at=datetime(2026, 4, 17, tzinfo=UTC),
            final_state={"any": "state"},
        )
        self.assertEqual(r.rating, "BUY")
        self.assertIsNone(r.cost_usd)


if __name__ == "__main__":
    unittest.main()

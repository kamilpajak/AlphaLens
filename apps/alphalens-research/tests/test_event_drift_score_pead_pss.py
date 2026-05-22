"""Tests for the PSS rank scorer (paradigm-14 PEAD v2 B1)."""

from __future__ import annotations

import unittest
from datetime import date

from alphalens_research.screeners.event_drift.av_earnings_ingestion import (
    AVEarningsAnnouncement,
)


def _event(
    ticker: str,
    reported: date,
    rep: float,
    est: float,
    *,
    period_end: date | None = None,
    report_time: str = "post-market",
) -> AVEarningsAnnouncement:
    return AVEarningsAnnouncement(
        ticker=ticker,
        period_end=period_end or date(reported.year, max(1, reported.month - 1), 1),
        reported_date=reported,
        reported_eps=rep,
        estimated_eps=est,
        report_time=report_time,  # type: ignore[arg-type]
    )


class TestPssFormula(unittest.TestCase):
    def test_formula_matches_spec(self) -> None:
        """PSS = (reportedEPS - estimatedEPS) / close(t-1)."""
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {
            "A": [_event("A", date(2020, 6, 10), rep=1.20, est=1.00)],  # surprise +0.20
            "B": [_event("B", date(2020, 6, 11), rep=2.10, est=2.05)],  # surprise +0.05
        }
        # close(t-1) = $100 for A, $50 for B — non-uniform price-scaling.
        closes = {
            ("A", date(2020, 6, 9)): 100.0,
            ("B", date(2020, 6, 10)): 50.0,
        }
        df = pss_rank(
            asof=date(2020, 6, 20),
            universe=["A", "B"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
        )
        # PSS(A) = 0.20 / 100 = 0.002 ; PSS(B) = 0.05 / 50 = 0.001
        # Both eligible; cross-sectional rank: A > B.
        a_row = df[df["ticker"] == "A"].iloc[0]
        b_row = df[df["ticker"] == "B"].iloc[0]
        self.assertAlmostEqual(float(a_row["pss"]), 0.002)
        self.assertAlmostEqual(float(b_row["pss"]), 0.001)
        self.assertGreater(float(a_row["percentile_rank"]), float(b_row["percentile_rank"]))


class TestCohortPIT(unittest.TestCase):
    def test_cohort_excludes_future_events(self) -> None:
        """Events with reported_date > asof MUST NOT appear — PIT correctness."""
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {
            "A": [
                _event("A", date(2020, 6, 1), rep=1.10, est=1.00),  # in cohort
                _event("A", date(2020, 7, 1), rep=1.30, est=1.00),  # future — exclude
            ],
        }
        closes = {("A", date(2020, 5, 31)): 50.0, ("A", date(2020, 6, 30)): 50.0}
        df = pss_rank(
            asof=date(2020, 6, 15),
            universe=["A"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
        )
        # Only the 2020-06-01 event should drive the score.
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["reported_date"], date(2020, 6, 1))

    def test_cohort_excludes_events_outside_window(self) -> None:
        """Events older than asof - cohort_window days MUST NOT appear."""
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {
            "A": [
                _event("A", date(2020, 4, 1), rep=1.10, est=1.00),  # > 45d ago — exclude
                _event("A", date(2020, 6, 1), rep=1.20, est=1.00),  # within 45d — keep
            ],
        }
        closes = {("A", date(2020, 3, 31)): 50.0, ("A", date(2020, 5, 31)): 50.0}
        df = pss_rank(
            asof=date(2020, 6, 15),
            universe=["A"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
            cohort_window_days=45,
        )
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["reported_date"], date(2020, 6, 1))


class TestEligibilityFilters(unittest.TestCase):
    def test_drops_penny_stock_close_below_5(self) -> None:
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {
            "PENNY": [_event("PENNY", date(2020, 6, 10), rep=0.20, est=0.10)],
            "OK": [_event("OK", date(2020, 6, 11), rep=1.20, est=1.00)],
        }
        closes = {
            ("PENNY", date(2020, 6, 9)): 4.99,  # below $5 — drop
            ("OK", date(2020, 6, 10)): 50.00,  # eligible
        }
        df = pss_rank(
            asof=date(2020, 6, 20),
            universe=["PENNY", "OK"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
        )
        self.assertEqual(list(df["ticker"]), ["OK"])

    def test_drops_abs_pss_outliers_above_threshold(self) -> None:
        """|PSS| < 0.20 cap — extreme surprises are usually data errors or
        special-situation reporting, not signal."""
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {
            "WILD": [_event("WILD", date(2020, 6, 10), rep=15.00, est=1.00)],  # PSS = 14/10 = 1.4
            "OK": [_event("OK", date(2020, 6, 11), rep=1.20, est=1.00)],  # PSS = 0.02
        }
        closes = {
            ("WILD", date(2020, 6, 9)): 10.0,
            ("OK", date(2020, 6, 10)): 10.0,
        }
        df = pss_rank(
            asof=date(2020, 6, 20),
            universe=["WILD", "OK"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
        )
        self.assertEqual(list(df["ticker"]), ["OK"])

    def test_drops_when_close_lookup_missing(self) -> None:
        """If close(t-1) is None (no price data), event must be dropped —
        cannot compute PSS denominator."""
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {"A": [_event("A", date(2020, 6, 10), rep=1.20, est=1.00)]}
        df = pss_rank(
            asof=date(2020, 6, 20),
            universe=["A"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: None,
        )
        self.assertEqual(len(df), 0)


class TestScoreDataFrameShape(unittest.TestCase):
    def test_columns_and_dtypes(self) -> None:
        import pandas as pd
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {
            "A": [_event("A", date(2020, 6, 10), rep=1.20, est=1.00)],
            "B": [_event("B", date(2020, 6, 11), rep=1.05, est=1.00)],
            "C": [_event("C", date(2020, 6, 12), rep=1.10, est=1.00)],
        }
        closes = {
            ("A", date(2020, 6, 9)): 100.0,
            ("B", date(2020, 6, 10)): 100.0,
            ("C", date(2020, 6, 11)): 100.0,
        }
        df = pss_rank(
            asof=date(2020, 6, 20),
            universe=["A", "B", "C"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
        )
        expected = {
            "ticker",
            "period_end",
            "reported_date",
            "report_time",
            "pss",
            "percentile_rank",
        }
        self.assertEqual(set(df.columns), expected)
        self.assertEqual(len(df), 3)
        self.assertTrue(pd.api.types.is_float_dtype(df["pss"]))
        self.assertTrue(pd.api.types.is_float_dtype(df["percentile_rank"]))
        # Percentile range in [0, 100]
        self.assertTrue((df["percentile_rank"] >= 0).all())
        self.assertTrue((df["percentile_rank"] <= 100).all())

    def test_empty_universe_returns_empty_frame(self) -> None:
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        df = pss_rank(
            asof=date(2020, 6, 20),
            universe=[],
            earnings_loader=lambda t: [],
            close_lookup=lambda t, d: None,
        )
        self.assertEqual(len(df), 0)
        # Schema still well-formed even when empty.
        self.assertIn("ticker", df.columns)
        self.assertIn("percentile_rank", df.columns)

    def test_ticker_with_zero_events_omitted_from_output(self) -> None:
        """Tickers whose earnings_loader returns [] (no AV cache, no events
        in window, or new IPO) must be silently omitted rather than emit
        a row with NaN PSS — downstream B2 thresholding would misread NaN."""
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {
            "EMPTY": [],  # no events at all
            "OK": [_event("OK", date(2020, 6, 11), rep=1.20, est=1.00)],
        }
        closes = {("OK", date(2020, 6, 10)): 100.0}
        df = pss_rank(
            asof=date(2020, 6, 20),
            universe=["EMPTY", "OK"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
        )
        self.assertEqual(list(df["ticker"]), ["OK"])

    def test_percentile_rank_ties_get_equal_mid_rank(self) -> None:
        """pandas `rank(method='average')` gives identical PSS values the
        average of their tied positions. Locks the tie-handling contract."""
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        # Three tickers, identical PSS → all should rank at the midpoint
        # (positions 1, 2, 3 averaged = 2; percentile = 2/3 * 100 = 66.67).
        events = {
            "A": [_event("A", date(2020, 6, 10), rep=1.10, est=1.00)],
            "B": [_event("B", date(2020, 6, 11), rep=1.10, est=1.00)],
            "C": [_event("C", date(2020, 6, 12), rep=1.10, est=1.00)],
        }
        closes = {
            ("A", date(2020, 6, 9)): 100.0,
            ("B", date(2020, 6, 10)): 100.0,
            ("C", date(2020, 6, 11)): 100.0,
        }
        df = pss_rank(
            asof=date(2020, 6, 20),
            universe=["A", "B", "C"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
        )
        ranks = df["percentile_rank"].tolist()
        self.assertEqual(len(set(ranks)), 1)  # all identical
        self.assertAlmostEqual(ranks[0], 200.0 / 3.0)  # ~66.67

    def test_multiple_events_per_ticker_picks_latest_in_cohort(self) -> None:
        """If a ticker has 2 events in cohort (rare), pick the most recent."""
        from alphalens_research.screeners.event_drift.score_pead_pss import pss_rank

        events = {
            "A": [
                _event("A", date(2020, 5, 25), rep=1.05, est=1.00),  # in cohort
                _event("A", date(2020, 6, 12), rep=1.30, est=1.00),  # in cohort, more recent
            ],
        }
        closes = {
            ("A", date(2020, 5, 24)): 100.0,
            ("A", date(2020, 6, 11)): 100.0,
        }
        df = pss_rank(
            asof=date(2020, 6, 15),
            universe=["A"],
            earnings_loader=lambda t: events[t],
            close_lookup=lambda t, d: closes.get((t, d)),
        )
        self.assertEqual(len(df), 1)
        # Latest event drives the score.
        self.assertEqual(df.iloc[0]["reported_date"], date(2020, 6, 12))
        self.assertAlmostEqual(float(df.iloc[0]["pss"]), 0.30 / 100.0)


if __name__ == "__main__":
    unittest.main()

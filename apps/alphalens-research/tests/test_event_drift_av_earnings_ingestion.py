"""Tests for AV EARNINGS ingestion reader (paradigm-14 PEAD v2 A2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


def _write_cache(cache_dir: Path, ticker: str, payload: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"earnings_{ticker.upper()}.json").write_text(json.dumps(payload))


def _aapl_q1_fy2018_payload() -> dict:
    """Real A3-validated AAPL Q1 FY2018 (post-2020 4:1 split-adjusted)."""
    return {
        "symbol": "AAPL",
        "annualEarnings": [],
        "quarterlyEarnings": [
            # Modern Q4 FY2025 (recent)
            {
                "fiscalDateEnding": "2025-09-30",
                "reportedDate": "2025-10-30",
                "reportedEPS": "1.85",
                "estimatedEPS": "1.80",
                "surprise": "0.05",
                "surprisePercentage": "2.7",
                "reportTime": "post-market",
            },
            # Q1 FY2018 anchor event (A3-validated)
            {
                "fiscalDateEnding": "2017-12-30",
                "reportedDate": "2018-02-01",
                "reportedEPS": "0.9725",
                "estimatedEPS": "0.965",
                "surprise": "0.0075",
                "surprisePercentage": "0.7772",
                "reportTime": "post-market",
            },
        ],
    }


class TestLoadAvEarnings(unittest.TestCase):
    def test_parses_quarterly_entries_into_typed_dataclasses(self) -> None:
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import (
            AVEarningsAnnouncement,
            load_av_earnings,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cache(cache, "AAPL", _aapl_q1_fy2018_payload())
            events = load_av_earnings("AAPL", cache_dir=cache)

            self.assertEqual(len(events), 2)
            for e in events:
                self.assertIsInstance(e, AVEarningsAnnouncement)
                self.assertEqual(e.ticker, "AAPL")
                self.assertIsInstance(e.period_end, date)
                self.assertIsInstance(e.reported_date, date)
                self.assertIsInstance(e.reported_eps, float)
                self.assertIsInstance(e.estimated_eps, float)

    def test_returns_sorted_by_reported_date_ascending(self) -> None:
        """B1 cohort-window scoring iterates forward in time; reader must
        return events oldest-first for downstream PIT correctness."""
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import load_av_earnings

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cache(cache, "AAPL", _aapl_q1_fy2018_payload())
            events = load_av_earnings("AAPL", cache_dir=cache)
            dates = [e.reported_date for e in events]
            self.assertEqual(dates, sorted(dates))

    def test_uppercase_ticker_input_resolved_to_canonical_cache_path(self) -> None:
        """Match av_earnings_client._cache_path canonicalisation: uppercase
        filename. Reader must lookup the same canonical path."""
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import load_av_earnings

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cache(cache, "AAPL", _aapl_q1_fy2018_payload())
            events = load_av_earnings("aapl", cache_dir=cache)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].ticker, "AAPL")

    def test_raises_when_ticker_not_cached(self) -> None:
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import load_av_earnings

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_av_earnings("MISSING", cache_dir=Path(tmp))


class TestEligibilityFilters(unittest.TestCase):
    def test_drops_entries_before_2017_06_01_default_cutoff(self) -> None:
        """1-quarter buffer before IS phase start 2018-01-01."""
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import load_av_earnings

        payload = {
            "symbol": "AAPL",
            "quarterlyEarnings": [
                # Pre-cutoff: drop.
                {
                    "fiscalDateEnding": "2017-03-31",
                    "reportedDate": "2017-05-01",
                    "reportedEPS": "1.00",
                    "estimatedEPS": "0.95",
                    "reportTime": "post-market",
                },
                # On cutoff: keep (>= bound).
                {
                    "fiscalDateEnding": "2017-06-30",
                    "reportedDate": "2017-06-01",
                    "reportedEPS": "1.10",
                    "estimatedEPS": "1.05",
                    "reportTime": "post-market",
                },
                # Post-cutoff: keep.
                {
                    "fiscalDateEnding": "2018-03-31",
                    "reportedDate": "2018-05-01",
                    "reportedEPS": "1.20",
                    "estimatedEPS": "1.15",
                    "reportTime": "post-market",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cache(cache, "AAPL", payload)
            events = load_av_earnings("AAPL", cache_dir=cache)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].reported_date, date(2017, 6, 1))
            self.assertEqual(events[1].reported_date, date(2018, 5, 1))

    def test_drops_entries_with_abs_estimated_eps_below_threshold(self) -> None:
        """|estimatedEPS| >= 0.10 — drops penny-EPS noise (price-scaled PSS
        explodes when denominator is tiny)."""
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import load_av_earnings

        payload = {
            "symbol": "X",
            "quarterlyEarnings": [
                # Below threshold: drop.
                {
                    "fiscalDateEnding": "2020-03-31",
                    "reportedDate": "2020-05-01",
                    "reportedEPS": "0.02",
                    "estimatedEPS": "0.05",
                    "reportTime": "post-market",
                },
                # Negative below threshold: drop.
                {
                    "fiscalDateEnding": "2020-06-30",
                    "reportedDate": "2020-08-01",
                    "reportedEPS": "-0.05",
                    "estimatedEPS": "-0.08",
                    "reportTime": "post-market",
                },
                # At threshold: keep.
                {
                    "fiscalDateEnding": "2020-09-30",
                    "reportedDate": "2020-11-01",
                    "reportedEPS": "0.15",
                    "estimatedEPS": "0.10",
                    "reportTime": "post-market",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cache(cache, "X", payload)
            events = load_av_earnings("X", cache_dir=cache)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].estimated_eps, 0.10)

    def test_drops_entries_with_null_or_missing_required_fields(self) -> None:
        """AV sometimes returns 'None' string (literal) for missing values.
        Defensive: drop those without failing the whole ticker."""
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import load_av_earnings

        payload = {
            "symbol": "X",
            "quarterlyEarnings": [
                # estimatedEPS='None': drop.
                {
                    "fiscalDateEnding": "2020-03-31",
                    "reportedDate": "2020-05-01",
                    "reportedEPS": "1.00",
                    "estimatedEPS": "None",
                    "reportTime": "post-market",
                },
                # reportedEPS missing: drop.
                {
                    "fiscalDateEnding": "2020-06-30",
                    "reportedDate": "2020-08-01",
                    "estimatedEPS": "0.95",
                    "reportTime": "post-market",
                },
                # All present: keep.
                {
                    "fiscalDateEnding": "2020-09-30",
                    "reportedDate": "2020-11-01",
                    "reportedEPS": "1.10",
                    "estimatedEPS": "1.05",
                    "reportTime": "post-market",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cache(cache, "X", payload)
            events = load_av_earnings("X", cache_dir=cache)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].reported_eps, 1.10)


class TestReportTimeHandling(unittest.TestCase):
    def test_report_time_present_preserved(self) -> None:
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import load_av_earnings

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cache(cache, "AAPL", _aapl_q1_fy2018_payload())
            events = load_av_earnings("AAPL", cache_dir=cache)
            self.assertEqual(events[0].report_time, "post-market")
            self.assertEqual(events[1].report_time, "post-market")

    def test_report_time_absent_defaults_to_post_market(self) -> None:
        """Pre-2010 AV history may lack reportTime (per ledger outcome).
        Default conservative: post-market → B2 enters close(t+1), no
        intraday-info lookahead."""
        from alphalens_research.screeners.event_drift.av_earnings_ingestion import load_av_earnings

        payload = {
            "symbol": "X",
            "quarterlyEarnings": [
                {
                    "fiscalDateEnding": "2020-03-31",
                    "reportedDate": "2020-05-01",
                    "reportedEPS": "1.10",
                    "estimatedEPS": "1.05",
                    # reportTime: absent
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _write_cache(cache, "X", payload)
            events = load_av_earnings("X", cache_dir=cache)
            self.assertEqual(events[0].report_time, "post-market")


if __name__ == "__main__":
    unittest.main()

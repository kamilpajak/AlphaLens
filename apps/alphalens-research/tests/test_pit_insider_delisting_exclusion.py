"""F4 invariant: ParquetInsiderScorer must exclude pre-delisting fire-sales.

Locks the F4 finding from `docs/research/pit_audit_2026_04_30_findings.md`.
Without this filter, naive backtests treat insider sales in the months
before bankruptcy/delisting as informed cluster activity. Those sales
are panic unloading, not information — a backtest that catches the
subsequent collapse attributes alpha to insider-detection skill when
the real driver is delisting selection bias (estimated 100-300 bps
inflation per the audit).

Contract: when ``ParquetInsiderScorer`` is constructed with a non-empty
``delisting_events`` argument, ``features_as_of(ticker, asof)`` returns
``None`` whenever the ticker has any delisting event within
``delisting_exclusion_days`` (default 180) of ``asof`` — including
delistings already in the past, since after delisting the ticker is gone.

Backwards compatible: when constructed without ``delisting_events``,
behavior is identical to the prior contract (no exclusion).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.data.store.delisting import DelistingEvent
from alphalens_research.screeners.insider_activity.parquet_scorer import ParquetInsiderScorer


def _write_dataset(root: Path) -> None:
    """Tiny dataset: AAPL on 3 distinct asof dates + DOOMED on 3 dates near its
    delisting + ALIVE far from any event.
    """
    year_dir = root / "year=2024"
    year_dir.mkdir(parents=True)
    table = pa.table(
        {
            "ticker": [
                "AAPL",
                "AAPL",
                "AAPL",
                "DOOMED",
                "DOOMED",
                "DOOMED",
                "ALIVE",
            ],
            "date": [
                date(2024, 1, 15),
                date(2024, 6, 10),
                date(2024, 11, 4),
                date(2024, 1, 15),
                date(2024, 6, 10),
                date(2024, 11, 4),
                date(2024, 6, 10),
            ],
            "has_features": [True, True, True, True, True, True, True],
            "insider_count": [3, 4, 5, 6, 7, 8, 9],
            "aggregate_dollar": [
                10000.0,
                20000.0,
                30000.0,
                40000.0,
                50000.0,
                60000.0,
                70000.0,
            ],
            "cluster_window_days": [30, 30, 30, 30, 30, 30, 30],
            "asof": [
                date(2024, 1, 15),
                date(2024, 6, 10),
                date(2024, 11, 4),
                date(2024, 1, 15),
                date(2024, 6, 10),
                date(2024, 11, 4),
                date(2024, 6, 10),
            ],
            "cached_at": [None] * 7,
        }
    )
    pq.write_table(table, year_dir / "part-0.parquet")


class TestDelistingExclusionDefault(unittest.TestCase):
    """No ``delisting_events`` passed → behavior unchanged from prior contract."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "insider_form4.parquet"
        self.root.mkdir()
        _write_dataset(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_delisting_arg_returns_features_for_doomed_ticker(self):
        scorer = ParquetInsiderScorer(self.root)
        feat = scorer.features_as_of("DOOMED", date(2024, 6, 10))
        self.assertIsNotNone(feat)
        self.assertEqual(feat["insider_count"], 7)

    def test_empty_delisting_list_returns_features_for_doomed_ticker(self):
        scorer = ParquetInsiderScorer(self.root, delisting_events=[])
        feat = scorer.features_as_of("DOOMED", date(2024, 6, 10))
        self.assertIsNotNone(feat)


class TestDelistingExclusionFireSaleFilter(unittest.TestCase):
    """Core F4 contract: exclude (ticker, asof) within N days BEFORE delisting."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "insider_form4.parquet"
        self.root.mkdir()
        _write_dataset(self.root)
        # DOOMED delists on 2024-12-01. With default 180-day exclusion:
        #   - 2024-06-10  → 174 days before delisting → EXCLUDE
        #   - 2024-11-04  →  27 days before delisting → EXCLUDE
        #   - 2024-01-15  → 321 days before delisting → KEEP (not yet a fire sale)
        self.events = [
            DelistingEvent(ticker="DOOMED", delisted_date=date(2024, 12, 1), reason="bankruptcy"),
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def test_excludes_when_delisting_within_180_days_after_asof(self):
        scorer = ParquetInsiderScorer(self.root, delisting_events=self.events)
        # 2024-06-10 + 180d = 2024-12-07; 2024-12-01 falls inside → exclude
        self.assertIsNone(scorer.features_as_of("DOOMED", date(2024, 6, 10)))

    def test_excludes_when_delisting_imminent(self):
        scorer = ParquetInsiderScorer(self.root, delisting_events=self.events)
        # 2024-11-04 + 180d covers 2024-12-01 with huge margin → exclude
        self.assertIsNone(scorer.features_as_of("DOOMED", date(2024, 11, 4)))

    def test_keeps_when_delisting_more_than_180_days_after_asof(self):
        scorer = ParquetInsiderScorer(self.root, delisting_events=self.events)
        # 2024-01-15 + 180d = 2024-07-13; 2024-12-01 is well outside → keep
        feat = scorer.features_as_of("DOOMED", date(2024, 1, 15))
        self.assertIsNotNone(feat)
        self.assertEqual(feat["insider_count"], 6)

    def test_excludes_post_delisting_lookups(self):
        # asof after delisting: ticker is gone, any cached row is stale.
        # We didn't write a 2025 row but the contract is symmetric — if a
        # stale cache row existed, exclusion still applies.
        events = [
            DelistingEvent(ticker="AAPL", delisted_date=date(2024, 3, 1), reason="merger"),
        ]
        scorer = ParquetInsiderScorer(self.root, delisting_events=events)
        # AAPL has rows on 2024-06-10 and 2024-11-04, both AFTER delisting → exclude.
        self.assertIsNone(scorer.features_as_of("AAPL", date(2024, 6, 10)))
        self.assertIsNone(scorer.features_as_of("AAPL", date(2024, 11, 4)))
        # 2024-01-15: 45 days before delisting → still inside exclusion window → exclude.
        self.assertIsNone(scorer.features_as_of("AAPL", date(2024, 1, 15)))

    def test_other_tickers_unaffected(self):
        scorer = ParquetInsiderScorer(self.root, delisting_events=self.events)
        # ALIVE has no delisting event → never excluded.
        feat = scorer.features_as_of("ALIVE", date(2024, 6, 10))
        self.assertIsNotNone(feat)
        self.assertEqual(feat["insider_count"], 9)
        # AAPL also unaffected (only DOOMED in events list).
        feat_aapl = scorer.features_as_of("AAPL", date(2024, 6, 10))
        self.assertIsNotNone(feat_aapl)


class TestDelistingExclusionCustomWindow(unittest.TestCase):
    """Exclusion window length is configurable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "insider_form4.parquet"
        self.root.mkdir()
        _write_dataset(self.root)
        self.events = [
            DelistingEvent(ticker="DOOMED", delisted_date=date(2024, 12, 1), reason="bankruptcy"),
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def test_custom_30_day_window_does_not_exclude_174_days_before(self):
        scorer = ParquetInsiderScorer(
            self.root, delisting_events=self.events, delisting_exclusion_days=30
        )
        # 2024-06-10 + 30d = 2024-07-10; 2024-12-01 well outside → keep
        feat = scorer.features_as_of("DOOMED", date(2024, 6, 10))
        self.assertIsNotNone(feat)

    def test_custom_30_day_window_still_excludes_imminent_delisting(self):
        scorer = ParquetInsiderScorer(
            self.root, delisting_events=self.events, delisting_exclusion_days=30
        )
        # 2024-11-04 + 30d = 2024-12-04; 2024-12-01 inside → exclude
        self.assertIsNone(scorer.features_as_of("DOOMED", date(2024, 11, 4)))

    def test_zero_window_disables_exclusion(self):
        scorer = ParquetInsiderScorer(
            self.root, delisting_events=self.events, delisting_exclusion_days=0
        )
        # window=0 disables the filter entirely → all DOOMED lookups return features
        self.assertIsNotNone(scorer.features_as_of("DOOMED", date(2024, 6, 10)))
        self.assertIsNotNone(scorer.features_as_of("DOOMED", date(2024, 11, 4)))
        self.assertIsNotNone(scorer.features_as_of("DOOMED", date(2024, 1, 15)))


if __name__ == "__main__":
    unittest.main()

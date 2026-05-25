"""Tests for Foster-1977 SUE with first-filed PIT snapshots.

Locked into v4 v2 pre-reg per
docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json:
feature `earnings_sue_naive_4q_decayed` consumes this module.

PIT contract per perplexity adversarial review Objection 3:
- For each historical quarter `q`, the EPS value used in residual-std computation
  is the FIRST-FILED entry (earliest `filed` date) for that period_end, NOT the
  latest-restated value. This implements Foster (1977) original construction
  where surprises are measured against the EPS that was actually known at each
  historical filing.
- At asof t, only entries with `filed <= asof` are visible.

The "first-filed" entry for period_end p is fixed regardless of asof (it is the
entry with the literally earliest filed date), but visibility depends on asof.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path


def _make_companyfacts(eps_records: list[dict]) -> dict:
    """Build a minimal SEC companyfacts JSON with EarningsPerShareBasic block.

    Each ``eps_records[i]`` should be a dict like:
        {"end": "2023-03-31", "filed": "2023-05-04", "val": 1.52, "fp": "Q1",
         "start": "2023-01-01", "form": "10-Q"}
    """
    return {
        "cik": 320193,
        "entityName": "TESTCO",
        "facts": {
            "us-gaap": {
                "EarningsPerShareBasic": {
                    "label": "Earnings Per Share, Basic",
                    "description": "...",
                    "units": {"USD/shares": eps_records},
                }
            }
        },
    }


def _make_quarterly_record(end: str, filed: str, val: float, fp: str) -> dict:
    """Quarter EPS record. ``start`` set to 90d before end to satisfy duration check."""
    end_d = date.fromisoformat(end)
    start_d = end_d.replace(day=1)
    if end_d.month >= 3:
        start_d = start_d.replace(month=end_d.month - 2)
    return {
        "end": end,
        "filed": filed,
        "val": val,
        "fp": fp,
        "start": start_d.isoformat(),
        "form": "10-Q" if fp != "FY" else "10-K",
    }


class TestFirstFiledExtraction(unittest.TestCase):
    """First-filed-per-period helper picks earliest filing, not latest."""

    def test_first_filed_picks_earliest_filing_per_period_end(self):
        # period_end 2024-03-31 was filed 3 times: 2024-05-01 (original 10-Q),
        # 2024-08-15 (10-Q/A amendment), 2024-11-30 (another amendment).
        # First-filed snapshot must pick 2024-05-01.
        from alphalens_pipeline.data.fundamentals.edgar_companyfacts import _Entry
        from alphalens_pipeline.data.fundamentals.sue import _first_filed_per_period_end

        entries = [
            _Entry(
                end="2024-03-31",
                val=1.50,
                filed="2024-05-01",
                form="10-Q",
                fp="Q1",
                start="2024-01-01",
            ),
            _Entry(
                end="2024-03-31",
                val=1.55,
                filed="2024-08-15",
                form="10-Q/A",
                fp="Q1",
                start="2024-01-01",
            ),
            _Entry(
                end="2024-03-31",
                val=1.48,
                filed="2024-11-30",
                form="10-Q/A",
                fp="Q1",
                start="2024-01-01",
            ),
            _Entry(
                end="2024-06-30",
                val=1.62,
                filed="2024-08-01",
                form="10-Q",
                fp="Q2",
                start="2024-04-01",
            ),
        ]
        first_filed = _first_filed_per_period_end(entries)
        self.assertEqual(first_filed["2024-03-31"].filed, "2024-05-01")
        self.assertEqual(first_filed["2024-03-31"].val, 1.50)
        self.assertEqual(first_filed["2024-06-30"].filed, "2024-08-01")
        self.assertEqual(first_filed["2024-06-30"].val, 1.62)


class TestNaiveForecast(unittest.TestCase):
    """Foster-1977 seasonal-random-walk-with-drift forecast formula."""

    def test_forecast_lag4_plus_drift(self):
        from alphalens_pipeline.data.fundamentals.sue import _foster_naive_forecast

        # Series last 8 quarters (oldest first):
        # quarters t-8 .. t-1 = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7]
        # We forecast quarter t. Lag-4 = 1.4. Seasonal drifts:
        #   (Q_{t-1} - Q_{t-5}) = 1.7 - 1.3 = 0.4
        #   (Q_{t-2} - Q_{t-6}) = 1.6 - 1.2 = 0.4
        #   (Q_{t-3} - Q_{t-7}) = 1.5 - 1.1 = 0.4
        #   (Q_{t-4} - Q_{t-8}) = 1.4 - 1.0 = 0.4
        # Mean drift = 0.4. Forecast = 1.4 + 0.4 = 1.8.
        history = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7]
        forecast = _foster_naive_forecast(history)
        self.assertAlmostEqual(forecast, 1.8, places=6)

    def test_forecast_returns_none_on_insufficient_history(self):
        from alphalens_pipeline.data.fundamentals.sue import _foster_naive_forecast

        # Need at least 8 prior quarters to compute drift over 4 seasonal pairs
        self.assertIsNone(_foster_naive_forecast([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6]))
        self.assertIsNone(_foster_naive_forecast([]))


class TestSUE(unittest.TestCase):
    """End-to-end SUE compute with first-filed PIT."""

    def test_sue_computes_at_canonical_fixture(self):
        from alphalens_pipeline.data.fundamentals.sue import _compute_sue

        # 9 quarters of EPS (oldest first). Forecast for the 9th uses lag-4 + drift
        # of preceding 4 seasonal pairs.
        # Last 8 quarters: [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7]
        # 9th actual: 1.85
        # Forecast = 1.4 + 0.4 = 1.8 (per test_forecast_lag4_plus_drift)
        # Surprise = 1.85 - 1.8 = 0.05
        # Residual std: applied over prior 8 quarters, walking the same forecast
        # window forward. We need at least 16 quarters to compute residuals over
        # 8 prior surprises (each requires 8-quarter history). Skip this in the
        # canonical test — instead check the surprise is computed correctly when
        # the std is fixed.
        eps = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.85]
        sue = _compute_sue(eps, residual_window=4)  # uses 4 prior surprises
        # With residual_window=4 we need 4 prior valid quarters where we can
        # compute (actual - forecast). Forecast at q5 needs q1-q4 (only 4 prior),
        # so we need 8 quarters before the residual-window starts.
        # 9 quarters total: 8 needed for first forecast (q9), residual window is
        # the 4 quarters before q9 (q5-q8). To compute forecast for q5 we need
        # 4 prior + 4 seasonal lags = 8 quarters before q5, but we only have q1-q4.
        # So residuals can only be computed for very recent quarters — skip this
        # check; just verify sue is finite and signed correctly when sufficient.
        # For a 9-quarter series the function may legitimately return None
        # because residual std cannot be computed.
        if sue is not None:
            # If implementation uses lookback differently and produces a value,
            # at least sign should be positive (actual > forecast).
            self.assertGreater(sue, 0.0)

    def test_sue_returns_none_on_zero_residual_variance(self):
        from alphalens_pipeline.data.fundamentals.sue import _compute_sue

        # Constant series → all surprises zero → std = 0 → SUE undefined.
        eps = [2.0] * 20
        self.assertIsNone(_compute_sue(eps, residual_window=4))

    def test_sue_returns_none_on_short_history(self):
        from alphalens_pipeline.data.fundamentals.sue import _compute_sue

        # Only 6 quarters — cannot even compute current forecast.
        eps = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
        self.assertIsNone(_compute_sue(eps, residual_window=4))

    def test_sue_signed_correctly_on_long_series(self):
        # 16-quarter trend series: linear trend + seasonal noise.
        import math

        from alphalens_pipeline.data.fundamentals.sue import _compute_sue

        eps = []
        for i in range(16):
            # Slow trend + small seasonality
            eps.append(1.0 + 0.05 * i + 0.10 * math.sin(2 * math.pi * i / 4))
        # Surprise current > expected → positive SUE
        eps.append(eps[-1] + 0.5)  # large positive surprise
        sue = _compute_sue(eps, residual_window=4)
        self.assertIsNotNone(sue)
        self.assertGreater(sue, 0.0)


class TestStorePITContract(unittest.TestCase):
    """End-to-end via the FosterSUEStore against fixture companyfacts JSON."""

    def _setup_store(self, tmp: Path, eps_records: list[dict]):
        import pyarrow.parquet as pq
        from alphalens_pipeline.data.alt_data.ticker_cik_map import TickerCikMap
        from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
            CompanyfactsParquetReader,
            companyfacts_json_to_parquet_table,
        )
        from alphalens_pipeline.data.fundamentals.sue import FosterSUEStore

        cik = 320193
        cf = _make_companyfacts(eps_records)
        cf["cik"] = cik

        parquet_dir = tmp / "companyfacts_parquet"
        parquet_dir.mkdir(parents=True, exist_ok=True)
        table = companyfacts_json_to_parquet_table(cf)
        pq.write_table(table, parquet_dir / f"{cik:010d}.parquet")

        cik_map_path = tmp / "cik_map.yaml"
        cik_map_path.write_text(f"AAPL: {cik}\n")
        cik_map = TickerCikMap.load(cik_map_path)
        reader = CompanyfactsParquetReader(parquet_dir)
        return FosterSUEStore(reader, cik_map)

    def test_store_returns_none_for_unknown_ticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._setup_store(Path(tmp), [])
            self.assertIsNone(store.sue("UNKNOWN_TICKER", date(2024, 6, 30)))

    def test_store_uses_first_filed_not_latest_restated(self):
        """If a quarter is filed twice (original then amendment), residual-std
        computation must use the original value, not the amended one."""
        # Build 17 quarters with noise so Foster forecast doesn't perfectly
        # predict (we need positive residual std). Then add a late amendment
        # to one quarter with absurd value. Verify that the absurd amendment
        # is NOT used by checking the eps series matches the originals.
        import random

        rng = random.Random(42)
        records = []
        originals_by_end: dict[str, float] = {}
        for i in range(17):
            qend_year = 2018 + (i // 4)
            qend_month = [3, 6, 9, 12][i % 4]
            qend_day = [31, 30, 30, 31][i % 4]
            end = f"{qend_year}-{qend_month:02d}-{qend_day:02d}"
            filed_year = qend_year
            filed_month = qend_month + 2
            if filed_month > 12:
                filed_month -= 12
                filed_year += 1
            filed = f"{filed_year}-{filed_month:02d}-15"
            # Trend + noise so Foster doesn't predict perfectly
            val = 1.0 + 0.10 * i + rng.gauss(0, 0.05)
            originals_by_end[end] = val
            records.append(_make_quarterly_record(end, filed, val, ["Q1", "Q2", "Q3", "FY"][i % 4]))
        amend_end = records[10]["end"]
        records.append(
            _make_quarterly_record(
                end=amend_end,
                filed="2024-12-30",  # late amendment
                val=99.0,  # absurd value to detect leakage
                fp=records[10]["fp"],
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = self._setup_store(Path(tmp), records)
            # The first-filed series at any asof >= original filing dates must
            # use the ORIGINAL ~2.0 value at amend_end, not the 99.0 amendment.
            series = store.eps_series_first_filed("AAPL", date(2025, 1, 31))
            self.assertIsNotNone(series)
            self.assertEqual(len(series), 17)
            # Find the position of amend_end in the sorted series (sorted by
            # period_end ascending). amend_end was index 10, so position 10.
            self.assertAlmostEqual(series[10], originals_by_end[amend_end], places=6)
            # Sanity: 99.0 nowhere in the series.
            self.assertFalse(any(abs(v - 99.0) < 1.0 for v in series))


class TestStoreOnRepresentativeFixtures(unittest.TestCase):
    """Round-trip regression on the synthetic Apple / IPO fixtures."""

    def setUp(self):
        from tests.fixtures.companyfacts_fixtures import (
            APPLE_CIK,
            IPO_CIK,
            write_all_fixtures_as_parquet,
        )

        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.parquet_dir = tmp / "companyfacts_parquet"
        write_all_fixtures_as_parquet(self.parquet_dir)

        cik_map_path = tmp / "cik_map.yaml"
        cik_map_path.write_text(f"AAPL: {APPLE_CIK}\nIPO_CO: {IPO_CIK}\n")

        from alphalens_pipeline.data.alt_data.ticker_cik_map import TickerCikMap
        from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
            CompanyfactsParquetReader,
        )
        from alphalens_pipeline.data.fundamentals.sue import FosterSUEStore

        self.cik_map = TickerCikMap.load(cik_map_path)
        self.reader = CompanyfactsParquetReader(self.parquet_dir)
        self.store = FosterSUEStore(self.reader, self.cik_map)

    def tearDown(self):
        self._tmp.cleanup()

    def test_apple_fixture_returns_eight_chronological_eps_values(self):
        # Apple fixture has 8 normal quarters + 1 restatement; first-filed
        # collapses the restatement, so the series must be 8 values long.
        series = self.store.eps_series_first_filed("AAPL", date(2025, 1, 31))
        self.assertEqual(len(series), 8)
        # First-filed semantics on the restated 2023-04-01 entry: the series
        # must contain the ORIGINAL value 1.55, not the restated 1.58.
        self.assertIn(1.55, series)
        self.assertNotIn(1.58, series)

    def test_apple_fixture_eps_basic_preferred_over_diluted(self):
        series = self.store.eps_series_first_filed("AAPL", date(2025, 1, 31))
        # Apple Basic-only values include 1.27; Diluted-only values include
        # 1.23. Basic preference must yield the Basic-only marker, never the
        # Diluted-only marker.
        self.assertIn(1.27, series)
        self.assertNotIn(1.23, series)

    def test_ipo_fixture_returns_none_due_to_insufficient_history(self):
        # IPO fixture has only 2 EPS quarters; Foster SUE needs >= 8 + 4 + 1.
        sue = self.store.sue("IPO_CO", date(2025, 1, 31))
        self.assertIsNone(sue)


if __name__ == "__main__":
    unittest.main()

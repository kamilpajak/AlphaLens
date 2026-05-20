"""Integration test for EdgarFundamentalsStore.ev_fcff_features_as_of.

Builds a tiny synthetic companyfacts parquet on disk + drives the store
through its public API to confirm:

1. The 16-field dict shape is preserved (no field accidentally dropped
   or renamed when we wired in the new fcf_margin_rolling_median).
2. fcf_margin_5y_median is non-None when the issuer has >= 8 aligned
   quarters of OCF / CapEx / Revenue (it used to be hardcoded None).
3. The store does not raise on a no-data CIK.

The synthetic parquet matches the canonical schema emitted by
``companyfacts_json_to_parquet_table`` so the store's reader doesn't
need any test-specific shimming.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq


def _row(**kw):
    """Build a parquet-schema-compatible row dict."""
    return {
        "taxonomy": kw.get("taxonomy", "us-gaap"),
        "concept": kw["concept"],
        "unit": kw.get("unit", "USD"),
        "period_start": date.fromisoformat(kw["period_start"]) if kw.get("period_start") else None,
        "period_end": date.fromisoformat(kw["period_end"]),
        "val": float(kw["val"]),
        "accn": kw.get("accn", "x"),
        "fy": kw.get("fy", 2024),
        "fp": kw.get("fp", "Q1"),
        "form": kw.get("form", "10-Q"),
        "filed_date": date.fromisoformat(kw["filed_date"]),
        "frame": kw.get("frame"),
    }


def _write_synthetic_parquet(path: Path, n_quarters: int = 12) -> None:
    """12 quarters of OCF + CapEx + Revenue + InterestExpense for one CIK.

    Each standalone-Q row spans ~90 days. Values are identical across
    quarters so the median is the per-quarter value:
    (120 - 20 - 10*0.79) / 500 ≈ 0.1842.
    """
    rows = []
    for i in range(n_quarters):
        yr = 2020 + i // 4
        q = i % 4
        q_start_month = q * 3 + 1
        q_end_month = q * 3 + 3
        end_day = {3: 31, 6: 30, 9: 30, 12: 31}[q_end_month]
        start = f"{yr}-{q_start_month:02d}-01"
        end = f"{yr}-{q_end_month:02d}-{end_day:02d}"
        filed = f"{yr}-{q_end_month + 1:02d}-15" if q_end_month < 12 else f"{yr + 1}-02-15"
        fp = "FY" if q == 3 else f"Q{q + 1}"
        form = "10-K" if q == 3 else "10-Q"
        for concept, val in (
            ("NetCashProvidedByUsedInOperatingActivities", 120.0),
            ("PaymentsToAcquirePropertyPlantAndEquipment", 20.0),
            ("Revenues", 500.0),
            ("InterestExpense", 10.0),
        ):
            rows.append(
                _row(
                    concept=concept,
                    period_start=start,
                    period_end=end,
                    val=val,
                    filed_date=filed,
                    fp=fp,
                    form=form,
                )
            )
        # One shares-outstanding row so the dict's shares_outstanding field
        # is populated (otherwise the store would still work, but the test
        # is more realistic this way).
        rows.append(
            _row(
                taxonomy="dei",
                concept="EntityCommonStockSharesOutstanding",
                unit="shares",
                period_start=None,
                period_end=end,
                val=1_000_000.0,
                filed_date=filed,
                fp=fp,
                form=form,
            )
        )

    table = pa.table(
        {
            "taxonomy": pa.array([r["taxonomy"] for r in rows], type=pa.string()),
            "concept": pa.array([r["concept"] for r in rows], type=pa.string()),
            "unit": pa.array([r["unit"] for r in rows], type=pa.string()),
            "period_start": pa.array([r["period_start"] for r in rows], type=pa.date32()),
            "period_end": pa.array([r["period_end"] for r in rows], type=pa.date32()),
            "val": pa.array([r["val"] for r in rows], type=pa.float64()),
            "accn": pa.array([r["accn"] for r in rows], type=pa.string()),
            "fy": pa.array([r["fy"] for r in rows], type=pa.int32()),
            "fp": pa.array([r["fp"] for r in rows], type=pa.string()),
            "form": pa.array([r["form"] for r in rows], type=pa.string()),
            "filed_date": pa.array([r["filed_date"] for r in rows], type=pa.date32()),
            "frame": pa.array([r["frame"] for r in rows], type=pa.string()),
        }
    )
    pq.write_table(table, path)


_EXPECTED_FIELDS = {
    "ocf_ttm",
    "capex_ttm",
    "interest_expense_ttm",
    "tax_rate",
    "revenue_ttm",
    "fcf_margin_5y_median",
    "price",
    "shares_outstanding",
    "long_term_debt",
    "short_term_debt",
    "cash_and_equivalents",
    "net_income_ttm",
    "publish_date_str",
    "operating_income_ttm",
    "total_equity",
    "da_ttm",
}


class TestEdgarFundamentalsStoreIntegration(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._cache = Path(self._tmpdir.name)
        # Synthetic CIK 1234567890 (10-digit zero-padded matches store
        # convention). Map it to a fake ticker via the store's CIK loader stub.
        _write_synthetic_parquet(self._cache / "1234567890.parquet")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _build_store(self):
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        sec_client = MagicMock()
        store = EdgarFundamentalsStore(
            cache_dir=self._cache,
            with_prices=False,
            sec_client=sec_client,
        )
        # Bypass the ticker→CIK lookup with a direct mapping for the test
        # CIK. Store uses self._cik_for(ticker); patch via attribute that
        # mirrors the production CIK-loader contract.
        store._cik_for = lambda ticker: "1234567890" if ticker == "TEST" else None
        return store

    def test_dict_has_all_16_canonical_fields(self):
        store = self._build_store()
        features = store.ev_fcff_features_as_of("TEST", date(2030, 1, 1))
        self.assertIsNotNone(features)
        self.assertEqual(set(features), _EXPECTED_FIELDS)

    def test_fcf_margin_populates_with_enough_quarters(self):
        store = self._build_store()
        features = store.ev_fcff_features_as_of("TEST", date(2030, 1, 1))
        self.assertIsNotNone(features["fcf_margin_5y_median"])
        # Per-quarter margin with default tax_rate=0.21:
        # (120 - 20 - 10 * 0.79) / 500 = 0.1842
        self.assertAlmostEqual(features["fcf_margin_5y_median"], 0.1842, places=3)

    def test_unknown_ticker_returns_none(self):
        store = self._build_store()
        self.assertIsNone(store.ev_fcff_features_as_of("UNKNOWN", date(2030, 1, 1)))


if __name__ == "__main__":
    unittest.main()

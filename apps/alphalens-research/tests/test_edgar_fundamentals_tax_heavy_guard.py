"""Tax-heavy sector guard on the ``RevenueFromContractWithCustomerIncluding-
AssessedTax`` fallback (issue #924).

Gross-vs-net gap between the Excluding and Including XBRL revenue tags is
negligible in tech/manufacturing but 10-20% of revenue in tax-heavy sectors
(fuel, tobacco, alcohol, retail, telecom, utilities). For those sectors the
store must serve net revenue when available and ``None`` when only the
Including tag exists — never gross revenue, which would understate
PS / EV-REV and break cross-sector comparability.

Modeled on ``test_edgar_fundamentals_shares_chain.py``'s temp-dir parquet +
stubbed ``SecEdgarClient.fetch_company_tickers`` pattern, plus
``test_sic_index.py``'s synthetic-parquet-and-cache-clear pattern for
pointing ``sic_index`` at a fixture.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq


def _row(**kw):
    return {
        "taxonomy": kw.get("taxonomy", "us-gaap"),
        "concept": kw["concept"],
        "unit": kw.get("unit", "USD"),
        "period_start": date.fromisoformat(kw["period_start"]) if kw.get("period_start") else None,
        "period_end": date.fromisoformat(kw["period_end"]),
        "val": float(kw["val"]),
        "accn": kw.get("accn", "x"),
        "fy": kw.get("fy", 2024),
        "fp": kw.get("fp", "FY"),
        "form": kw.get("form", "10-K"),
        "filed_date": date.fromisoformat(kw["filed_date"]),
        "frame": kw.get("frame"),
    }


def _write_companyfacts_parquet(path: Path, rows: list[dict]) -> None:
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


def _stub_sec_client(ticker_to_cik: dict[str, str]) -> MagicMock:
    c = MagicMock()
    c.fetch_company_tickers.return_value = {
        i: {"ticker": t, "cik_str": int(cik)} for i, (t, cik) in enumerate(ticker_to_cik.items())
    }
    return c


def _four_contiguous_quarters(concept: str) -> list[dict]:
    """4 contiguous standalone-quarter rows -> the 4Q-sum TTM path (460.0)."""
    return [
        _row(
            concept=concept,
            period_start="2025-04-01",
            period_end="2025-06-30",
            val=100.0,
            filed_date="2025-08-01",
            form="10-Q",
            fp="Q2",
        ),
        _row(
            concept=concept,
            period_start="2025-07-01",
            period_end="2025-09-30",
            val=110.0,
            filed_date="2025-11-01",
            form="10-Q",
            fp="Q3",
        ),
        _row(
            concept=concept,
            period_start="2025-10-01",
            period_end="2025-12-31",
            val=120.0,
            filed_date="2026-02-15",
            form="10-K",
            fp="FY",
        ),
        _row(
            concept=concept,
            period_start="2026-01-01",
            period_end="2026-03-31",
            val=130.0,
            filed_date="2026-05-01",
            form="10-Q",
            fp="Q1",
        ),
    ]


class _TaxHeavyGuardTestCase(unittest.TestCase):
    """Points ``sic_index`` at a synthetic parquet and clears every
    process-global lru_cache the guard touches (sic_index's three caches
    plus the store's own ``_is_tax_heavy`` memoisation) so fixtures from
    one test never leak into the next.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _point_sic_index_at(self, rows: list[dict]) -> None:
        from alphalens_pipeline.data.fundamentals import sic_index
        from alphalens_pipeline.data.store import edgar_fundamentals as ef

        sic_path = Path(self._tmp.name) / "sic_index.parquet"
        table = pa.Table.from_pylist(
            rows,
            schema=pa.schema(
                [
                    ("ticker", pa.string()),
                    ("cik", pa.string()),
                    ("sic", pa.int32()),
                    ("sic_description", pa.string()),
                ]
            ),
        )
        pq.write_table(table, sic_path)
        patcher = patch.object(sic_index, "_SIC_INDEX_PATH", sic_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        for cache in (
            sic_index._load_index,
            sic_index._load_lookup_dicts,
            sic_index._load_sic3_peers,
            ef._is_tax_heavy,
        ):
            cache.cache_clear()
            self.addCleanup(cache.cache_clear)


class TestTaxHeavyGuard(_TaxHeavyGuardTestCase):
    def test_tax_heavy_sic_with_including_only_data_serves_none(self):
        """Tax-heavy SIC 2111 (cigarettes, within the 2100-2199 tobacco
        excise range) + Including-tag-only companyfacts -> None rather
        than gross revenue.
        """
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

        self._point_sic_index_at(
            [{"ticker": "TAXH", "cik": "1", "sic": 2111, "sic_description": "Cigarettes"}]
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0000000101"
            _write_companyfacts_parquet(
                tdp / f"{cik}.parquet",
                _four_contiguous_quarters("RevenueFromContractWithCustomerIncludingAssessedTax"),
            )
            store = EdgarFundamentalsStore(
                cache_dir=tdp, sec_client=_stub_sec_client({"TAXH": cik})
            )
            features = store.ev_fcff_features_as_of("TAXH", date(2026, 5, 19))
            self.assertIsNone(features["revenue_ttm"])

    def test_non_tax_heavy_sic_with_including_only_data_is_served(self):
        """Non-tax-heavy SIC 7371 (computer services) + Including-tag-only
        companyfacts -> revenue_ttm resolves via the full REVENUE chain.
        """
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

        self._point_sic_index_at(
            [{"ticker": "TECH", "cik": "2", "sic": 7371, "sic_description": "Computer Services"}]
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0000000102"
            _write_companyfacts_parquet(
                tdp / f"{cik}.parquet",
                _four_contiguous_quarters("RevenueFromContractWithCustomerIncludingAssessedTax"),
            )
            store = EdgarFundamentalsStore(
                cache_dir=tdp, sec_client=_stub_sec_client({"TECH": cik})
            )
            features = store.ev_fcff_features_as_of("TECH", date(2026, 5, 19))
            self.assertAlmostEqual(features["revenue_ttm"], 460.0, places=2)

    def test_tax_heavy_sic_with_excluding_data_is_served_net(self):
        """Tax-heavy SIC + FRESH Excluding-tag data -> served as net
        revenue (the guard only blocks the gross Including fallback, it
        does not null out the whole revenue line for the sector).
        """
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

        self._point_sic_index_at(
            [{"ticker": "TAXN", "cik": "3", "sic": 2111, "sic_description": "Cigarettes"}]
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0000000103"
            _write_companyfacts_parquet(
                tdp / f"{cik}.parquet",
                _four_contiguous_quarters("RevenueFromContractWithCustomerExcludingAssessedTax"),
            )
            store = EdgarFundamentalsStore(
                cache_dir=tdp, sec_client=_stub_sec_client({"TAXN": cik})
            )
            features = store.ev_fcff_features_as_of("TAXN", date(2026, 5, 19))
            self.assertAlmostEqual(features["revenue_ttm"], 460.0, places=2)

    def test_returned_dict_keys_unchanged_by_the_guard(self):
        """Parity contract: the guard must not add/remove any of the 16
        fields the downstream scorers depend on.
        """
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

        self._point_sic_index_at(
            [{"ticker": "TAXH", "cik": "1", "sic": 2111, "sic_description": "Cigarettes"}]
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0000000104"
            _write_companyfacts_parquet(
                tdp / f"{cik}.parquet",
                _four_contiguous_quarters("RevenueFromContractWithCustomerIncludingAssessedTax"),
            )
            store = EdgarFundamentalsStore(
                cache_dir=tdp, sec_client=_stub_sec_client({"TAXH": cik})
            )
            features = store.ev_fcff_features_as_of("TAXH", date(2026, 5, 19))
            self.assertEqual(
                set(features.keys()),
                {
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
                },
            )


class TestTaxHeavySicRangesWellFormed(unittest.TestCase):
    """Clone of ``test_sector_etf.py::TestSicRangesWellFormed`` for the new
    ``_TAX_HEAVY_SIC_RANGES`` table.
    """

    def test_ranges_are_ascending_and_non_overlapping(self):
        from alphalens_pipeline.data.store.edgar_fundamentals import _TAX_HEAVY_SIC_RANGES

        prev_hi = -1
        for lo, hi, _label in _TAX_HEAVY_SIC_RANGES:
            self.assertLessEqual(lo, hi, f"range {lo}-{hi} inverted")
            self.assertGreater(lo, prev_hi, f"range starting {lo} overlaps prior {prev_hi}")
            prev_hi = hi


class TestIsTaxHeavy(unittest.TestCase):
    """Direct unit coverage of ``_is_tax_heavy``'s fail-open contract."""

    def test_unmapped_ticker_fails_open_to_false(self):
        """No SIC (e.g. missing index / unresolved ticker) -> False. The
        guard is forward-insurance, not a whitelist -- an unknown sector
        must not lose revenue coverage it has today.
        """
        from alphalens_pipeline.data.fundamentals import sic_index
        from alphalens_pipeline.data.store import edgar_fundamentals as ef

        nonexistent = Path("/tmp/__alphalens_nonexistent_sic_index__/sic_index.parquet")
        with patch.object(sic_index, "_SIC_INDEX_PATH", nonexistent):
            sic_index._load_index.cache_clear()
            sic_index._load_lookup_dicts.cache_clear()
            ef._is_tax_heavy.cache_clear()
            self.addCleanup(sic_index._load_index.cache_clear)
            self.addCleanup(sic_index._load_lookup_dicts.cache_clear)
            self.addCleanup(ef._is_tax_heavy.cache_clear)
            self.assertFalse(ef._is_tax_heavy("NOPE"))


if __name__ == "__main__":
    unittest.main()

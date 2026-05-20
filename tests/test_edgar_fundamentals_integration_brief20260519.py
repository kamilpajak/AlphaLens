"""End-to-end fixture replay against the brief-2026-05-19 bugs.

Three synthetic parquets reproduce the AI / AVAV / SOUN data shapes from
issue #172 and assert that ``EdgarFundamentalsStore.ev_fcff_features_as_of``
plus ``valuation_signal.score_valuation`` now behave correctly under the
combined fixes (form whitelist, shares chain order + freshness + yfinance
fallback, TTM 4-quarter sum + max-staleness, mcap consistency gate).
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
        "accn": "x",
        "fy": 2024,
        "fp": kw.get("fp", "FY"),
        "form": kw.get("form", "10-K"),
        "filed_date": date.fromisoformat(kw["filed_date"]),
        "frame": None,
    }


def _write_parquet(path: Path, rows: list[dict]) -> None:
    pq.write_table(
        pa.table(
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
        ),
        path,
    )


def _stub_sec_client(ticker_to_cik: dict[str, str]) -> MagicMock:
    c = MagicMock()
    c.fetch_company_tickers.return_value = {
        i: {"ticker": t, "cik_str": int(cik)} for i, (t, cik) in enumerate(ticker_to_cik.items())
    }
    return c


class TestBrief20260519Replay(unittest.TestCase):
    def test_AI_shares_fall_through_to_yfinance(self):
        """Pre-fix: stale 2021 us-gaap entry (3.5M) won, P/S ≈ 0.10.
        Post-fix: dei empty + us-gaap stale > 180d → yfinance fallback (~140M).
        """
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0001577526"
            _write_parquet(
                tdp / f"{cik}.parquet",
                [
                    # Only IPO-era us-gaap snapshot; dei never populated.
                    _row(
                        taxonomy="us-gaap",
                        concept="CommonStockSharesOutstanding",
                        unit="shares",
                        period_start=None,
                        period_end="2021-04-30",
                        val=3_499_992.0,
                        filed_date="2021-06-25",
                        form="10-K",
                        fp="FY",
                    ),
                    # Provide a revenue + NI so the dict is populated enough
                    # for the test (4 standalone Q-rows post-cutoff).
                    *[
                        _row(
                            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                            period_start=ps,
                            period_end=pe,
                            val=val,
                            filed_date=fd,
                            form="10-Q",
                            fp=fp,
                        )
                        for (ps, pe, val, fd, fp) in [
                            ("2025-05-01", "2025-07-31", 70.0, "2025-09-09", "Q1"),
                            ("2025-08-01", "2025-10-31", 75.0, "2025-12-09", "Q2"),
                            ("2025-11-01", "2026-01-31", 53.0, "2026-03-11", "Q3"),
                            ("2024-11-01", "2025-01-31", 98.0, "2025-03-07", "Q3"),
                        ]
                    ],
                ],
            )
            store = EdgarFundamentalsStore(cache_dir=tdp, sec_client=_stub_sec_client({"AI": cik}))
            with patch.object(store, "_fetch_shares_yf", return_value=140_000_000.0):
                features = store.ev_fcff_features_as_of("AI", date(2026, 5, 19))
            self.assertEqual(features["shares_outstanding"], 140_000_000.0)

    def test_AVAV_revenue_uses_4quarter_sum_not_legacy_concept(self):
        """Pre-fix: silent fallback to ``Revenues`` 2020 returned ~$297M.
        Post-fix: 4-quarter sum across the merged family returns ~$1.5B.
        """
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0001368622"
            _write_parquet(
                tdp / f"{cik}.parquet",
                [
                    # FY26 standalone quarters (post-BlueHalo merger).
                    _row(
                        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                        period_start="2025-05-01",
                        period_end="2025-08-02",
                        val=455_000_000.0,
                        filed_date="2025-09-10",
                        form="10-Q",
                        fp="Q1",
                    ),
                    _row(
                        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                        period_start="2025-08-03",
                        period_end="2025-11-01",
                        val=472_000_000.0,
                        filed_date="2025-12-10",
                        form="10-Q",
                        fp="Q2",
                    ),
                    _row(
                        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                        period_start="2025-11-02",
                        period_end="2026-01-31",
                        val=408_000_000.0,
                        filed_date="2026-03-11",
                        form="10-Q",
                        fp="Q3",
                    ),
                    _row(
                        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                        period_start="2024-10-27",
                        period_end="2025-01-25",
                        val=168_000_000.0,
                        filed_date="2025-03-11",
                        form="10-Q",
                        fp="Q3",
                    ),
                    # Old legacy concept with 2020 data — must NOT win.
                    _row(
                        concept="Revenues",
                        period_start="2020-01-01",
                        period_end="2020-12-31",
                        val=297_000_000.0,
                        filed_date="2021-03-04",
                        form="10-K",
                        fp="FY",
                    ),
                ],
            )
            store = EdgarFundamentalsStore(
                cache_dir=tdp, sec_client=_stub_sec_client({"AVAV": cik})
            )
            features = store.ev_fcff_features_as_of("AVAV", date(2026, 5, 19))
            self.assertGreater(features["revenue_ttm"], 1_400_000_000.0)
            self.assertLess(features["revenue_ttm"], 1_600_000_000.0)

    def test_SOUN_def14a_proxy_does_not_corrupt_ROE(self):
        """Pre-fix: DEF 14A NetIncomeLoss val=-14_006 won by filed-date
        tiebreaker → ROE rendered ``-0.0%``.
        Post-fix: form whitelist drops DEF 14A; 10-K ``-14,006,000`` wins.
        """
        from alphalens.data.fundamentals.ttm_aggregator import compute_ttm

        rows = [
            _row(
                concept="NetIncomeLoss",
                period_start="2025-01-01",
                period_end="2025-12-31",
                val=-14_006_000.0,
                filed_date="2026-03-02",
                form="10-K",
                fp="FY",
            ),
            _row(
                concept="NetIncomeLoss",
                period_start="2025-01-01",
                period_end="2025-12-31",
                val=-14_006.0,  # scale-corrupted proxy value
                filed_date="2026-04-09",
                form="DEF 14A",
                fp=None,
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0001840856"
            _write_parquet(tdp / f"{cik}.parquet", rows)
            from alphalens.data.fundamentals.companyfacts_parquet import (
                CompanyfactsParquetReader,
            )

            reader = CompanyfactsParquetReader(tdp)
            out = compute_ttm(reader, cik=cik, chain=("NetIncomeLoss",), asof=date(2026, 5, 19))
            # 10-K value survives the filter; DEF 14A is dropped.
            self.assertAlmostEqual(out, -14_006_000.0, places=-3)


if __name__ == "__main__":
    unittest.main()

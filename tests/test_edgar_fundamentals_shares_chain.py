"""Shares-outstanding 3-tier chain in ``EdgarFundamentalsStore``.

Issue #172 Bug 1: the previous shares-resolution order was us-gaap →
dei, which contradicted the docstring in ``concept_chains.py:121-122``
("DEI is the modern primary; us-gaap is the legacy fallback"). For
C3.ai both us-gaap was stale and dei was empty, so the brief read 37×
under the true share count.

Post-fix order:

1. dei:EntityCommonStockSharesOutstanding (cover-page, modern, fresh)
2. us-gaap:CommonStockSharesOutstanding (legacy, with 180-day staleness gate)
3. yfinance fallback (``Ticker.get_shares_full`` + ``fast_info.shares``)

Both XBRL chains pass through :func:`latest_instant` with
``max_age_days=180`` so stale entries do not satisfy the chain.
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


def _write_parquet(path: Path, rows: list[dict]) -> None:
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
    """Returns the CIK map that EdgarFundamentalsStore expects from
    ``SecEdgarClient.fetch_company_tickers``.
    """
    c = MagicMock()
    c.fetch_company_tickers.return_value = {
        i: {"ticker": t, "cik_str": int(cik)} for i, (t, cik) in enumerate(ticker_to_cik.items())
    }
    return c


class TestSharesChainOrder(unittest.TestCase):
    def test_dei_wins_over_us_gaap_when_both_fresh(self):
        """Modern primary: dei:EntityCommonStockSharesOutstanding."""
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0000000001"
            _write_parquet(
                tdp / f"{cik}.parquet",
                [
                    _row(
                        taxonomy="us-gaap",
                        concept="CommonStockSharesOutstanding",
                        unit="shares",
                        period_end="2026-01-31",
                        val=100_000_000.0,
                        filed_date="2026-03-11",
                        form="10-Q",
                        fp="Q3",
                    ),
                    _row(
                        taxonomy="dei",
                        concept="EntityCommonStockSharesOutstanding",
                        unit="shares",
                        period_end="2026-01-31",
                        val=140_000_000.0,
                        filed_date="2026-03-11",
                        form="10-Q",
                        fp="Q3",
                    ),
                ],
            )
            store = EdgarFundamentalsStore(cache_dir=tdp, sec_client=_stub_sec_client({"AI": cik}))
            features = store.ev_fcff_features_as_of("AI", date(2026, 5, 19))
            self.assertEqual(features["shares_outstanding"], 140_000_000.0)

    def test_us_gaap_fallback_when_dei_absent(self):
        """No dei row → us-gaap kicks in (still gated on 180-day age)."""
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0000000002"
            _write_parquet(
                tdp / f"{cik}.parquet",
                [
                    _row(
                        taxonomy="us-gaap",
                        concept="CommonStockSharesOutstanding",
                        unit="shares",
                        period_end="2026-01-31",
                        val=100_000_000.0,
                        filed_date="2026-03-11",
                        form="10-Q",
                        fp="Q3",
                    ),
                ],
            )
            store = EdgarFundamentalsStore(cache_dir=tdp, sec_client=_stub_sec_client({"FOO": cik}))
            features = store.ev_fcff_features_as_of("FOO", date(2026, 5, 19))
            self.assertEqual(features["shares_outstanding"], 100_000_000.0)

    def test_stale_us_gaap_rejected_by_180d_gate(self):
        """C3.ai-shaped: only stale us-gaap → shares is None (no yfinance mocked)."""
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0001577526"
            _write_parquet(
                tdp / f"{cik}.parquet",
                [
                    _row(
                        taxonomy="us-gaap",
                        concept="CommonStockSharesOutstanding",
                        unit="shares",
                        period_end="2021-04-30",
                        val=3_499_992.0,
                        filed_date="2021-06-25",
                        form="10-K",
                        fp="FY",
                    ),
                ],
            )
            store = EdgarFundamentalsStore(cache_dir=tdp, sec_client=_stub_sec_client({"AI": cik}))
            # Disable yfinance fallback for this test to isolate the gate.
            with patch.object(store, "_fetch_shares_yf", return_value=None):
                features = store.ev_fcff_features_as_of("AI", date(2026, 5, 19))
            self.assertIsNone(features["shares_outstanding"])

    def test_yfinance_fallback_invoked_when_both_xbrl_chains_dead(self):
        """C3.ai-shaped with yfinance live → 3rd tier returns ~130M."""
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0001577526"
            _write_parquet(
                tdp / f"{cik}.parquet",
                [
                    _row(
                        taxonomy="us-gaap",
                        concept="CommonStockSharesOutstanding",
                        unit="shares",
                        period_end="2021-04-30",
                        val=3_499_992.0,
                        filed_date="2021-06-25",
                        form="10-K",
                        fp="FY",
                    ),
                ],
            )
            store = EdgarFundamentalsStore(cache_dir=tdp, sec_client=_stub_sec_client({"AI": cik}))
            with patch.object(store, "_fetch_shares_yf", return_value=130_000_000.0) as mock_yf:
                features = store.ev_fcff_features_as_of("AI", date(2026, 5, 19))
            self.assertEqual(features["shares_outstanding"], 130_000_000.0)
            mock_yf.assert_called_once_with("AI", date(2026, 5, 19))

    def test_yfinance_transient_exception_not_cached(self):
        """Zen finding #3 (PR #174): a yfinance rate-limit / network blip
        must not permanently disable the fallback for the lifetime of the
        store. The second call must re-attempt and pick up the value
        (None is cached only for definitive "no data" results).
        """
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            store = EdgarFundamentalsStore(cache_dir=tdp, sec_client=_stub_sec_client({}))

            # Mock yfinance.Ticker so the first invocation raises and the
            # second returns a real fast_info.shares value.
            ticker_mock_attempt = [0]

            class _FakeFastInfo:
                shares = 140_000_000.0

            class _FakeTicker:
                def __init__(self, t):
                    ticker_mock_attempt[0] += 1
                    if ticker_mock_attempt[0] == 1:
                        raise RuntimeError("network blip")
                    self.fast_info = _FakeFastInfo()

                def get_shares_full(self, start, end):
                    return None

            fake_yf = MagicMock()
            fake_yf.Ticker.side_effect = _FakeTicker

            with patch.dict("sys.modules", {"yfinance": fake_yf}):
                first = store._fetch_shares_yf("AI", date(2026, 5, 19))
                self.assertIsNone(first)  # transient failure
                self.assertNotIn("AI", store._shares_cache)  # not cached
                second = store._fetch_shares_yf("AI", date(2026, 5, 19))
            self.assertEqual(second, 140_000_000.0)
            self.assertEqual(store._shares_cache["AI"], 140_000_000.0)

    def test_yfinance_fallback_skipped_when_xbrl_succeeds(self):
        """Don't waste yfinance roundtrips when EDGAR is fresh."""
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cik = "0000000003"
            _write_parquet(
                tdp / f"{cik}.parquet",
                [
                    _row(
                        taxonomy="dei",
                        concept="EntityCommonStockSharesOutstanding",
                        unit="shares",
                        period_end="2026-01-31",
                        val=200_000_000.0,
                        filed_date="2026-03-11",
                        form="10-Q",
                        fp="Q3",
                    ),
                ],
            )
            store = EdgarFundamentalsStore(cache_dir=tdp, sec_client=_stub_sec_client({"BAR": cik}))
            with patch.object(store, "_fetch_shares_yf") as mock_yf:
                features = store.ev_fcff_features_as_of("BAR", date(2026, 5, 19))
            self.assertEqual(features["shares_outstanding"], 200_000_000.0)
            mock_yf.assert_not_called()


if __name__ == "__main__":
    unittest.main()

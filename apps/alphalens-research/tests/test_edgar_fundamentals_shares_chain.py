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
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

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
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

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
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

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
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

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

    def test_yfinance_none_not_cached_value_is(self):
        """Zen finding #3 (PR #174): a ``None`` from the canonical client (an
        exhausted-transient rate-limit / network blip OR a permanent miss) must
        NOT be cached, so a later candidate in the same batch re-attempts; only
        a definitive value is memoised for the store lifetime. The retry itself
        now lives inside :class:`YFinanceClient`; the store keeps only the
        don't-cache-None invariant and the PIT ``asof`` passthrough.
        """
        from alphalens_pipeline.data.store import edgar_fundamentals as ef

        with tempfile.TemporaryDirectory() as td:
            store = ef.EdgarFundamentalsStore(cache_dir=Path(td), sec_client=_stub_sec_client({}))
            fake_client = MagicMock()
            fake_client.shares.side_effect = [None, 140_000_000.0]
            with patch.object(ef, "get_default_yfinance_client", return_value=fake_client):
                first = store._fetch_shares_yf("AI", date(2026, 5, 19))
                self.assertIsNone(first)  # client returned None
                self.assertNotIn("AI", store._shares_cache)  # not cached
                second = store._fetch_shares_yf("AI", date(2026, 5, 19))
            self.assertEqual(second, 140_000_000.0)
            self.assertEqual(store._shares_cache["AI"], 140_000_000.0)
            fake_client.shares.assert_called_with("AI", asof=date(2026, 5, 19))

    def test_yfinance_fallback_skipped_when_xbrl_succeeds(self):
        """Don't waste yfinance roundtrips when EDGAR is fresh."""
        from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

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


class TestPriceFetchDelegation(unittest.TestCase):
    """Price prefetch + per-ticker fallback delegate to the canonical
    :class:`YFinanceClient` (its shared throttle + bounded retry replace the
    store's old raw ``yfinance`` calls — the fragility behind a single name
    losing every price-dependent multiple after a Yahoo 429 burst)."""

    def test_batch_fetch_prices_populates_from_client(self):
        from alphalens_pipeline.data.store import edgar_fundamentals as ef

        with tempfile.TemporaryDirectory() as td:
            store = ef.EdgarFundamentalsStore(cache_dir=Path(td), sec_client=_stub_sec_client({}))
            fake = MagicMock()
            fake.batch_last_close.return_value = {"AAA": 10.0, "BBB": 20.0}
            with patch.object(ef, "get_default_yfinance_client", return_value=fake):
                store._batch_fetch_prices(["aaa", "bbb"])
            self.assertEqual(store._prices, {"AAA": 10.0, "BBB": 20.0})
            fake.batch_last_close.assert_called_once_with(["aaa", "bbb"])

    def test_fetch_price_delegates_then_serves_from_cache(self):
        from alphalens_pipeline.data.store import edgar_fundamentals as ef

        with tempfile.TemporaryDirectory() as td:
            store = ef.EdgarFundamentalsStore(cache_dir=Path(td), sec_client=_stub_sec_client({}))
            fake = MagicMock()
            fake.last_price.return_value = 241.16
            with patch.object(ef, "get_default_yfinance_client", return_value=fake):
                first = store._fetch_price("FDS", date(2026, 6, 12))
                second = store._fetch_price("FDS", date(2026, 6, 12))
            self.assertEqual(first, 241.16)
            self.assertEqual(second, 241.16)
            fake.last_price.assert_called_once_with("FDS")  # 2nd served from cache

    def test_fetch_price_none_not_cached(self):
        from alphalens_pipeline.data.store import edgar_fundamentals as ef

        with tempfile.TemporaryDirectory() as td:
            store = ef.EdgarFundamentalsStore(cache_dir=Path(td), sec_client=_stub_sec_client({}))
            fake = MagicMock()
            fake.last_price.return_value = None
            with patch.object(ef, "get_default_yfinance_client", return_value=fake):
                self.assertIsNone(store._fetch_price("X", date(2026, 6, 12)))
                self.assertNotIn("X", store._prices)

    def test_fetch_price_prefers_prefetched_batch_value(self):
        """A value already in the batch cache short-circuits the per-ticker
        client call entirely."""
        from alphalens_pipeline.data.store import edgar_fundamentals as ef

        with tempfile.TemporaryDirectory() as td:
            store = ef.EdgarFundamentalsStore(cache_dir=Path(td), sec_client=_stub_sec_client({}))
            store._prices["MTCH"] = 33.0
            fake = MagicMock()
            with patch.object(ef, "get_default_yfinance_client", return_value=fake):
                self.assertEqual(store._fetch_price("mtch", date(2026, 6, 12)), 33.0)
            fake.last_price.assert_not_called()


if __name__ == "__main__":
    unittest.main()

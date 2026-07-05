"""Unit tests for the sector-relative EDGE outcome (PR-2b, D4 decoupling).

``sector_excess_return = forward_return − sector_etf_window_return`` measures a
candidate against ITS OWN SPDR sector ETF over the SAME window as the SPY
benchmark-excess — a different series from the SPY-derived market_state label
(memo §4.2). Mirrors ``test_benchmark_excess``: fake bar_fetch + patched sector
resolution, no network.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

_ASOF_LAST_CLOSED = dt.date(2026, 7, 1)


def _bars_factory(reference: float, last_close: float, *, counter: list | None = None):
    """A bar_fetch returning two bars anchored on the fetch's ``start`` (arrival)."""

    def fetch(ticker, start, end):
        if counter is not None:
            counter.append(ticker)
        return [
            {"t": int(start.timestamp() * 1000), "c": reference, "v": 1000},
            {
                "t": int((start + dt.timedelta(minutes=470)).timestamp() * 1000),
                "c": last_close,
                "v": 1000,
            },
        ]

    return fetch


def _row(
    ticker="NVDA",
    *,
    forward_return: float | None = 0.15,
    brief_date="2026-06-11",
    matured_at="2026-06-25",
):
    return {
        "ticker": ticker,
        "forward_return": forward_return,
        "brief_date": brief_date,
        "matured_at": matured_at,
    }


class TestComputeSectorExcessForRow(unittest.TestCase):
    def test_resolved_sector_returns_etf_window_and_excess(self):
        from alphalens_pipeline.feedback import sector_excess

        # window return = (110 − 100) / 100 = 0.10; excess = 0.15 − 0.10 = 0.05
        fetch = _bars_factory(100.0, 110.0)
        with patch.object(sector_excess, "sector_etf_for_ticker", return_value="XLK"):
            etf, wret, excess = sector_excess.compute_sector_excess_for_row(
                _row(), bar_fetch=fetch, last_closed_session=_ASOF_LAST_CLOSED
            )

        self.assertEqual(etf, "XLK")
        self.assertAlmostEqual(wret, 0.10, places=6)
        self.assertAlmostEqual(excess, 0.05, places=6)

    def test_unresolvable_sector_returns_all_none(self):
        from alphalens_pipeline.feedback import sector_excess

        fetch = _bars_factory(100.0, 110.0)
        with patch.object(sector_excess, "sector_etf_for_ticker", return_value=None):
            result = sector_excess.compute_sector_excess_for_row(
                _row("ZZZZ"), bar_fetch=fetch, last_closed_session=_ASOF_LAST_CLOSED
            )

        self.assertEqual(result, (None, None, None))

    def test_missing_forward_return_keeps_etf_but_null_metric(self):
        from alphalens_pipeline.feedback import sector_excess

        fetch = _bars_factory(100.0, 110.0)
        with patch.object(sector_excess, "sector_etf_for_ticker", return_value="XLK"):
            etf, wret, excess = sector_excess.compute_sector_excess_for_row(
                _row(forward_return=None), bar_fetch=fetch, last_closed_session=_ASOF_LAST_CLOSED
            )

        self.assertEqual(etf, "XLK")
        self.assertIsNone(wret)
        self.assertIsNone(excess)

    def test_never_falls_back_to_spy(self):
        from alphalens_pipeline.feedback import sector_excess

        # A row whose sector is unresolvable must NEVER be benchmarked against SPY.
        fetch = _bars_factory(100.0, 999.0)  # would give a huge excess if used
        with patch.object(sector_excess, "sector_etf_for_ticker", return_value=None):
            _etf, _wret, excess = sector_excess.compute_sector_excess_for_row(
                _row("ZZZZ"), bar_fetch=fetch, last_closed_session=_ASOF_LAST_CLOSED
            )

        self.assertIsNone(excess)


class TestEnrichStoreSectorExcess(unittest.TestCase):
    def _write(self, root: Path, rows: list[dict]):
        pd.DataFrame(rows).to_parquet(root / "2026-06-11.parquet", index=False)

    def _sector_map(self, ticker):
        return {"NVDA": "XLK", "AAPL": "XLK"}.get(ticker)  # ZZZZ → None

    def test_enrich_stamps_four_columns_and_excludes_unresolvable(self):
        from alphalens_pipeline.feedback import sector_excess

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, [_row("NVDA"), _row("ZZZZ")])
            fetch = _bars_factory(100.0, 110.0)
            with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._sector_map):
                n = sector_excess.enrich_store_with_sector_excess(
                    root, bar_fetch=fetch, now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
                )

            out = pd.read_parquet(root / "2026-06-11.parquet")
            for col in sector_excess.SECTOR_EXCESS_COLUMNS:
                self.assertIn(col, out.columns)
            nvda = out[out["ticker"] == "NVDA"].iloc[0]
            self.assertEqual(nvda["sector_etf_ticker"], "XLK")
            self.assertAlmostEqual(nvda["sector_excess_return"], 0.05, places=6)
            zzzz = out[out["ticker"] == "ZZZZ"].iloc[0]
            self.assertTrue(pd.isna(zzzz["sector_excess_return"]))
            # version is stamped on EVERY row (poolability key)
            self.assertTrue(
                (out["outcome_benchmark_version"] == sector_excess.OUTCOME_BENCHMARK_VERSION).all()
            )
            self.assertEqual(n, 1)  # only NVDA got a non-null excess

    def test_shared_sector_window_is_fetched_once(self):
        from alphalens_pipeline.feedback import sector_excess

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, [_row("NVDA"), _row("AAPL")])  # both → XLK, same window
            calls: list[str] = []
            fetch = _bars_factory(100.0, 110.0, counter=calls)
            with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._sector_map):
                sector_excess.enrich_store_with_sector_excess(
                    root, bar_fetch=fetch, now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
                )

            self.assertEqual(calls, ["XLK"])  # memoized: one fetch for the shared window

    def test_idempotent(self):
        from alphalens_pipeline.feedback import sector_excess

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, [_row("NVDA")])
            fetch = _bars_factory(100.0, 110.0)
            with patch.object(sector_excess, "sector_etf_for_ticker", side_effect=self._sector_map):
                sector_excess.enrich_store_with_sector_excess(
                    root, bar_fetch=fetch, now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
                )
                first = pd.read_parquet(root / "2026-06-11.parquet")[
                    "sector_excess_return"
                ].tolist()
                sector_excess.enrich_store_with_sector_excess(
                    root, bar_fetch=fetch, now=dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
                )
                second = pd.read_parquet(root / "2026-06-11.parquet")[
                    "sector_excess_return"
                ].tolist()

            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

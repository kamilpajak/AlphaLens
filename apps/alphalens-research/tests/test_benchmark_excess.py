"""Tests for the population-ladder benchmark-excess enrichment.

The edge-dashboard headline is the benchmark-RELATIVE move (memo §3.1):
``market_excess_return = forward_return − benchmark_window_return`` where BOTH
legs are raw close-to-close returns over the SAME arrival→exit window. The
candidate's ``realized_r`` is risk-normalised and is NOT comparable to a raw
index return, so the excess is computed at the RETURN level, never the R level.

These tests pin:
* the excess is forward_return − benchmark over the same window (return units);
* a missing forward_return / unrecoverable window / empty benchmark fetch leaves
  BOTH columns None (no fudged value);
* terminal rows use ``matured_at`` as the exit; ongoing rows use last-closed;
* the store enrichment writes the two columns onto every parquet and is
  idempotent.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback.benchmark_excess import (
    BENCHMARK_COLUMNS,
    compute_market_excess_for_row,
    enrich_store_with_benchmark_excess,
)
from alphalens_pipeline.paper.calendar import session_on_or_after, session_open_utc

UTC = dt.UTC


def _spy_bars(arrival_open: dt.datetime, *, reference: float, last_close: float):
    """Two SPY bars: one in the arrival VWAP window (sets reference), one at the end.

    The first bar's close is the VWAP reference (single bar -> VWAP == its close);
    the last bar's close is the horizon-end print. So the benchmark window return
    is ``(last_close - reference) / reference``.
    """
    return [
        {
            "t": int(arrival_open.timestamp() * 1000),
            "o": reference,
            "h": reference,
            "l": reference,
            "c": reference,
            "v": 1000,
        },
        {
            "t": int((arrival_open + dt.timedelta(days=10)).timestamp() * 1000),
            "o": last_close,
            "h": last_close,
            "l": last_close,
            "c": last_close,
            "v": 1000,
        },
    ]


class TestComputeMarketExcessForRow(unittest.TestCase):
    def setUp(self) -> None:
        self.brief_date = dt.date(2026, 5, 18)
        self.arrival_session = session_on_or_after(self.brief_date)
        self.arrival_open = session_open_utc(self.arrival_session)
        self.last_closed = dt.date(2026, 6, 2)

    def test_excess_is_forward_minus_benchmark_at_return_level(self) -> None:
        # SPY +2% over the window; candidate forward_return +5% -> excess +3%.
        bars = _spy_bars(self.arrival_open, reference=100.0, last_close=102.0)
        row = {
            "brief_date": self.brief_date,
            "ticker": "AMPL",
            "terminal": True,
            "matured_at": dt.date(2026, 5, 27),
            "forward_return": 0.05,
        }
        bench, excess = compute_market_excess_for_row(
            row, bar_fetch=lambda *_: bars, last_closed_session=self.last_closed
        )
        assert bench is not None
        self.assertAlmostEqual(bench, 0.02, places=6)
        assert excess is not None
        self.assertAlmostEqual(excess, 0.03, places=6)

    def test_missing_forward_return_yields_none_none(self) -> None:
        row = {
            "brief_date": self.brief_date,
            "ticker": "X",
            "terminal": True,
            "matured_at": dt.date(2026, 5, 27),
            "forward_return": None,
        }
        bench, excess = compute_market_excess_for_row(
            row,
            bar_fetch=lambda *_: _spy_bars(self.arrival_open, reference=100.0, last_close=102.0),
            last_closed_session=self.last_closed,
        )
        self.assertIsNone(bench)
        self.assertIsNone(excess)

    def test_empty_benchmark_fetch_yields_none_none_not_fudge(self) -> None:
        row = {
            "brief_date": self.brief_date,
            "ticker": "X",
            "terminal": True,
            "matured_at": dt.date(2026, 5, 27),
            "forward_return": 0.05,
        }
        bench, excess = compute_market_excess_for_row(
            row, bar_fetch=lambda *_: [], last_closed_session=self.last_closed
        )
        self.assertIsNone(bench)
        self.assertIsNone(excess)

    def test_ongoing_row_uses_last_closed_session_as_exit(self) -> None:
        # matured_at is None (ongoing); the window must still resolve via the
        # last-closed session and produce a value.
        bars = _spy_bars(self.arrival_open, reference=100.0, last_close=101.0)
        row = {
            "brief_date": self.brief_date,
            "ticker": "BLBD",
            "terminal": False,
            "matured_at": None,
            "forward_return": 0.03,
        }
        bench, excess = compute_market_excess_for_row(
            row, bar_fetch=lambda *_: bars, last_closed_session=self.last_closed
        )
        assert bench is not None
        self.assertAlmostEqual(bench, 0.01, places=6)
        assert excess is not None
        self.assertAlmostEqual(excess, 0.02, places=6)


class TestEnrichStore(unittest.TestCase):
    def test_enrich_writes_columns_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            brief_date = dt.date(2026, 5, 18)
            arrival_open = session_open_utc(session_on_or_after(brief_date))
            df = pd.DataFrame(
                [
                    {
                        "brief_date": brief_date,
                        "ticker": "AMPL",
                        "plannable": True,
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": 0.05,
                    },
                    {
                        "brief_date": brief_date,
                        "ticker": "NOFWD",
                        "plannable": True,
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": None,
                    },
                ]
            )
            df.to_parquet(store / f"{brief_date.isoformat()}.parquet")

            bars = _spy_bars(arrival_open, reference=100.0, last_close=102.0)
            n = enrich_store_with_benchmark_excess(
                store,
                bar_fetch=lambda *_: bars,
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            self.assertEqual(n, 1)  # only the row with a forward_return

            out = pd.read_parquet(store / f"{brief_date.isoformat()}.parquet")
            for col in BENCHMARK_COLUMNS:
                self.assertIn(col, out.columns)
            ampl = out[out["ticker"] == "AMPL"].iloc[0]
            self.assertAlmostEqual(float(ampl["market_excess_return"]), 0.03, places=6)
            nofwd = out[out["ticker"] == "NOFWD"].iloc[0]
            self.assertTrue(pd.isna(nofwd["market_excess_return"]))

            # Idempotent: a second run re-computes the same values.
            n2 = enrich_store_with_benchmark_excess(
                store,
                bar_fetch=lambda *_: bars,
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            self.assertEqual(n2, 1)


class TestEnrichDeadline(unittest.TestCase):
    """A tripped deadline stops all fetching before the first row is processed."""

    def test_enrich_stops_fetching_when_deadline_tripped(self):
        # GIVEN a store with two rows that both have forward_return (both would
        # normally trigger a benchmark fetch) and a deadline that has already
        # expired (budget=-1.0, monotonic fixed at 0.0 so deadline = -1.0 < 0.0
        # and should_stop() is True from the very first call).
        from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline

        dead = _RunDeadline(-1.0, monotonic=lambda: 0.0)
        calls: list[str] = []

        def _fetch(t, s, e):
            calls.append(t)
            return []

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            brief_date = dt.date(2026, 5, 18)
            df = pd.DataFrame(
                [
                    {
                        "brief_date": brief_date,
                        "ticker": "AA",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": 0.05,
                    },
                    {
                        "brief_date": brief_date,
                        "ticker": "BB",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 28),
                        "forward_return": 0.03,
                    },
                ]
            )
            df.to_parquet(store / f"{brief_date.isoformat()}.parquet")

            # WHEN enrichment runs with a tripped deadline
            n = enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
                deadline=dead,
            )

        # THEN no fetch was issued and the function returned cleanly with 0
        self.assertEqual(calls, [])
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()

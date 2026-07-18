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
from alphalens_pipeline.feedback.bar_window import ARRIVAL_VWAP_WINDOW_MIN, _window_vwap
from alphalens_pipeline.feedback.benchmark_excess import (
    _HORIZON_SESSION_SPAN_MIN,
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


class TestEnrichSelfHeal(unittest.TestCase):
    """Root-cause fixes for the persistent NULL benchmark on recent /edge dates.

    Two failure modes the nightly sweep had: (1) it processed parquets OLDEST
    first, so under the shared run deadline the recent (dashboard-visible) dates
    could be starved; (2) it rewrote the whole column, so a transient benchmark
    fetch miss DESTROYED an already-good value. The enrich now visits newest
    first and never overwrites an existing benchmark with a fresh None.
    """

    def test_processes_newest_parquet_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            # (brief_date, matured session) — both matured dates are real XNYS
            # sessions (avoid Memorial Day 2026-05-25).
            old, old_exit = dt.date(2026, 5, 18), dt.date(2026, 5, 27)
            new, new_exit = dt.date(2026, 6, 1), dt.date(2026, 6, 8)
            for d, exit_d in ((old, old_exit), (new, new_exit)):
                pd.DataFrame(
                    [
                        {
                            "brief_date": d,
                            "ticker": "AA",
                            "terminal": True,
                            "matured_at": exit_d,
                            "forward_return": 0.05,
                        }
                    ]
                ).to_parquet(store / f"{d.isoformat()}.parquet")

            fetched_starts: list[dt.datetime] = []

            def _fetch(_t, start, _e):
                fetched_starts.append(start)
                return _spy_bars(start, reference=100.0, last_close=102.0)

            enrich_store_with_benchmark_excess(
                store, bar_fetch=_fetch, now=dt.datetime(2026, 6, 10, tzinfo=UTC)
            )
            # The newest date's arrival window must be fetched before the old one's,
            # so a deadline-truncated sweep heals the dashboard-visible dates first.
            new_arrival = session_open_utc(session_on_or_after(new))
            self.assertEqual(fetched_starts[0], new_arrival)

    def test_transient_none_does_not_overwrite_existing_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            d = dt.date(2026, 5, 18)
            pd.DataFrame(
                [
                    {
                        "brief_date": d,
                        "ticker": "AA",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": 0.05,
                        "benchmark_window_return": 0.02,
                        "market_excess_return": 0.03,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            # A transient outage: the fetch returns no bars, so the benchmark
            # recomputes to None. The previously-good value must be KEPT.
            enrich_store_with_benchmark_excess(
                store, bar_fetch=lambda *_: [], now=dt.datetime(2026, 6, 3, tzinfo=UTC)
            )
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(row["benchmark_window_return"]), 0.02, places=6)
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.03, places=6)


class TestBenchmarkAnchorInvariants(unittest.TestCase):
    """Pins the verified market_excess anchor invariants (NO anchor bug exists).

    A false-alarm investigation back-solved an implied SPY reference from SPY's
    DAILY cash close and concluded the benchmark leg was anchored one session
    early. It is not: production anchors BOTH legs to the same arrival session
    (``session_on_or_after`` -> ``session_open_utc`` -> arrival 30-min VWAP), and
    the exit leg uses the LAST available minute bar (an after-hours print ~480 min
    past the exit-session open), NOT the 16:00 ET cash close. These tests lock
    those invariants so a future refactor cannot silently introduce the anchor
    shift the scare implied.
    """

    _EXCHANGE = "XNYS"

    @staticmethod
    def _ms(d: dt.datetime) -> int:
        return int(d.timestamp() * 1000)

    def test_arrival_anchor_is_brief_date_open_when_holiday_inside_window(self) -> None:
        # brief_date 2026-06-18 (Thu). Juneteenth 2026-06-19 (Fri) is a market
        # holiday STRICTLY INSIDE the [arrival, exit] holding window — it must
        # never pull the arrival anchor back to the prior session.
        self.assertEqual(
            session_on_or_after(dt.date(2026, 6, 18), self._EXCHANGE), dt.date(2026, 6, 18)
        )
        self.assertEqual(
            session_open_utc(dt.date(2026, 6, 18), self._EXCHANGE),
            dt.datetime(2026, 6, 18, 13, 30, tzinfo=UTC),
        )
        # Juneteenth itself is not a session -> rolls forward to Monday June 22.
        self.assertEqual(
            session_on_or_after(dt.date(2026, 6, 19), self._EXCHANGE), dt.date(2026, 6, 22)
        )

    def test_window_vwap_excludes_prior_session_and_post_window_bars(self) -> None:
        # The arrival reference VWAP must use ONLY bars in [arrival_open, +30min):
        # a prior-session (June-17) bar and a bar past the 30-min window are both
        # rejected, so no June-17 / overnight price leaks into the SPY reference.
        arrival_open = dt.datetime(2026, 6, 18, 13, 30, tzinfo=UTC)
        end = arrival_open + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN)
        bars = [
            {"t": self._ms(dt.datetime(2026, 6, 17, 19, 0, tzinfo=UTC)), "c": 740.0, "v": 1000},
            {"t": self._ms(arrival_open), "c": 747.0, "v": 1000},
            {"t": self._ms(arrival_open + dt.timedelta(minutes=10)), "c": 745.0, "v": 1000},
            {"t": self._ms(arrival_open + dt.timedelta(minutes=45)), "c": 760.0, "v": 1000},
        ]
        vwap = _window_vwap(bars, arrival_open, end)
        # Mean of the two in-window closes (747, 745); never pulled to 740 or 760.
        assert vwap is not None
        self.assertAlmostEqual(vwap, 746.0, places=6)

    def test_exit_leg_uses_last_minute_bar_not_intermediate_cash_close(self) -> None:
        # The CRL/2026-06-18 case that triggered the false alarm. The exit leg must
        # take the LAST bar (an after-hours print, 737.96 at June-24 21:30 UTC),
        # NOT an earlier 16:00-ET cash-close bar (733.24). Using the cash close
        # manufactures an implied ~740.7 (June-17-looking) reference.
        arrival_open = session_open_utc(
            session_on_or_after(dt.date(2026, 6, 18), self._EXCHANGE), self._EXCHANGE
        )
        seen_start: list[dt.datetime] = []

        def _fetch(ticker, start, end):
            seen_start.append(start)
            self.assertEqual(ticker, "SPY")
            return [
                {"t": self._ms(arrival_open), "c": 745.4737, "v": 1000},  # arrival 30-min VWAP
                {
                    "t": self._ms(dt.datetime(2026, 6, 24, 20, 0, tzinfo=UTC)),
                    "c": 733.24,
                    "v": 1000,
                },
                {
                    "t": self._ms(dt.datetime(2026, 6, 24, 21, 30, tzinfo=UTC)),
                    "c": 737.96,
                    "v": 1000,
                },
            ]

        row = {
            "brief_date": dt.date(2026, 6, 18),
            "ticker": "CRL",
            "terminal": True,
            "matured_at": dt.date(2026, 6, 24),
            "forward_return": 0.104290606614145,  # CRL/06-18 stored value
        }
        bench, excess = compute_market_excess_for_row(
            row,
            bar_fetch=_fetch,
            last_closed_session=dt.date(2026, 6, 30),
            exchange=self._EXCHANGE,
        )
        # The SPY fetch anchored to the June-18 arrival open (NOT June-17).
        self.assertEqual(seen_start[0], dt.datetime(2026, 6, 18, 13, 30, tzinfo=UTC))
        assert bench is not None and excess is not None
        # Benchmark uses the LAST bar (737.96), not the cash-close bar (733.24).
        self.assertAlmostEqual(bench, (737.96 - 745.4737) / 745.4737, places=6)
        # Reproduces the stored CRL/06-18 market_excess (~+11.44%) — the +0.8pp
        # "understatement" the daily-close back-out reported does not exist.
        self.assertAlmostEqual(excess, 0.11436972113938475, places=4)

    def test_window_span_constants(self) -> None:
        # The two moving parts of the anchor convention; a silent change to either
        # is exactly what would shift the metric.
        self.assertEqual(ARRIVAL_VWAP_WINDOW_MIN, 30)
        self.assertEqual(_HORIZON_SESSION_SPAN_MIN, 480)

    def test_mid_week_and_post_holiday_arrivals_anchor_to_their_own_open(self) -> None:
        # No-regression: a plain mid-week arrival (Thu June-11) and the session
        # immediately AFTER a holiday (Mon June-22, after Juneteenth) both anchor
        # to their own session open at 13:30 UTC.
        for d in (dt.date(2026, 6, 11), dt.date(2026, 6, 22)):
            self.assertEqual(session_on_or_after(d, self._EXCHANGE), d)
            self.assertEqual(
                session_open_utc(d, self._EXCHANGE),
                dt.datetime(d.year, d.month, d.day, 13, 30, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()

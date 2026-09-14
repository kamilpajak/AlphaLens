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
import time
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback.bar_window import ARRIVAL_VWAP_WINDOW_MIN, _window_vwap
from alphalens_pipeline.feedback.benchmark_excess import (
    BENCHMARK_COLUMNS,
    BENCHMARK_LEG_VERSION,
    compute_market_excess_for_row,
    enrich_store_with_benchmark_excess,
)
from alphalens_pipeline.feedback.ladder_config import ladder_arrival_session
from alphalens_pipeline.paper.calendar import session_open_utc

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


def _spy_closes(close: float):
    """Grouped-daily stub: SPY's official close is ``close`` on EVERY session."""
    return lambda _session: {"SPY": {"c": close}}


class TestComputeMarketExcessForRow(unittest.TestCase):
    def setUp(self) -> None:
        self.brief_date = dt.date(2026, 5, 18)
        self.arrival_session = ladder_arrival_session(self.brief_date)
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
            row,
            bar_fetch=lambda *_: bars,
            exit_close_of=lambda _t, _s: 102.0,
            last_closed_session=self.last_closed,
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
            exit_close_of=lambda _t, _s: 102.0,
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
            row,
            bar_fetch=lambda *_: [],
            exit_close_of=lambda _t, _s: 102.0,
            last_closed_session=self.last_closed,
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
            row,
            bar_fetch=lambda *_: bars,
            exit_close_of=lambda _t, _s: 101.0,
            last_closed_session=self.last_closed,
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
            arrival_open = session_open_utc(ladder_arrival_session(brief_date))
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
                grouped_fetch=_spy_closes(102.0),
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
                grouped_fetch=_spy_closes(102.0),
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
                grouped_fetch=_spy_closes(102.0),
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
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 10, tzinfo=UTC),
            )
            # The newest date's arrival window must be fetched before the old one's,
            # so a deadline-truncated sweep heals the dashboard-visible dates first.
            new_arrival = ladder_arrival_session(new)
            self.assertEqual(fetched_starts[0], session_open_utc(new_arrival))

    def test_newest_first_under_deadline_heals_recent_and_leaves_old(self) -> None:
        # The interaction that justifies newest-first: a deadline that trips after
        # the FIRST (newest) file must leave the newest date HEALED and the oldest
        # UNPROCESSED for the next run — not the other way round.
        from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
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

            # monotonic: start(0) -> newest-row check(0, under budget) -> oldest-row
            # check(100, over budget 10 -> stop). Deadline trips only after the
            # newest file is written.
            seq = iter([0.0, 0.0, 100.0])
            dead = _RunDeadline(10.0, monotonic=lambda: next(seq, 100.0))

            def _fetch(_t, start, _e):
                return _spy_bars(start, reference=100.0, last_close=102.0)

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 10, tzinfo=UTC),
                deadline=dead,
            )
            new_df = pd.read_parquet(store / f"{new.isoformat()}.parquet")
            old_df = pd.read_parquet(store / f"{old.isoformat()}.parquet")
            # Newest date healed...
            self.assertFalse(pd.isna(new_df.iloc[0]["benchmark_window_return"]))
            # ...oldest left untouched for the next run (never rewritten -> no column).
            self.assertNotIn("benchmark_window_return", old_df.columns)

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
                        "benchmark_window_exit": "2026-05-27",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            # A transient outage: the fetch returns no bars, so the benchmark
            # recomputes to None. The previously-good value must be KEPT.
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=lambda *_: [],
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(row["benchmark_window_return"]), 0.02, places=6)
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.03, places=6)

    def test_transient_none_drops_a_stale_pair_inconsistent_with_forward(self) -> None:
        # Maturation-transition hazard: the monitor advances forward_return AND sets
        # matured_at in one rewrite while carrying the OLD (benchmark, excess) pair
        # verbatim. The frozen-window gate alone would keep that stale pair on a
        # fetch miss, violating excess == forward - benchmark. The keep must verify
        # the stored pair is still CONSISTENT with the current forward_return.
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
                        # forward advanced to 0.09 on maturation; the carried pair
                        # (bench 0.02, excess 0.03) was computed against the OLD
                        # forward 0.05 -> 0.03 != 0.09 - 0.02, i.e. STALE.
                        "forward_return": 0.09,
                        "benchmark_window_return": 0.02,
                        "market_excess_return": 0.03,
                        "benchmark_window_exit": "2026-05-27",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=lambda *_: [],
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertTrue(pd.isna(row["benchmark_window_return"]))
            self.assertTrue(pd.isna(row["market_excess_return"]))

    def test_transient_none_nulls_an_ongoing_rows_stale_benchmark(self) -> None:
        # An ONGOING row's exit window GROWS every session, so a preserved older
        # benchmark would be stale against a freshly-advanced forward_return (and
        # could leak into excess-telemetry). Only a frozen (terminal, matured_at)
        # window may keep its last-good value; an ongoing miss must go NULL and
        # recompute next run.
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            d = dt.date(2026, 5, 18)
            pd.DataFrame(
                [
                    {
                        "brief_date": d,
                        "ticker": "AA",
                        "terminal": False,
                        "matured_at": None,
                        "forward_return": 0.05,
                        "benchmark_window_return": 0.02,
                        "market_excess_return": 0.03,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=lambda *_: [],
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertTrue(pd.isna(row["benchmark_window_return"]))
            self.assertTrue(pd.isna(row["market_excess_return"]))


class TestBenchmarkAnchorInvariants(unittest.TestCase):
    """Pins the market_excess anchor invariants (NO anchor bug exists).

    A false-alarm investigation back-solved an implied SPY reference from SPY's
    DAILY cash close and concluded the benchmark leg was anchored one session
    early. It is not: production anchors BOTH legs to the same arrival session
    (``ladder_arrival_session`` -> ``session_open_utc`` -> arrival 30-min VWAP).
    Since #1445 the exit leg is the OFFICIAL close of the exit session (the same
    print the candidate leg uses, #1444), no longer the last available minute
    bar. These tests lock those invariants so a future refactor cannot silently
    introduce the anchor shift the scare implied, or reintroduce the after-hours
    exit print.
    """

    _EXCHANGE = "XNYS"

    @staticmethod
    def _ms(d: dt.datetime) -> int:
        return int(d.timestamp() * 1000)

    def test_arrival_anchor_is_the_session_after_the_brief(self) -> None:
        # The SPY leg shares the ladder's arrival (#1416): the first session after
        # the brief exists. A Wed 2026-06-17 brief arrives Thu June 18 at 13:30 UTC.
        self.assertEqual(
            ladder_arrival_session(dt.date(2026, 6, 17), self._EXCHANGE), dt.date(2026, 6, 18)
        )
        self.assertEqual(
            session_open_utc(dt.date(2026, 6, 18), self._EXCHANGE),
            dt.datetime(2026, 6, 18, 13, 30, tzinfo=UTC),
        )
        # A Thu June-18 brief skips Juneteenth (Fri, not a session) -> Monday June 22.
        self.assertEqual(
            ladder_arrival_session(dt.date(2026, 6, 18), self._EXCHANGE), dt.date(2026, 6, 22)
        )

    def test_market_window_starts_at_the_ladder_arrival(self) -> None:
        # The SPY fetch must start where the ladder window starts, never at the
        # brief's own (already closed) session.
        seen_start: list[dt.datetime] = []

        def _fetch(ticker, start, end):
            seen_start.append(start)
            return []

        row = {
            "brief_date": dt.date(2026, 6, 16),  # Tue
            "ticker": "CRL",
            "terminal": True,
            "matured_at": dt.date(2026, 6, 24),
            "forward_return": 0.1,
        }
        compute_market_excess_for_row(
            row,
            bar_fetch=_fetch,
            exit_close_of=lambda _t, _s: 740.0,
            last_closed_session=dt.date(2026, 6, 30),
            exchange=self._EXCHANGE,
        )
        self.assertEqual(seen_start[0], dt.datetime(2026, 6, 17, 13, 30, tzinfo=UTC))

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

    def test_exit_leg_is_the_official_close_not_the_last_minute_bar(self) -> None:
        # The CRL/2026-06-18 case that triggered the false alarm, under #1445: the
        # exit leg is the OFFICIAL June-24 close (733.24, the closing-auction print
        # the candidate leg also uses), NOT the last after-hours minute bar
        # (737.96 at 21:30 UTC) the pass took until #1445 — and the reference is
        # still the June-18 arrival VWAP, never a June-17-looking back-out.
        # A Wed June-17 brief arrives Thu June 18 (#1416).
        arrival_open = session_open_utc(
            ladder_arrival_session(dt.date(2026, 6, 17), self._EXCHANGE), self._EXCHANGE
        )
        seen_start: list[dt.datetime] = []

        def _fetch(ticker, start, end):
            seen_start.append(start)
            self.assertEqual(ticker, "SPY")
            return [
                {"t": self._ms(arrival_open), "c": 745.4737, "v": 1000},  # arrival 30-min VWAP
                {
                    "t": self._ms(dt.datetime(2026, 6, 24, 21, 30, tzinfo=UTC)),
                    "c": 737.96,
                    "v": 1000,
                },
            ]

        row = {
            "brief_date": dt.date(2026, 6, 17),
            "ticker": "CRL",
            "terminal": True,
            "matured_at": dt.date(2026, 6, 24),
            "forward_return": 0.104290606614145,  # CRL/06-18-arrival stored value
        }
        bench, excess = compute_market_excess_for_row(
            row,
            bar_fetch=_fetch,
            exit_close_of=lambda _t, session: 733.24 if session == dt.date(2026, 6, 24) else None,
            last_closed_session=dt.date(2026, 6, 30),
            exchange=self._EXCHANGE,
        )
        # The SPY fetch anchored to the June-18 arrival open (NOT June-17).
        self.assertEqual(seen_start[0], dt.datetime(2026, 6, 18, 13, 30, tzinfo=UTC))
        assert bench is not None and excess is not None
        # Benchmark uses the official close (733.24), not the after-hours bar (737.96).
        self.assertAlmostEqual(bench, (733.24 - 745.4737) / 745.4737, places=6)
        self.assertAlmostEqual(excess, 0.104290606614145 - (733.24 - 745.4737) / 745.4737, places=9)

    def test_window_span_constant(self) -> None:
        # The one moving part of the anchor convention; a silent change is
        # exactly what would shift the metric.
        self.assertEqual(ARRIVAL_VWAP_WINDOW_MIN, 30)

    def test_mid_week_and_post_holiday_arrivals_anchor_to_their_own_open(self) -> None:
        # No-regression: a plain mid-week arrival (Thu June-11, from a Wed brief)
        # and the session immediately AFTER a holiday (Mon June-22, from a Thu
        # brief across Juneteenth) both anchor to their own session open at 13:30.
        for brief, d in (
            (dt.date(2026, 6, 10), dt.date(2026, 6, 11)),
            (dt.date(2026, 6, 18), dt.date(2026, 6, 22)),
        ):
            self.assertEqual(ladder_arrival_session(brief, self._EXCHANGE), d)
            self.assertEqual(
                session_open_utc(d, self._EXCHANGE),
                dt.datetime(d.year, d.month, d.day, 13, 30, tzinfo=UTC),
            )


class ReuseFirstBenchmarkExcess(unittest.TestCase):
    """Reuse-first: a settled TERMINAL row is served from its stored pair with
    NO fetch, so the wall-clock budget is spent only on gaps + ongoing rows.
    """

    def test_settled_terminal_row_is_reused_without_fetch(self) -> None:
        # A TERMINAL row with a consistent stored (benchmark, excess) pair is
        # reused verbatim -> the bar-fetch spy must NOT be called.
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
                        "benchmark_window_exit": "2026-05-27",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            calls: list[str] = []

            def _fetch(t, s, e):
                calls.append(t)
                return _spy_bars(s, reference=100.0, last_close=999.0)

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )

            self.assertEqual(calls, [])
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(row["benchmark_window_return"]), 0.02, places=6)
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.03, places=6)

    def test_gap_terminal_row_is_fetched(self) -> None:
        # A TERMINAL row with a NULL benchmark (gap) IS fetched and filled.
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            d = dt.date(2026, 5, 18)
            pd.DataFrame(
                [
                    {
                        "brief_date": d,
                        "ticker": "GAP",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": 0.05,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            arrival_open = session_open_utc(ladder_arrival_session(d))
            calls: list[str] = []

            def _fetch(t, s, e):
                calls.append(t)
                return _spy_bars(arrival_open, reference=100.0, last_close=102.0)

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )

            self.assertEqual(calls, ["SPY"])
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.03, places=6)

    def test_ongoing_row_is_not_reused(self) -> None:
        # An ONGOING row (matured_at None) with a stored pair is NOT reused —
        # its window keeps growing every session, so it must recompute.
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            d = dt.date(2026, 5, 18)
            pd.DataFrame(
                [
                    {
                        "brief_date": d,
                        "ticker": "ONG",
                        "terminal": False,
                        "matured_at": None,
                        "forward_return": 0.05,
                        "benchmark_window_return": 0.02,
                        "market_excess_return": 0.03,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            calls: list[str] = []

            def _fetch(t, s, e):
                calls.append(t)
                return _spy_bars(s, reference=100.0, last_close=101.0)

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(101.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )

            self.assertEqual(calls, ["SPY"])
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            # Recomputed against the new fetch (1%), not the stale stored 2%.
            self.assertAlmostEqual(float(row["benchmark_window_return"]), 0.01, places=6)

    def test_inconsistent_terminal_pair_is_recomputed(self) -> None:
        # A TERMINAL row whose stored excess != forward - benchmark is stale
        # (e.g. forward advanced past a maturation rewrite) -> recomputed.
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            d = dt.date(2026, 5, 18)
            pd.DataFrame(
                [
                    {
                        "brief_date": d,
                        "ticker": "STALE",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": 0.09,
                        "benchmark_window_return": 0.02,
                        "market_excess_return": 0.03,  # stale: 0.09-0.02 != 0.03
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            calls: list[str] = []

            def _fetch(t, s, e):
                calls.append(t)
                return _spy_bars(s, reference=100.0, last_close=102.0)

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )

            self.assertEqual(calls, ["SPY"])
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.07, places=6)

    def test_budget_drains_gap_because_settled_rows_dont_fetch(self) -> None:
        # Pins the starvation fix: budget consumed ONLY by real fetches. Store
        # has a newest parquet with 2 settled terminal rows (different windows,
        # so 2 distinct fetches PRE-fix) and an oldest parquet with 1 gap row.
        # Deadline budget ~= 1.5 fetches. PRE-fix the 2 settled-row fetches
        # exhaust the budget before the oldest gap file is even opened -> gap
        # starved. POST-fix the settled rows fetch nothing -> budget intact ->
        # the gap fills.
        from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline

        elapsed = [0.0]

        def spy_fetch(ticker, start, end):
            elapsed[0] += 60.0  # one fetch burns 60s of budget
            return _spy_bars(start, reference=100.0, last_close=102.0)

        dead = _RunDeadline(90.0, monotonic=lambda: elapsed[0])

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            new = dt.date(2026, 6, 1)
            pd.DataFrame(
                [
                    {
                        "brief_date": new,
                        "ticker": "AA",
                        "terminal": True,
                        "matured_at": dt.date(2026, 6, 8),
                        "forward_return": 0.05,
                        "benchmark_window_return": 0.02,
                        "market_excess_return": 0.03,
                        "benchmark_window_exit": "2026-06-08",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    },
                    {
                        "brief_date": new,
                        "ticker": "BB",
                        "terminal": True,
                        "matured_at": dt.date(2026, 6, 9),
                        "forward_return": 0.04,
                        "benchmark_window_return": 0.01,
                        "market_excess_return": 0.03,
                        "benchmark_window_exit": "2026-06-09",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    },
                ]
            ).to_parquet(store / f"{new.isoformat()}.parquet")

            old = dt.date(2026, 5, 18)
            pd.DataFrame(
                [
                    {
                        "brief_date": old,
                        "ticker": "GAP",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": 0.06,
                    }
                ]
            ).to_parquet(store / f"{old.isoformat()}.parquet")

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=spy_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 10, tzinfo=UTC),
                deadline=dead,
            )

            old_row = pd.read_parquet(store / f"{old.isoformat()}.parquet").iloc[0]
            self.assertFalse(pd.isna(old_row["benchmark_window_return"]))


class TestCheapTerminalMaturationComposition(unittest.TestCase):
    """(I3) End-to-end lock for Change B (population_ladder_monitor's cheap NO_FILL
    terminal maturation, ``_cheap_update_row``): it nulls any carried
    ``(benchmark_window_return, market_excess_return)`` pair on the terminal
    transition, so THIS module's reuse-first (``_has_consistent_stored_pair``)
    sees a GAP on the next enrich pass and recomputes it -- instead of freezing
    a value computed against the row's prior, still-growing ONGOING window.
    """

    def test_nulled_pair_from_cheap_terminal_maturation_is_recomputed(self) -> None:
        # Simulates exactly what Change B leaves on disk: a TERMINAL NO_FILL row
        # whose benchmark pair was nulled by the cheap terminal-maturation branch.
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
                        "benchmark_window_return": None,
                        "market_excess_return": None,
                        "benchmark_window_exit": "2026-05-27",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            arrival_open = session_open_utc(ladder_arrival_session(d))
            calls: list[str] = []

            def _fetch(t, s, e):
                calls.append(t)
                return _spy_bars(arrival_open, reference=100.0, last_close=102.0)

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )

            self.assertEqual(calls, ["SPY"], "a nulled pair is a GAP -> must be fetched")
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(row["benchmark_window_return"]), 0.02, places=6)
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.03, places=6)

    def test_why_change_b_matters_a_non_nulled_stale_consistent_pair_would_freeze(self) -> None:
        # Documents WHY Change B is load-bearing: WITHOUT it, a stale-but-still-
        # CONSISTENT (bench, excess) pair carried verbatim from the ONGOING window
        # (0.03 == 0.05 - 0.02, so it passes the consistency check even though it
        # was computed against the growing window, not the final terminal one)
        # would be reused/frozen -- never recomputed with the terminal window's
        # true benchmark. This is the counterfactual the null in Change B prevents.
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
                        "benchmark_window_exit": "2026-05-27",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    }
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            arrival_open = session_open_utc(ladder_arrival_session(d))
            calls: list[str] = []

            def _fetch(t, s, e):
                calls.append(t)
                return _spy_bars(arrival_open, reference=100.0, last_close=999.0)

            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )

            self.assertEqual(calls, [], "a consistent stored pair is reused, NOT recomputed")
            row = pd.read_parquet(store / f"{d.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(row["benchmark_window_return"]), 0.02, places=6)
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.03, places=6)


class TestEnrichSkipWriteAndLogFormat(unittest.TestCase):
    """M3 (skip the parquet write when a file's rows were all reused) + M4
    (the ``enriched N (reused M, fetched F)`` INFO summary line). Neither had
    a dedicated assertion before — this pins both as genuinely red-if-removed.
    """

    def test_all_reused_file_is_not_rewritten(self) -> None:
        # GIVEN a store parquet where EVERY row is a settled TERMINAL row with
        # a consistent stored pair -> every row is reused (n_fetched == 0 for
        # this file), so the write must be skipped: mtime stays bit-for-bit
        # identical and the values are untouched.
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            d = dt.date(2026, 5, 18)
            path = store / f"{d.isoformat()}.parquet"
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
                        "benchmark_window_exit": "2026-05-27",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    },
                    {
                        "brief_date": d,
                        "ticker": "BB",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 28),
                        "forward_return": 0.04,
                        "benchmark_window_return": 0.01,
                        "market_excess_return": 0.03,
                        "benchmark_window_exit": "2026-05-28",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    },
                ]
            ).to_parquet(path)
            mtime_before = path.stat().st_mtime_ns

            calls: list[str] = []

            def _fetch(t, s, e):
                calls.append(t)
                return _spy_bars(s, reference=100.0, last_close=999.0)

            # WHEN enrichment runs over an all-reused file
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )

            # THEN no fetch was issued, the file was never rewritten...
            self.assertEqual(calls, [])
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)
            # ...and the stored values are exactly what was seeded.
            out = pd.read_parquet(path)
            aa = out[out["ticker"] == "AA"].iloc[0]
            bb = out[out["ticker"] == "BB"].iloc[0]
            self.assertAlmostEqual(float(aa["market_excess_return"]), 0.03, places=6)
            self.assertAlmostEqual(float(bb["market_excess_return"]), 0.03, places=6)

    def test_a_gap_row_triggers_rewrite(self) -> None:
        # GIVEN a store parquet with a single GAP (null benchmark) TERMINAL
        # row -> that row must be fetched, so the file is rewritten (mtime
        # changes) and the gap gets filled.
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            d = dt.date(2026, 5, 18)
            path = store / f"{d.isoformat()}.parquet"
            pd.DataFrame(
                [
                    {
                        "brief_date": d,
                        "ticker": "GAP",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": 0.05,
                    }
                ]
            ).to_parquet(path)
            mtime_before = path.stat().st_mtime_ns
            time.sleep(0.01)  # ensure a distinguishable mtime tick

            arrival_open = session_open_utc(ladder_arrival_session(d))

            def _fetch(t, s, e):
                return _spy_bars(arrival_open, reference=100.0, last_close=102.0)

            # WHEN enrichment runs over a file with a real gap
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=_fetch,
                grouped_fetch=_spy_closes(102.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )

            # THEN the file was rewritten...
            self.assertNotEqual(path.stat().st_mtime_ns, mtime_before)
            # ...and the gap is filled.
            row = pd.read_parquet(path).iloc[0]
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.03, places=6)

    def test_summary_log_line_reports_enriched_reused_fetched(self) -> None:
        # GIVEN a mixed store: one settled (reused) terminal row and one gap
        # (fetched) terminal row in the same file -> enriched=2 (both end up
        # with a non-null excess), reused=1, fetched=1.
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
                        "benchmark_window_exit": "2026-05-27",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    },
                    {
                        "brief_date": d,
                        "ticker": "GAP",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 28),
                        "forward_return": 0.04,
                    },
                ]
            ).to_parquet(store / f"{d.isoformat()}.parquet")

            def _fetch(t, s, e):
                return _spy_bars(s, reference=100.0, last_close=102.0)

            # WHEN enrichment runs, capturing the module logger at INFO
            with self.assertLogs(
                "alphalens_pipeline.feedback.benchmark_excess", level="INFO"
            ) as cm:
                enrich_store_with_benchmark_excess(
                    store,
                    bar_fetch=_fetch,
                    grouped_fetch=_spy_closes(102.0),
                    now=dt.datetime(2026, 6, 3, tzinfo=UTC),
                )

            # THEN the summary line reports the exact reused/fetched split.
            self.assertIn(
                "benchmark-excess: enriched 2 (reused 1, fetched 1)",
                cm.output[-1],
            )

    def test_summary_log_excludes_deadline_stopped_file_rows(self) -> None:
        # A file whose sweep is cut off mid-way by the deadline is left UNWRITTEN
        # (retried next run), so its partially-processed rows must NOT be counted
        # in the summary line. Two gap rows in distinct windows; budget allows the
        # first fetch but trips before the second -> stopped_early, file not written
        # -> the log must report enriched 0, not 1.
        from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline

        elapsed = [0.0]

        def spy_fetch(t, s, e):
            elapsed[0] += 60.0
            return _spy_bars(s, reference=100.0, last_close=102.0)

        dead = _RunDeadline(50.0, monotonic=lambda: elapsed[0])

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            d = dt.date(2026, 5, 18)
            path = store / f"{d.isoformat()}.parquet"
            pd.DataFrame(
                [
                    {
                        "brief_date": d,
                        "ticker": "AA",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 27),
                        "forward_return": 0.05,
                    },
                    {
                        "brief_date": d,
                        "ticker": "BB",
                        "terminal": True,
                        "matured_at": dt.date(2026, 5, 28),
                        "forward_return": 0.04,
                    },
                ]
            ).to_parquet(path)

            with self.assertLogs(
                "alphalens_pipeline.feedback.benchmark_excess", level="INFO"
            ) as cm:
                enrich_store_with_benchmark_excess(
                    store,
                    bar_fetch=spy_fetch,
                    grouped_fetch=_spy_closes(102.0),
                    now=dt.datetime(2026, 6, 3, tzinfo=UTC),
                    deadline=dead,
                )

            self.assertIn(
                "benchmark-excess: enriched 0 (reused 0, fetched 0)",
                cm.output[-1],
            )
            # The stopped file was left untouched: the benchmark column was never
            # added (the seeded frame had none, and stopped_early skips the write).
            out = pd.read_parquet(path)
            self.assertNotIn("benchmark_window_return", out.columns)


class TestBenchmarkWindowExitStamp(unittest.TestCase):
    """The pass records WHICH exit session a pair was computed over
    (``benchmark_window_exit``) and reuses the pair only while that still equals
    the row's ``matured_at`` (#1444).

    Why: the 2026-09-11 rebuild computed every window to the rebuild night; when
    ``matured_at`` was repaired the pairs stayed internally consistent
    (``excess == forward - bench``) and were reused forever. Consistency alone
    cannot see a moved window.
    """

    _BRIEF = dt.date(2026, 5, 18)
    _EXIT = dt.date(2026, 5, 27)

    def _store_with(self, tmp: str, **overrides) -> Path:
        store = Path(tmp)
        row = {
            "brief_date": self._BRIEF,
            "ticker": "AA",
            "terminal": True,
            "matured_at": self._EXIT,
            "forward_return": 0.05,
            "benchmark_window_return": 0.02,
            "market_excess_return": 0.03,
            "benchmark_window_exit": self._EXIT.isoformat(),
            "benchmark_leg_version": BENCHMARK_LEG_VERSION,
        }
        row.update(overrides)
        pd.DataFrame([row]).to_parquet(store / f"{self._BRIEF.isoformat()}.parquet")
        return store

    @staticmethod
    def _spy(calls: list[str]):
        def _fetch(t, s, e):
            calls.append(t)
            return _spy_bars(s, reference=100.0, last_close=101.0)  # window return 0.01

        return _fetch

    def _read(self, store: Path) -> pd.Series:
        return pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").iloc[0]

    def test_pair_whose_recorded_exit_matches_matured_at_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with(tmp)
            calls: list[str] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy(calls),
                grouped_fetch=_spy_closes(101.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            self.assertEqual(calls, [])
            self.assertAlmostEqual(
                float(self._read(store)["benchmark_window_return"]), 0.02, places=9
            )

    def test_consistent_pair_whose_recorded_exit_moved_is_recomputed(self) -> None:
        # The #1444 shape: the pair is arithmetically consistent, but it was
        # computed over a window ending on another session than matured_at.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with(tmp, benchmark_window_exit="2026-09-10")
            calls: list[str] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy(calls),
                grouped_fetch=_spy_closes(101.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            self.assertEqual(calls, ["SPY"])
            row = self._read(store)
            self.assertAlmostEqual(float(row["benchmark_window_return"]), 0.01, places=9)
            self.assertAlmostEqual(float(row["market_excess_return"]), 0.04, places=9)
            self.assertEqual(row["benchmark_window_exit"], self._EXIT.isoformat())

    def test_consistent_pair_without_a_recorded_exit_is_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            pd.DataFrame(
                [
                    {
                        "brief_date": self._BRIEF,
                        "ticker": "AA",
                        "terminal": True,
                        "matured_at": self._EXIT,
                        "forward_return": 0.05,
                        "benchmark_window_return": 0.02,
                        "market_excess_return": 0.03,
                    }
                ]
            ).to_parquet(store / f"{self._BRIEF.isoformat()}.parquet")
            calls: list[str] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy(calls),
                grouped_fetch=_spy_closes(101.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            self.assertEqual(calls, ["SPY"])
            self.assertEqual(self._read(store)["benchmark_window_exit"], self._EXIT.isoformat())

    def test_second_run_over_a_stamped_store_fetches_nothing_and_leaves_the_file_alone(
        self,
    ) -> None:
        # Pins that the REUSE branch carries the stamp: if only the fetch branch
        # wrote it, every reused row would lose it and the store would refetch forever.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with(tmp, benchmark_window_exit=None)
            first: list[str] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy(first),
                grouped_fetch=_spy_closes(101.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            self.assertEqual(first, ["SPY"], "precondition: the first run stamps the row")
            path = store / f"{self._BRIEF.isoformat()}.parquet"
            mtime_before = path.stat().st_mtime_ns

            second: list[str] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy(second),
                grouped_fetch=_spy_closes(101.0),
                now=dt.datetime(2026, 6, 4, tzinfo=UTC),
            )

            self.assertEqual(second, [])
            self.assertEqual(path.stat().st_mtime_ns, mtime_before)
            self.assertEqual(self._read(store)["benchmark_window_exit"], self._EXIT.isoformat())

    def test_window_cache_is_not_seeded_from_a_pair_whose_exit_moved(self) -> None:
        # Sibling rows share (arrival, exit). A stale-but-consistent row must not
        # serve its sibling's gap from the cache; the sibling pays a real fetch.
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            pd.DataFrame(
                [
                    {
                        "brief_date": self._BRIEF,
                        "ticker": "AA",
                        "terminal": True,
                        "matured_at": self._EXIT,
                        "forward_return": 0.05,
                        "benchmark_window_return": 0.02,
                        "market_excess_return": 0.03,
                        "benchmark_window_exit": "2026-09-10",
                        "benchmark_leg_version": BENCHMARK_LEG_VERSION,
                    },
                    {
                        "brief_date": self._BRIEF,
                        "ticker": "BB",
                        "terminal": True,
                        "matured_at": self._EXIT,
                        "forward_return": 0.05,
                        "benchmark_window_return": None,
                        "market_excess_return": None,
                        "benchmark_window_exit": None,
                    },
                ]
            ).to_parquet(store / f"{self._BRIEF.isoformat()}.parquet")
            calls: list[str] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy(calls),
                grouped_fetch=_spy_closes(101.0),
                now=dt.datetime(2026, 6, 3, tzinfo=UTC),
            )
            df = pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").set_index("ticker")
            self.assertEqual(calls, ["SPY"])  # one real fetch serves both rows
            self.assertAlmostEqual(float(df.loc["BB", "benchmark_window_return"]), 0.01, places=9)
            self.assertAlmostEqual(float(df.loc["AA", "benchmark_window_return"]), 0.01, places=9)


class TestStoredPairIsSettledIsColumnParametric(unittest.TestCase):
    def test_sector_named_columns(self) -> None:
        from alphalens_pipeline.feedback.benchmark_excess import stored_pair_is_settled

        row = pd.Series(
            {
                "matured_at": dt.date(2026, 5, 27),
                "forward_return": 0.15,
                "sector_etf_window_return": 0.10,
                "sector_excess_return": 0.05,
                "sector_window_exit": "2026-05-27",
            }
        )
        kw = {
            "window_col": "sector_etf_window_return",
            "excess_col": "sector_excess_return",
            "exit_col": "sector_window_exit",
        }
        self.assertTrue(stored_pair_is_settled(row, **kw))
        self.assertFalse(
            stored_pair_is_settled(
                pd.Series({**row.to_dict(), "sector_window_exit": "2026-09-10"}), **kw
            )
        )
        self.assertFalse(
            stored_pair_is_settled(pd.Series({**row.to_dict(), "sector_excess_return": 0.06}), **kw)
        )
        self.assertFalse(
            stored_pair_is_settled(pd.Series({**row.to_dict(), "matured_at": None}), **kw)
        )


class TestSplitInvalidatedRowsGetNoBenchmark(unittest.TestCase):
    """A ``SPLIT_INVALIDATED`` quarantine keeps its raw replay ``forward_return`` as
    telemetry of what tripped the guard; it is not an outcome, so the pass must
    give it no pair — and must not REUSE a pair it once had (#1452).

    The fixture is the production row (MQ, 2026-05-29): a settled, consistent,
    correctly stamped pair that reuse-first would otherwise carry for ever.
    """

    _BRIEF = dt.date(2026, 5, 29)
    _EXIT = dt.date(2026, 9, 10)
    _NOW = dt.datetime(2026, 9, 15, tzinfo=UTC)

    @staticmethod
    def _quarantined(**overrides) -> dict:
        row = {
            "brief_date": TestSplitInvalidatedRowsGetNoBenchmark._BRIEF,
            "ticker": "MQ",
            "terminal": True,
            "ladder_classification": "SPLIT_INVALIDATED",
            "matured_at": TestSplitInvalidatedRowsGetNoBenchmark._EXIT,
            "forward_return": 3.2281,
            "benchmark_window_return": 0.0032,
            "market_excess_return": 3.2249,
            "benchmark_window_exit": TestSplitInvalidatedRowsGetNoBenchmark._EXIT.isoformat(),
            "benchmark_leg_version": BENCHMARK_LEG_VERSION,
        }
        row.update(overrides)
        return row

    @staticmethod
    def _spy(calls: list[str]):
        def _fetch(t, s, e):
            calls.append(t)
            return _spy_bars(s, reference=100.0, last_close=101.0)  # window return 0.01

        return _fetch

    def _run(self, store: Path, rows: list[dict], calls: list[str]) -> Path:
        path = store / f"{self._BRIEF.isoformat()}.parquet"
        pd.DataFrame(rows).to_parquet(path)
        enrich_store_with_benchmark_excess(
            store, bar_fetch=self._spy(calls), grouped_fetch=_spy_closes(101.0), now=self._NOW
        )
        return path

    def test_a_settled_quarantined_pair_is_nulled_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            calls: list[str] = []
            path = self._run(store, [self._quarantined()], calls)
            out = pd.read_parquet(path).iloc[0]
            self.assertEqual(calls, [])
            self.assertTrue(pd.isna(out["market_excess_return"]))
            self.assertTrue(pd.isna(out["benchmark_window_return"]))
            self.assertTrue(pd.isna(out["benchmark_window_exit"]))
            self.assertAlmostEqual(float(out["forward_return"]), 3.2281, places=9)

    def test_a_quarantined_gap_is_not_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            calls: list[str] = []
            path = self._run(
                store,
                [
                    self._quarantined(
                        benchmark_window_return=None,
                        market_excess_return=None,
                        benchmark_window_exit=None,
                    )
                ],
                calls,
            )
            out = pd.read_parquet(path).iloc[0]
            self.assertEqual(calls, [])
            self.assertTrue(pd.isna(out["market_excess_return"]))

    def test_a_gap_sibling_in_the_same_window_still_gets_its_own_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            calls: list[str] = []
            sibling = {
                "brief_date": self._BRIEF,
                "ticker": "BB",
                "terminal": True,
                "ladder_classification": "TP_FULL",
                "matured_at": self._EXIT,
                "forward_return": 0.05,
                "benchmark_window_return": None,
                "market_excess_return": None,
                "benchmark_window_exit": None,
            }
            path = self._run(store, [self._quarantined(), sibling], calls)
            df = pd.read_parquet(path).set_index("ticker")
            self.assertLessEqual(len(calls), 1)
            self.assertAlmostEqual(float(df.loc["BB", "benchmark_window_return"]), 0.01, places=9)
            self.assertAlmostEqual(float(df.loc["BB", "market_excess_return"]), 0.04, places=9)
            self.assertTrue(pd.isna(df.loc["MQ", "market_excess_return"]))

    def test_the_summary_line_counts_the_quarantine_as_fetched_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            calls: list[str] = []
            with self.assertLogs(
                "alphalens_pipeline.feedback.benchmark_excess", level="INFO"
            ) as logs:
                self._run(store, [self._quarantined()], calls)
            self.assertTrue(
                any("enriched 0 (reused 0, fetched 1)" in line for line in logs.output),
                logs.output,
            )

    def test_row_is_quarantined_predicate(self) -> None:
        from alphalens_pipeline.feedback.benchmark_excess import row_is_quarantined

        self.assertTrue(row_is_quarantined({"ladder_classification": "SPLIT_INVALIDATED"}))
        self.assertTrue(
            row_is_quarantined(pd.Series({"ladder_classification": "SPLIT_INVALIDATED"}))
        )
        for value in (None, "", float("nan"), "NO_FILL", "TP_FULL"):
            with self.subTest(value=value):
                self.assertFalse(row_is_quarantined({"ladder_classification": value}))
                self.assertFalse(row_is_quarantined(pd.Series({"ladder_classification": value})))
        self.assertFalse(row_is_quarantined({}))
        self.assertFalse(row_is_quarantined(pd.Series({"ticker": "MQ"})))


def _grouped(closes: dict[dt.date, dict[str, float]], *, calls: list[dt.date] | None = None):
    """A grouped-daily stub: session -> {TICKER: {"c": close}}; empty for unknown sessions."""

    def _fetch(session: dt.date) -> dict[str, dict[str, float]]:
        if calls is not None:
            calls.append(session)
        return {t: {"c": c} for t, c in closes.get(session, {}).items()}

    return _fetch


class TestOfficialCloseExitPrint(unittest.TestCase):
    """The benchmark leg ends at the OFFICIAL close of the exit session, read from
    the monitor's grouped-daily cache — the same print the candidate leg uses
    since #1444 — and the minute fetch covers only the arrival VWAP window (#1445).
    """

    _BRIEF = dt.date(2026, 5, 18)
    _ARRIVAL = ladder_arrival_session(dt.date(2026, 5, 18))  # 2026-05-19
    _EXIT = dt.date(2026, 5, 27)
    _EXIT_2 = dt.date(2026, 5, 28)
    _NOW = dt.datetime(2026, 6, 3, tzinfo=UTC)

    @staticmethod
    def _row(ticker: str, matured_at: dt.date | None, forward: float = 0.05) -> dict:
        return {
            "brief_date": TestOfficialCloseExitPrint._BRIEF,
            "ticker": ticker,
            "terminal": matured_at is not None,
            "matured_at": matured_at,
            "forward_return": forward,
        }

    @staticmethod
    def _spy(calls: list[tuple[dt.datetime, dt.datetime]], *, reference: float = 100.0):
        def _fetch(t, s, e):
            calls.append((s, e))
            # A 17:30-style after-hours print that must NEVER be the exit leg.
            return _spy_bars(s, reference=reference, last_close=999.0)

        return _fetch

    def _store(self, tmp: str, rows: list[dict]) -> Path:
        store = Path(tmp)
        pd.DataFrame(rows).to_parquet(store / f"{self._BRIEF.isoformat()}.parquet")
        return store

    def test_minute_fetch_covers_only_the_arrival_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, [self._row("AA", self._EXIT)])
            calls: list[tuple[dt.datetime, dt.datetime]] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy(calls),
                grouped_fetch=_grouped({self._EXIT: {"SPY": 102.0}}),
                now=self._NOW,
            )
            arrival_open = session_open_utc(self._ARRIVAL)
            self.assertEqual(
                calls,
                [(arrival_open, arrival_open + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN))],
            )

    def test_exit_leg_is_the_official_close_not_the_last_minute_bar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, [self._row("AA", self._EXIT)])
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy([]),
                grouped_fetch=_grouped({self._EXIT: {"SPY": 102.0}}),
                now=self._NOW,
            )
            out = pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(out["benchmark_window_return"]), 0.02, places=9)
            self.assertAlmostEqual(float(out["market_excess_return"]), 0.03, places=9)

    def test_rows_sharing_an_arrival_pay_one_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, [self._row("AA", self._EXIT), self._row("BB", self._EXIT_2)])
            calls: list[tuple[dt.datetime, dt.datetime]] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy(calls),
                grouped_fetch=_grouped({self._EXIT: {"SPY": 102.0}, self._EXIT_2: {"SPY": 104.0}}),
                now=self._NOW,
            )
            df = pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").set_index("ticker")
            self.assertEqual(len(calls), 1)
            self.assertAlmostEqual(float(df.loc["AA", "benchmark_window_return"]), 0.02, places=9)
            self.assertAlmostEqual(float(df.loc["BB", "benchmark_window_return"]), 0.04, places=9)

    def test_missing_official_close_leaves_none_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, [self._row("AA", self._EXIT)])
            with self.assertLogs("alphalens_pipeline.feedback.benchmark_excess", "WARNING") as logs:
                enrich_store_with_benchmark_excess(
                    store,
                    bar_fetch=self._spy([]),
                    grouped_fetch=_grouped({}),  # no session payload at all
                    now=self._NOW,
                )
            out = pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").iloc[0]
            self.assertTrue(pd.isna(out["benchmark_window_return"]))
            self.assertTrue(pd.isna(out["market_excess_return"]))
            self.assertTrue(any("official close" in line for line in logs.output), logs.output)

    def test_a_missing_close_warns_once_per_session_not_per_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = [self._row(t, self._EXIT) for t in ("AA", "BB", "CC")]
            store = self._store(tmp, rows)
            with self.assertLogs("alphalens_pipeline.feedback.benchmark_excess", "WARNING") as logs:
                enrich_store_with_benchmark_excess(
                    store, bar_fetch=self._spy([]), grouped_fetch=_grouped({}), now=self._NOW
                )
            self.assertEqual(sum("official close" in line for line in logs.output), 1, logs.output)

    def test_session_without_a_spy_row_yields_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, [self._row("AA", self._EXIT)])
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy([]),
                grouped_fetch=_grouped({self._EXIT: {"AAPL": 190.0}}),
                now=self._NOW,
            )
            out = pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").iloc[0]
            self.assertTrue(pd.isna(out["market_excess_return"]))

    def test_grouped_map_is_read_from_disk_before_fetching(self) -> None:
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            _write_grouped_cache_atomic,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, [self._row("AA", self._EXIT), self._row("BB", self._EXIT_2)])
            _write_grouped_cache_atomic(store, self._EXIT, {"SPY": {"c": 102.0}})
            grouped_calls: list[dt.date] = []
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy([]),
                grouped_fetch=_grouped({self._EXIT_2: {"SPY": 104.0}}, calls=grouped_calls),
                now=self._NOW,
            )
            # The cached session was never fetched; the missing one was fetched once
            # and is now on disk for the next run.
            self.assertEqual(grouped_calls, [self._EXIT_2])
            self.assertTrue((store / "grouped" / f"{self._EXIT_2.isoformat()}.parquet").exists())
            df = pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").set_index("ticker")
            self.assertAlmostEqual(float(df.loc["AA", "benchmark_window_return"]), 0.02, places=9)

    def test_ongoing_row_uses_the_last_closed_sessions_official_close(self) -> None:
        from alphalens_pipeline.paper.calendar import previous_trading_day

        last_closed = previous_trading_day(self._NOW.date())
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, [self._row("AA", None, forward=0.03)])
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy([]),
                grouped_fetch=_grouped({last_closed: {"SPY": 101.0}}),
                now=self._NOW,
            )
            out = pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").iloc[0]
            self.assertAlmostEqual(float(out["benchmark_window_return"]), 0.01, places=9)
            self.assertAlmostEqual(float(out["market_excess_return"]), 0.02, places=9)

    def test_a_zero_official_close_is_refused_not_divided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, [self._row("AA", self._EXIT)])
            enrich_store_with_benchmark_excess(
                store,
                bar_fetch=self._spy([]),
                grouped_fetch=_grouped({self._EXIT: {"SPY": 0.0}}),
                now=self._NOW,
            )
            out = pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet").iloc[0]
            self.assertTrue(pd.isna(out["benchmark_window_return"]))


class TestBenchmarkLegVersion(unittest.TestCase):
    """A settled pair is reused only under the CURRENT leg convention
    (``benchmark_leg_version``); the exit-print change (#1445) recomputes the
    whole store through this gate, with no operator step."""

    _BRIEF = dt.date(2026, 5, 18)
    _EXIT = dt.date(2026, 5, 27)
    _NOW = dt.datetime(2026, 6, 3, tzinfo=UTC)

    def _settled(self, **overrides) -> dict:
        from alphalens_pipeline.feedback.benchmark_excess import BENCHMARK_LEG_VERSION

        row = {
            "brief_date": self._BRIEF,
            "ticker": "AA",
            "terminal": True,
            "matured_at": self._EXIT,
            "forward_return": 0.05,
            "benchmark_window_return": 0.02,
            "market_excess_return": 0.03,
            "benchmark_window_exit": self._EXIT.isoformat(),
            "benchmark_leg_version": BENCHMARK_LEG_VERSION,
        }
        row.update(overrides)
        return row

    def _run(self, tmp: str, rows: list[dict], calls: list[str]) -> pd.DataFrame:
        store = Path(tmp)
        pd.DataFrame(rows).to_parquet(store / f"{self._BRIEF.isoformat()}.parquet")

        def _fetch(t, s, e):
            calls.append(t)
            return _spy_bars(s, reference=100.0, last_close=999.0)

        enrich_store_with_benchmark_excess(
            store,
            bar_fetch=_fetch,
            grouped_fetch=_grouped({self._EXIT: {"SPY": 104.0}}),
            now=self._NOW,
        )
        return pd.read_parquet(store / f"{self._BRIEF.isoformat()}.parquet")

    def test_current_version_pair_is_reused_with_no_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []
            out = self._run(tmp, [self._settled()], calls).iloc[0]
            self.assertEqual(calls, [])
            self.assertAlmostEqual(float(out["benchmark_window_return"]), 0.02, places=9)

    def test_pair_without_the_version_column_is_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            row = self._settled()
            del row["benchmark_leg_version"]
            calls: list[str] = []
            out = self._run(tmp, [row], calls).iloc[0]
            self.assertEqual(calls, ["SPY"])
            self.assertAlmostEqual(float(out["benchmark_window_return"]), 0.04, places=9)

    def test_pair_under_another_version_is_recomputed_and_relabelled(self) -> None:
        from alphalens_pipeline.feedback.benchmark_excess import BENCHMARK_LEG_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []
            out = self._run(
                tmp, [self._settled(benchmark_leg_version="spy-v1-last-bar")], calls
            ).iloc[0]
            self.assertEqual(calls, ["SPY"])
            self.assertAlmostEqual(float(out["benchmark_window_return"]), 0.04, places=9)
            self.assertEqual(out["benchmark_leg_version"], BENCHMARK_LEG_VERSION)

    def test_version_is_written_on_every_row_including_ongoing(self) -> None:
        from alphalens_pipeline.feedback.benchmark_excess import (
            BENCHMARK_COLUMNS,
            BENCHMARK_LEG_VERSION,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ongoing = {
                "brief_date": self._BRIEF,
                "ticker": "BB",
                "terminal": False,
                "matured_at": None,
                "forward_return": 0.01,
            }
            out = self._run(tmp, [self._settled(), ongoing], [])
            self.assertIn("benchmark_leg_version", BENCHMARK_COLUMNS)
            self.assertTrue((out["benchmark_leg_version"] == BENCHMARK_LEG_VERSION).all())

    def test_reused_row_seeds_the_anchor_for_a_gap_sibling(self) -> None:
        # The settled row implies reference = close / (1 + window) = 104 / 1.02;
        # its sibling with the same arrival pays no fetch and gets its own exit.
        with tempfile.TemporaryDirectory() as tmp:
            sibling = {
                "brief_date": self._BRIEF,
                "ticker": "BB",
                "terminal": True,
                "matured_at": self._EXIT,
                "forward_return": 0.05,
            }
            calls: list[str] = []
            settled = self._settled(benchmark_window_return=0.04, market_excess_return=0.01)
            out = self._run(tmp, [settled, sibling], calls).set_index("ticker")
            self.assertEqual(calls, [])
            self.assertAlmostEqual(float(out.loc["BB", "benchmark_window_return"]), 0.04, places=9)


if __name__ == "__main__":
    unittest.main()

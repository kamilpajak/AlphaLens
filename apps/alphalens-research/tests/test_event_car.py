"""Tests for the event-anchored outcome pass (epic #1293, #1297)."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_pipeline.events import insider_cluster as ic
from alphalens_pipeline.feedback import event_car as ec
from alphalens_pipeline.feedback.population_ladder_monitor import _write_grouped_cache_atomic
from alphalens_pipeline.paper.calendar import advance_trading_sessions
from alphalens_research.diagnostics import insider_cluster_retro as icr

D = dt.date
ARRIVAL = D(2026, 3, 4)  # Wednesday
NOW = dt.datetime(2026, 6, 1, 7, 0, tzinfo=dt.UTC)  # everything matured


def _sessions(n: int, start: dt.date = ARRIVAL) -> list[dt.date]:
    return [advance_trading_sessions(start, i) for i in range(n)]


def _grouped(
    sessions, stock: dict[dt.date, tuple[float, float]], spy: tuple[float, float] = (100.0, 102.0)
):
    """{session: {TICKER: {o, c}, SPY: {o, c}}} with a linear SPY path."""
    out = {}
    n = len(sessions)
    for i, s in enumerate(sessions):
        spy_c = spy[0] + (spy[1] - spy[0]) * i / max(n - 1, 1)
        day = {"SPY": {"o": spy[0], "c": spy_c}}
        if s in stock:
            o, c = stock[s]
            day["AAA"] = {"o": o, "c": c}
        out[s] = day
    return out


class TestEventCarFromGrouped(unittest.TestCase):
    def test_car_20_is_open_to_close_minus_spy(self):
        sessions = _sessions(20)
        stock = {s: (10.0, 10.0 if i < 19 else 11.0) for i, s in enumerate(sessions)}
        grouped = _grouped(sessions, stock, spy=(100.0, 102.0))
        car = ec.event_car_from_grouped(grouped, "AAA", arrival=ARRIVAL, horizon_sessions=19)
        self.assertAlmostEqual(car, 0.10 - 0.02, places=9)

    def test_car_40_uses_arrival_plus_39_sessions(self):
        sessions = _sessions(40)
        stock = {s: (10.0, 10.0 if i < 39 else 12.0) for i, s in enumerate(sessions)}
        grouped = _grouped(sessions, stock, spy=(100.0, 100.0))
        self.assertAlmostEqual(
            ec.event_car_from_grouped(grouped, "AAA", arrival=ARRIVAL, horizon_sessions=39), 0.20
        )
        # one session short -> None
        self.assertIsNone(
            ec.event_car_from_grouped(
                _grouped(sessions[:-1], stock), "AAA", arrival=ARRIVAL, horizon_sessions=39
            )
        )

    def test_missing_stock_or_spy_session_yields_none(self):
        sessions = _sessions(20)
        stock = dict.fromkeys(sessions, (10.0, 10.0))
        del stock[sessions[5]]
        self.assertIsNone(
            ec.event_car_from_grouped(
                _grouped(sessions, stock), "AAA", arrival=ARRIVAL, horizon_sessions=19
            )
        )
        grouped = _grouped(sessions, dict.fromkeys(sessions, (10.0, 10.0)))
        del grouped[sessions[-1]]["SPY"]
        self.assertIsNone(
            ec.event_car_from_grouped(grouped, "AAA", arrival=ARRIVAL, horizon_sessions=19)
        )

    def test_split_guard_nulls_the_horizon(self):
        sessions = _sessions(20)
        stock = {s: (10.0, 10.0 if i < 10 else 4.0) for i, s in enumerate(sessions)}  # 0.4 ratio
        self.assertIsNone(
            ec.event_car_from_grouped(
                _grouped(sessions, stock), "AAA", arrival=ARRIVAL, horizon_sessions=19
            )
        )

    def test_matches_the_research_helper_on_identical_series(self):
        sessions = _sessions(40)
        rng = np.random.default_rng(0)
        closes = 10.0 * np.cumprod(1 + rng.normal(0, 0.02, 40))
        stock = {s: (float(closes[i]) * 0.995, float(closes[i])) for i, s in enumerate(sessions)}
        grouped = _grouped(sessions, stock, spy=(100.0, 101.0))
        idx = pd.DatetimeIndex([pd.Timestamp(s) for s in sessions])
        stock_df = pd.DataFrame(
            {"open": [stock[s][0] for s in sessions], "close": [stock[s][1] for s in sessions]},
            index=idx,
        )
        bench_df = pd.DataFrame(
            {
                "open": [grouped[s]["SPY"]["o"] for s in sessions],
                "close": [grouped[s]["SPY"]["c"] for s in sessions],
            },
            index=idx,
        )
        for h in (19, 39):
            self.assertAlmostEqual(
                ec.event_car_from_grouped(grouped, "AAA", arrival=ARRIVAL, horizon_sessions=h),
                icr.event_car(stock_df, bench_df, arrival=ARRIVAL, horizon_sessions=h),
                places=12,
            )
        self.assertEqual(
            (ic.SPLIT_RATIO_LO, ic.SPLIT_RATIO_HI), (icr.SPLIT_RATIO_LO, icr.SPLIT_RATIO_HI)
        )
        self.assertEqual(ic.HORIZON_SESSIONS_PRIMARY, icr.HORIZON_SESSIONS_PRIMARY)
        self.assertEqual(ic.HORIZON_SESSIONS_SECONDARY, icr.HORIZON_SESSIONS_SECONDARY)


class TestIsEventRow(unittest.TestCase):
    def test_source_or_overlap_marks_an_event_row(self):
        self.assertTrue(ec.is_event_row({"source": "insider_cluster"}))
        self.assertTrue(ec.is_event_row({"source": "thematic", "event_overlap": True}))
        self.assertTrue(ec.is_event_row({"source": "thematic", "event_overlap": np.bool_(True)}))
        self.assertFalse(ec.is_event_row({"source": "thematic", "event_overlap": False}))
        self.assertFalse(ec.is_event_row({"source": "thematic", "event_overlap": float("nan")}))
        self.assertFalse(ec.is_event_row({"source": None}))
        self.assertFalse(ec.is_event_row({}))


class _StoreBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = Path(self._td.name) / "population_ladders"
        self.store.mkdir()
        self.fetch_calls: list[dt.date] = []

    def tearDown(self):
        self._td.cleanup()

    def _fetch(self, session: dt.date):
        self.fetch_calls.append(session)
        return {}

    def _seed_cache(self, sessions, stock):
        for s, day in _grouped(sessions, stock).items():
            _write_grouped_cache_atomic(self.store, s, day)

    def _write_rows(self, rows: list[dict], brief_date: dt.date = ARRIVAL) -> Path:
        path = self.store / f"{brief_date.isoformat()}.parquet"
        pd.DataFrame(rows).to_parquet(path)
        return path

    @staticmethod
    def _row(ticker: str, *, source: str, overlap: bool = False) -> dict:
        return {
            "brief_date": ARRIVAL,
            "ticker": ticker,
            "plannable": True,
            "terminal": False,
            "source": source,
            "event_overlap": overlap,
        }


class TestEnrichStore(_StoreBase):
    def test_event_rows_get_both_horizons_and_thematic_rows_stay_null(self):
        sessions = _sessions(40)
        self._seed_cache(
            sessions, {s: (10.0, 10.0 if i < 39 else 12.0) for i, s in enumerate(sessions)}
        )
        path = self._write_rows(
            [self._row("AAA", source="insider_cluster"), self._row("QUBT", source="thematic")]
        )
        n = ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=NOW)
        df = pd.read_parquet(path).set_index("ticker")
        self.assertEqual(n, 1)
        # flat stock over 20 sessions vs SPY at +2*19/39 % by session 19 (linear path)
        self.assertAlmostEqual(df.loc["AAA", "car_20_event"], -(2.0 * 19 / 39) / 100.0, places=9)
        self.assertTrue(np.isfinite(df.loc["AAA", "car_20_event"]))
        self.assertAlmostEqual(df.loc["AAA", "car_40_event"], 0.20 - 0.02, places=9)
        self.assertEqual(df.loc["AAA", "event_car_version"], ec.EVENT_CAR_VERSION)
        self.assertTrue(pd.isna(df.loc["QUBT", "car_20_event"]))
        self.assertTrue(pd.isna(df.loc["QUBT", "car_40_event"]))
        self.assertTrue(pd.isna(df.loc["QUBT", "event_car_version"]))
        self.assertEqual(self.fetch_calls, [])  # everything came from the cache

    def test_overlap_thematic_row_is_scored(self):
        sessions = _sessions(40)
        self._seed_cache(sessions, dict.fromkeys(sessions, (10.0, 10.0)))
        path = self._write_rows([self._row("AAA", source="thematic", overlap=True)])
        ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=NOW)
        df = pd.read_parquet(path)
        self.assertTrue(np.isfinite(df.loc[0, "car_20_event"]))

    def test_immature_row_car_20_none_until_arrival_plus_19_closed(self):
        sessions = _sessions(40)
        self._seed_cache(sessions, dict.fromkeys(sessions, (10.0, 10.0)))
        path = self._write_rows([self._row("AAA", source="insider_cluster")])
        early = dt.datetime.combine(
            advance_trading_sessions(ARRIVAL, 10), dt.time(7), tzinfo=dt.UTC
        )
        ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=early)
        df = pd.read_parquet(path)
        self.assertTrue(pd.isna(df.loc[0, "car_20_event"]))
        self.assertTrue(pd.isna(df.loc[0, "car_40_event"]))
        mid = dt.datetime.combine(advance_trading_sessions(ARRIVAL, 20), dt.time(7), tzinfo=dt.UTC)
        ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=mid)
        df = pd.read_parquet(path)
        self.assertTrue(np.isfinite(df.loc[0, "car_20_event"]))
        self.assertTrue(pd.isna(df.loc[0, "car_40_event"]))

    def test_columns_exist_even_when_no_event_rows(self):
        path = self._write_rows([self._row("QUBT", source="thematic")])
        n = ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=NOW)
        df = pd.read_parquet(path)
        self.assertEqual(n, 0)
        for col in ec.EVENT_CAR_COLUMNS:
            self.assertIn(col, df.columns)

    def test_idempotent_and_no_rewrite_when_settled(self):
        sessions = _sessions(40)
        self._seed_cache(sessions, dict.fromkeys(sessions, (10.0, 10.0)))
        path = self._write_rows([self._row("AAA", source="insider_cluster")])
        ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=NOW)
        first = pd.read_parquet(path)
        mtime = path.stat().st_mtime_ns
        ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=NOW)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        pd.testing.assert_frame_equal(pd.read_parquet(path), first)

    def test_fetches_only_uncached_sessions(self):
        sessions = _sessions(40)
        self._seed_cache(sessions[:30], dict.fromkeys(sessions, (10.0, 10.0)))
        self._write_rows([self._row("AAA", source="insider_cluster")])
        ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=NOW)
        self.assertEqual(self.fetch_calls, sessions[30:])

    def test_deadline_stops_before_the_next_file(self):
        class Deadline:
            def __init__(self):
                self.calls = 0

            def should_stop(self):
                self.calls += 1
                return True

        sessions = _sessions(40)
        self._seed_cache(sessions, dict.fromkeys(sessions, (10.0, 10.0)))
        path = self._write_rows([self._row("AAA", source="insider_cluster")])
        before = path.stat().st_mtime_ns
        n = ec.enrich_store_with_event_car(
            self.store, grouped_fetch=self._fetch, now=NOW, deadline=Deadline()
        )
        self.assertEqual(n, 0)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_bad_parquet_is_skipped(self):
        (self.store / "2026-03-03.parquet").write_bytes(b"not a parquet")
        sessions = _sessions(40)
        self._seed_cache(sessions, dict.fromkeys(sessions, (10.0, 10.0)))
        path = self._write_rows([self._row("AAA", source="insider_cluster")])
        n = ec.enrich_store_with_event_car(self.store, grouped_fetch=self._fetch, now=NOW)
        self.assertEqual(n, 1)
        self.assertTrue(np.isfinite(pd.read_parquet(path).loc[0, "car_20_event"]))


if __name__ == "__main__":
    unittest.main()

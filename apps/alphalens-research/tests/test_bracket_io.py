"""The IO paths of the bracket-cost scripts, on temporary directories.

These are the functions that actually ran against the production stores. They
were the least-tested part of the work and the one that produced its worst
failure — a guard reading invented keys reported 413 of 413 rows without
structure, which no unit test could catch because none of them touched the
loaders.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from scripts import read_bracket_cost as read_cost
from scripts import replay_bracket_arms as replay_arms

_PRODUCTION_LADDERS = Path.home() / ".alphalens" / "population_ladders"


def _bars(bars: int = 300, start: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(20260822)
    close = start * np.cumprod(1.0 + rng.normal(0.0005, 0.02, bars))
    span = np.abs(rng.normal(0.0, 0.015, bars)) * close
    return pd.DataFrame(
        {
            "open": close - span / 3,
            "high": close + span,
            "low": close - span,
            "close": close,
            "volume": np.full(bars, 2_000_000.0),
        },
        index=pd.date_range("2025-06-02", periods=bars, freq="B"),
    )


class TestLoadFunnel(unittest.TestCase):
    def test_recovers_asof_from_the_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            pd.DataFrame({"ticker": ["AAA"], "bracket_verdict": ["too_big"]}).to_parquet(
                d / "2026-08-06.parquet"
            )
            pd.DataFrame({"ticker": ["BBB"], "bracket_verdict": ["in_bracket"]}).to_parquet(
                d / "2026-08-07.parquet"
            )

            out = replay_arms.load_funnel(d)

            self.assertEqual(len(out), 2)
            self.assertEqual(sorted(out["asof"]), [dt.date(2026, 8, 6), dt.date(2026, 8, 7)])

    def test_empty_directory_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                replay_arms.load_funnel(Path(tmp))


class TestLoadGroupedOhlcv(unittest.TestCase):
    def _write_grouped(self, d: Path, days: int = 5) -> None:
        for i in range(days):
            day = dt.date(2026, 8, 3) + dt.timedelta(days=i)
            ts = int(pd.Timestamp(day).timestamp() * 1000)
            pd.DataFrame(
                {
                    "T": ["AAA", "BBB", "ZZZ"],
                    "t": [ts] * 3,
                    "o": [10.0, 20.0, 30.0],
                    "h": [11.0, 21.0, 31.0],
                    "l": [9.0, 19.0, 29.0],
                    "c": [10.5, 20.5, 30.5],
                    "v": [1e6] * 3,
                }
            ).to_parquet(d / f"{day.isoformat()}.parquet")

    def test_returns_only_the_wanted_tickers_with_a_date_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write_grouped(d)

            out = replay_arms.load_grouped_ohlcv({"aaa", "BBB"}, grouped_dir=d)

            self.assertEqual(sorted(out), ["AAA", "BBB"])
            self.assertEqual(list(out["AAA"].columns), ["open", "high", "low", "close", "volume"])
            self.assertEqual(len(out["AAA"]), 5)
            self.assertTrue(out["AAA"].index.is_monotonic_increasing)

    def test_missing_directory_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = replay_arms.load_grouped_ohlcv({"AAA"}, grouped_dir=Path(tmp) / "absent")

            self.assertEqual(out, {})


class TestBuildSetups(unittest.TestCase):
    def test_cuts_history_at_asof_so_no_future_bar_reaches_the_builder(self):
        frame = _bars()
        asof = frame.index[200].date()
        rows = pd.DataFrame({"ticker": ["AAA"], "asof": [asof]})

        setups, no_structure = replay_arms.build_setups(rows, {"AAA": frame})

        self.assertIn((asof, "AAA"), setups)
        self.assertEqual(no_structure, 0)
        # The setup's anchor close must be the asof bar, never a later one.
        self.assertAlmostEqual(
            setups[(asof, "AAA")]["asof_close"], float(frame["close"].iloc[200]), places=6
        )

    def test_ticker_without_bars_is_skipped_not_counted_as_no_structure(self):
        rows = pd.DataFrame({"ticker": ["MISSING"], "asof": [dt.date(2026, 8, 6)]})

        setups, no_structure = replay_arms.build_setups(rows, {})

        self.assertEqual(setups, {})
        self.assertEqual(no_structure, 0)

    def test_too_short_history_counts_as_no_structure(self):
        short = _bars(bars=3)
        rows = pd.DataFrame({"ticker": ["AAA"], "asof": [short.index[-1].date()]})

        setups, no_structure = replay_arms.build_setups(rows, {"AAA": short})

        self.assertEqual(setups, {})
        self.assertEqual(no_structure, 1)


class TestPrepare(unittest.TestCase):
    def test_writes_one_brief_per_day_and_balances_attrition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            funnel, grouped, briefs = root / "f", root / "g", root / "b"
            for p in (funnel, grouped, briefs):
                p.mkdir()

            frame = _bars()
            asof = frame.index[250].date()
            pd.DataFrame(
                {
                    "ticker": ["AAA", "NOBARS"],
                    "theme": ["t1", "t2"],
                    "bracket_verdict": ["too_big", "in_bracket"],
                    "market_cap": [50e9, 3e9],
                }
            ).to_parquet(funnel / f"{asof.isoformat()}.parquet")
            for ts in frame.index:
                pd.DataFrame(
                    {
                        "T": ["AAA"],
                        "t": [int(ts.timestamp() * 1000)],
                        "o": [float(frame.at[ts, "open"])],
                        "h": [float(frame.at[ts, "high"])],
                        "l": [float(frame.at[ts, "low"])],
                        "c": [float(frame.at[ts, "close"])],
                        "v": [1e6],
                    }
                ).to_parquet(grouped / f"{ts.date().isoformat()}.parquet")

            original = replay_arms.GROUPED_DIR
            replay_arms.GROUPED_DIR = grouped
            try:
                att = replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs)
            finally:
                replay_arms.GROUPED_DIR = original

            self.assertTrue(att.balanced())
            self.assertEqual(att.in_scope, 2)
            self.assertEqual(att.no_bars, 1)
            written = pd.read_parquet(briefs / f"{asof.isoformat()}.parquet")
            self.assertEqual(list(written["ticker"]), ["AAA"])
            self.assertTrue(bool(written["verified"].iloc[0]))
            self.assertTrue(json.loads(written["brief_trade_setup"].iloc[0])["entry_tiers"])


class TestProductionStoreGuards(unittest.TestCase):
    """Both write paths must refuse the production ladder store by name."""

    def test_replay_refuses_the_production_store(self):
        with self.assertRaises(SystemExit):
            replay_arms.replay(briefs_dir=Path("/nonexistent"), store_dir=_PRODUCTION_LADDERS)

    def test_benchmark_refuses_the_production_store(self):
        with self.assertRaises(SystemExit):
            replay_arms.benchmark(store_dir=_PRODUCTION_LADDERS)


class TestReadLoaders(unittest.TestCase):
    def test_load_replayed_joins_arms_onto_ladder_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, briefs = root / "s", root / "b"
            store.mkdir()
            briefs.mkdir()
            pd.DataFrame(
                {
                    "brief_date": [dt.date(2026, 8, 6)],
                    "ticker": ["AAA"],
                    "terminal": [True],
                    "realized_r": [0.5],
                    "ladder_classification": ["TP_FULL"],
                }
            ).to_parquet(store / "2026-08-06.parquet")
            pd.DataFrame(
                {
                    "ticker": ["AAA"],
                    "arm": [read_cost.ARM_DISCARDED],
                    "market_cap": [50e9],
                    "theme": ["t1"],
                }
            ).to_parquet(briefs / "2026-08-06.parquet")

            out = read_cost.load_replayed(store, briefs)

            self.assertEqual(out["arm"].iloc[0], read_cost.ARM_DISCARDED)
            self.assertEqual(out["market_cap"].iloc[0], 50e9)

    def test_load_replayed_without_a_store_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                read_cost.load_replayed(Path(tmp), Path(tmp))

    def test_load_funnel_absent_returns_an_empty_frame_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = read_cost._load_funnel(Path(tmp))

            self.assertTrue(out.empty)
            self.assertEqual(read_cost.excluded_verdict_counts(out), {"too_small": 0, "no_mcap": 0})

    def test_load_production_absent_returns_an_empty_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = read_cost._load_production(Path(tmp))

            self.assertTrue(out.empty)
            self.assertIn("ladder_classification", out.columns)


class TestReportShape(unittest.TestCase):
    def test_report_covers_every_contract_block(self):
        frame = pd.DataFrame(
            {
                "arm": [read_cost.ARM_DISCARDED, read_cost.ARM_KEPT],
                "ticker": ["AAA", "BBB"],
                "theme": ["t1", "t1"],
                "brief_date": [dt.date(2026, 8, 6)] * 2,
                "terminal": [True, False],
                "market_cap": [60e9, 3e9],
                "realized_r": [0.5, None],
                "ladder_classification": ["TP_FULL", "OPEN"],
                "market_excess_return": [0.01, None],
            }
        )

        out = read_cost.report(frame)

        for block in (
            "primary",
            "attrition",
            "by_arm",
            "mega_split",
            "prompt_change_strata",
            "by_theme",
            "positive_control",
        ):
            self.assertIn(block, out)
        self.assertEqual(out["attrition"]["rows"], 2)
        self.assertEqual(out["attrition"]["ongoing"], 1)


if __name__ == "__main__":
    unittest.main()

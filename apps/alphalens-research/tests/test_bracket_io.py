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
from types import SimpleNamespace
from typing import Any

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
    def test_reads_the_injected_grouped_dir_not_the_real_store(self):
        """The first version reassigned a module global, which a default
        argument had already captured. It therefore read the developer's real
        grouped store, found a real ticker called AAA, and passed on that
        machine while failing on CI, where no store exists."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            funnel, grouped, briefs = root / "f", root / "g", root / "b"
            for d in (funnel, grouped, briefs):
                d.mkdir()
            pd.DataFrame(
                {
                    "ticker": ["AAA"],
                    "theme": ["t1"],
                    "bracket_verdict": ["too_big"],
                    "market_cap": [50e9],
                }
            ).to_parquet(funnel / "2026-08-06.parquet")

            att = replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)

            self.assertEqual(att.no_bars, 1)
            self.assertEqual(att.ongoing, 0)

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

            att = replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)

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


class TestPrepareFreezesGeometry(unittest.TestCase):
    """A scheduled prepare must not re-derive a day it already wrote.

    The grouped daily store is SPLIT-ADJUSTED and retro-adjusts history when a
    split happens, so re-deriving a setup weeks later can move every level of a
    ladder that is already being replayed. The proposal's geometry is fixed at
    the proposal date or the measurement drifts under its own feet.
    """

    def _fixture(self, root: Path):
        funnel, grouped, briefs = root / "f", root / "g", root / "b"
        for d in (funnel, grouped, briefs):
            d.mkdir()
        frame = _bars()
        asof = frame.index[250].date()
        pd.DataFrame(
            {
                "ticker": ["AAA"],
                "theme": ["t1"],
                "bracket_verdict": ["too_big"],
                "market_cap": [50e9],
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
        return funnel, grouped, briefs, asof

    def test_existing_day_is_not_rewritten_even_if_prices_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            funnel, grouped, briefs, asof = self._fixture(Path(tmp))
            replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)
            first = pd.read_parquet(briefs / f"{asof.isoformat()}.parquet")

            # Simulate a retro-adjustment: every historical close halves.
            for path in grouped.glob("*.parquet"):
                day = pd.read_parquet(path)
                for col in ("o", "h", "l", "c"):
                    day[col] = day[col] / 2.0
                day.to_parquet(path)

            replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)
            second = pd.read_parquet(briefs / f"{asof.isoformat()}.parquet")

            self.assertEqual(
                first["brief_trade_setup"].iloc[0], second["brief_trade_setup"].iloc[0]
            )

    def test_rebuild_flag_reopens_a_frozen_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            funnel, grouped, briefs, asof = self._fixture(Path(tmp))
            replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)
            first = pd.read_parquet(briefs / f"{asof.isoformat()}.parquet")
            for path in grouped.glob("*.parquet"):
                day = pd.read_parquet(path)
                for col in ("o", "h", "l", "c"):
                    day[col] = day[col] / 2.0
                day.to_parquet(path)

            replay_arms.prepare(
                funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped, rebuild=True
            )
            second = pd.read_parquet(briefs / f"{asof.isoformat()}.parquet")

            self.assertNotEqual(
                first["brief_trade_setup"].iloc[0], second["brief_trade_setup"].iloc[0]
            )

    def test_a_new_day_is_still_written_alongside_frozen_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            funnel, grouped, briefs, asof = self._fixture(Path(tmp))
            replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)
            later = asof + dt.timedelta(days=1)
            pd.DataFrame(
                {
                    "ticker": ["AAA"],
                    "theme": ["t1"],
                    "bracket_verdict": ["in_bracket"],
                    "market_cap": [3e9],
                }
            ).to_parquet(funnel / f"{later.isoformat()}.parquet")

            replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)

            self.assertTrue((briefs / f"{later.isoformat()}.parquet").exists())

    def test_a_row_added_to_a_frozen_day_still_joins(self):
        """The funnel for an asof is rewritten AFTER that date — measured on the
        VPS, where 2026-08-18's file was last modified on 2026-08-20. A
        day-level freeze would strand every row that arrives late, so the freeze
        is per ROW: existing rows keep their geometry byte-for-byte, new ones
        join with a setup built now.
        """
        with tempfile.TemporaryDirectory() as tmp:
            funnel, grouped, briefs, asof = self._fixture(Path(tmp))
            replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)
            first = pd.read_parquet(briefs / f"{asof.isoformat()}.parquet")

            # Same asof gains a second proposal, the way a later slot would add
            # one, and every historical close halves under a retro-adjustment.
            pd.DataFrame(
                {
                    "ticker": ["AAA", "BBB"],
                    "theme": ["t1", "t2"],
                    "bracket_verdict": ["too_big", "in_bracket"],
                    "market_cap": [50e9, 3e9],
                }
            ).to_parquet(funnel / f"{asof.isoformat()}.parquet")
            for path in grouped.glob("*.parquet"):
                day = pd.read_parquet(path)
                day["T"] = "AAA"
                extra = day.copy()
                extra["T"] = "BBB"
                for col in ("o", "h", "l", "c"):
                    day[col] = day[col] / 2.0
                pd.concat([day, extra], ignore_index=True).to_parquet(path)

            replay_arms.prepare(funnel_dir=funnel, briefs_dir=briefs, grouped_dir=grouped)
            second = pd.read_parquet(briefs / f"{asof.isoformat()}.parquet").set_index("ticker")

            self.assertEqual(sorted(second.index), ["AAA", "BBB"])
            # AAA was already measured: its geometry must not have moved.
            self.assertEqual(
                first.set_index("ticker").loc["AAA", "brief_trade_setup"],
                second.loc["AAA", "brief_trade_setup"],
            )


class TestReplayPasses(unittest.TestCase):
    """Brand-new rows draw from a hardcoded 50-per-run budget.

    Measured on the real funnel: 17.2 proposals/day before the 2026-08-18
    prompt change and **51.5/day after it, peaking at 68** — above the budget.
    One pass per fire would accumulate a backlog that never drains, silently,
    because the job still exits 0.

    The pass-count behaviour itself is pinned in TestReplayEarlyExitSignal; the
    earlier version of this class asserted a fetch-count exit that the first
    live run disproved.
    """

    def test_default_passes_cover_the_measured_daily_rate(self):
        """4 passes x 50 = 200/day against a measured 51.5/day mean, 68 peak."""
        self.assertGreaterEqual(replay_arms.DEFAULT_REPLAY_PASSES * 50, 150)

    def test_refuses_the_production_store_before_doing_any_work(self):
        with self.assertRaises(SystemExit):
            replay_arms.replay(store_dir=_PRODUCTION_LADDERS, _replay_fn=lambda *a, **k: [])


class TestBriefWritesAreAtomic(unittest.TestCase):
    def test_a_failing_write_leaves_the_previous_file_intact(self):
        """An OOM or timeout mid-write must not wedge the next scheduled run:
        _load_existing_briefs would fail to parse a torn parquet."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-08-06.parquet"
            good = pd.DataFrame({"ticker": ["AAA"], "arm": ["discarded"]})
            replay_arms.write_brief(good, path)
            before = path.read_bytes()

            class Boom(pd.DataFrame):
                def to_parquet(self, *a, **k):
                    raise OSError("disk full")

            with self.assertRaises(OSError):
                replay_arms.write_brief(Boom({"ticker": ["BBB"]}), path)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(tmp).glob("*")), [path])


class TestBuiltAtProvenance(unittest.TestCase):
    """A row built later than its day-mates can differ from them if the
    split-adjusted store was retro-adjusted in between. Measured today: 413 of
    413 setups re-derive identically, so the exposure is nil so far — which is
    exactly when a provenance stamp is cheap to add and impossible to backfill.
    """

    def test_every_written_row_carries_the_date_it_was_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            funnel, grouped, briefs = root / "f", root / "g", root / "b"
            for d in (funnel, grouped, briefs):
                d.mkdir()
            frame = _bars()
            asof = frame.index[250].date()
            pd.DataFrame(
                {
                    "ticker": ["AAA"],
                    "theme": ["t1"],
                    "bracket_verdict": ["too_big"],
                    "market_cap": [50e9],
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

            replay_arms.prepare(
                funnel_dir=funnel,
                briefs_dir=briefs,
                grouped_dir=grouped,
                built_on=dt.date(2026, 8, 23),
            )

            written = pd.read_parquet(briefs / f"{asof.isoformat()}.parquet")
            self.assertEqual(list(written["built_at"]), ["2026-08-23"])


class TestReplayEarlyExitSignal(unittest.TestCase):
    """The early exit must key on UNRESOLVED rows, not on fetch count.

    Measured on the first live run: all four passes reported 85 fetches and the
    exit never triggered. `fetches` counts the main budget too, and ongoing rows
    legitimately consume it every night by design — so it is never 0 while any
    position is open, and it cannot distinguish "new rows still draining" from
    "steady state".
    """

    def _store(self, tmp: Path, unresolved: list[int]) -> tuple[Path, Any]:
        """A store whose unresolved count follows ``unresolved`` on each pass."""
        store = tmp / "s"
        store.mkdir()
        state = {"i": -1}

        def fake_replay(briefs_dir, **kwargs):
            state["i"] += 1
            n = unresolved[min(state["i"], len(unresolved) - 1)]
            pd.DataFrame(
                {
                    "brief_date": [dt.date(2026, 8, 6)] * (n + 1),
                    "ticker": [f"T{i}" for i in range(n + 1)],
                    "ladder_classification": [None] * n + ["TP_FULL"],
                }
            ).to_parquet(store / "2026-08-06.parquet")
            return [SimpleNamespace(fetches=85, brief_date=dt.date(2026, 8, 6))]

        return store, fake_replay

    def test_stops_once_a_pass_resolves_nothing_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 60 unresolved -> 10 -> 0 -> 0: the fourth pass is unnecessary.
            store, fake = self._store(Path(tmp), [60, 10, 0, 0])

            n = replay_arms.replay(
                briefs_dir=Path(tmp), store_dir=store, max_passes=6, _replay_fn=fake
            )

            self.assertEqual(n, 3)

    def test_steady_state_costs_one_pass(self):
        """Nothing unresolved from the start — one pass, not four."""
        with tempfile.TemporaryDirectory() as tmp:
            store, fake = self._store(Path(tmp), [0])

            n = replay_arms.replay(
                briefs_dir=Path(tmp), store_dir=store, max_passes=4, _replay_fn=fake
            )

            self.assertEqual(n, 1)

    def test_a_constant_fetch_count_does_not_keep_it_looping(self):
        """The live failure exactly: fetches never fall, unresolved never moves."""
        with tempfile.TemporaryDirectory() as tmp:
            store, fake = self._store(Path(tmp), [7, 7, 7, 7])

            n = replay_arms.replay(
                briefs_dir=Path(tmp), store_dir=store, max_passes=6, _replay_fn=fake
            )

            self.assertEqual(n, 2)

    def test_respects_max_passes_when_the_count_never_stops_falling(self):
        """The ceiling itself, pinned. Without this only the early exits are
        covered, and a refactor that moved the increment could run past it."""
        with tempfile.TemporaryDirectory() as tmp:
            store, fake = self._store(Path(tmp), [100, 80, 60, 40])

            n = replay_arms.replay(
                briefs_dir=Path(tmp), store_dir=store, max_passes=2, _replay_fn=fake
            )

            self.assertEqual(n, 2)

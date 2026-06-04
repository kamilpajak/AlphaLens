"""Tests for the broker-free POPULATION ladder monitor (PR-2).

The monitor is a SECOND measurement metric beside the fixed-5-day shadow_return.
It replays EVERY brief candidate's ladder against its real price path UNTIL the
position is terminal (TP_FULL / SL_HIT / PARTIAL_TP_THEN_SL / TIME_STOP / NO_FILL)
over the ladder's real ~42-trading-session hold, NOT 5 days. Population mirrors
the paper planner (verified + a plannable trade_setup). Telemetry only, NEVER a
re-weighting loop. Click-orthogonal: it reads briefs + Polygon ONLY, never the
decisions/click ledger.

These tests pin the nine behaviours required by the PR-2 spec:
plannable-selection-mirrors-planner, matured-time-stop, open-mark-to-close,
terminal-freeze, carry-forward-on-fetch-failure, incremental-cache, broker-free
(AST), the read-side guardrails, and the atomic write.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback.population_ladder_monitor import (
    MONITOR_LOOKBACK_DAYS,
    replay_population_ladders,
    summarize_population_ladders,
)

UTC = dt.UTC

# A clean OK trade setup: single entry, single TP, disaster stop below. Plannable
# (status OK + schema 1.0.0 + suggested_size_pct + disaster_stop + entry_tiers).
_OK_SETUP = {
    "status": "OK",
    "schema_version": "1.0.0",
    "suggested_size_pct": 2.0,
    "disaster_stop": 95.0,
    "atr": 2.0,
    "order_ttl_days": 7,
    "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
    "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0}],
}

# Not plannable: status not OK.
_NO_STRUCTURE_SETUP = {"status": "NO_STRUCTURE", "disaster_stop": None, "entry_tiers": []}

# A three-tier setup whose full-fill geometry pins the size-field algebra by hand:
# full blended = 0.28*100 + 0.34*98 + 0.38*95 = 28 + 33.32 + 36.1 = 97.42
# stop_distance_full = (97.42 - 70.0)/97.42 = 27.42/97.42 ≈ 0.281
# suggested 4% -> gross fraction 0.04; implied_risk_full ≈ 0.04 * 0.281 ≈ 0.01125.
_THREE_TIER_SETUP = {
    "status": "OK",
    "schema_version": "1.0.0",
    "suggested_size_pct": 4.0,
    "disaster_stop": 70.0,
    "order_ttl_days": 7,
    "entry_tiers": [
        {"limit": 100.0, "alloc_pct": 28.0},
        {"limit": 98.0, "alloc_pct": 34.0},
        {"limit": 95.0, "alloc_pct": 38.0},
    ],
    "tp_tranches": [{"target": 130.0, "tranche_pct": 100.0}],
}


def _write_brief(briefs_dir: Path, brief_date: dt.date, rows: list[dict]) -> None:
    frame_rows = []
    for r in rows:
        frame_rows.append(
            {
                "ticker": r["ticker"],
                "theme": r.get("theme", "ai"),
                "verified": r.get("verified", True),
                "brief_trade_setup": json.dumps(r["setup"]) if r["setup"] is not None else None,
            }
        )
    df = pd.DataFrame(frame_rows)
    df.to_parquet(briefs_dir / f"{brief_date.isoformat()}.parquet")


class _MonitorTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.briefs_dir = self.root / "briefs"
        self.briefs_dir.mkdir()
        self.store_dir = self.root / "population_ladders"

    def tearDown(self):
        self._td.cleanup()

    def _read_store(self, brief_date: dt.date) -> pd.DataFrame:
        return pd.read_parquet(self.store_dir / f"{brief_date.isoformat()}.parquet")


class TestPlannableSelection(_MonitorTestBase):
    def test_plannable_selection_mirrors_planner(self):
        # GIVEN one verified+plannable candidate and one non-plannable (status not
        # OK). WHEN the monitor runs. THEN the plannable one is replayed and the
        # non-plannable one is recorded with a reason and NOT replayed.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)  # well past the 42-session hold
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "NVDA", "setup": _OK_SETUP},
                {"ticker": "XYZ", "setup": _NO_STRUCTURE_SETUP},
            ],
        )
        fetched: list[str] = []

        def _fetch(ticker, start, end):
            fetched.append(ticker)
            base = int(start.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        df = self._read_store(brief_date).set_index("ticker")
        self.assertTrue(bool(df.loc["NVDA", "plannable"]))
        self.assertFalse(bool(df.loc["XYZ", "plannable"]))
        self.assertIsNotNone(df.loc["XYZ", "nonplannable_reason"])
        # Non-plannable is never fetched/replayed.
        self.assertNotIn("XYZ", fetched)
        self.assertIn("NVDA", fetched)


class TestTerminalOutcomes(_MonitorTestBase):
    def test_matured_sideways_drifter_is_time_stop(self):
        # GIVEN a matured candidate whose price fills E1 then drifts sideways for
        # the whole hold (never TP, never SL). WHEN replayed past 42 sessions.
        # THEN it is a terminal TIME_STOP with realized_r set and open_r None.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch(ticker, start, end):
            # Fill at 100 on the first bar, then hold flat at ~100 across the
            # whole window (never reaches TP 110 nor SL 95).
            minute = 60_000
            base = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            bars = []
            t = base
            while t < end_ms:
                bars.append({"t": t, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "v": 1000.0})
                t += minute * 60  # hourly stride keeps the bar count modest
            return bars

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertTrue(bool(row["terminal"]))
        self.assertEqual(row["ladder_classification"], "TIME_STOP")
        self.assertIsNotNone(row["realized_r"])
        self.assertTrue(pd.isna(row["open_r"]))

    def test_holding_days_reflects_actual_hold_not_full_span(self):
        # GIVEN a MATURED candidate whose E1 fills then TP_FULL hits the SAME day
        # (a fast early exit). WHEN replayed long after the 42-session window.
        # THEN holding_days reflects the SHORT realised hold (first fill -> exit),
        # NOT the full ~42-session span (the coarse-bound regression).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)  # well past 42 sessions
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            return [
                {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},  # fill E1
                {
                    "t": base + minute,
                    "o": 100.0,
                    "h": 111.0,
                    "l": 100.0,
                    "c": 110.5,
                    "v": 1000.0,
                },  # TP 110
            ]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(row["ladder_classification"], "TP_FULL")
        self.assertTrue(bool(row["terminal"]))
        # First fill + exit are the same session => 0 trading days; must be tiny,
        # never the ~42-session full-hold span the old coarse bound reported.
        self.assertLessEqual(int(row["holding_days_elapsed"]), 1)

    def test_open_row_marks_to_last_close(self):
        # GIVEN a candidate filled but whose position_expiry session is still in
        # the FUTURE (hold not elapsed). WHEN replayed. THEN it is NOT terminal,
        # open_r is set (mark-to-last-close) and realized_r is None.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)  # only ~10 sessions in, < 42
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            return [
                {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {"t": base + minute, "o": 100.0, "h": 103.0, "l": 100.0, "c": 102.0, "v": 1000.0},
            ]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertFalse(bool(row["terminal"]))
        self.assertTrue(pd.isna(row["realized_r"]))
        self.assertIsNotNone(row["open_r"])
        self.assertFalse(pd.isna(row["open_r"]))

    def _no_fill_fetch(self, ticker, start, end):
        # Price stays strictly ABOVE the E1 limit (100) across the whole window,
        # so the dip-buy limit is never touched -> NO_FILL.
        base = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        day = 86_400_000
        bars, t = [], base
        while t < end_ms:
            bars.append({"t": t, "o": 105.0, "h": 106.0, "l": 104.0, "c": 105.0, "v": 1000.0})
            t += day
        return bars or [{"t": base, "o": 105.0, "h": 106.0, "l": 104.0, "c": 105.0, "v": 1000.0}]

    def test_no_fill_ongoing_while_entry_ttl_open(self):
        # GIVEN a candidate whose entries never touch, observed only a few sessions
        # in (entry-TTL = 7 sessions NOT yet elapsed). WHEN replayed. THEN the row
        # is NO_FILL but NOT terminal -- the limits are still live, so it must be
        # re-checked (not frozen, which would miss a later dip-fill).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 5, 6, 7, 0, tzinfo=UTC)  # ~3 sessions in, < 7-session entry TTL
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=self._no_fill_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(row["ladder_classification"], "NO_FILL")
        self.assertFalse(bool(row["terminal"]))  # entry TTL still open -> ongoing

    def test_no_fill_terminal_once_entry_ttl_elapsed(self):
        # GIVEN the same never-filling candidate observed long after the 7-session
        # entry TTL has fully elapsed. WHEN replayed. THEN NO_FILL is now TERMINAL
        # (the limits expired unfilled -- a final no-fill).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)  # well past entry TTL (and the hold)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=self._no_fill_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(row["ladder_classification"], "NO_FILL")
        self.assertTrue(bool(row["terminal"]))  # entry TTL elapsed -> terminal


class TestTerminalFreeze(_MonitorTestBase):
    def test_terminal_row_is_not_refetched(self):
        # GIVEN a matured candidate replayed to terminal on run 1. WHEN the monitor
        # runs again. THEN the terminal row is NOT re-fetched and is byte-identical.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch_tp(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            return [
                {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {"t": base + minute, "o": 100.0, "h": 111.0, "l": 100.0, "c": 110.0, "v": 1000.0},
            ]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_tp,
            now=now,
        )
        before = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertTrue(bool(before["terminal"]))
        self.assertEqual(before["ladder_classification"], "TP_FULL")

        # Second run: the fetch must NOT be called for the already-terminal ticker.
        called: list[str] = []

        def _fetch_guard(ticker, start, end):
            called.append(ticker)
            return _fetch_tp(ticker, start, end)

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_guard,
            now=now,
        )
        self.assertNotIn("NVDA", called, "terminal row must not be re-fetched")
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(before["ladder_classification"], after["ladder_classification"])
        self.assertEqual(before["realized_r"], after["realized_r"])
        self.assertEqual(before["sequence_str"], after["sequence_str"])


class TestCarryForward(_MonitorTestBase):
    def test_fetch_failure_carries_prior_row(self):
        # GIVEN an OPEN (non-terminal) row written on run 1. WHEN run 2's fetch
        # RAISES for that ticker. THEN the prior row is carried forward verbatim
        # and the file row count does NOT shrink.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)  # < 42 sessions -> stays OPEN
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch_ok(ticker, start, end):
            base = int(start.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 103.0, "l": 99.0, "c": 102.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_ok,
            now=now,
        )
        before = self._read_store(brief_date)
        before_row = before.set_index("ticker").loc["NVDA"]
        self.assertFalse(bool(before_row["terminal"]))

        def _fetch_boom(ticker, start, end):
            # A realistic Polygon-class failure (the client raises PolygonError /
            # ValueError on auth / malformed payload). The monitor must carry the
            # prior row forward rather than dropping the ticker.
            raise ValueError("polygon outage")

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_boom,
            now=now,
        )
        after = self._read_store(brief_date)
        self.assertEqual(len(before), len(after), "row count must not shrink on fetch failure")
        after_row = after.set_index("ticker").loc["NVDA"]
        # Prior row carried forward verbatim.
        self.assertEqual(before_row["ladder_classification"], after_row["ladder_classification"])
        if pd.isna(before_row["open_r"]):
            self.assertTrue(pd.isna(after_row["open_r"]))
        else:
            self.assertEqual(before_row["open_r"], after_row["open_r"])

    def test_brand_new_ticker_failing_first_night_gets_placeholder(self):
        # GIVEN a brand-new ticker whose FIRST night's fetch fails. THEN it gets a
        # retryable placeholder row (terminal False, classification None), not dropped.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch_boom(ticker, start, end):
            raise ValueError("polygon outage")

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_boom,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertFalse(bool(row["terminal"]))
        self.assertTrue(
            row["ladder_classification"] is None or pd.isna(row["ladder_classification"])
        )


class TestIncrementalCache(_MonitorTestBase):
    def test_second_run_fetches_only_tail(self):
        # GIVEN an OPEN row whose cache holds bars up to some last ts on run 1.
        # WHEN run 2 runs at a later `now` (horizon end advanced). THEN the fetch
        # window START advances past the last cached bar ts (tail-only fetch) and
        # the per-ticker cache file grows rather than being refetched whole.
        brief_date = dt.date(2026, 5, 1)
        now1 = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        fetch_windows: list[tuple[str, dt.datetime, dt.datetime]] = []

        def _fetch(ticker, start, end):
            fetch_windows.append((ticker, start, end))
            # Return one bar per day across [start, end) so the cache grows with
            # the window.
            base = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            day = 86_400_000
            bars = []
            t = base
            while t < end_ms:
                bars.append({"t": t, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 1000.0})
                t += day
            return bars

        replay_population_ladders(
            self.briefs_dir,
            end_date=now1.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now1,
        )
        # Cache is keyed by (ticker, arrival_session); 2026-05-01 is a trading day
        # so arrival == brief_date.
        cache_path = self.store_dir / "bars" / "NVDA_2026-05-01.parquet"
        self.assertTrue(cache_path.exists())
        n_after_run1 = len(pd.read_parquet(cache_path))
        first_start = fetch_windows[0][1]

        # Second run, later now -> the horizon window is longer, so there are new
        # tail bars to fetch.
        now2 = dt.datetime(2026, 5, 25, 7, 0, tzinfo=UTC)
        fetch_windows.clear()
        replay_population_ladders(
            self.briefs_dir,
            end_date=now2.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now2,
        )
        self.assertTrue(fetch_windows, "second run must fetch the tail")
        second_start = fetch_windows[0][1]
        # The fetch START on run 2 advanced strictly past run 1's start (tail-only).
        self.assertGreater(second_start, first_start)
        n_after_run2 = len(pd.read_parquet(cache_path))
        self.assertGreater(n_after_run2, n_after_run1, "cache must grow, not be refetched whole")

    def test_resurfacing_ticker_uses_per_arrival_caches(self):
        # GIVEN the SAME ticker surfaced on TWO different brief dates (different
        # arrival sessions). WHEN the monitor runs. THEN each brief gets its OWN
        # cache keyed by (ticker, arrival) — a later-arrival brief can NEVER set
        # the cache floor for an earlier-arrival one (the cross-contamination bug).
        bd_early = dt.date(2026, 5, 1)
        bd_late = dt.date(2026, 5, 8)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)  # both matured
        _write_brief(self.briefs_dir, bd_early, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        _write_brief(self.briefs_dir, bd_late, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        fetch_starts: list[dt.datetime] = []

        def _fetch(ticker, start, end):
            fetch_starts.append(start)
            base = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            day = 86_400_000
            bars, t = [], base
            while t < end_ms:
                bars.append({"t": t, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "v": 1000.0})
                t += day
            return bars

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        bars_dir = self.store_dir / "bars"
        self.assertTrue((bars_dir / "NVDA_2026-05-01.parquet").exists())
        self.assertTrue((bars_dir / "NVDA_2026-05-08.parquet").exists())
        # The earlier brief's cache must START at its OWN arrival (2026-05-01),
        # never floored at the later brief's arrival.
        early_first_ts = int(pd.read_parquet(bars_dir / "NVDA_2026-05-01.parquet")["t"].min())
        early_first = dt.datetime.fromtimestamp(early_first_ts / 1000, tz=UTC).date()
        self.assertEqual(early_first, dt.date(2026, 5, 1))


class TestBrokerFree(unittest.TestCase):
    """The monitor must import none of the forbidden broker / click modules."""

    def _module_path(self) -> Path:
        return (
            Path(__file__).resolve().parents[2]
            / "alphalens-pipeline"
            / "alphalens_pipeline"
            / "feedback"
            / "population_ladder_monitor.py"
        )

    def test_module_imports_no_broker_or_click_module(self):
        forbidden_modules = {
            "alphalens_feedback",
            "alphalens_feedback.store",
            "alphalens_pipeline.paper.ledger",
            "alphalens_pipeline.paper.planner",
            "alphalens_pipeline.paper.broker",
        }
        tree = ast.parse(self._module_path().read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertNotIn(alias.name, forbidden_modules)
                    self.assertNotEqual(top, "alphalens_feedback")
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotIn(module, forbidden_modules)
                self.assertNotEqual(module.split(".")[0], "alphalens_feedback")
                # `from alphalens_pipeline.paper import ledger/planner/broker`
                if module == "alphalens_pipeline.paper":
                    imported = {a.name for a in node.names}
                    self.assertEqual(imported & {"ledger", "planner", "broker"}, set())


class TestReadSideSummary(_MonitorTestBase):
    def _populate(self, now: dt.datetime) -> dt.date:
        brief_date = dt.date(2026, 5, 1)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "AAA", "setup": _OK_SETUP},
                {"ticker": "BBB", "setup": _OK_SETUP},
                {"ticker": "XYZ", "setup": _NO_STRUCTURE_SETUP},
            ],
        )

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            if ticker == "AAA":
                # Fills + full TP -> terminal.
                return [
                    {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                    {"t": base + minute, "o": 100, "h": 111.0, "l": 100.0, "c": 110.0, "v": 1000.0},
                ]
            # BBB drifts -> open at this `now`.
            return [{"t": base, "o": 100.0, "h": 103.0, "l": 99.0, "c": 102.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        return brief_date

    def test_summary_separates_open_from_realized_and_reports_denominators(self):
        # Use a `now` where AAA matures (TP terminal) but BBB stays open.
        now = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        self._populate(now)
        summary = summarize_population_ladders(self.store_dir)

        # Two denominators present.
        self.assertIn("n_plannable", summary)
        self.assertIn("n_brief", summary)
        self.assertEqual(summary["n_brief"], 3)  # AAA, BBB, XYZ
        self.assertEqual(summary["n_plannable"], 2)  # AAA, BBB (XYZ not plannable)

        # Realized vs open reported separately (distinct keys).
        self.assertIn("realized_mean", summary)
        self.assertIn("realized_n", summary)
        self.assertIn("open_mean", summary)
        self.assertIn("open_n", summary)
        # AAA terminal (realized), BBB open.
        self.assertEqual(summary["realized_n"], 1)
        self.assertEqual(summary["open_n"], 1)

        # Holding-period distribution present.
        self.assertIn("holding_days_p50", summary)
        self.assertIn("holding_days_p95", summary)

        # Single-stratum honesty flag documented.
        self.assertIn("regime_stratified", summary)
        self.assertIs(summary["regime_stratified"], False)

    def test_open_marks_excluded_from_realized_mean(self):
        # The realized mean must come ONLY from terminal rows; the open
        # mark-to-market must never be pooled into it.
        now = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        self._populate(now)
        summary = summarize_population_ladders(self.store_dir)
        df = self._read_store(dt.date(2026, 5, 1)).set_index("ticker")
        aaa_realized = df.loc["AAA", "realized_r"]
        # realized_mean equals AAA's realized_r (the only terminal row), proving
        # BBB's open mark is not folded in.
        self.assertAlmostEqual(summary["realized_mean"], float(aaa_realized), places=6)


class TestAtomicWrite(_MonitorTestBase):
    def test_no_tmp_file_left_on_success(self):
        # On a successful run no `*.parquet.tmp` may remain in the store dir.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        tmp_files = list(self.store_dir.glob("*.parquet.tmp"))
        self.assertEqual(tmp_files, [], "no .parquet.tmp may be left after a successful write")
        self.assertTrue((self.store_dir / f"{brief_date.isoformat()}.parquet").exists())


class TestSizeFields(_MonitorTestBase):
    """Portfolio-size (two-layer) fields: additive, never touch the R-space edge."""

    def test_full_fill_tp_pins_size_field_algebra(self):
        # GIVEN a fully-filled terminal TP_FULL on the single-tier _OK_SETUP.
        # WHEN replayed to terminal. THEN the size fields compose exactly:
        #   suggested_gross_weight_pct = 2/100 = 0.02
        #   full_ladder_blended_entry  = 100.0
        #   stop_distance_pct_full     = (100-95)/100 = 0.05
        #   implied_risk_pct_full      = 0.02 * 0.05 = 0.001
        #   filled fully -> realized mirrors full; realized_r(TP@110) = (110-100)/5 = 2.0
        #   realized_return_pct_of_book = realized_r * realized_risk_pct = 2.0 * 0.001 = 0.002
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            return [
                {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {"t": base + minute, "o": 100.0, "h": 111.0, "l": 100.0, "c": 110.0, "v": 1000.0},
            ]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertTrue(bool(row["terminal"]))
        self.assertEqual(row["ladder_classification"], "TP_FULL")
        # Edge layer unchanged.
        self.assertAlmostEqual(float(row["realized_r"]), 2.0, places=6)
        # Signal-time geometry.
        self.assertAlmostEqual(float(row["suggested_gross_weight_pct"]), 0.02, places=9)
        self.assertAlmostEqual(float(row["full_ladder_blended_entry"]), 100.0, places=6)
        self.assertAlmostEqual(float(row["stop_distance_pct_full"]), 0.05, places=9)
        self.assertAlmostEqual(float(row["implied_risk_pct_full"]), 0.001, places=9)
        # Outcome-time geometry.
        self.assertEqual(int(row["tiers_filled_count"]), 1)
        self.assertAlmostEqual(float(row["realized_gross_weight_pct"]), 0.02, places=9)
        self.assertAlmostEqual(float(row["stop_distance_pct"]), 0.05, places=9)
        self.assertAlmostEqual(float(row["realized_risk_pct"]), 0.001, places=9)
        self.assertAlmostEqual(float(row["realized_return_pct_of_book"]), 0.002, places=9)
        # Open analogue is None when terminal.
        self.assertTrue(pd.isna(row["open_return_pct_of_book"]))

    def test_three_tier_full_fill_hand_computed_contribution(self):
        # GIVEN the three-tier setup; price gaps down through all 3 limits on bar 1
        # then rallies to the single TP (130). WHEN replayed. THEN:
        #   full blended = 0.28*100 + 0.34*98 + 0.38*95 = 97.42
        #   stop_distance = (97.42-70)/97.42 ≈ 0.281
        #   suggested 4% -> 0.04; full-fill -> realized mirrors full geometry.
        #   realized_r(TP@130) = (130-97.42)/(97.42-70) = 32.58/27.42 ≈ 1.1882
        #   realized_return_pct_of_book = realized_r * 0.04 * 0.281 ≈ 0.01336
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _THREE_TIER_SETUP}])

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            return [
                # Bar 1 dips through all three limits (low 94 < 95) without hitting stop 70.
                {"t": base, "o": 100.0, "h": 100.5, "l": 94.0, "c": 96.0, "v": 1000.0},
                # Bar 2 rallies to TP 130.
                {"t": base + minute, "o": 96.0, "h": 131.0, "l": 96.0, "c": 130.0, "v": 1000.0},
            ]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(row["ladder_classification"], "TP_FULL")
        self.assertEqual(int(row["tiers_filled_count"]), 3)
        full_blended = 0.28 * 100.0 + 0.34 * 98.0 + 0.38 * 95.0  # 97.42
        stop_dist = (full_blended - 70.0) / full_blended
        self.assertAlmostEqual(float(row["full_ladder_blended_entry"]), full_blended, places=6)
        self.assertAlmostEqual(float(row["stop_distance_pct_full"]), stop_dist, places=9)
        self.assertAlmostEqual(float(row["suggested_gross_weight_pct"]), 0.04, places=9)
        # Full fill -> realized risk == implied risk.
        self.assertAlmostEqual(float(row["realized_risk_pct"]), 0.04 * stop_dist, places=9)
        expected_r = (130.0 - full_blended) / (full_blended - 70.0)
        self.assertAlmostEqual(float(row["realized_r"]), expected_r, places=6)
        expected_contrib = expected_r * 0.04 * stop_dist
        self.assertAlmostEqual(
            float(row["realized_return_pct_of_book"]), expected_contrib, places=9
        )
        # Sanity: ≈ +1.34% of book.
        self.assertAlmostEqual(float(row["realized_return_pct_of_book"]), 0.01336, places=4)

    def test_partial_fill_scales_realized_gross_weight(self):
        # GIVEN the three-tier setup; only E1 (limit 100) fills then the position
        # is time-stopped (sideways). WHEN replayed. THEN:
        #   tiers_filled_count = 1
        #   filled_fraction = alloc(E1)/sum(alloc) = 28/100 = 0.28
        #   realized_gross_weight_pct = 0.04 * 0.28 = 0.0112
        #   realized blended = 100 (only E1) -> stop_distance = (100-70)/100 = 0.30
        #   realized_risk_pct = 0.0112 * 0.30 = 0.00336
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _THREE_TIER_SETUP}])

        def _fetch(ticker, start, end):
            # E1 (100) fills; price never dips to 98/95 (low stays at 99.5) and never
            # reaches TP 130 nor SL 70 -> partial fill, time-stop.
            minute = 60_000
            base = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            bars, t = [], base
            while t < end_ms:
                bars.append({"t": t, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "v": 1000.0})
                t += minute * 60
            return bars

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertTrue(bool(row["terminal"]))
        self.assertEqual(int(row["tiers_filled_count"]), 1)
        self.assertAlmostEqual(float(row["realized_gross_weight_pct"]), 0.04 * 0.28, places=9)
        # Realized blended (E1 only) = 100 -> stop distance 0.30 (NOT the full 0.281).
        self.assertAlmostEqual(float(row["full_ladder_blended_entry"]), 97.42, places=4)
        self.assertAlmostEqual(float(row["stop_distance_pct"]), 0.30, places=9)
        self.assertAlmostEqual(float(row["realized_risk_pct"]), 0.04 * 0.28 * 0.30, places=9)
        # realized_return_pct_of_book = realized_r * realized_risk_pct.
        self.assertAlmostEqual(
            float(row["realized_return_pct_of_book"]),
            float(row["realized_r"]) * (0.04 * 0.28 * 0.30),
            places=9,
        )

    def test_no_fill_size_fields_are_null_or_zero_no_crash(self):
        # GIVEN a candidate whose entries never touch (terminal NO_FILL).
        # WHEN replayed. THEN signal-time fields are still populated (intent), but
        # the outcome-time deployment fields are 0 / NULL and nothing crashes.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            day = 86_400_000
            bars, t = [], base
            while t < end_ms:
                bars.append({"t": t, "o": 105.0, "h": 106.0, "l": 104.0, "c": 105.0, "v": 1000.0})
                t += day
            return bars or [
                {"t": base, "o": 105.0, "h": 106.0, "l": 104.0, "c": 105.0, "v": 1000.0}
            ]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(row["ladder_classification"], "NO_FILL")
        # Signal-time intent still present (geometry is fill-independent).
        self.assertAlmostEqual(float(row["suggested_gross_weight_pct"]), 0.02, places=9)
        self.assertAlmostEqual(float(row["stop_distance_pct_full"]), 0.05, places=9)
        # Nothing deployed.
        self.assertEqual(int(row["tiers_filled_count"]), 0)
        self.assertEqual(float(row["realized_gross_weight_pct"]), 0.0)
        self.assertTrue(pd.isna(row["stop_distance_pct"]))
        self.assertEqual(float(row["realized_risk_pct"]), 0.0)
        self.assertTrue(pd.isna(row["realized_return_pct_of_book"]))
        self.assertTrue(pd.isna(row["open_return_pct_of_book"]))

    def test_missing_suggested_size_yields_null_size_fields_no_crash(self):
        # GIVEN a setup with no suggested_size_pct (a malformed / size-less setup —
        # the live planner rejects it as non-plannable, but _size_fields must still
        # degrade gracefully). WHEN size fields are computed for a full-fill TP.
        # THEN size-independent geometry (stop_distance_pct_full) is still computed,
        # every weight-bearing field is NULL, and nothing crashes.
        from alphalens_pipeline.feedback.ladder_replay import replay_ladder
        from alphalens_pipeline.feedback.population_ladder_monitor import _size_fields

        setup = dict(_OK_SETUP)
        del setup["suggested_size_pct"]
        bars = [
            {"t": 0, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
            {"t": 60_000, "o": 100.0, "h": 111.0, "l": 100.0, "c": 110.0, "v": 1000.0},
        ]
        outcome = replay_ladder(setup, bars, reference_close=100.0)
        self.assertEqual(outcome.classification, "TP_FULL")
        sf = _size_fields(setup, outcome, realized_r=outcome.realized_r, open_r=None)

        self.assertIsNone(sf["suggested_gross_weight_pct"])
        # Geometry that does NOT need the size is still computed.
        self.assertAlmostEqual(sf["stop_distance_pct_full"], 0.05, places=9)
        self.assertAlmostEqual(sf["stop_distance_pct"], 0.05, places=9)
        # Every weight-bearing field is NULL when the size is unknown.
        self.assertIsNone(sf["implied_risk_pct_full"])
        self.assertIsNone(sf["realized_gross_weight_pct"])
        self.assertIsNone(sf["realized_risk_pct"])
        self.assertIsNone(sf["realized_return_pct_of_book"])
        # tiers_filled_count is still observable (a position WAS opened).
        self.assertEqual(sf["tiers_filled_count"], 1)

    def test_nonplannable_row_has_null_size_fields(self):
        # GIVEN a non-plannable candidate (status not OK -> the planner skips it).
        # WHEN the monitor runs. THEN its row carries every size column as NULL.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "XYZ", "setup": _NO_STRUCTURE_SETUP}])

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["XYZ"]
        self.assertFalse(bool(row["plannable"]))
        for col in (
            "suggested_gross_weight_pct",
            "full_ladder_blended_entry",
            "stop_distance_pct_full",
            "implied_risk_pct_full",
            "tiers_filled_count",
            "realized_gross_weight_pct",
            "stop_distance_pct",
            "realized_risk_pct",
            "realized_return_pct_of_book",
            "open_return_pct_of_book",
        ):
            self.assertIn(col, row.index)
            self.assertTrue(pd.isna(row[col]), f"{col} must be NULL on a non-plannable row")

    def test_open_trade_populates_open_contribution_not_realized(self):
        # GIVEN a filled-but-still-open position (hold not elapsed). WHEN replayed.
        # THEN open_return_pct_of_book is populated (open_r * realized_risk_pct) and
        # realized_return_pct_of_book is NULL (no terminal outcome yet).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)  # ~10 sessions < 42
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            return [
                {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {"t": base + minute, "o": 100.0, "h": 103.0, "l": 100.0, "c": 102.0, "v": 1000.0},
            ]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertFalse(bool(row["terminal"]))
        self.assertTrue(pd.isna(row["realized_return_pct_of_book"]))
        self.assertFalse(pd.isna(row["open_return_pct_of_book"]))
        # open_return_pct_of_book == open_r * realized_risk_pct.
        self.assertAlmostEqual(
            float(row["open_return_pct_of_book"]),
            float(row["open_r"]) * float(row["realized_risk_pct"]),
            places=9,
        )


class TestSizeAwareSummary(_MonitorTestBase):
    """summarize_population_ladders gains a size layer WITHOUT touching the edge layer."""

    def _populate_terminal(self, now: dt.datetime) -> dt.date:
        brief_date = dt.date(2026, 5, 1)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "AAA", "setup": _OK_SETUP},
                {"ticker": "BBB", "setup": _OK_SETUP},
            ],
        )

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            # Both fill then TP_FULL -> terminal realized.
            return [
                {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {"t": base + minute, "o": 100.0, "h": 111.0, "l": 100.0, "c": 110.0, "v": 1000.0},
            ]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        return brief_date

    def test_equal_weight_edge_metric_unchanged_and_size_layer_added(self):
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        self._populate_terminal(now)
        summary = summarize_population_ladders(self.store_dir)

        # Edge layer (equal-weight) intact: both AAA, BBB are TP_FULL realized_r=2.0.
        self.assertEqual(summary["realized_n"], 2)
        self.assertAlmostEqual(summary["realized_mean"], 2.0, places=6)
        self.assertIs(summary["regime_stratified"], False)

        # Size layer present + correct (per-trade realized_risk_pct = 0.02*0.05 = 0.001).
        self.assertIn("total_realized_contribution_pct_of_book", summary)
        self.assertIn("size_weighted_realized_r", summary)
        self.assertIn("mean_realized_risk_pct", summary)
        self.assertIn("mean_tiers_filled_count", summary)
        # Each contributes 2.0 * 0.001 = 0.002; two trades -> 0.004 total.
        self.assertAlmostEqual(summary["total_realized_contribution_pct_of_book"], 0.004, places=9)
        # size_weighted_realized_r = sum(r*risk)/sum(risk) = (2*0.001+2*0.001)/(0.002) = 2.0.
        self.assertAlmostEqual(summary["size_weighted_realized_r"], 2.0, places=6)
        self.assertAlmostEqual(summary["mean_realized_risk_pct"], 0.001, places=9)
        self.assertAlmostEqual(summary["mean_tiers_filled_count"], 1.0, places=6)

    def test_size_layer_guards_empty_store(self):
        summary = summarize_population_ladders(self.store_dir)
        self.assertEqual(summary["realized_n"], 0)
        self.assertIsNone(summary["realized_mean"])
        # New size keys present + null/zero, never crash on an empty store.
        self.assertIsNone(summary["total_realized_contribution_pct_of_book"])
        self.assertIsNone(summary["size_weighted_realized_r"])
        self.assertIsNone(summary["mean_realized_risk_pct"])
        self.assertIsNone(summary["mean_tiers_filled_count"])


class TestSizeFieldsCarryForward(_MonitorTestBase):
    def test_old_format_prior_row_without_size_cols_is_tolerated(self):
        # GIVEN an OPEN row written by an OLD monitor (no size columns). WHEN run 2's
        # fetch fails so the prior row is carried forward. THEN it does not crash and
        # the missing size columns surface as NaN (re-populated on the next replay).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)  # OPEN
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch_ok(ticker, start, end):
            base = int(start.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 103.0, "l": 99.0, "c": 102.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_ok,
            now=now,
        )
        # Simulate an OLD-format parquet: drop the new size columns entirely.
        path = self.store_dir / f"{brief_date.isoformat()}.parquet"
        df = pd.read_parquet(path)
        size_cols = [
            "suggested_gross_weight_pct",
            "full_ladder_blended_entry",
            "stop_distance_pct_full",
            "implied_risk_pct_full",
            "tiers_filled_count",
            "realized_gross_weight_pct",
            "stop_distance_pct",
            "realized_risk_pct",
            "realized_return_pct_of_book",
            "open_return_pct_of_book",
        ]
        df = df.drop(columns=[c for c in size_cols if c in df.columns])
        df.to_parquet(path)

        def _fetch_boom(ticker, start, end):
            raise ValueError("polygon outage")

        # Must not raise even though the prior row lacks the new columns.
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_boom,
            now=now,
        )
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        # Carried-forward row: missing size columns default to NaN.
        for col in size_cols:
            self.assertIn(col, after.index)
            self.assertTrue(pd.isna(after[col]))


_SIZE_COLS = [
    "suggested_gross_weight_pct",
    "full_ladder_blended_entry",
    "stop_distance_pct_full",
    "implied_risk_pct_full",
    "tiers_filled_count",
    "realized_gross_weight_pct",
    "stop_distance_pct",
    "realized_risk_pct",
    "realized_return_pct_of_book",
    "open_return_pct_of_book",
]
# The frozen replay verdict columns the enricher must NEVER touch.
_VERDICT_COLS = [
    "ladder_classification",
    "blended_entry",
    "realized_r",
    "open_r",
    "mfe",
    "mae",
    "mfe_pct",
    "mae_pct",
    "forward_return",
    "sequence_str",
    "terminal",
]


def _null_size_columns(path: Path) -> pd.DataFrame:
    """Set every size column to NaN on the store parquet at ``path`` (in place)."""
    df = pd.read_parquet(path)
    for col in _SIZE_COLS:
        df[col] = None
    df.to_parquet(path)
    return pd.read_parquet(path)


class TestSizeFieldEnrichment(_MonitorTestBase):
    """Backfill the size overlay onto terminal rows frozen before the size feature."""

    def _three_tier_full_fill_fetch(self):
        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            minute = 60_000
            return [
                {"t": base, "o": 100.0, "h": 100.5, "l": 94.0, "c": 96.0, "v": 1000.0},
                {"t": base + minute, "o": 96.0, "h": 131.0, "l": 96.0, "c": 130.0, "v": 1000.0},
            ]

        return _fetch

    def _seed_terminal_row(self, setup: dict) -> tuple[dt.date, Path]:
        """Replay one full-fill TP terminal row and return (brief_date, store path)."""
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": setup}])
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=self._three_tier_full_fill_fetch(),
            now=now,
        )
        return brief_date, self.store_dir / f"{brief_date.isoformat()}.parquet"

    def test_null_size_terminal_row_is_recomputed_verdict_unchanged(self):
        # GIVEN a terminal full-fill TP row whose size columns were nulled (the
        # pre-PR-431 freeze case). WHEN enrich_store_with_size_fields runs. THEN the
        # size overlay is recomputed to the exact hand-verified algebra and the
        # frozen verdict (classification / realized_r / sequence_str) is unchanged.
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            enrich_store_with_size_fields,
        )

        brief_date, path = self._seed_terminal_row(_THREE_TIER_SETUP)
        before = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertTrue(bool(before["terminal"]))
        self.assertEqual(before["ladder_classification"], "TP_FULL")

        nulled = _null_size_columns(path).set_index("ticker").loc["NVDA"]
        for col in _SIZE_COLS:
            self.assertTrue(pd.isna(nulled[col]))

        n = enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        self.assertEqual(n, 1)

        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        # Size overlay restored to the hand-computed three-tier algebra.
        full_blended = 0.28 * 100.0 + 0.34 * 98.0 + 0.38 * 95.0  # 97.42
        stop_dist = (full_blended - 70.0) / full_blended
        self.assertAlmostEqual(float(after["suggested_gross_weight_pct"]), 0.04, places=9)
        self.assertAlmostEqual(float(after["full_ladder_blended_entry"]), full_blended, places=6)
        self.assertAlmostEqual(float(after["stop_distance_pct_full"]), stop_dist, places=9)
        self.assertEqual(int(after["tiers_filled_count"]), 3)
        self.assertAlmostEqual(float(after["realized_risk_pct"]), 0.04 * stop_dist, places=9)
        expected_r = (130.0 - full_blended) / (full_blended - 70.0)
        expected_contrib = expected_r * 0.04 * stop_dist
        self.assertAlmostEqual(
            float(after["realized_return_pct_of_book"]), expected_contrib, places=9
        )
        # FROZEN verdict columns byte-identical before/after.
        for col in _VERDICT_COLS:
            b, a = before[col], after[col]
            if pd.isna(b):
                self.assertTrue(pd.isna(a), f"{col} should remain NaN")
            else:
                self.assertEqual(a, b, f"{col} must be unchanged")

    def test_brief_setup_unavailable_for_ticker_stays_null_no_fudge(self):
        # GIVEN a frozen terminal row whose brief no longer carries a parseable
        # setup for that ticker (size genuinely unknowable post-hoc). WHEN enrichment
        # runs. THEN every size column stays NULL (never fudged) and 0 rows enriched.
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            enrich_store_with_size_fields,
        )

        brief_date, path = self._seed_terminal_row(_THREE_TIER_SETUP)
        _null_size_columns(path)
        # Rewrite the brief so NVDA's setup is gone (only an unrelated ticker remains).
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "AAPL", "setup": _OK_SETUP}])

        n = enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        self.assertEqual(n, 0)
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        for col in _SIZE_COLS:
            self.assertTrue(pd.isna(after[col]), f"{col} must stay NULL (no fudge)")

    def test_missing_brief_leaves_row_null_resilient(self):
        # GIVEN a terminal row whose brief parquet has been removed. WHEN enrichment
        # runs. THEN it does not raise, enriches 0 rows, and the size stays NULL.
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            enrich_store_with_size_fields,
        )

        brief_date, path = self._seed_terminal_row(_THREE_TIER_SETUP)
        _null_size_columns(path)
        (self.briefs_dir / f"{brief_date.isoformat()}.parquet").unlink()

        n = enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        self.assertEqual(n, 0)
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        for col in _SIZE_COLS:
            self.assertTrue(pd.isna(after[col]))

    def test_idempotent_second_run_enriches_zero(self):
        # GIVEN an already-enriched store. WHEN enrichment runs again. THEN it
        # reports 0 rows and leaves the populated size fields byte-identical.
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            enrich_store_with_size_fields,
        )

        brief_date, path = self._seed_terminal_row(_THREE_TIER_SETUP)
        _null_size_columns(path)
        first = enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        self.assertEqual(first, 1)
        snapshot = self._read_store(brief_date).set_index("ticker").loc["NVDA"]

        second = enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        self.assertEqual(second, 0)
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        for col in _SIZE_COLS:
            b, a = snapshot[col], after[col]
            if pd.isna(b):
                self.assertTrue(pd.isna(a))
            else:
                self.assertAlmostEqual(float(a), float(b), places=9)

    def test_filled_fraction_rederived_from_sequence_str_matches_replay(self):
        # GIVEN a partial-fill terminal row (only E1 of the 3-tier setup fills) whose
        # size was nulled. WHEN enrichment recomputes from sequence_str + brief tiers.
        # THEN the re-derived filled_fraction matches the original replay's:
        #   alloc(E1)/sum(alloc) = 28/(28+34+38) = 0.28
        #   realized_gross_weight_pct = 0.04 * 0.28 = 0.0112
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            _parse_filled_entry_ids,
            _rederive_filled_fraction,
            enrich_store_with_size_fields,
        )

        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _THREE_TIER_SETUP}])

        def _fetch(ticker, start, end):
            # E1 (100) fills; never dips to 98/95, never hits TP/SL -> partial, time-stop.
            minute = 60_000
            base = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            bars, t = [], base
            while t < end_ms:
                bars.append({"t": t, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "v": 1000.0})
                t += minute * 60
            return bars

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        path = self.store_dir / f"{brief_date.isoformat()}.parquet"
        before = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(int(before["tiers_filled_count"]), 1)
        original_gross = float(before["realized_gross_weight_pct"])

        # Sanity: the adapter re-derives the SAME filled_fraction the engine used.
        filled_ids = _parse_filled_entry_ids(before["sequence_str"])
        self.assertEqual(filled_ids, ["E1"])
        frac = _rederive_filled_fraction(_THREE_TIER_SETUP, filled_ids)
        self.assertAlmostEqual(frac, 0.28, places=9)

        _null_size_columns(path)
        enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertAlmostEqual(float(after["realized_gross_weight_pct"]), 0.04 * 0.28, places=9)
        self.assertAlmostEqual(float(after["realized_gross_weight_pct"]), original_gross, places=9)

    def test_nonplannable_and_already_sized_rows_skipped(self):
        # GIVEN a store with a non-plannable row (NULL size by design) and a freshly
        # replayed terminal row (already sized). WHEN enrichment runs. THEN neither is
        # touched (non-plannable stays NULL; already-sized is left as-is) and n == 0.
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            enrich_store_with_size_fields,
        )

        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "NVDA", "setup": _THREE_TIER_SETUP},
                {"ticker": "XYZ", "setup": _NO_STRUCTURE_SETUP},
            ],
        )
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=self._three_tier_full_fill_fetch(),
            now=now,
        )
        # Both already correct: terminal sized, non-plannable NULL.
        n = enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        self.assertEqual(n, 0)
        df = self._read_store(brief_date).set_index("ticker")
        self.assertFalse(pd.isna(df.loc["NVDA", "realized_gross_weight_pct"]))
        self.assertTrue(pd.isna(df.loc["XYZ", "realized_gross_weight_pct"]))


class TestLookbackConstant(unittest.TestCase):
    def test_monitor_lookback_is_distinct_and_large_enough(self):
        # The monitor uses its OWN lookback (>= ~60 calendar days for 42 sessions),
        # NOT shadow_return's 14-day window.
        self.assertGreaterEqual(MONITOR_LOOKBACK_DAYS, 60)
        self.assertNotEqual(MONITOR_LOOKBACK_DAYS, 14)


if __name__ == "__main__":
    unittest.main()

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


class TestLookbackConstant(unittest.TestCase):
    def test_monitor_lookback_is_distinct_and_large_enough(self):
        # The monitor uses its OWN lookback (>= ~60 calendar days for 42 sessions),
        # NOT shadow_return's 14-day window.
        self.assertGreaterEqual(MONITOR_LOOKBACK_DAYS, 60)
        self.assertNotEqual(MONITOR_LOOKBACK_DAYS, 14)


if __name__ == "__main__":
    unittest.main()

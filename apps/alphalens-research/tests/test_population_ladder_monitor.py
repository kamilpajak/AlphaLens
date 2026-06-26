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
from alphalens_pipeline.feedback.ladder_replay import GRID_CONFIGS, replay_ladder
from alphalens_pipeline.feedback.population_ladder_monitor import (
    _SPLIT_SCREEN_THRESHOLD,
    _TOUCH_EPS,
    MONITOR_LOOKBACK_DAYS,
    _carry_prior,
    _cheap_open_r,
    _coerce_session,
    _engine_cutoffs,
    _RunDeadline,
    _screen_decision,
    _stamp_scorer_version,
    _stamp_theme,
    replay_population_ladders,
    summarize_population_ladders,
)
from alphalens_pipeline.paper.calendar import (
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
    session_open_utc,
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


class TestLadderConfigVersionStamp(_MonitorTestBase):
    def test_plannable_row_carries_config_token_nonplannable_does_not(self):
        # GIVEN a plannable + a non-plannable candidate. WHEN the monitor runs.
        # THEN the plannable row carries a parseable config token reflecting the
        # entry-TTL it used, and the non-plannable row carries no token.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "NVDA", "setup": _OK_SETUP},
                {"ticker": "XYZ", "setup": _NO_STRUCTURE_SETUP},
            ],
        )

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
        df = self._read_store(brief_date).set_index("ticker")
        self.assertIn("ladder_config_version", df.columns)
        token = df.loc["NVDA", "ladder_config_version"]
        payload = json.loads(str(token))
        # order_ttl in the token is the entry-TTL the row actually used (numpy
        # int64 compares equal to the python int the token carries).
        self.assertEqual(payload["order_ttl_days"], df.loc["NVDA", "entry_ttl_days"])
        self.assertIn("time_stop_days", payload)
        # Non-plannable was never replayed -> no geometry token (None serialises
        # to NaN in the all-None column).
        xyz_token = df.loc["XYZ", "ladder_config_version"]
        self.assertTrue(pd.isna(xyz_token) or xyz_token in (None, ""))


class TestGridRealizedRStamp(_MonitorTestBase):
    def test_resolved_plannable_row_carries_grid_json(self):
        # GIVEN a plannable candidate resolved by a minute replay. THEN the row
        # carries a grid_realized_r_json map with every alternate-exit config; the
        # non-plannable row carries none.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "NVDA", "setup": _OK_SETUP},
                {"ticker": "XYZ", "setup": _NO_STRUCTURE_SETUP},
            ],
        )

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
        df = self._read_store(brief_date).set_index("ticker")
        self.assertIn("grid_realized_r_json", df.columns)
        grid = json.loads(str(df.loc["NVDA", "grid_realized_r_json"]))
        self.assertEqual(set(grid), set(GRID_CONFIGS))
        # Non-plannable was never replayed -> no grid (None -> NaN in the column).
        xyz_grid = df.loc["XYZ", "grid_realized_r_json"]
        self.assertTrue(pd.isna(xyz_grid) or xyz_grid in (None, ""))

    def test_resolved_plannable_row_carries_entry_counterfactual(self):
        # The entry-side counterfactual (realized_r_full_fill) is stamped on a
        # resolved plannable row and absent on the non-plannable one.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "NVDA", "setup": _OK_SETUP},
                {"ticker": "XYZ", "setup": _NO_STRUCTURE_SETUP},
            ],
        )

        def _fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            # Fill E1 (low<=100) AND hit TP1 (high>=110) -> TP_FULL terminal, so
            # realized_r is set (not an ongoing None).
            return [{"t": base, "o": 100.0, "h": 110.0, "l": 99.0, "c": 109.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        df = self._read_store(brief_date).set_index("ticker")
        self.assertIn("realized_r_full_fill", df.columns)
        # _OK_SETUP has a single entry tier, so the full-fill blend IS that tier:
        # the counterfactual must equal the as-specified realized_r.
        self.assertFalse(pd.isna(df.loc["NVDA", "realized_r_full_fill"]))
        self.assertAlmostEqual(
            float(df.loc["NVDA", "realized_r_full_fill"]),
            float(df.loc["NVDA", "realized_r"]),
            places=4,
        )
        self.assertTrue(pd.isna(df.loc["XYZ", "realized_r_full_fill"]))


class TestStampThemeUnit(unittest.TestCase):
    def test_fills_missing_theme(self):
        self.assertEqual(_stamp_theme({}, "ai")["theme"], "ai")
        self.assertIsNone(_stamp_theme({}, None)["theme"])

    def test_preserves_existing_theme(self):
        # A frozen row already carrying a theme keeps it (provenance), even when a
        # later brief would supply a different one.
        self.assertEqual(_stamp_theme({"theme": "defense"}, "ai")["theme"], "defense")

    def test_backfills_empty_or_none(self):
        self.assertEqual(_stamp_theme({"theme": None}, "ai")["theme"], "ai")
        self.assertEqual(_stamp_theme({"theme": ""}, "ai")["theme"], "ai")

    def test_slugifies_incoming_theme(self):
        # A spaced brief theme is canonicalised to a slug on the way in.
        self.assertEqual(_stamp_theme({}, "AI ethics")["theme"], "ai_ethics")
        self.assertEqual(_stamp_theme({}, "high gas prices")["theme"], "high_gas_prices")

    def test_canonicalises_preserved_spaced_theme(self):
        # A row frozen with a SPACED theme (stamped before slug canonicalisation)
        # is re-slugged to the SAME concept on the next stamp — provenance (which
        # concept) is kept, only the format changes.
        self.assertEqual(_stamp_theme({"theme": "gas prices"}, "x")["theme"], "gas_prices")
        self.assertEqual(
            _stamp_theme({"theme": "defense_procurement"}, "x")["theme"], "defense_procurement"
        )


class TestThemeProvenance(_MonitorTestBase):
    def test_replayed_row_carries_brief_theme(self):
        # GIVEN a candidate whose brief carries a theme. WHEN replayed. THEN the
        # store row carries that theme (it travels with the outcome record, not a
        # downstream join).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [{"ticker": "NVDA", "setup": _OK_SETUP, "theme": "AI infra"}],
        )

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
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        # Brief theme "AI infra" is stamped as its canonical slug.
        self.assertEqual(row["theme"], "ai_infra")

    def test_frozen_terminal_keeps_original_theme_when_brief_theme_drifts(self):
        # GIVEN a terminal row stamped with theme T1. WHEN a later run sees the SAME
        # date's brief now carrying a drifted theme T2 (the 6x/day churn). THEN the
        # FROZEN row keeps T1 (provenance preserved, not re-stamped to T2).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)

        def _fetch(ticker, start, end):  # fast TP_FULL → terminal on run 1
            base = int(start.timestamp() * 1000)
            minute = 60_000
            return [
                {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {"t": base + minute, "o": 100.0, "h": 200.0, "l": 100.0, "c": 200.0, "v": 1000.0},
            ]

        _write_brief(
            self.briefs_dir,
            brief_date,
            [{"ticker": "NVDA", "setup": _OK_SETUP, "theme": "defense"}],
        )
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        self.assertTrue(
            bool(self._read_store(brief_date).set_index("ticker").loc["NVDA"]["terminal"])
        )

        # Brief theme drifts; re-run. The frozen terminal must keep its original theme.
        _write_brief(
            self.briefs_dir,
            brief_date,
            [{"ticker": "NVDA", "setup": _OK_SETUP, "theme": "ai-mania"}],
        )
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(row["theme"], "defense")


class TestScorerVersionUnit(unittest.TestCase):
    def test_stamp_scorer_version_sets_column(self):
        # _stamp_scorer_version writes scorer_config_version onto the row.
        row = _stamp_scorer_version({}, "scorer-v1-test")
        self.assertEqual(row["scorer_config_version"], "scorer-v1-test")

    def test_stamp_scorer_version_none_when_falsy(self):
        # A falsy version (empty string / None) stores None, not "".
        self.assertIsNone(_stamp_scorer_version({}, None)["scorer_config_version"])
        self.assertIsNone(_stamp_scorer_version({}, "")["scorer_config_version"])

    def test_carry_prior_backfills_scorer_config_version(self):
        # _carry_prior must back-fill scorer_config_version to None for rows that
        # predate the column (old-format parquets).
        carried = _carry_prior({"ticker": "X"})
        self.assertIn("scorer_config_version", carried)
        self.assertIsNone(carried["scorer_config_version"])


class TestScorerVersionProvenance(_MonitorTestBase):
    def test_replayed_row_carries_scorer_config_version(self):
        # GIVEN a candidate whose brief carries scorer_config_version. WHEN replayed.
        # THEN the store row carries that version (provenance from the brief).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)

        # Write a brief parquet that includes scorer_config_version.
        df = pd.DataFrame(
            [
                {
                    "ticker": "NVDA",
                    "theme": "ai",
                    "verified": True,
                    "brief_trade_setup": json.dumps(_OK_SETUP),
                    "scorer_config_version": "scorer-v1-test",
                }
            ]
        )
        df.to_parquet(self.briefs_dir / f"{brief_date.isoformat()}.parquet")

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
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(row["scorer_config_version"], "scorer-v1-test")


class TestScorerVersionFreshBrief(_MonitorTestBase):
    """Cover the _carry_or_placeholder path (horizon < arrival_session).

    When the monitor runs on the SAME day the brief was published (market has not
    yet closed for that session), the replay horizon is strictly before the arrival
    session.  _screen_one delegates to _carry_or_placeholder without fetching any
    bars.  Two sub-cases:

    * prior is None (first run ever): a retryable placeholder is written (line 1675).
    * prior is not None (second run, same narrow window): the placeholder from run 1
      is carried forward verbatim (line 1670).

    Both cases must stamp scorer_config_version from the brief.
    """

    def _write_brief_with_version(
        self, brief_date: dt.date, ticker: str, scorer_version: str
    ) -> None:
        # Write a brief parquet with scorer_config_version so the loader picks it up.
        df = pd.DataFrame(
            [
                {
                    "ticker": ticker,
                    "theme": "ai",
                    "verified": True,
                    "brief_trade_setup": json.dumps(_OK_SETUP),
                    "scorer_config_version": scorer_version,
                }
            ]
        )
        df.to_parquet(self.briefs_dir / f"{brief_date.isoformat()}.parquet")

    def test_fresh_brief_no_prior_yields_placeholder_with_scorer_version(self):
        # GIVEN a plannable candidate published TODAY (brief_date == now.date()).
        # WHEN the monitor runs before the session closes (last_closed_session is
        # the day before brief_date). THEN the arrival session is in the future
        # relative to the horizon, so _carry_or_placeholder is called with
        # prior=None and a retryable placeholder row is written that carries the
        # brief's scorer_config_version.
        brief_date = dt.date(2026, 5, 5)  # Tuesday
        # now is early morning of brief_date: last_closed_session = 2026-05-04 (Monday)
        # arrival_session = session_on_or_after(2026-05-05) = 2026-05-05
        # horizon = min(position_expiry, 2026-05-04) < arrival_session → placeholder.
        now = dt.datetime(2026, 5, 5, 7, 0, tzinfo=UTC)
        self._write_brief_with_version(brief_date, "AAPL", "scorer-fresh-v1")

        fetched: list[str] = []

        def _fetch(ticker, start, end):
            fetched.append(ticker)
            base = int(start.timestamp() * 1000)
            return [{"t": base, "o": 150.0, "h": 151.0, "l": 149.0, "c": 150.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
        )
        # No bars should be fetched — the horizon is before arrival, so the cheap
        # path and the minute resolve are both skipped.
        self.assertNotIn("AAPL", fetched)
        row = self._read_store(brief_date).set_index("ticker").loc["AAPL"]
        # Placeholder row: not terminal, scorer_config_version propagated from brief.
        self.assertFalse(bool(row["terminal"]))
        self.assertEqual(row["scorer_config_version"], "scorer-fresh-v1")

    def test_fresh_brief_with_prior_carries_prior_with_scorer_version(self):
        # GIVEN the same fresh-brief scenario run TWICE with the same narrow now.
        # WHEN run 2 sees the placeholder written by run 1 as an existing prior.
        # THEN _carry_or_placeholder is called with prior=not-None and carries
        # that prior forward, stamping scorer_config_version from the current brief.
        brief_date = dt.date(2026, 5, 5)
        now = dt.datetime(2026, 5, 5, 7, 0, tzinfo=UTC)
        self._write_brief_with_version(brief_date, "AAPL", "scorer-fresh-v2")

        def _no_fetch(ticker, start, end):
            return []

        # Run 1: prior=None → placeholder written (line 1675).
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_no_fetch,
            now=now,
        )
        row_run1 = self._read_store(brief_date).set_index("ticker").loc["AAPL"]
        self.assertFalse(bool(row_run1["terminal"]))

        # Run 2: prior=placeholder from run 1 → _carry_or_placeholder with prior
        # not None → carried row (line 1670); scorer_config_version must propagate.
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_no_fetch,
            now=now,
        )
        row_run2 = self._read_store(brief_date).set_index("ticker").loc["AAPL"]
        self.assertFalse(bool(row_run2["terminal"]))
        self.assertEqual(row_run2["scorer_config_version"], "scorer-fresh-v2")


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
        self.assertLessEqual(int(row.loc["holding_days_elapsed"]), 1)

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

    def test_missing_brief_leaves_size_null(self):
        # GIVEN a nulled terminal row whose brief file no longer exists. WHEN
        # enrichment runs. THEN it recomputes nothing (n == 0) and the row stays
        # NULL — the date is genuinely unresolvable, never fudged.
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            enrich_store_with_size_fields,
        )

        brief_date, path = self._seed_terminal_row(_THREE_TIER_SETUP)
        _null_size_columns(path)
        (self.briefs_dir / f"{brief_date.isoformat()}.parquet").unlink()

        n = enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        self.assertEqual(n, 0)
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        for col in _SIZE_COLS:
            self.assertTrue(pd.isna(row[col]))

    def test_ticker_absent_from_brief_leaves_size_null(self):
        # GIVEN a nulled terminal NVDA row whose brief was rewritten without NVDA.
        # WHEN enrichment runs. THEN the setup lookup misses, n == 0, size stays NULL.
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            enrich_store_with_size_fields,
        )

        brief_date, path = self._seed_terminal_row(_THREE_TIER_SETUP)
        _null_size_columns(path)
        # Rewrite the brief with a DIFFERENT ticker so NVDA's setup is absent.
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "OTHER", "setup": _THREE_TIER_SETUP}])

        n = enrich_store_with_size_fields(self.store_dir, self.briefs_dir)
        self.assertEqual(n, 0)
        row = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        for col in _SIZE_COLS:
            self.assertTrue(pd.isna(row[col]))


class TestSizeEnrichmentHelpers(unittest.TestCase):
    """Direct unit tests of the size-backfill pure helpers + their guard branches."""

    def test_parse_filled_entry_ids(self):
        from alphalens_pipeline.feedback.population_ladder_monitor import _parse_filled_entry_ids

        self.assertEqual(_parse_filled_entry_ids("E1->E2->TP1->SL"), ["E1", "E2"])
        self.assertEqual(_parse_filled_entry_ids("E1->E1->TP1"), ["E1"])  # de-dup
        self.assertEqual(_parse_filled_entry_ids("TP1->SL"), [])  # no entry crossings
        self.assertEqual(_parse_filled_entry_ids(None), [])
        self.assertEqual(_parse_filled_entry_ids(123), [])  # non-string

    def test_rederive_filled_fraction(self):
        from alphalens_pipeline.feedback.population_ladder_monitor import _rederive_filled_fraction

        self.assertIsNone(_rederive_filled_fraction(_THREE_TIER_SETUP, []))  # nothing filled
        # E1 of 28/34/38 -> 0.28; E1+E2 -> 0.62.
        self.assertAlmostEqual(_rederive_filled_fraction(_THREE_TIER_SETUP, ["E1"]), 0.28, places=9)
        self.assertAlmostEqual(
            _rederive_filled_fraction(_THREE_TIER_SETUP, ["E1", "E2"]), 0.62, places=9
        )
        # An id absent from the ladder is ignored -> no usable fill -> None.
        self.assertIsNone(_rederive_filled_fraction(_THREE_TIER_SETUP, ["E9"]))

    def test_size_fields_from_row_no_suggested_size_leaves_book_fields_null(self):
        # A NO_STRUCTURE setup (no suggested_size, no usable geometry) yields a dict
        # whose book-weight fields are None (unknowable) — never fudged. The count
        # fields are still concrete (0 / 0.0), so the dict is returned as-is.
        from alphalens_pipeline.feedback.population_ladder_monitor import _size_fields_from_row

        row = {"blended_entry": None, "sequence_str": None, "realized_r": None, "open_r": None}
        fields = _size_fields_from_row(row, _NO_STRUCTURE_SETUP)
        self.assertIsNotNone(fields)
        self.assertIsNone(fields["suggested_gross_weight_pct"])
        self.assertIsNone(fields["realized_gross_weight_pct"])
        self.assertIsNone(fields["realized_return_pct_of_book"])
        self.assertEqual(fields["tiers_filled_count"], 0)

    def test_needs_size_enrichment(self):
        from alphalens_pipeline.feedback.population_ladder_monitor import _needs_size_enrichment

        base = {"plannable": True, "realized_gross_weight_pct": None}
        self.assertTrue(_needs_size_enrichment(base))
        self.assertFalse(_needs_size_enrichment({**base, "realized_gross_weight_pct": 0.04}))
        self.assertFalse(_needs_size_enrichment({**base, "plannable": False}))

    def test_load_setups_for_date_broad_except_returns_none(self):
        # An unanticipated load_brief error (not FileNotFoundError/ValueError) must be
        # caught and yield None — the sweep continues (the zen-review hardening).
        from unittest.mock import patch

        from alphalens_pipeline.feedback import population_ladder_monitor as mon

        with patch.object(mon, "load_brief", side_effect=RuntimeError("boom")):
            self.assertIsNone(mon._load_setups_for_date(dt.date(2026, 5, 1), Path("/nonexistent")))


class TestLookbackConstant(unittest.TestCase):
    def test_monitor_lookback_is_distinct_and_large_enough(self):
        # The monitor uses its OWN lookback (>= ~60 calendar days for 42 sessions),
        # NOT shadow_return's 14-day window.
        self.assertGreaterEqual(MONITOR_LOOKBACK_DAYS, 60)
        self.assertNotEqual(MONITOR_LOOKBACK_DAYS, 14)


# --------------------------------------------------------------------------- #
# Grouped-daily two-tier screen — unit + integration tests.                   #
# --------------------------------------------------------------------------- #

_XNYS = "XNYS"
_DAY_MS = 86_400_000
_MIN_MS = 60_000


def _grouped(**by_ticker_ohlc) -> dict[str, dict]:
    """Build a grouped-daily map {TICKER: {o,h,l,c,v,vw,t}} from kwargs.

    Each value is a 5-tuple (o, h, l, c, v); t/vw are synthesized.
    """
    out = {}
    for ticker, (o, h, low, c, v) in by_ticker_ohlc.items():
        out[ticker.upper()] = {"t": 0, "o": o, "h": h, "l": low, "c": c, "v": v, "vw": c}
    return out


def _open_prior(
    *,
    setup: dict,
    last_priced_session: dt.date,
    last_resolved_session: dt.date | None = None,
    blended_entry: float = 100.0,
    last_close: float = 100.0,
    reference_close: float = 100.0,
    sequence_str: str = "E1",
    classification: str = "OPEN",
    realized_risk_pct: float = 0.001,
) -> dict:
    """A minimal OPEN prior store row sufficient for the screen + cheap path.

    ``last_resolved_session`` defaults to ``last_priced_session`` so a prior built
    here looks freshly minute-resolved (R7 does NOT fire). Pass an explicit
    OLDER session to model a row the cheap path has advanced past its last resolve.
    """
    return {
        "ladder_classification": classification,
        "terminal": False,
        "blended_entry": blended_entry,
        "last_close": last_close,
        "reference_close": reference_close,
        "last_priced_session": last_priced_session,
        "last_resolved_session": (
            last_resolved_session if last_resolved_session is not None else last_priced_session
        ),
        "open_r": 0.0,
        "realized_r": None,
        "sequence_str": sequence_str,
        "holding_days_elapsed": 1,
        "realized_risk_pct": realized_risk_pct,
    }


class TestScreenDecision(unittest.TestCase):
    """The needs_minute_resolve predicate, exercised directly on prior + grouped."""

    def setUp(self):
        self.brief_date = dt.date(2026, 5, 1)
        self.cutoffs = _engine_cutoffs(self.brief_date, _OK_SETUP, _XNYS)
        self.arrival = self.cutoffs[0]
        # A few sessions in (entry window for _OK_SETUP = 7 sessions still open).
        self.last_priced = advance_trading_sessions(self.arrival, 1, _XNYS)
        self.new_session = advance_trading_sessions(self.arrival, 2, _XNYS)
        self.last_closed = self.new_session
        self.prev_session = advance_trading_sessions(self.arrival, 1, _XNYS)

    def _decide(self, prior, grouped_by_session):
        last_resolved = _coerce_session(prior.get("last_resolved_session")) if prior else None
        return _screen_decision(
            prior,
            "NVDA",
            _OK_SETUP,
            [self.new_session],
            grouped_by_session,
            self.cutoffs,
            self.last_priced,
            last_resolved,
            self.last_closed,
            _XNYS,
        )

    def test_no_touch_open_is_cheap(self):
        # OPEN @ blended 100, stop 95, lowest TP 110. A day entirely inside
        # (95, 110) touches nothing -> no resolve.
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        grouped = {
            self.new_session: _grouped(NVDA=(101.0, 103.0, 99.0, 102.0, 1000)),
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        self.assertFalse(self._decide(prior, grouped).needs_resolve)

    def test_stop_touch_forces_resolve(self):
        # Day low pierces the disaster_stop (95) -> resolve, touched.
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        grouped = {
            self.new_session: _grouped(NVDA=(100.0, 101.0, 94.0, 96.0, 1000)),
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        d = self._decide(prior, grouped)
        self.assertTrue(d.needs_resolve)
        self.assertTrue(d.touched)

    def test_tp_touch_forces_resolve(self):
        # Day high reaches the lowest un-hit TP (110) -> resolve.
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        grouped = {
            self.new_session: _grouped(NVDA=(100.0, 111.0, 99.0, 109.0, 1000)),
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        self.assertTrue(self._decide(prior, grouped).needs_resolve)

    def test_eps_band_resolves_low_one_tick_above_stop(self):
        # Daily low one tick ABOVE the stop but within eps still resolves (the true
        # minute low could be one tick below -> SL_HIT). 95*(1+eps)=95.2375.
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        near = 95.0 * (1 + _TOUCH_EPS) - 0.01  # inside the band
        self.assertGreater(near, 95.0)  # genuinely above the raw stop
        grouped = {
            self.new_session: _grouped(NVDA=(100.0, 101.0, near, 99.0, 1000)),
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        self.assertTrue(self._decide(prior, grouped).needs_resolve)

    def test_missing_daily_bar_fails_closed(self):
        # The new session's grouped map is absent for the ticker -> resolve (fail closed).
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        grouped = {
            self.new_session: _grouped(OTHER=(100.0, 101.0, 99.0, 100.0, 1000)),  # no NVDA
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        self.assertTrue(self._decide(prior, grouped).needs_resolve)

    def test_missing_session_gap_fails_closed(self):
        # The session is an upstream gap (None) -> resolve (never cheap-advance).
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        grouped = {
            self.new_session: None,
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        self.assertTrue(self._decide(prior, grouped).needs_resolve)

    def test_missing_prev_close_fails_closed_when_no_last_close(self):
        # prev_c unavailable from cache AND prior has no last_close -> resolve.
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        prior["last_close"] = None
        grouped = {self.new_session: _grouped(NVDA=(100.0, 103.0, 99.0, 102.0, 1000))}
        self.assertTrue(self._decide(prior, grouped).needs_resolve)

    def test_split_class_day_resolves(self):
        # |c*/prev_c - 1| = 0.5 (a 2:1 unadjusted halving) -> resolve.
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        grouped = {
            self.new_session: _grouped(NVDA=(50.0, 52.0, 49.0, 50.0, 1000)),  # ~half
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        self.assertTrue(self._decide(prior, grouped).needs_resolve)

    def test_normal_vol_day_not_split(self):
        # |c*/prev_c - 1| = 0.05 is well below 0.18 -> not a split trigger (and no
        # level touch) -> cheap.
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.last_priced)
        grouped = {
            self.new_session: _grouped(NVDA=(100.0, 106.0, 99.0, 105.0, 1000)),
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        self.assertFalse(self._decide(prior, grouped).needs_resolve)

    def test_brand_new_prior_is_forced_resolve(self):
        d = _screen_decision(
            None,
            "NVDA",
            _OK_SETUP,
            [self.new_session],
            {self.new_session: _grouped(NVDA=(100.0, 101.0, 99.0, 100.0, 1000))},
            self.cutoffs,
            None,
            None,
            self.last_closed,
            _XNYS,
        )
        self.assertTrue(d.needs_resolve)
        self.assertTrue(d.forced)

    def test_partial_tp_open_always_resolves(self):
        prior = _open_prior(
            setup=_OK_SETUP,
            last_priced_session=self.last_priced,
            classification="PARTIAL_TP_OPEN",
            sequence_str="E1->TP1",
        )
        grouped = {
            self.new_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        d = self._decide(prior, grouped)
        self.assertTrue(d.needs_resolve)

    def test_periodic_forced_resolve_when_far_behind(self):
        # last_priced > K=5 sessions behind last_closed -> forced resolve even with
        # no touch.
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=self.arrival)
        far_closed = advance_trading_sessions(self.arrival, 7, _XNYS)
        new_sessions = [advance_trading_sessions(self.arrival, i, _XNYS) for i in range(1, 8)]
        grouped = {s: _grouped(NVDA=(100.0, 101.0, 99.0, 100.0, 1000)) for s in new_sessions}
        grouped[self.arrival] = _grouped(NVDA=(100.0, 101.0, 99.0, 100.0, 1000))
        d = _screen_decision(
            prior,
            "NVDA",
            _OK_SETUP,
            new_sessions,
            grouped,
            self.cutoffs,
            self.arrival,
            self.arrival,
            far_closed,
            _XNYS,
        )
        self.assertTrue(d.needs_resolve)
        self.assertTrue(d.forced)


class TestUnfilledEntryTierTouch(unittest.TestCase):
    """An OPEN row with un-filled entry tiers resolves on a new low reaching them."""

    def setUp(self):
        self.brief_date = dt.date(2026, 5, 1)
        self.cutoffs = _engine_cutoffs(self.brief_date, _THREE_TIER_SETUP, _XNYS)
        self.arrival = self.cutoffs[0]
        self.last_priced = advance_trading_sessions(self.arrival, 1, _XNYS)
        self.new_session = advance_trading_sessions(self.arrival, 2, _XNYS)
        self.prev_session = self.last_priced

    def _decide(self, prior, grouped, last_closed):
        return _screen_decision(
            prior,
            "NVDA",
            _THREE_TIER_SETUP,
            [self.new_session],
            grouped,
            self.cutoffs,
            self.last_priced,
            self.last_priced,
            last_closed,
            _XNYS,
        )

    def test_new_low_to_e2_while_window_open_resolves(self):
        # E1 (100) filled; E2 (98) un-filled; entry window still open. A new daily
        # low of 97.5 reaches E2 (but not stop 70 nor any TP) -> resolve.
        prior = _open_prior(
            setup=_THREE_TIER_SETUP,
            last_priced_session=self.last_priced,
            blended_entry=100.0,
            sequence_str="E1",
        )
        grouped = {
            self.new_session: _grouped(NVDA=(99.0, 99.5, 97.5, 98.0, 1000)),
            self.prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        d = self._decide(prior, grouped, self.new_session)
        self.assertTrue(d.needs_resolve)

    def test_e2_dropped_once_entry_window_closed(self):
        # SAME E2-reaching low, but the entry window has fully closed (last_closed is
        # past entry_expiry). E2 is dropped from the touch set -> no resolve.
        entry_expiry = self.cutoffs[1]
        far_closed = advance_trading_sessions(entry_expiry, 2, _XNYS)
        new_session = advance_trading_sessions(entry_expiry, 1, _XNYS)
        prev_session = entry_expiry
        prior = _open_prior(
            setup=_THREE_TIER_SETUP,
            last_priced_session=entry_expiry,
            blended_entry=100.0,
            sequence_str="E1",
        )
        grouped = {
            new_session: _grouped(NVDA=(99.0, 99.5, 97.5, 98.0, 1000)),
            prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        d = _screen_decision(
            prior,
            "NVDA",
            _THREE_TIER_SETUP,
            [new_session],
            grouped,
            self.cutoffs,
            entry_expiry,
            entry_expiry,
            far_closed,
            _XNYS,
        )
        self.assertFalse(d.needs_resolve)


class TestCheapPathNoMultiDayFreeze(unittest.TestCase):
    """A legitimate multi-day trend (no single-day split) must NOT freeze the mark.

    The cheap path has NO multi-day implausible guard: ``_screen_decision`` is the
    sole, exact split gate (it checks every consecutive day-over-day close ratio),
    so a c* that drifted >18% from a multi-day-old prior mark via small daily steps
    still cheap-advances. The removed guard compared c* to a possibly days-old
    ``prior.last_close``, which false-froze compounding trends.
    """

    def test_cheap_update_advances_on_large_multiday_trend(self):
        from alphalens_pipeline.feedback.population_ladder_monitor import _cheap_update_row

        brief_date = dt.date(2026, 5, 1)
        cutoffs = _engine_cutoffs(brief_date, _OK_SETUP, _XNYS)
        arrival = cutoffs[0]
        last_priced = advance_trading_sessions(arrival, 1, _XNYS)
        new_session = advance_trading_sessions(arrival, 2, _XNYS)
        last_closed = new_session
        # Prior mark 80; c* 100 = +25% accumulated over several no-split sessions.
        # The OLD guard (|100/80-1| = 0.25 > 0.18) would have frozen this; the fix
        # advances it (the per-day screen, run upstream, found no single-day split).
        prior = _open_prior(
            setup=_OK_SETUP, last_priced_session=last_priced, last_close=80.0, blended_entry=100.0
        )
        self.assertGreater(abs(100.0 / 80.0 - 1), _SPLIT_SCREEN_THRESHOLD)
        # No-touch bar: high 101 < TP 110, low 99 > disaster_stop 95.
        grouped = {new_session: _grouped(NVDA=(99.5, 101.0, 99.0, 100.0, 1000))}
        result = _cheap_update_row(
            _OK_SETUP,
            prior,
            "NVDA",
            [new_session],
            grouped,
            cutoffs,
            last_closed,
            reference_close=100.0,
        )
        self.assertIsNotNone(result, "a legit multi-day trend must NOT be frozen")
        assert result is not None
        row, category = result
        self.assertEqual(category, "cheap")
        self.assertEqual(row["last_close"], 100.0)
        # forward_return is refreshed for the ongoing mark (gemini MEDIUM fix —
        # applies to every ongoing state, not only OPEN).
        self.assertAlmostEqual(row["forward_return"], (100.0 - 100.0) / 100.0, places=9)


class TestCheapAdvanceWidensExcursionBand(unittest.TestCase):
    """A cheap daily advance refreshes ``open_r`` from the close, so the carried
    minute-replay ``mfe`` / ``mae`` band MUST be widened to keep containing it.

    edge-data audit 2026-06-18: 30% of OPEN rows had ``open_r`` outside ``[mae, mfe]``
    because the cheap path moved the mark but left the (older) minute-replay band
    frozen. A daily close is a point ON the path, so the running max-favorable
    excursion is at least ``open_r`` and the max-adverse at most ``open_r``.
    """

    def _advance(self, c_star: float, *, prior_mfe, prior_mae) -> dict:
        from alphalens_pipeline.feedback.population_ladder_monitor import _cheap_update_row

        brief_date = dt.date(2026, 5, 1)
        cutoffs = _engine_cutoffs(brief_date, _OK_SETUP, _XNYS)
        arrival = cutoffs[0]
        last_priced = advance_trading_sessions(arrival, 1, _XNYS)
        new_session = advance_trading_sessions(arrival, 2, _XNYS)
        prior = _open_prior(setup=_OK_SETUP, last_priced_session=last_priced)
        prior["mfe"] = prior_mfe
        prior["mae"] = prior_mae
        grouped = {new_session: _grouped(NVDA=(c_star, c_star, c_star, c_star, 1000))}
        result = _cheap_update_row(
            _OK_SETUP,
            prior,
            "NVDA",
            [new_session],
            grouped,
            cutoffs,
            new_session,
            reference_close=100.0,
        )
        assert result is not None
        row, category = result
        self.assertEqual(category, "cheap")
        return row

    def test_upside_close_extends_mfe_so_band_contains_open_r(self):
        # c*=108 -> open_r = (108-100)/5 = 1.6, far above the stale mfe 0.2.
        row = self._advance(108.0, prior_mfe=0.2, prior_mae=-0.1)
        self.assertAlmostEqual(row["open_r"], 1.6, places=9)
        self.assertGreaterEqual(row["mfe"], row["open_r"])  # band contains the mark
        self.assertAlmostEqual(row["mfe"], 1.6, places=9)  # widened up to the mark
        self.assertAlmostEqual(row["mae"], -0.1, places=9)  # downside bound untouched

    def test_downside_close_extends_mae_so_band_contains_open_r(self):
        # c*=97 -> open_r = (97-100)/5 = -0.6, below the stale mae -0.1.
        row = self._advance(97.0, prior_mfe=0.2, prior_mae=-0.1)
        self.assertAlmostEqual(row["open_r"], -0.6, places=9)
        self.assertLessEqual(row["mae"], row["open_r"])  # band contains the mark
        self.assertAlmostEqual(row["mae"], -0.6, places=9)  # widened down to the mark
        self.assertAlmostEqual(row["mfe"], 0.2, places=9)  # upside bound untouched

    def test_band_seeded_from_open_r_when_prior_has_no_excursions(self):
        # A prior with no mfe/mae (e.g. first cheap night after a fill) seeds the
        # band from the live open_r rather than leaving it null/NaN.
        row = self._advance(108.0, prior_mfe=None, prior_mae=None)
        self.assertAlmostEqual(row["open_r"], 1.6, places=9)
        self.assertAlmostEqual(row["mfe"], 1.6, places=9)
        self.assertAlmostEqual(row["mae"], 1.6, places=9)


class TestCheapOpenRMatchesReplay(unittest.TestCase):
    """The load-bearing property: cheap open_r == full replay_ladder open_r."""

    def test_cheap_open_r_equals_replay_realized_r_no_touch(self):
        # An OPEN no-TP position marked to a daily close c* via the cheap formula
        # must equal replay_ladder's mark-to-last-close realized_r over an RTH path
        # whose last close is c*, with NO level touched.
        c_star = 103.0
        blended, stop = 100.0, 95.0
        cheap = _cheap_open_r(c_star, blended, stop)

        # Build an RTH minute path: fill E1 at 100 on bar 1, drift to close c* with
        # no TP (110) / SL (95) touch.
        bars = [
            {"t": 0, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
            {"t": _MIN_MS, "o": 100.0, "h": 104.0, "l": 100.0, "c": c_star, "v": 1000.0},
        ]
        outcome = replay_ladder(_OK_SETUP, bars, reference_close=100.0)
        self.assertEqual(outcome.classification, "OPEN")
        self.assertAlmostEqual(cheap, outcome.realized_r, places=9)


class _GroupedConsistentBase(_MonitorTestBase):
    """Integration base that serves RTH minute bars + a CONSISTENT grouped-daily map.

    The minute fetch returns one RTH bar per session (at the session open); the
    grouped fetch returns the SAME daily OHLC, so daily [low, high] is a true
    superset of the minute path by construction.
    """

    def _session_open_ms(self, session: dt.date) -> int:
        return int(session_open_utc(session, _XNYS).timestamp() * 1000)


class TestRthAlignment(_GroupedConsistentBase):
    def test_after_hours_stop_ignored_by_rth_filter(self):
        # GIVEN a post-16:00 ET bar that pierces the disaster_stop, recovered by the
        # next open. WHEN replayed. THEN _filter_bars_to_rth drops the AH bar so the
        # minute path never records an SL_HIT (daily + minute AGREE).
        from alphalens_pipeline.feedback.population_ladder_monitor import _filter_bars_to_rth

        brief_date = dt.date(2026, 5, 1)
        arrival = session_on_or_after(brief_date, _XNYS)
        open_ms = int(session_open_utc(arrival, _XNYS).timestamp() * 1000)
        bars = [
            {"t": open_ms, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},  # RTH fill
            # An after-hours bar 9 hours after open (well past the 390-min RTH close)
            # that pierces the stop 95.
            {
                "t": open_ms + 9 * 60 * _MIN_MS,
                "o": 96.0,
                "h": 96.0,
                "l": 90.0,
                "c": 95.5,
                "v": 10.0,
            },
        ]
        kept = _filter_bars_to_rth(bars, arrival, arrival, _XNYS)
        # Only the RTH bar survives; the AH stop-piercing bar is dropped.
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["t"], open_ms)
        outcome = replay_ladder(_OK_SETUP, kept, reference_close=100.0)
        self.assertFalse(outcome.sl_hit)


class TestGroupedDailyIntegration(_GroupedConsistentBase):
    def test_no_touch_night_zero_minute_fetches_but_advances_open_r(self):
        # GIVEN an OPEN row established on run 1 (one minute fetch). WHEN run 2 runs a
        # later night whose new daily bar touches no level. THEN run 2 issues ZERO
        # minute fetches yet advances open_r / forward_return from the daily close.
        brief_date = dt.date(2026, 5, 1)
        arrival = session_on_or_after(brief_date, _XNYS)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _minute_fetch_run1(ticker, start, end):
            open_ms = int(session_open_utc(arrival, _XNYS).timestamp() * 1000)
            return [
                {"t": open_ms, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {
                    "t": open_ms + _MIN_MS,
                    "o": 100.0,
                    "h": 103.0,
                    "l": 100.0,
                    "c": 102.0,
                    "v": 1000.0,
                },
            ]

        now1 = dt.datetime(2026, 5, 8, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now1.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute_fetch_run1,
            grouped_fetch=lambda d: {},  # run 1 = brand-new, screen never consults grouped
            now=now1,
        )
        before = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertFalse(bool(before["terminal"]))
        self.assertEqual(before["ladder_classification"], "OPEN")
        open_r_before = float(before["open_r"])

        # Run 2: a later night. The daily grouped bar drifts to 104 (no stop/TP/entry
        # touch). The minute fetch must NOT be called.
        minute_calls: list[str] = []

        def _minute_guard(ticker, start, end):
            minute_calls.append(ticker)
            return _minute_fetch_run1(ticker, start, end)

        def _grouped_fetch(date):
            # Same daily OHLC for every needed date: a quiet drift up to 104.
            return {
                "NVDA": {
                    "t": 0,
                    "o": 102.0,
                    "h": 105.0,
                    "l": 101.0,
                    "c": 104.0,
                    "v": 1000.0,
                    "vw": 104.0,
                }
            }

        now2 = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now2.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute_guard,
            grouped_fetch=_grouped_fetch,
            now=now2,
        )
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertEqual(minute_calls, [], "no-touch night must issue ZERO minute fetches")
        self.assertEqual(after["ladder_classification"], "OPEN")
        # open_r advanced to the cheap mark: (104 - 100)/(100 - 95) = 0.8.
        self.assertAlmostEqual(float(after["open_r"]), (104.0 - 100.0) / (100.0 - 95.0), places=6)
        self.assertNotAlmostEqual(float(after["open_r"]), open_r_before, places=6)
        # open_return_pct_of_book recomputed from the refreshed open_r.
        self.assertAlmostEqual(
            float(after["open_return_pct_of_book"]),
            float(after["open_r"]) * float(after["realized_risk_pct"]),
            places=9,
        )

    def test_touch_night_resolves_via_minute_fetch(self):
        # GIVEN the same OPEN row. WHEN run 2's daily bar pierces the stop. THEN the
        # minute fetch IS called and the row resolves precisely.
        brief_date = dt.date(2026, 5, 1)
        arrival = session_on_or_after(brief_date, _XNYS)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _minute_run1(ticker, start, end):
            open_ms = int(session_open_utc(arrival, _XNYS).timestamp() * 1000)
            return [
                {"t": open_ms, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {
                    "t": open_ms + _MIN_MS,
                    "o": 100.0,
                    "h": 103.0,
                    "l": 100.0,
                    "c": 102.0,
                    "v": 1000.0,
                },
            ]

        now1 = dt.datetime(2026, 5, 8, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now1.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute_run1,
            grouped_fetch=lambda d: {},
            now=now1,
        )

        minute_calls: list[str] = []

        def _minute_run2(ticker, start, end):
            minute_calls.append(ticker)
            open_ms = int(session_open_utc(arrival, _XNYS).timestamp() * 1000)
            later = advance_trading_sessions(arrival, 6, _XNYS)
            later_ms = int(session_open_utc(later, _XNYS).timestamp() * 1000)
            return [
                {"t": open_ms, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {
                    "t": later_ms,
                    "o": 96.0,
                    "h": 96.0,
                    "l": 94.0,
                    "c": 95.0,
                    "v": 1000.0,
                },  # SL pierce
            ]

        def _grouped(date):
            return {
                "NVDA": {
                    "t": 0,
                    "o": 96.0,
                    "h": 96.0,
                    "l": 94.0,
                    "c": 95.0,
                    "v": 1000.0,
                    "vw": 95.0,
                }
            }

        now2 = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now2.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute_run2,
            grouped_fetch=_grouped,
            now=now2,
        )
        self.assertIn("NVDA", minute_calls, "a touch night must resolve via the minute fetch")
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        self.assertTrue(bool(after["terminal"]))
        self.assertEqual(after["ladder_classification"], "SL_HIT")

    def test_two_disjoint_tickers_share_one_grouped_fetch(self):
        # GIVEN two OPEN candidates on the SAME date with disjoint tickers. WHEN the
        # screen runs. THEN the grouped-daily fetch is called ONCE per session (the
        # whole-market payload is shared), and both resolve from the single cache.
        brief_date = dt.date(2026, 5, 1)
        arrival = session_on_or_after(brief_date, _XNYS)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [{"ticker": "AAA", "setup": _OK_SETUP}, {"ticker": "BBB", "setup": _OK_SETUP}],
        )

        def _minute(ticker, start, end):
            open_ms = int(session_open_utc(arrival, _XNYS).timestamp() * 1000)
            return [
                {"t": open_ms, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {
                    "t": open_ms + _MIN_MS,
                    "o": 100.0,
                    "h": 103.0,
                    "l": 100.0,
                    "c": 102.0,
                    "v": 1000.0,
                },
            ]

        now1 = dt.datetime(2026, 5, 8, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now1.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute,
            grouped_fetch=lambda d: {},
            now=now1,
        )

        grouped_dates: list[dt.date] = []

        def _grouped(date):
            grouped_dates.append(date)
            return {
                "AAA": {
                    "t": 0,
                    "o": 102.0,
                    "h": 105.0,
                    "l": 101.0,
                    "c": 104.0,
                    "v": 1000.0,
                    "vw": 104.0,
                },
                "BBB": {
                    "t": 0,
                    "o": 102.0,
                    "h": 105.0,
                    "l": 101.0,
                    "c": 104.0,
                    "v": 1000.0,
                    "vw": 104.0,
                },
            }

        now2 = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now2.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute,
            grouped_fetch=_grouped,
            now=now2,
        )
        # Each date fetched at most once (no per-candidate re-fetch).
        self.assertEqual(len(grouped_dates), len(set(grouped_dates)))
        df = self._read_store(brief_date).set_index("ticker")
        # Both advanced cheaply to the same open_r.
        self.assertAlmostEqual(
            float(df.loc["AAA", "open_r"]), float(df.loc["BBB", "open_r"]), places=9
        )

    def test_missing_daily_bar_does_not_cheap_advance(self):
        # GIVEN an OPEN row. WHEN the new session's grouped map lacks the ticker
        # (halt / gap) AND the minute fetch fails. THEN the row is carried (NOT
        # cheap-advanced) — open_r/last_close unchanged.
        brief_date = dt.date(2026, 5, 1)
        arrival = session_on_or_after(brief_date, _XNYS)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _minute_run1(ticker, start, end):
            open_ms = int(session_open_utc(arrival, _XNYS).timestamp() * 1000)
            return [
                {"t": open_ms, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {
                    "t": open_ms + _MIN_MS,
                    "o": 100.0,
                    "h": 103.0,
                    "l": 100.0,
                    "c": 102.0,
                    "v": 1000.0,
                },
            ]

        now1 = dt.datetime(2026, 5, 8, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now1.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute_run1,
            grouped_fetch=lambda d: {},
            now=now1,
        )
        before = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        open_r_before = before["open_r"]

        def _minute_boom(ticker, start, end):
            raise ValueError("polygon outage")

        now2 = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now2.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute_boom,
            grouped_fetch=lambda d: {
                "OTHER": {"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}
            },
            now=now2,
        )
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        # Carried verbatim: open_r unchanged (NOT advanced from a phantom close).
        if pd.isna(open_r_before):
            self.assertTrue(pd.isna(after["open_r"]))
        else:
            self.assertAlmostEqual(float(after["open_r"]), float(open_r_before), places=9)


class TestForcedResolvePrecedenceAndFairness(_MonitorTestBase):
    def test_periodic_forced_resolve_precedes_no_fill_cheap_flip(self):
        # GIVEN a long-quiet NO_FILL row whose last_priced is > K sessions behind
        # last_closed AND whose entry window has closed (a cheap NO_FILL terminal
        # flip would otherwise fire). WHEN screened. THEN the R7 periodic resolve
        # takes precedence (needs_resolve True, forced True) — the cheap flip is NOT
        # taken that night.
        brief_date = dt.date(2026, 5, 1)
        cutoffs = _engine_cutoffs(brief_date, _OK_SETUP, _XNYS)
        arrival = cutoffs[0]
        entry_expiry = cutoffs[1]
        # last_priced far behind; last_closed well past entry_expiry.
        last_priced = arrival
        last_closed = advance_trading_sessions(entry_expiry, 3, _XNYS)
        new_sessions = [advance_trading_sessions(arrival, i, _XNYS) for i in range(1, 9)]
        grouped = {s: _grouped(NVDA=(105.0, 106.0, 104.0, 105.0, 1000)) for s in new_sessions}
        grouped[arrival] = _grouped(NVDA=(105.0, 106.0, 104.0, 105.0, 1000))
        prior = _open_prior(
            setup=_OK_SETUP,
            last_priced_session=last_priced,
            classification="NO_FILL",
            sequence_str="",
            blended_entry=None,
        )
        d = _screen_decision(
            prior,
            "NVDA",
            _OK_SETUP,
            new_sessions,
            grouped,
            cutoffs,
            last_priced,
            last_priced,
            last_closed,
            _XNYS,
        )
        self.assertTrue(d.needs_resolve)
        self.assertTrue(d.forced)

    def test_r7_fires_for_cheap_advanced_open_position(self):
        # REGRESSION: the cheap path advances last_priced_session EVERY night, so a
        # position that is cheap-advanced nightly keeps last_priced == last_closed
        # and never accumulates K sessions there. R7 must gate on
        # last_resolved_session (the last ACTUAL minute resolve) so it still fires:
        # last_priced is fresh (== last_closed) but last_resolved is > K sessions
        # behind -> needs_resolve True, forced True.
        brief_date = dt.date(2026, 5, 1)
        cutoffs = _engine_cutoffs(brief_date, _OK_SETUP, _XNYS)
        arrival = cutoffs[0]
        # last_resolved at arrival; cheap-advanced nightly to a last_priced that is
        # exactly the (fresh) last_closed, K+1 sessions past the last resolve.
        last_resolved = arrival
        last_closed = advance_trading_sessions(arrival, 6, _XNYS)  # 6 > K=5
        last_priced = last_closed  # cheap path kept it fully fresh
        new_session = last_closed
        prev_session = previous_trading_day(last_closed, _XNYS)
        # A no-touch day entirely inside (stop 95, TP 110) — the ONLY thing that can
        # force a resolve here is R7 (no level touch, no split, no missing bar).
        grouped = {
            new_session: _grouped(NVDA=(101.0, 103.0, 99.0, 102.0, 1000)),
            prev_session: _grouped(NVDA=(100.0, 101.0, 99.5, 100.0, 1000)),
        }
        prior = _open_prior(
            setup=_OK_SETUP,
            last_priced_session=last_priced,
            last_resolved_session=last_resolved,
        )

        # Control: gating R7 on a FRESH last_resolved (== last_priced == last_closed)
        # would NOT resolve — proving the only trigger here is the stale resolve.
        fresh = _screen_decision(
            prior,
            "NVDA",
            _OK_SETUP,
            [new_session],
            grouped,
            cutoffs,
            last_priced,
            last_priced,
            last_closed,
            _XNYS,
        )
        self.assertFalse(fresh.needs_resolve, "no touch + fresh resolve must be cheap")

        d = _screen_decision(
            prior,
            "NVDA",
            _OK_SETUP,
            [new_session],
            grouped,
            cutoffs,
            last_priced,
            last_resolved,
            last_closed,
            _XNYS,
        )
        self.assertTrue(d.needs_resolve, "R7 must fire despite a fresh last_priced_session")
        self.assertTrue(d.forced)

    def test_crash_flood_resolves_touches_within_bounded_drain(self):
        # GIVEN many OPEN candidates that ALL get a stop-piercing daily bar on a
        # single crash night (more than fit one tight budget). WHEN the monitor runs.
        # THEN every touched name resolves to SL_HIT across the bounded drain and no
        # touch is permanently displaced. (Two nights suffice here.)
        from alphalens_pipeline.feedback import population_ladder_monitor as mon

        brief_date = dt.date(2026, 5, 1)
        arrival = session_on_or_after(brief_date, _XNYS)
        tickers = [f"T{i:02d}" for i in range(6)]
        _write_brief(
            self.briefs_dir,
            brief_date,
            [{"ticker": t, "setup": _OK_SETUP} for t in tickers],
        )

        def _minute_open(ticker, start, end):
            open_ms = int(session_open_utc(arrival, _XNYS).timestamp() * 1000)
            return [
                {"t": open_ms, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {
                    "t": open_ms + _MIN_MS,
                    "o": 100.0,
                    "h": 103.0,
                    "l": 100.0,
                    "c": 102.0,
                    "v": 1000.0,
                },
            ]

        # Run 1: establish all OPEN (brand-new -> forced budget, generous).
        now1 = dt.datetime(2026, 5, 8, 7, 0, tzinfo=UTC)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now1.date(),
            store_dir=self.store_dir,
            bar_fetch=_minute_open,
            grouped_fetch=lambda d: {},
            now=now1,
        )

        def _minute_crash(ticker, start, end):
            open_ms = int(session_open_utc(arrival, _XNYS).timestamp() * 1000)
            later = advance_trading_sessions(arrival, 6, _XNYS)
            later_ms = int(session_open_utc(later, _XNYS).timestamp() * 1000)
            return [
                {"t": open_ms, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
                {"t": later_ms, "o": 96.0, "h": 96.0, "l": 94.0, "c": 95.0, "v": 1000.0},
            ]

        def _grouped_crash(date):
            return {
                t: {"t": 0, "o": 96.0, "h": 96.0, "l": 94.0, "c": 95.0, "v": 1000.0, "vw": 95.0}
                for t in tickers
            }

        # Tight main budget (2): the crash flood (6 touches) cannot all resolve in
        # one night, but drains over a couple of nights.
        now2 = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        original = mon._MAX_FETCHES_PER_RUN
        try:
            mon._MAX_FETCHES_PER_RUN = 2
            for _ in range(4):  # repeated nights drain the queue
                replay_population_ladders(
                    self.briefs_dir,
                    end_date=now2.date(),
                    store_dir=self.store_dir,
                    bar_fetch=_minute_crash,
                    grouped_fetch=_grouped_crash,
                    now=now2,
                )
        finally:
            mon._MAX_FETCHES_PER_RUN = original

        df = self._read_store(brief_date).set_index("ticker")
        for t in tickers:
            self.assertEqual(df.loc[t, "ladder_classification"], "SL_HIT", f"{t} should resolve")
            self.assertTrue(bool(df.loc[t, "terminal"]))


class TestLastPricedSessionBackfill(_MonitorTestBase):
    def test_old_format_row_without_screen_cols_backfills_none(self):
        # GIVEN an OPEN row written then stripped of the screen columns (an OLD-format
        # parquet). WHEN run 2's minute fetch fails so the prior is carried. THEN the
        # missing screen columns surface as NaN (re-populated on the next resolve).
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 5, 15, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch_ok(ticker, start, end):
            base = int(start.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 103.0, "l": 99.0, "c": 102.0, "v": 1000.0}]

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_ok,
            grouped_fetch=lambda d: {},
            now=now,
        )
        path = self.store_dir / f"{brief_date.isoformat()}.parquet"
        df = pd.read_parquet(path)
        screen_cols = ["last_priced_session", "reference_close"]
        df = df.drop(columns=[c for c in screen_cols if c in df.columns])
        df.to_parquet(path)

        def _fetch_boom(ticker, start, end):
            raise ValueError("polygon outage")

        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_boom,
            grouped_fetch=lambda d: {
                "OTHER": {"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}
            },
            now=now,
        )
        after = self._read_store(brief_date).set_index("ticker").loc["NVDA"]
        for col in screen_cols:
            self.assertIn(col, after.index)
            self.assertTrue(pd.isna(after[col]))


class TestRunDeadlineIntegration(_MonitorTestBase):
    def test_date_loop_exits_immediately_when_deadline_pre_tripped(self):
        """Date loop breaks at the top once the deadline has latched.

        When should_stop() is True before the first offset, no date is processed:
        - no fetch issued
        - reports list is empty
        - no store parquet written for any lookback date
        """
        import datetime as dt

        from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline

        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        fetched = []

        def _fetch(t, s, e):
            fetched.append(t)
            base = int(s.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1e3}]

        # deadline already past at construction (budget -1s) -> should_stop() True immediately
        dead = _RunDeadline(-1.0, monotonic=lambda: 0.0)
        reports = replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,
            deadline=dead,
        )
        # Date loop must break at the very first offset — zero dates processed.
        self.assertEqual(fetched, [])
        self.assertEqual(reports, [])
        # No store file written for the brief date (loop never reached _replay_one_date).
        store_file = self.store_dir / f"{brief_date.isoformat()}.parquet"
        self.assertFalse(store_file.exists())

    def test_no_deadline_resolves_normally(self):
        import datetime as dt

        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])

        def _fetch(t, s, e):
            base = int(s.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1e3}]

        reports = replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch,
            now=now,  # deadline defaults to None -> never stops
        )
        self.assertTrue(all(r.stopped_for_deadline == 0 for r in reports))


class TestBreakerOnlyCountsPolygonError(_MonitorTestBase):
    """Breaker must count ONLY PolygonError, not local data/IO faults."""

    def test_value_error_does_not_trip_breaker(self):
        # GIVEN a bar_fetch that raises ValueError (local data fault, NOT a
        # Polygon network/auth error). WHEN replay runs and the fetch raises many
        # times. THEN the deadline breaker is NOT tripped — repeated local errors
        # must NOT advance the consecutive-fail counter.

        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "AAA", "setup": _OK_SETUP},
                {"ticker": "BBB", "setup": _OK_SETUP},
                {"ticker": "CCC", "setup": _OK_SETUP},
            ],
        )

        def _fetch_value_error(ticker, start, end):
            raise ValueError("bad parquet cache — local fault, not polygon")

        # breaker_fails=2 so only 2 consecutive PolygonErrors would stop it;
        # 3 ValueError raises must leave it running.
        deadline = _RunDeadline(10_000.0, breaker_fails=2, monotonic=lambda: 0.0)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_value_error,
            now=now,
            deadline=deadline,
        )
        # Breaker must NOT have stopped — ValueError is not a Polygon fault.
        self.assertFalse(deadline.should_stop(), "breaker must not trip on ValueError")
        self.assertEqual(deadline.stopped_reason, None)

    def test_polygon_error_trips_breaker_after_consecutive_threshold(self):
        # GIVEN a bar_fetch that raises PolygonError (a real Polygon network error).
        # WHEN replay runs and the fetch raises consecutively past the threshold.
        # THEN the deadline breaker IS tripped.
        from alphalens_pipeline.data.alt_data.polygon_client import PolygonError

        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(
            self.briefs_dir,
            brief_date,
            [
                {"ticker": "AAA", "setup": _OK_SETUP},
                {"ticker": "BBB", "setup": _OK_SETUP},
                {"ticker": "CCC", "setup": _OK_SETUP},
            ],
        )

        def _fetch_polygon_error(ticker, start, end):
            raise PolygonError("upstream timeout")

        # breaker_fails=2: trips after 2 consecutive PolygonErrors.
        deadline = _RunDeadline(10_000.0, breaker_fails=2, monotonic=lambda: 0.0)
        replay_population_ladders(
            self.briefs_dir,
            end_date=now.date(),
            store_dir=self.store_dir,
            bar_fetch=_fetch_polygon_error,
            now=now,
            deadline=deadline,
        )
        # Breaker MUST have stopped after consecutive PolygonErrors.
        self.assertTrue(deadline.should_stop(), "breaker must trip after consecutive PolygonErrors")
        self.assertEqual(deadline.stopped_reason, "breaker")


if __name__ == "__main__":
    unittest.main()

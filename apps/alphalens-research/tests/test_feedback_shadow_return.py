"""Tests for the Track A v2 PR-3 shadow-return computation.

``shadow_return`` is the arrival-price counterfactual: the return from the
opening-window VWAP of the first tradable session on-or-after ``brief_date`` to
the same window ``HOLDING_HORIZON_TRADING_DAYS`` later. It is computed from
Polygon minute bars REGARDLESS of fill status (the §4 anti-survivorship intent:
even never-filled candidates carry a counterfactual). ``realized_return`` is the
realised (filled) leg from the paper ledger blended prices — a separate column,
unit-consistent (both decimal fractions) so ``realized_return − shadow_return``
is the §6 execution gap.

All bar fetches are injected, so these tests never hit the network. The live
Polygon probe lives in ``test_polygon_live`` (opt-in).
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.feedback import shadow_return as sr
from alphalens_pipeline.feedback.store import Decision, FeedbackStore
from alphalens_pipeline.paper import ledger as paper_ledger

UTC = dt.UTC
# brief_date well in the past so the +5-session horizon has matured vs _NOW.
_BRIEF_DATE = dt.date(2026, 5, 15)  # Friday
_NOW = dt.datetime(2026, 6, 1, 2, 0, tzinfo=UTC)


def _seed_decision(path: Path, *, ticker: str = "NVDA", theme: str = "ai") -> str:
    with FeedbackStore.open(path) as fb:
        row_id, _ = fb.insert(
            Decision(
                brief_date=_BRIEF_DATE,
                ticker=ticker,
                theme=theme,
                surfaced_at=dt.datetime(2026, 5, 15, 6, 30, tzinfo=UTC),
                action="interested",
                action_at=dt.datetime(2026, 5, 15, 8, 0, tzinfo=UTC),
            )
        )
    return row_id


def _seed_plan(
    path: Path,
    *,
    ticker: str = "NVDA",
    account: str = "test",
    exit_kind: str = "TP_HIT",
    blended_entry_price: float | None = 100.0,
    blended_exit_price: float | None = 120.0,
) -> int:
    with paper_ledger.open_ledger(path) as conn:
        plan = paper_ledger.insert_planned(
            conn,
            brief_date=_BRIEF_DATE,
            ticker=ticker,
            theme="ai",
            planned_at=dt.datetime(2026, 5, 15, 13, 5, tzinfo=UTC),
            suggested_size_pct=2.0,
            scale_factor=1.0,
            final_size_pct=2.0,
            paper_equity=100_000.0,
            total_notional=2_000.0,
            gross_notional=2_000.0,
            disaster_stop=90.0,
            order_ttl_days=2,
            tiers=[(0, 100.0, 20, 100.0, "entry")],
            tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
            account=account,
        )
        paper_ledger.insert_plan_outcome(
            conn,
            plan_id=plan.plan_id,
            exit_kind=exit_kind,
            closed_at=dt.datetime(2026, 5, 22, 20, 0, tzinfo=UTC),
            blended_entry_price=blended_entry_price,
            blended_exit_price=blended_exit_price,
        )
        return plan.plan_id


def _bars(close_vol: list[tuple[float, float]], window_start: dt.datetime) -> list[dict]:
    """Build consecutive 1-min Polygon agg bars from ``window_start``."""
    bars = []
    for i, (close, vol) in enumerate(close_vol):
        t = int((window_start + dt.timedelta(minutes=i)).timestamp() * 1000)
        bars.append({"t": t, "o": close, "h": close, "l": close, "c": close, "v": vol})
    return bars


class TestWindowVwap(unittest.TestCase):
    _START = dt.datetime(2026, 5, 15, 13, 30, tzinfo=UTC)
    _END = dt.datetime(2026, 5, 15, 14, 0, tzinfo=UTC)

    def test_vwap_weighted_by_volume(self):
        bars = _bars([(100.0, 10.0), (110.0, 30.0)], self._START)
        # (100*10 + 110*30) / 40 = 107.5, NOT the simple mean 105.
        self.assertAlmostEqual(sr._window_vwap(bars, self._START, self._END), 107.5)

    def test_zero_volume_falls_back_to_mean_close(self):
        bars = _bars([(100.0, 0.0), (110.0, 0.0)], self._START)
        # All-zero volume (thin name) -> simple mean of closes.
        self.assertAlmostEqual(sr._window_vwap(bars, self._START, self._END), 105.0)

    def test_no_bars_in_window_returns_none(self):
        # Bars exist but all fall OUTSIDE [start, end) -> None.
        outside = _bars([(100.0, 10.0)], self._END + dt.timedelta(minutes=5))
        self.assertIsNone(sr._window_vwap(outside, self._START, self._END))

    def test_empty_bars_returns_none(self):
        self.assertIsNone(sr._window_vwap([], self._START, self._END))


class TestComputeShadowReturns(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.fb_path = Path(self._td.name) / "feedback.db"
        self.ledger_path = Path(self._td.name) / "paper_ledger.db"

    def tearDown(self):
        self._td.cleanup()

    def _fetch(self, row_id: str) -> Decision:
        with FeedbackStore.open(self.fb_path) as fb:
            return fb.get(row_id)

    def _two_window_fetch(self, arrival_vwap: float, horizon_vwap: float):
        """A bar_fetch that returns flat bars at the given VWAP per window,
        keyed by the window start date (arrival session vs horizon session)."""
        from alphalens_pipeline.paper.calendar import advance_trading_sessions, session_on_or_after

        arrival_day = session_on_or_after(_BRIEF_DATE)
        horizon_day = advance_trading_sessions(_BRIEF_DATE, sr.HOLDING_HORIZON_TRADING_DAYS)

        def fetch(ticker, start, end):
            if start.date() == arrival_day:
                return _bars([(arrival_vwap, 100.0)], start)
            if start.date() == horizon_day:
                return _bars([(horizon_vwap, 100.0)], start)
            return []

        return fetch

    def _run(self, bar_fetch, account: str = "test"):
        return sr.compute_shadow_returns(
            self.fb_path,
            self.ledger_path,
            brief_date=_BRIEF_DATE,
            account=account,
            bar_fetch=bar_fetch,
            now=_NOW,
        )

    def test_filled_uses_arrival_and_horizon_vwap_not_realised_exit(self):
        row_id = _seed_decision(self.fb_path)
        # Seed realised prices that would give a DIFFERENT realised return
        # (120/100 = +20%) so we prove shadow uses bars, not the exit fill.
        _seed_plan(self.ledger_path, exit_kind="TP_HIT")
        report = self._run(self._two_window_fetch(arrival_vwap=100.0, horizon_vwap=110.0))
        self.assertTrue(report.matured)
        self.assertEqual(report.n_priced, 1)
        d = self._fetch(row_id)
        # shadow = (110 - 100)/100 = +10%, independent of the +20% realised exit
        self.assertAlmostEqual(d.shadow_return, 0.10)
        self.assertNotAlmostEqual(d.shadow_return, 0.20)

    def test_realized_return_for_filled(self):
        row_id = _seed_decision(self.fb_path)
        _seed_plan(
            self.ledger_path,
            exit_kind="TP_HIT",
            blended_entry_price=100.0,
            blended_exit_price=120.0,
        )
        self._run(self._two_window_fetch(100.0, 110.0))
        d = self._fetch(row_id)
        # realized = (120 - 100)/100 = +20%
        self.assertAlmostEqual(d.realized_return, 0.20)

    def test_unfilled_carries_shadow_but_null_realized(self):
        # §4 anti-survivorship: a never-filled candidate still gets a shadow
        # counterfactual; realized_return is NULL (no realised leg).
        row_id = _seed_decision(self.fb_path)
        _seed_plan(
            self.ledger_path,
            exit_kind="UNFILLED",
            blended_entry_price=None,
            blended_exit_price=None,
        )
        report = self._run(self._two_window_fetch(100.0, 95.0))
        self.assertEqual(report.n_priced, 1)
        d = self._fetch(row_id)
        self.assertAlmostEqual(d.shadow_return, -0.05)
        self.assertIsNone(d.realized_return)
        self.assertEqual(d.fill_status, "UNFILLED")

    def test_immature_horizon_skips_whole_run(self):
        # brief_date so recent the +5-session horizon is >= today. Polygon
        # Basic serves only past sessions, so computing now would silently
        # stamp nothing — guard must skip the WHOLE run with a loud warning.
        recent_brief = dt.date(2026, 5, 29)  # horizon +5 sessions = 2026-06-05
        _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path)
        with self.assertLogs("alphalens_pipeline.feedback.shadow_return", level="WARNING") as cm:
            report = sr.compute_shadow_returns(
                self.fb_path,
                self.ledger_path,
                brief_date=recent_brief,
                account="test",
                bar_fetch=lambda *a: [],
                now=_NOW,
            )
        self.assertFalse(report.matured)
        self.assertEqual(report.n_priced, 0)
        self.assertTrue(any("has not matured" in m for m in cm.output))

    def test_one_ticker_fetch_error_does_not_abort_sweep(self):
        from alphalens_pipeline.data.alt_data.polygon_client import PolygonRateLimitError
        from alphalens_pipeline.paper.calendar import advance_trading_sessions, session_on_or_after

        id_a = _seed_decision(self.fb_path, ticker="AAA", theme="t1")
        id_b = _seed_decision(self.fb_path, ticker="BBB", theme="t2")
        _seed_plan(self.ledger_path, ticker="AAA", exit_kind="TP_HIT")
        _seed_plan(self.ledger_path, ticker="BBB", exit_kind="TP_HIT")

        arrival_day = session_on_or_after(_BRIEF_DATE)
        horizon_day = advance_trading_sessions(_BRIEF_DATE, sr.HOLDING_HORIZON_TRADING_DAYS)

        def fetch(ticker, start, end):
            if ticker == "AAA":
                raise PolygonRateLimitError("429")
            if start.date() == arrival_day:
                return _bars([(100.0, 100.0)], start)
            if start.date() == horizon_day:
                return _bars([(108.0, 100.0)], start)
            return []

        with self.assertLogs("alphalens_pipeline.feedback.shadow_return", level="WARNING"):
            report = self._run(fetch)
        self.assertEqual(report.n_priced, 1)
        self.assertEqual(report.n_skipped, 1)
        self.assertIsNone(self._fetch(id_a).shadow_return)
        self.assertAlmostEqual(self._fetch(id_b).shadow_return, 0.08)

    def test_empty_bars_is_skip_not_error(self):
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, exit_kind="TP_HIT")
        with self.assertLogs("alphalens_pipeline.feedback.shadow_return", level="WARNING"):
            report = self._run(lambda *a: [])  # no bars for any window
        self.assertEqual(report.n_no_bars, 1)
        self.assertEqual(report.n_priced, 0)
        self.assertIsNone(self._fetch(row_id).shadow_return)

    def test_implausible_return_skipped_as_likely_corporate_action(self):
        # adjusted=false: a split in the window fabricates a ~-50% move. Guard
        # skips it rather than stamping a corrupted number.
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, exit_kind="TP_HIT")
        # 100 -> 35 is -65%, past the 60% guard (a 2:1+ split-like artifact).
        with self.assertLogs("alphalens_pipeline.feedback.shadow_return", level="WARNING"):
            report = self._run(self._two_window_fetch(arrival_vwap=100.0, horizon_vwap=35.0))
        self.assertEqual(report.n_skipped, 1)
        self.assertEqual(report.n_priced, 0)
        self.assertIsNone(self._fetch(row_id).shadow_return)

    def test_idempotent(self):
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, exit_kind="TP_HIT")
        fetch = self._two_window_fetch(100.0, 110.0)
        self._run(fetch)
        first = self._fetch(row_id).shadow_return
        self._run(fetch)
        self.assertAlmostEqual(self._fetch(row_id).shadow_return, first)

    def test_account_scoping_excludes_main(self):
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, account="main", exit_kind="TP_HIT")
        report = self._run(self._two_window_fetch(100.0, 110.0), account="test")
        self.assertEqual(report.n_outcomes, 0)
        self.assertIsNone(self._fetch(row_id).shadow_return)


if __name__ == "__main__":
    unittest.main()

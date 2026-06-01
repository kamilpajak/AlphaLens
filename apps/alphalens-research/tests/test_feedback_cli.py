"""Tests for ``alphalens feedback report`` operator CLI."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_cli.main import app
from alphalens_feedback.store import Decision, FeedbackStore
from typer.testing import CliRunner

UTC = dt.UTC


def _seed_decisions(path: Path, decisions: list[Decision]) -> None:
    with FeedbackStore.open(path) as fb:
        for d in decisions:
            fb.insert(d)


def _interested(ticker: str = "NVDA", theme: str = "ai") -> Decision:
    return Decision(
        brief_date=dt.date(2026, 5, 28),
        ticker=ticker,
        theme=theme,
        surfaced_at=dt.datetime(2026, 5, 28, 6, 30, tzinfo=UTC),
        action="interested",
        action_at=dt.datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
    )


def _dismissed(reason: str, ticker: str = "TSLA", theme: str = "ev") -> Decision:
    category = {
        "wrong_theme": "thesis_setup",
        "too_expensive": "thesis_setup",
        "bad_setup": "thesis_setup",
        "business_management": "risk_quality",
        "risk_jurisdiction": "risk_quality",
        "dont_understand": "risk_quality",
        "already_have_exposure": "portfolio_style",
        "liquidity_too_low": "portfolio_style",
        "not_my_style": "portfolio_style",
        "other": "other",
    }[reason]
    return Decision(
        brief_date=dt.date(2026, 5, 28),
        ticker=ticker,
        theme=theme,
        surfaced_at=dt.datetime(2026, 5, 28, 6, 30, tzinfo=UTC),
        action="dismissed",
        action_at=dt.datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
        dismiss_category=category,
        dismiss_reason=reason,
        dismiss_note="bc" if reason == "other" else None,
    )


class TestFeedbackReportCommand(unittest.TestCase):
    """`alphalens feedback report` operator dashboard."""

    def setUp(self):
        self.runner = CliRunner()
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_missing_ledger_prints_friendly_message(self):
        # Brand-new install without any feedback yet — exit 0, no crash.
        result = self.runner.invoke(app, ["feedback", "report", "--ledger", str(self.path)])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("nothing to report yet", result.stdout)

    def test_empty_ledger_prints_friendly_message(self):
        # Ledger exists (e.g. created by an earlier session) but no rows.
        with FeedbackStore.open(self.path):
            pass
        result = self.runner.invoke(app, ["feedback", "report", "--ledger", str(self.path)])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("is empty", result.stdout)

    def test_report_shows_action_distribution(self):
        _seed_decisions(
            self.path,
            [
                _interested("NVDA", "ai"),
                _interested("AMD", "ai"),
                _dismissed("wrong_theme", "FOO", "bar"),
            ],
        )
        result = self.runner.invoke(app, ["feedback", "report", "--ledger", str(self.path)])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("total decisions: 3", result.stdout)
        self.assertIn("interested", result.stdout)
        self.assertIn("dismissed", result.stdout)
        self.assertIn("wrong_theme", result.stdout)

    def test_warns_when_other_exceeds_15_percent(self):
        # 2 'other' out of 6 dismissed = 33% > 15% threshold → warning.
        decisions = [_dismissed("other", f"X{i}", "thm") for i in range(2)]
        decisions += [_dismissed("wrong_theme", f"Y{i}", "thm") for i in range(2)]
        decisions += [_dismissed("too_expensive", f"Z{i}", "thm") for i in range(2)]
        _seed_decisions(self.path, decisions)
        result = self.runner.invoke(app, ["feedback", "report", "--ledger", str(self.path)])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("⚠", result.stdout)
        self.assertIn("taxonomy may have a gap", result.stdout)

    def test_no_warning_when_other_within_threshold(self):
        # 1 'other' out of 10 dismissed = 10% < 15% threshold → no warning.
        decisions = [_dismissed("other", "AA0", "thm")]
        decisions += [_dismissed("wrong_theme", f"B{i}", "thm") for i in range(9)]
        _seed_decisions(self.path, decisions)
        result = self.runner.invoke(app, ["feedback", "report", "--ledger", str(self.path)])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertNotIn("⚠", result.stdout)


class TestFeedbackJoinOutcomesCommand(unittest.TestCase):
    """`alphalens feedback join-outcomes` drives the decision<->paper join."""

    def setUp(self):
        self.runner = CliRunner()
        self._td = tempfile.TemporaryDirectory()
        self.fb_path = Path(self._td.name) / "feedback.db"
        self.ledger_path = Path(self._td.name) / "paper_ledger.db"

    def tearDown(self):
        self._td.cleanup()

    def test_join_stamps_decision_from_paper_outcome(self):
        from alphalens_pipeline.paper import ledger as paper_ledger

        # seed a clicked decision
        _seed_decisions(self.fb_path, [_interested("NVDA", "ai")])
        # seed a matching paper plan + outcome on the 'test' account
        with paper_ledger.open_ledger(self.ledger_path) as conn:
            plan = paper_ledger.insert_planned(
                conn,
                brief_date=dt.date(2026, 5, 28),
                ticker="NVDA",
                theme="ai",
                planned_at=dt.datetime(2026, 5, 28, 13, 5, tzinfo=UTC),
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
                account="test",
            )
            paper_ledger.insert_plan_outcome(
                conn,
                plan_id=plan.plan_id,
                exit_kind="TP_HIT",
                closed_at=dt.datetime(2026, 5, 30, 20, 0, tzinfo=UTC),
            )

        result = self.runner.invoke(
            app,
            [
                "feedback",
                "join-outcomes",
                "--date",
                "2026-05-28",
                "--account",
                "test",
                "--ledger",
                str(self.fb_path),
                "--paper-ledger",
                str(self.ledger_path),
            ],
        )
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("1/1", result.stdout)
        with FeedbackStore.open(self.fb_path) as fb:
            row = fb.list_by_brief_date(dt.date(2026, 5, 28))[0]
            self.assertEqual(row.fill_status, "FILLED")
            self.assertEqual(row.exit_kind, "TP_HIT")


class TestFeedbackComputeShadowReturnsCommand(unittest.TestCase):
    """`alphalens feedback compute-shadow-returns` drives the Polygon shadow pass."""

    def setUp(self):
        self.runner = CliRunner()
        self._td = tempfile.TemporaryDirectory()
        self.fb_path = Path(self._td.name) / "feedback.db"
        self.ledger_path = Path(self._td.name) / "paper_ledger.db"

    def tearDown(self):
        self._td.cleanup()

    def test_compute_shadow_returns_stamps_shadow(self):
        from alphalens_pipeline.feedback import shadow_return as sr
        from alphalens_pipeline.paper import ledger as paper_ledger
        from alphalens_pipeline.paper.calendar import (
            advance_trading_sessions,
            session_on_or_after,
            session_open_utc,
        )

        # brief_date in the past so the +5-session horizon has matured.
        brief = dt.date(2026, 5, 15)

        def _seed_decision_for(brief_date):
            with FeedbackStore.open(self.fb_path) as fb:
                fb.insert(
                    Decision(
                        brief_date=brief_date,
                        ticker="NVDA",
                        theme="ai",
                        surfaced_at=dt.datetime(2026, 5, 15, 6, 30, tzinfo=UTC),
                        action="interested",
                        action_at=dt.datetime(2026, 5, 15, 8, 0, tzinfo=UTC),
                    )
                )

        _seed_decision_for(brief)
        with paper_ledger.open_ledger(self.ledger_path) as conn:
            plan = paper_ledger.insert_planned(
                conn,
                brief_date=brief,
                ticker="NVDA",
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
                account="test",
            )
            paper_ledger.insert_plan_outcome(
                conn,
                plan_id=plan.plan_id,
                exit_kind="TP_HIT",
                closed_at=dt.datetime(2026, 5, 22, 20, 0, tzinfo=UTC),
                blended_entry_price=100.0,
                blended_exit_price=120.0,
            )

        arrival_open = session_open_utc(session_on_or_after(brief))
        horizon_open = session_open_utc(
            advance_trading_sessions(brief, sr.HOLDING_HORIZON_TRADING_DAYS)
        )

        def fake_fetch(ticker, start, end):
            close = 100.0 if start == arrival_open else 110.0 if start == horizon_open else None
            if close is None:
                return []
            return [{"t": int(start.timestamp() * 1000), "c": close, "v": 100.0}]

        orig = sr._default_bar_fetch
        sr._default_bar_fetch = fake_fetch
        try:
            result = self.runner.invoke(
                app,
                [
                    "feedback",
                    "compute-shadow-returns",
                    "--date",
                    "2026-05-15",
                    "--account",
                    "test",
                    "--ledger",
                    str(self.fb_path),
                    "--paper-ledger",
                    str(self.ledger_path),
                ],
            )
        finally:
            sr._default_bar_fetch = orig

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("1 priced", result.stdout)
        with FeedbackStore.open(self.fb_path) as fb:
            row = fb.list_by_brief_date(brief)[0]
            self.assertAlmostEqual(row.shadow_return, 0.10)
            self.assertAlmostEqual(row.realized_return, 0.20)


class TestFeedbackBackfillShadowReturnsCommand(unittest.TestCase):
    """`alphalens feedback backfill-shadow-returns` is the nightly timer's entrypoint.

    It sweeps a fixed look-back window and prices every matured date, so the
    systemd unit needs no date arithmetic. The seed date is anchored relative to
    *today* so it always lands inside the default 14-day window regardless of
    when the suite runs.
    """

    def setUp(self):
        self.runner = CliRunner()
        self._td = tempfile.TemporaryDirectory()
        self.fb_path = Path(self._td.name) / "feedback.db"
        self.ledger_path = Path(self._td.name) / "paper_ledger.db"

    def tearDown(self):
        self._td.cleanup()

    def test_backfill_prices_matured_date_and_prints_aggregate(self):
        from alphalens_pipeline.feedback import shadow_return as sr
        from alphalens_pipeline.paper import ledger as paper_ledger
        from alphalens_pipeline.paper.calendar import (
            advance_trading_sessions,
            session_on_or_after,
            session_open_utc,
        )

        # 12 calendar days back: the +5-session horizon has matured AND the date
        # is inside the default 14-day window (no fixed-date drift as time passes).
        brief = dt.datetime.now(UTC).date() - dt.timedelta(days=12)
        with FeedbackStore.open(self.fb_path) as fb:
            fb.insert(
                Decision(
                    brief_date=brief,
                    ticker="NVDA",
                    theme="ai",
                    surfaced_at=dt.datetime.now(UTC),
                    action="interested",
                    action_at=dt.datetime.now(UTC),
                )
            )
        with paper_ledger.open_ledger(self.ledger_path) as conn:
            plan = paper_ledger.insert_planned(
                conn,
                brief_date=brief,
                ticker="NVDA",
                theme="ai",
                planned_at=dt.datetime.now(UTC),
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
                account="test",
            )
            paper_ledger.insert_plan_outcome(
                conn,
                plan_id=plan.plan_id,
                exit_kind="TP_HIT",
                closed_at=dt.datetime.now(UTC),
                blended_entry_price=100.0,
                blended_exit_price=120.0,
            )

        arrival_open = session_open_utc(session_on_or_after(brief))
        horizon_open = session_open_utc(
            advance_trading_sessions(brief, sr.HOLDING_HORIZON_TRADING_DAYS)
        )

        def fake_fetch(ticker, start, end):
            close = 100.0 if start == arrival_open else 110.0 if start == horizon_open else None
            if close is None:
                return []
            return [{"t": int(start.timestamp() * 1000), "c": close, "v": 100.0}]

        orig = sr._default_bar_fetch
        sr._default_bar_fetch = fake_fetch
        try:
            result = self.runner.invoke(
                app,
                [
                    "feedback",
                    "backfill-shadow-returns",
                    "--account",
                    "test",
                    "--ledger",
                    str(self.fb_path),
                    "--paper-ledger",
                    str(self.ledger_path),
                ],
            )
        finally:
            sr._default_bar_fetch = orig

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("backfill", result.stdout)
        self.assertIn("1 priced", result.stdout)
        with FeedbackStore.open(self.fb_path) as fb:
            row = fb.list_by_brief_date(brief)[0]
            self.assertAlmostEqual(row.shadow_return, 0.10)

    def test_cli_lookback_default_in_sync_with_module(self):
        from alphalens_cli.commands.feedback import _DEFAULT_LOOKBACK_DAYS
        from alphalens_pipeline.feedback.shadow_return import DEFAULT_LOOKBACK_DAYS

        # typer.Option evaluates its default at import time and the CLI lazy-imports
        # the feedback module inside the command body, so the literal is duplicated
        # CLI-side. Pin parity (same hazard as preaudit _DEFAULT_SMOKE_TIMEOUT_S).
        self.assertEqual(_DEFAULT_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS)
        self.assertEqual(_DEFAULT_LOOKBACK_DAYS, 14)


class TestFeedbackExecutionModesCommand(unittest.TestCase):
    """`alphalens feedback execution-modes` — read-only inert recommendation."""

    def setUp(self):
        self.runner = CliRunner()
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_missing_ledger_prints_friendly_message(self):
        result = self.runner.invoke(
            app, ["feedback", "execution-modes", "--ledger", str(self.path)]
        )
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("no matured decisions yet", result.stdout)

    def test_inert_banner_below_gate(self):
        # A handful of priced decisions (far below the 50 gate) → GATE INERT,
        # every cell LIMIT, ledger untouched (read-only).
        with FeedbackStore.open(self.path) as fb:
            for i in range(3):
                rid, _ = fb.insert(
                    Decision(
                        brief_date=dt.date(2026, 5, 20),
                        ticker=f"AAA{i}",
                        theme="ai",
                        surfaced_at=dt.datetime(2026, 5, 20, 6, 30, tzinfo=UTC),
                        action="interested",
                        action_at=dt.datetime(2026, 5, 20, 8, 0, tzinfo=UTC),
                        market_regime_at_entry="mid",
                    )
                )
                fb.stamp_outcome(
                    rid,
                    fill_status="FILLED",
                    exit_kind="TP_HIT",
                    outcome_plan_id="p1",
                    outcome_computed_at=dt.datetime(2026, 5, 27, 2, 0, tzinfo=UTC),
                    shadow_return=0.05,
                    realized_return=0.03,
                )
        result = self.runner.invoke(
            app, ["feedback", "execution-modes", "--ledger", str(self.path)]
        )
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("GATE INERT", result.stdout)
        self.assertIn("LIMIT", result.stdout)
        self.assertNotIn("MARKET", result.stdout)


if __name__ == "__main__":
    unittest.main()

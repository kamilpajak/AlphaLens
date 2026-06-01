"""Tests for ``alphalens feedback report`` operator CLI."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_cli.main import app
from alphalens_pipeline.feedback.store import Decision, FeedbackStore
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


if __name__ == "__main__":
    unittest.main()

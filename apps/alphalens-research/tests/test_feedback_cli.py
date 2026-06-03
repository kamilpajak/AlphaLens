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


class TestFeedbackBackfillCommand(unittest.TestCase):
    """`alphalens feedback backfill-shadow-returns` is the nightly timer's entrypoint.

    The name is retained for the existing systemd unit; the command now drives
    only the broker-free ladder + population-monitor replays. The seed date is
    anchored relative to *today* so it always lands inside the default 14-day
    window regardless of when the suite runs.
    """

    def setUp(self):
        self.runner = CliRunner()
        self._td = tempfile.TemporaryDirectory()
        self.fb_path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_cli_lookback_default_in_sync_with_module(self):
        from alphalens_cli.commands.feedback import _DEFAULT_LOOKBACK_DAYS
        from alphalens_pipeline.feedback.bar_window import DEFAULT_LOOKBACK_DAYS

        # typer.Option evaluates its default at import time and the CLI lazy-imports
        # the feedback module inside the command body, so the literal is duplicated
        # CLI-side. Pin parity (same hazard as preaudit _DEFAULT_SMOKE_TIMEOUT_S).
        self.assertEqual(_DEFAULT_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS)
        self.assertEqual(_DEFAULT_LOOKBACK_DAYS, 14)

    def test_backfill_runs_ladder_replay_and_stamps(self):
        # The broker-free ladder step must stamp the gen-4 columns from the
        # nightly command. Provide a brief parquet + stub the ladder bar fetch.
        import json

        import pandas as pd
        from alphalens_pipeline.feedback import ladder_backfill as lb

        brief = dt.datetime.now(UTC).date() - dt.timedelta(days=12)
        briefs_dir = Path(self._td.name) / "thematic_briefs"
        briefs_dir.mkdir()
        setup = {
            "status": "OK",
            "disaster_stop": 95.0,
            "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
            "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0}],
        }
        pd.DataFrame(
            [{"ticker": "NVDA", "theme": "ai", "brief_trade_setup": json.dumps(setup)}]
        ).to_parquet(briefs_dir / f"{brief.isoformat()}.parquet")
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

        def fake_fetch(ticker, start, end):
            base = int(start.timestamp() * 1000)
            return [
                {"t": base, "h": 101.0, "l": 99.0, "c": 100.0, "v": 100.0},
                {"t": base + 60_000, "h": 111.0, "l": 100.0, "c": 110.0, "v": 100.0},
            ]

        orig_lb = lb._default_bar_fetch
        lb._default_bar_fetch = fake_fetch
        try:
            result = self.runner.invoke(
                app,
                [
                    "feedback",
                    "backfill-shadow-returns",
                    "--ledger",
                    str(self.fb_path),
                    "--briefs-dir",
                    str(briefs_dir),
                ],
            )
        finally:
            lb._default_bar_fetch = orig_lb

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("ladder-replay", result.stdout)
        with FeedbackStore.open(self.fb_path) as fb:
            row = fb.conn.execute(
                "SELECT ladder_classification, realized_r FROM decisions "
                "WHERE brief_date = ? AND ticker = 'NVDA'",
                (brief.isoformat(),),
            ).fetchone()
        self.assertEqual(row["ladder_classification"], "TP_FULL")


if __name__ == "__main__":
    unittest.main()

"""Tests for the ``alphalens feedback backfill-shadow-returns`` operator CLI."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_cli.main import app
from alphalens_feedback.store import Decision, FeedbackStore
from typer.testing import CliRunner

UTC = dt.UTC


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

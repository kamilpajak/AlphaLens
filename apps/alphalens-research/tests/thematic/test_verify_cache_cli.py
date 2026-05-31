"""End-to-end CLI tests for ``alphalens thematic verify-cache``.

Smoke level — exercises typer wiring + exit codes + Telegram alert
opt-in path (with a mocked sender). The verifier's correctness is
covered by ``test_verify_cache.py``; this suite only pins the
shell-callable contract that systemd's ExecStartPost relies on.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_cli.commands.thematic import thematic_app
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS
from typer.testing import CliRunner


def _seed_parquet(path: Path, n_rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if n_rows == 0:
        df = pd.DataFrame(columns=NEWS_COLUMNS)
    else:
        df = pd.DataFrame(
            [
                {
                    "url": f"https://example.com/{i}",
                    "title": f"news {i}",
                    "timestamp": pd.Timestamp("2026-05-29 12:00:00+00:00"),
                    "source": "polygon",
                    "tickers": ["NVDA"],
                    "summary": "",
                    "extra": "{}",
                }
                for i in range(n_rows)
            ],
            columns=NEWS_COLUMNS,
        )
    df.to_parquet(path, index=False)


class _CLIBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "thematic_news"
        self.cache.mkdir()
        self.runner = CliRunner()

    def tearDown(self):
        self._tmp.cleanup()

    def _invoke(self, *args: str):
        return self.runner.invoke(thematic_app, ["verify-cache", *args])


# Pin "today" to a fixed date so the CLI's default ``today`` (UTC now)
# doesn't drift the expected file set in the cache. Each test seeds
# the cache against this anchor.
#
# Tests below pass ``--lag-days 0`` explicitly so the window includes
# the anchor itself — matching the seeding pattern that pre-dated the
# PR-E bugfix. The lag=1 default is exercised by the dedicated
# ``TestLagDaysDefault`` class at the bottom; legacy tests stay opted
# out so a refactor that breaks the lag flag fails BOTH paths loudly.
_ANCHOR = dt.date(2026, 5, 29)


class TestExitCodes(_CLIBase):
    def test_all_present_exits_zero(self):
        for i in range(3):
            d = _ANCHOR - dt.timedelta(days=i)
            _seed_parquet(self.cache / f"{d.isoformat()}.parquet", n_rows=4)
        result = self._invoke(
            "--lag-days",
            "0",
            "--days",
            "3",
            "--cache-dir",
            str(self.cache),
            "--today",
            _ANCHOR.isoformat(),
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("3/3 dates present", result.stdout)

    def test_one_missing_exits_one(self):
        # Seed yesterday + day-before-yesterday; leave today missing.
        for i in (1, 2):
            d = _ANCHOR - dt.timedelta(days=i)
            _seed_parquet(self.cache / f"{d.isoformat()}.parquet", n_rows=4)
        result = self._invoke(
            "--lag-days",
            "0",
            "--days",
            "3",
            "--cache-dir",
            str(self.cache),
            "--today",
            _ANCHOR.isoformat(),
        )
        self.assertEqual(result.exit_code, 1)
        # Combined stdout+stderr — Typer's runner merges by default. The
        # stderr "MISSING:" line carries the operator-facing date.
        combined = result.stdout + (result.stderr or "")
        self.assertIn("2/3 dates present", combined)
        self.assertIn("2026-05-29", combined)


class TestAlertDispatch(_CLIBase):
    def test_alert_flag_dispatches_telegram_on_missing(self):
        with (
            mock.patch.dict(
                "os.environ",
                {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"},
            ),
            mock.patch(
                "alphalens_pipeline.edgar_detector.dispatch.handlers.telegram"
                ".TelegramHandler.send_message"
            ) as mock_send,
        ):
            result = self._invoke(
                "--lag-days",
                "0",
                "--days",
                "1",
                "--cache-dir",
                str(self.cache),
                "--today",
                _ANCHOR.isoformat(),
                "--alert",
            )
        self.assertEqual(result.exit_code, 1)
        mock_send.assert_called_once()
        # Digest carries the missing date so the operator can correlate.
        digest = mock_send.call_args[0][0]
        self.assertIn("2026-05-29", digest)

    def test_alert_flag_without_credentials_logs_and_skips(self):
        # Missing day + --alert + missing env vars → exit 1 but no
        # Telegram send. Critical: fresh checkouts without secrets must
        # not crash the systemd ExecStartPost path.
        #
        # Scope the env wipe to ONLY the two Telegram vars rather than
        # clearing all of ``os.environ`` — a future CLI extension that
        # happens to read PATH / HOME would otherwise silently break
        # under this test (zen review LOW finding 2026-05-29).
        import os as _os

        env_patch = {
            k: v
            for k, v in _os.environ.items()
            if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        }
        with (
            mock.patch.dict("os.environ", env_patch, clear=True),
            mock.patch(
                "alphalens_pipeline.edgar_detector.dispatch.handlers.telegram"
                ".TelegramHandler.send_message"
            ) as mock_send,
        ):
            result = self._invoke(
                "--lag-days",
                "0",
                "--days",
                "1",
                "--cache-dir",
                str(self.cache),
                "--today",
                _ANCHOR.isoformat(),
                "--alert",
            )
        self.assertEqual(result.exit_code, 1)
        mock_send.assert_not_called()

    def test_no_alert_flag_means_no_dispatch_even_with_credentials(self):
        with (
            mock.patch.dict(
                "os.environ",
                {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"},
            ),
            mock.patch(
                "alphalens_pipeline.edgar_detector.dispatch.handlers.telegram"
                ".TelegramHandler.send_message"
            ) as mock_send,
        ):
            result = self._invoke(
                "--lag-days",
                "0",
                "--days",
                "1",
                "--cache-dir",
                str(self.cache),
                "--today",
                _ANCHOR.isoformat(),
            )
        self.assertEqual(result.exit_code, 1)
        mock_send.assert_not_called()


class TestLagDaysDefault(_CLIBase):
    """Pins the production-default semantic: ``--lag-days`` is 1 unless
    overridden. PR-E shipped without the flag and the systemd hook
    fired against an anchor for which the ingest had not yet written
    a file — guaranteed false-positive MISSING + halt on the next
    ExecStartPost. See ``docs/research/paper_trading_non_trading_day_2026_05_29.md``
    §5.1 follow-up.
    """

    def test_no_lag_flag_excludes_anchor_from_expected_set(self):
        """Anchor is 2026-05-29 (Fri); cache holds yesterday only
        (2026-05-28). With the default ``--lag-days 1`` the window
        ends on Thu and the verifier exits 0 — even though Fri's
        parquet is intentionally absent (ingest writes T-1)."""
        _seed_parquet(self.cache / "2026-05-28.parquet", n_rows=4)
        # Intentionally no 2026-05-29.parquet.

        result = self._invoke(
            "--days",
            "1",
            "--cache-dir",
            str(self.cache),
            "--today",
            _ANCHOR.isoformat(),
        )

        self.assertEqual(result.exit_code, 0, msg=f"stdout={result.stdout!r}")
        self.assertIn("1/1 dates present", result.stdout)

    def test_no_lag_flag_reports_missing_when_yesterday_is_absent(self):
        """If yesterday's parquet IS missing, the verifier MUST still
        fire (the lag default only excludes the anchor itself, not the
        rest of the lagged window)."""
        # Cache empty.

        result = self._invoke(
            "--days",
            "1",
            "--cache-dir",
            str(self.cache),
            "--today",
            _ANCHOR.isoformat(),
        )

        self.assertEqual(result.exit_code, 1)
        combined = result.stdout + (result.stderr or "")
        # The default-lag window for days=1, today=05-29 is [05-28].
        self.assertIn("2026-05-28", combined)


if __name__ == "__main__":
    unittest.main()

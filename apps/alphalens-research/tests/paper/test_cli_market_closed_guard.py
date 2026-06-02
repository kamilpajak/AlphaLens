"""Tests for ``alphalens paper {submit,reconcile}`` market-closed guard.

PR-A behaviour: ``submit`` and ``reconcile`` skip when today is not a
trading day on XNYS (weekend / US public holiday). ``--allow-closed-market``
opts back in for ad-hoc operator runs (manual reconciles, smoke tests).

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_cli.commands.paper import paper_app
from typer.testing import CliRunner

# Fri 2026-05-29 is a normal XNYS session; Sat 2026-05-30 is closed;
# Mon 2026-05-25 is Memorial Day. The guard reads ``today_utc`` (UTC date
# as observed by the process) — patching ``alphalens_pipeline.paper.calendar.is_trading_day``
# directly is cleaner than freezing wall-clock time for these tests.
_NON_TRADING_DAY = dt.date(2026, 5, 30)  # Saturday
_NON_TRADING_HOLIDAY = dt.date(2026, 5, 25)  # Memorial Day Mon
_TRADING_DAY = dt.date(2026, 5, 29)  # Fri


def _run(args: list[str]) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(paper_app, args)
    return result.exit_code, result.stdout


class TestSubmitGuard(unittest.TestCase):
    """``alphalens paper submit`` exits 0 + emits a deferral message on
    non-trading days, without touching Alpaca or the ledger.
    """

    def test_submit_skips_on_weekend(self):
        with (
            mock.patch(
                "alphalens_cli.commands.paper._today_utc",
                return_value=_NON_TRADING_DAY,
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.alpaca_client.get_default_alpaca_client",
            ) as mock_client_factory,
        ):
            code, out = _run(["submit", "--date", "2026-05-29"])
        self.assertEqual(code, 0)
        self.assertIn("market closed", out.lower())
        # Critical: guard fires BEFORE the Alpaca client is constructed —
        # otherwise a fresh checkout without ALPACA_API_KEY would crash
        # on Saturdays.
        mock_client_factory.assert_not_called()

    def test_submit_skips_on_us_holiday(self):
        with (
            mock.patch(
                "alphalens_cli.commands.paper._today_utc",
                return_value=_NON_TRADING_HOLIDAY,
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.alpaca_client.get_default_alpaca_client",
            ) as mock_client_factory,
        ):
            code, out = _run(["submit", "--date", "2026-05-22"])
        self.assertEqual(code, 0)
        self.assertIn("market closed", out.lower())
        mock_client_factory.assert_not_called()

    def test_submit_skips_message_names_next_session(self):
        with (
            mock.patch(
                "alphalens_cli.commands.paper._today_utc",
                return_value=_NON_TRADING_DAY,
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.alpaca_client.get_default_alpaca_client",
            ),
        ):
            code, out = _run(["submit", "--date", "2026-05-29"])
        self.assertEqual(code, 0)
        # The operator-facing message should anchor the deferral to the
        # next session open. Mon 2026-06-01 is the next XNYS open after
        # Sat 2026-05-30.
        self.assertIn("2026-06-01", out)

    def test_submit_allow_closed_market_bypasses_guard(self):
        # When --allow-closed-market is passed, the guard short-circuits
        # OFF and submission proceeds. We verify by observing that the
        # Alpaca client factory IS called (the guard would have prevented
        # it). Downstream submit_for_date is mocked to a no-op return.
        with (
            mock.patch(
                "alphalens_cli.commands.paper._today_utc",
                return_value=_NON_TRADING_DAY,
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.alpaca_client.get_default_alpaca_client",
            ) as mock_client_factory,
            mock.patch(
                "alphalens_pipeline.paper.submitter.submit_for_date",
            ) as mock_submit,
        ):
            mock_submit.return_value = _stub_submit_report()
            code, _ = _run(
                [
                    "submit",
                    "--date",
                    "2026-05-29",
                    "--allow-closed-market",
                ]
            )
        self.assertEqual(code, 0)
        mock_client_factory.assert_called_once()
        mock_submit.assert_called_once()

    def test_submit_proceeds_normally_on_trading_day(self):
        with (
            mock.patch(
                "alphalens_cli.commands.paper._today_utc",
                return_value=_TRADING_DAY,
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.alpaca_client.get_default_alpaca_client",
            ),
            mock.patch(
                "alphalens_pipeline.paper.submitter.submit_for_date",
            ) as mock_submit,
        ):
            mock_submit.return_value = _stub_submit_report()
            code, _ = _run(["submit", "--date", "2026-05-29"])
        self.assertEqual(code, 0)
        mock_submit.assert_called_once()


class TestReconcileGuard(unittest.TestCase):
    """``alphalens paper reconcile`` mirrors ``submit``'s guard. Even
    though reconcile is read-mostly, on non-trading days Alpaca state
    cannot have changed since the last session close — so the GET burst
    is wasted plus the TTL / time-stop sweeps (which PR-B will switch
    to trading-day arithmetic) would fire spuriously on calendar-day
    math.
    """

    def test_reconcile_skips_on_weekend(self):
        with (
            mock.patch(
                "alphalens_cli.commands.paper._today_utc",
                return_value=_NON_TRADING_DAY,
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.alpaca_client.get_default_alpaca_client",
            ) as mock_client_factory,
        ):
            code, out = _run(["reconcile"])
        self.assertEqual(code, 0)
        self.assertIn("market closed", out.lower())
        mock_client_factory.assert_not_called()

    def test_reconcile_allow_closed_market_bypasses_guard(self):
        with (
            mock.patch(
                "alphalens_cli.commands.paper._today_utc",
                return_value=_NON_TRADING_DAY,
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.alpaca_client.get_default_alpaca_client",
            ) as mock_client_factory,
            mock.patch(
                "alphalens_pipeline.paper.reconciler.reconcile_orders",
            ) as mock_reconcile,
            mock.patch("alphalens_cli.commands.paper.emit_domain_metrics"),
        ):
            mock_reconcile.return_value = _stub_reconcile_report()
            code, _ = _run(["reconcile", "--allow-closed-market"])
        self.assertEqual(code, 0)
        mock_client_factory.assert_called_once()
        mock_reconcile.assert_called_once()

    def test_reconcile_proceeds_normally_on_trading_day(self):
        with (
            mock.patch(
                "alphalens_cli.commands.paper._today_utc",
                return_value=_TRADING_DAY,
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.alpaca_client.get_default_alpaca_client",
            ),
            mock.patch(
                "alphalens_pipeline.paper.reconciler.reconcile_orders",
            ) as mock_reconcile,
            mock.patch("alphalens_cli.commands.paper.emit_domain_metrics"),
        ):
            mock_reconcile.return_value = _stub_reconcile_report()
            code, _ = _run(["reconcile"])
        self.assertEqual(code, 0)
        mock_reconcile.assert_called_once()


# --------------------------------------------------------------------- helpers


def _stub_submit_report():
    """Minimal ``SubmitReport`` shape for the trading-day happy path.
    Real shape lives in ``alphalens_pipeline.paper.submitter``; the CLI
    only reads a small slice of attributes."""

    class _Report:
        brief_date = _TRADING_DAY
        n_plans_processed = 0
        n_orders_submitted = 0
        outcomes: list = []

    return _Report()


def _stub_reconcile_report():
    class _Report:
        n_orders_checked = 0
        n_orders_transitioned = 0
        n_fills_appended = 0
        n_exits_attached = 0
        n_exits_failed = 0
        n_entries_canceled = 0
        n_filled_without_sl = 0
        n_ledger_broker_desync = 0
        outcomes: list = []

    return _Report()


if __name__ == "__main__":
    unittest.main()

"""Tests for ``alphalens paper is-trading-day``.

This subcommand is the gate the systemd ``ExecCondition=`` lines in
``alphalens-paper-submit.{service,timer}`` + ``alphalens-paper-
reconcile.{service,timer}`` use to skip US public holidays that fall on
weekdays (the ``OnCalendar=Mon..Fri`` filter only covers weekends).

Exit code contract — systemd ``ExecCondition=`` semantics:

* **0** — today is a trading day, proceed with the unit.
* **1** — today is not a trading day, systemd skips the unit silently
  (no logged failure, ``systemctl --user status`` shows "condition
  failed").

A non-zero exit MUST NOT be a hard error; it MUST be a clean
``sys.exit(1)`` so systemd does not interpret it as a unit failure
and trip the ``AlphalensJobFailed`` alert.

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_cli.commands.paper import paper_app
from typer.testing import CliRunner

# Same anchor dates as ``test_cli_market_closed_guard.py`` so the two
# suites read against the same calendar reality.
_TRADING_DAY = dt.date(2026, 5, 29)  # Friday
_WEEKEND = dt.date(2026, 5, 30)  # Saturday
_US_HOLIDAY_WEEKDAY = dt.date(2026, 5, 25)  # Memorial Day Monday


def _run(args: list[str]) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(paper_app, args)
    return result.exit_code, result.stdout + result.stderr


class TestIsTradingDayExitCodes(unittest.TestCase):
    """Round-trip exit codes for the four canonical day classes."""

    def test_exits_0_on_xnys_trading_day(self) -> None:
        # Default ``--exchange XNYS``; Friday non-holiday.
        with mock.patch(
            "alphalens_cli.commands.paper._today_utc",
            return_value=_TRADING_DAY,
        ):
            code, _ = _run(["is-trading-day"])
        self.assertEqual(
            code,
            0,
            "XNYS Friday non-holiday MUST exit 0 — systemd ExecCondition treats 0 as 'proceed'.",
        )

    def test_exits_1_on_weekend(self) -> None:
        with mock.patch(
            "alphalens_cli.commands.paper._today_utc",
            return_value=_WEEKEND,
        ):
            code, _ = _run(["is-trading-day"])
        self.assertEqual(
            code,
            1,
            "Saturday MUST exit 1 — systemd ExecCondition treats 1 as 'skip cleanly'.",
        )

    def test_exits_1_on_us_holiday_weekday(self) -> None:
        # Memorial Day is the highest-impact case — the Mon..Fri
        # OnCalendar filter alone would fire on it and ExecCondition is
        # the only thing that catches it.
        with mock.patch(
            "alphalens_cli.commands.paper._today_utc",
            return_value=_US_HOLIDAY_WEEKDAY,
        ):
            code, _ = _run(["is-trading-day"])
        self.assertEqual(
            code,
            1,
            "Memorial Day Monday MUST exit 1 — this is the case "
            "ExecCondition exists for. OnCalendar=Mon..Fri would "
            "otherwise fire on it.",
        )

    def test_date_flag_overrides_today_utc(self) -> None:
        # Operator uses ``--date`` to ask "is THIS date a trading day?"
        # for ad-hoc cron debugging. Today's wall clock must not leak
        # into the answer.
        with mock.patch(
            "alphalens_cli.commands.paper._today_utc",
            # Patch to a WEEKEND so we can prove --date wins.
            return_value=_WEEKEND,
        ):
            code, _ = _run(["is-trading-day", "--date", _TRADING_DAY.isoformat()])
        self.assertEqual(
            code,
            0,
            "--date must override _today_utc(); the Friday arg here "
            "must yield exit 0 even though _today_utc returns Saturday.",
        )

    def test_exchange_flag_accepted_for_forward_compatibility(self) -> None:
        # The paper harness is parametric on MIC code per CLAUDE.md
        # exchange-agnostic policy. ``alphalens paper is-trading-day
        # --exchange XWAR`` is the natural extension point once Warsaw
        # routing lands; pin the CLI surface here so a future caller
        # can't drop the flag silently.
        with mock.patch(
            "alphalens_cli.commands.paper._today_utc",
            return_value=_TRADING_DAY,
        ):
            code, _ = _run(["is-trading-day", "--exchange", "XNYS"])
        self.assertEqual(code, 0)


class TestPrintsTerseStatusToStderr(unittest.TestCase):
    """The CLI prints a one-line status to stderr so the systemd
    journal captures it. ExecCondition fires silently in production —
    the print is for operator debugging via ``systemctl --user start
    --no-block`` then ``journalctl --user``.
    """

    def test_message_carries_date_and_exchange(self) -> None:
        with mock.patch(
            "alphalens_cli.commands.paper._today_utc",
            return_value=_WEEKEND,
        ):
            _, output = _run(["is-trading-day"])
        # Both anchors appear so a journal grep on EITHER hits.
        self.assertIn(_WEEKEND.isoformat(), output)
        self.assertIn("XNYS", output)


if __name__ == "__main__":
    unittest.main()

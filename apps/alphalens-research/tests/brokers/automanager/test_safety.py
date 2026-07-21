"""Hermetic tests for the pure-predicate safety gate.

check is a pure function of inputs + two rails read at call time (KILL file,
ALLOW_ORDERS). Writes nothing — even the daily-loss branch RETURNS Refuse. One
refusal branch per test; first failing rail wins.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager.picks import Pick
from alphalens_pipeline.brokers.automanager.safety import (
    ALLOW_ORDERS_ENV,
    DAILY_LOSS_LIMIT_R_ENV,
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
    Allow,
    BrokerView,
    JournalView,
    Refuse,
    check,
)


@dataclass
class _StubSession:
    alive: bool


_PICK = Pick(ticker="KO", date=dt.date(2026, 7, 20), armed_ts="ts", status="armed")
_CLEAR_JOURNAL = JournalView(open_bracket_count=0, gross_committed=0.0, realized_r_today=0.0)
_CLEAR_BROKER = BrokerView(open_position_count=0, equity=1_000.0)


class SafetyGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.kill = Path(self._tmp.name) / "KILL"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_all_rails_clear_allows(self) -> None:
        d = check(
            _PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill
        )
        self.assertIsInstance(d, Allow)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_kill_file_present_refuses_first(self) -> None:
        self.kill.write_text("stop", encoding="utf-8")
        d = check(
            _PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn("KILL", d.reason)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_dead_chain_refuses(self) -> None:
        d = check(
            _PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=False), kill_path=self.kill
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn("chain", d.reason.lower())

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "0"}, clear=False)
    def test_allow_orders_not_set_refuses(self) -> None:
        d = check(
            _PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill
        )
        self.assertIsInstance(d, Refuse)
        self.assertIn(ALLOW_ORDERS_ENV, d.reason)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1", MAX_OPEN_ENV: "2"}, clear=False)
    def test_max_open_cap_refuses(self) -> None:
        journal = JournalView(open_bracket_count=1, gross_committed=0.0, realized_r_today=0.0)
        broker = BrokerView(open_position_count=1, equity=1_000.0)
        d = check(_PICK, journal, broker, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn("MAX_OPEN", d.reason)

    @mock.patch.dict(
        os.environ, {ALLOW_ORDERS_ENV: "1", PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=False
    )
    def test_portfolio_gross_cap_refuses(self) -> None:
        journal = JournalView(open_bracket_count=0, gross_committed=1_200.0, realized_r_today=0.0)
        d = check(_PICK, journal, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn("gross", d.reason.lower())

    @mock.patch.dict(
        os.environ, {ALLOW_ORDERS_ENV: "1", DAILY_LOSS_LIMIT_R_ENV: "3.0"}, clear=False
    )
    def test_daily_loss_limit_refuses_without_side_effects(self) -> None:
        journal = JournalView(open_bracket_count=0, gross_committed=0.0, realized_r_today=-3.5)
        d = check(_PICK, journal, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn("loss", d.reason.lower())
        self.assertFalse(self.kill.exists())


if __name__ == "__main__":
    unittest.main()

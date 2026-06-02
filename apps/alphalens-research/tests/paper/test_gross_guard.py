"""Tests for memo §6.1 Path B closed-loop live-gross WARNING."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from alphalens_pipeline.paper.gross_guard import check_live_gross


@dataclass
class _StubAccount:
    equity: float = 1_000_000.0
    long_market_value: float = 0.0


class _StubBrokerClient:
    def __init__(self, account: _StubAccount) -> None:
        self._account = account

    def get_account(self) -> _StubAccount:
        return self._account


class TestUnderEquity(unittest.TestCase):
    def test_zero_long_market_value_no_warning(self):
        client = _StubBrokerClient(_StubAccount())
        report = check_live_gross(client)
        self.assertEqual(report.gross_ratio, 0.0)
        self.assertFalse(report.warning_emitted)

    def test_within_equity_no_warning(self):
        """Steady-state operation: long_market_value < equity → no warning."""
        client = _StubBrokerClient(_StubAccount(long_market_value=666_000.0))
        report = check_live_gross(client)
        self.assertAlmostEqual(report.gross_ratio, 0.666, places=3)
        self.assertFalse(report.warning_emitted)

    def test_exactly_at_equity_no_warning(self):
        """Boundary: gross_ratio == 1.0 is NOT a warning (strict >). The
        memo's escalation triggers above 100%."""
        client = _StubBrokerClient(_StubAccount(long_market_value=1_000_000.0))
        report = check_live_gross(client)
        self.assertAlmostEqual(report.gross_ratio, 1.0)
        self.assertFalse(report.warning_emitted)


class TestOverEquityWarns(unittest.TestCase):
    def test_above_equity_emits_warning(self):
        client = _StubBrokerClient(_StubAccount(equity=1_000_000.0, long_market_value=1_200_000.0))
        with self.assertLogs("alphalens_pipeline.paper.gross_guard", level="WARNING") as cm:
            report = check_live_gross(client)
        self.assertAlmostEqual(report.gross_ratio, 1.2)
        self.assertTrue(report.warning_emitted)
        self.assertTrue(any("GROSS GUARD" in m for m in cm.output))
        self.assertTrue(any("§6.1" in m for m in cm.output))


class TestDefensiveDecoding(unittest.TestCase):
    def test_non_numeric_equity_skips_gracefully(self):
        """Some SDK versions return strings; the guard logs a warning and
        skips rather than crashing the reconcile pass."""

        @dataclass
        class _BadAccount:
            equity: str = "not-a-number"
            long_market_value: float = 0.0

        client = _StubBrokerClient(_BadAccount())  # type: ignore[arg-type]
        with self.assertLogs("alphalens_pipeline.paper.gross_guard", level="WARNING") as cm:
            report = check_live_gross(client)
        self.assertFalse(report.warning_emitted)
        self.assertTrue(any("equity not numeric" in m for m in cm.output))

    def test_missing_long_market_value_treated_as_zero(self):
        @dataclass
        class _NoMarketValueAccount:
            equity: float = 1_000_000.0
            long_market_value: None = None

        client = _StubBrokerClient(_NoMarketValueAccount())  # type: ignore[arg-type]
        report = check_live_gross(client)
        self.assertEqual(report.long_market_value, 0.0)
        self.assertFalse(report.warning_emitted)


if __name__ == "__main__":
    unittest.main()

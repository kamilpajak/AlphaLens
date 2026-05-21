"""Cross-source market-cap consistency gate in ``score_valuation``.

Issue #172 Bug 1 structural concern: brief UI displays
``yfinance.fast_info.market_cap`` while the Layer-4 scorer internally
computes mcap as ``price × EDGAR shares_outstanding``. The two diverged
37× for C3.ai because the EDGAR shares chain returned a stale 2021
post-IPO snapshot (3.5M vs ~140M). UI showed the right mcap; scorer
ranked AI as "deeply undervalued" (composite 93%ile).

Fix: when ``score_valuation`` is given an ``external_mcap_fetcher`` it
calls it once per ticker, computes the EDGAR-derived mcap, and degrades
the entire valuation cohort to ``None`` multiples when the divergence
exceeds :data:`MCAP_CONSISTENCY_TOLERANCE`. The ticker can still surface
in the brief — but its valuation_composite is honestly absent rather
than misleadingly extreme.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import MagicMock

from alphalens.thematic.screening import valuation_signal


def _features(price=10.0, shares=3_499_992.0, revenue_ttm=307_000_000.0, **overrides):
    base = {
        "ocf_ttm": -200_000_000.0,
        "capex_ttm": 20_000_000.0,
        "interest_expense_ttm": 0.0,
        "tax_rate": 0.21,
        "revenue_ttm": revenue_ttm,
        "fcf_margin_5y_median": -0.50,
        "price": price,
        "shares_outstanding": shares,
        "long_term_debt": 0.0,
        "short_term_debt": 0.0,
        "cash_and_equivalents": 700_000_000.0,
        "net_income_ttm": -350_000_000.0,
    }
    base.update(overrides)
    return base


class TestMcapConsistencyGate(unittest.TestCase):
    def test_divergence_above_tolerance_degrades_multiples(self):
        """C3.ai-shaped: EDGAR mcap $35M, yfinance mcap $1.28B → degrade."""
        fetcher = MagicMock(return_value=_features())
        external = MagicMock(return_value=1_280_000_000.0)
        out = valuation_signal.score_valuation(
            ticker="AI",
            asof=dt.date(2026, 5, 19),
            peers=["FOO", "BAR"],
            feature_fetcher=fetcher,
            external_mcap_fetcher=external,
        )
        self.assertIsNone(out["ps"])
        self.assertIsNone(out["ev_rev"])
        self.assertIsNone(out["pe"])
        self.assertIsNone(out["composite_sector_percentile"])
        external.assert_called_once_with("AI", dt.date(2026, 5, 19))

    def test_divergence_preserves_fcf_margin(self):
        """Zen finding #2 (PR #174): ``fcf_margin = effective_fcff / revenue_ttm``
        does NOT depend on market cap or shares, so a mcap-source divergence
        cannot invalidate it. The gate must surface this quality signal
        even when the multiples cohort is wiped.
        """
        # Build a feature set where actual FCFF is computable (so fcf_margin
        # is a real number, not the 5y-median fallback).
        fetcher = MagicMock(
            return_value=_features(
                ocf_ttm=200_000_000.0,
                capex_ttm=80_000_000.0,
                interest_expense_ttm=10_000_000.0,
                tax_rate=0.21,
                revenue_ttm=1_000_000_000.0,
                fcf_margin_5y_median=0.10,
            )
        )
        external = MagicMock(return_value=1_280_000_000.0)
        out = valuation_signal.score_valuation(
            ticker="AI",
            asof=dt.date(2026, 5, 19),
            peers=[],
            feature_fetcher=fetcher,
            external_mcap_fetcher=external,
        )
        self.assertIsNone(out["ps"])
        self.assertIsNone(out["ev_rev"])
        self.assertIsNone(out["pe"])
        # fcf_margin survives: (200M + 10M*(1-0.21) - 80M) / 1B = 0.1279
        # (interest is added back to OCF to undo its post-interest sign).
        self.assertAlmostEqual(out["fcf_margin"], 0.1279, places=4)

    def test_within_tolerance_keeps_multiples(self):
        """Stale-free case: edgar mcap = external × (1 ± 5%) → keep multiples."""
        fetcher = MagicMock(return_value=_features(price=10.0, shares=140_000_000.0))
        external = MagicMock(return_value=1_400_000_000.0)  # exact match
        out = valuation_signal.score_valuation(
            ticker="AI",
            asof=dt.date(2026, 5, 19),
            peers=[],
            feature_fetcher=fetcher,
            external_mcap_fetcher=external,
        )
        self.assertIsNotNone(out["ps"])

    def test_external_fetcher_none_disables_gate(self):
        """Backward-compat: no fetcher → behavior unchanged."""
        fetcher = MagicMock(return_value=_features())
        out = valuation_signal.score_valuation(
            ticker="AI",
            asof=dt.date(2026, 5, 19),
            peers=[],
            feature_fetcher=fetcher,
            external_mcap_fetcher=None,
        )
        # Multiples are still computed off the (potentially bogus) EDGAR
        # mcap when no external reference is supplied.
        self.assertIsNotNone(out["ps"])

    def test_external_fetcher_returns_none_disables_gate(self):
        """yfinance can fail (delisted, network) — skip gate, don't crash."""
        fetcher = MagicMock(return_value=_features(price=10.0, shares=140_000_000.0))
        external = MagicMock(return_value=None)
        out = valuation_signal.score_valuation(
            ticker="AI",
            asof=dt.date(2026, 5, 19),
            peers=[],
            feature_fetcher=fetcher,
            external_mcap_fetcher=external,
        )
        self.assertIsNotNone(out["ps"])

    def test_edgar_mcap_missing_skips_gate(self):
        """Shares=None → no EDGAR mcap to compare; gate is a no-op."""
        fetcher = MagicMock(return_value=_features(price=10.0, shares=None, revenue_ttm=1000.0))
        external = MagicMock(return_value=1_280_000_000.0)
        out = valuation_signal.score_valuation(
            ticker="AI",
            asof=dt.date(2026, 5, 19),
            peers=[],
            feature_fetcher=fetcher,
            external_mcap_fetcher=external,
        )
        # No mcap → no multiples regardless of gate; just must not crash.
        self.assertIsNone(out["ps"])


if __name__ == "__main__":
    unittest.main()

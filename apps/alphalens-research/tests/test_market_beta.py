"""The market-beta contract (#996): behaviour, and where it lives.

It lives in the pipeline so a pipeline stamper can use it: one implementation,
reachable without importing the research lab.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from alphalens_pipeline.feedback import market_beta as mb
from alphalens_research.diagnostics import fixed_horizon as fh


class TestMarketBetaHome(unittest.TestCase):
    def test_there_is_one_implementation(self):
        self.assertFalse(hasattr(fh, "estimate_beta"))

    def test_importing_it_loads_no_research_module(self):
        code = (
            "import sys; import alphalens_pipeline.feedback.market_beta; "
            "print(sorted(m for m in sys.modules if m.startswith('alphalens_research')))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(out, "[]")


def _closes(returns, start=100.0):
    """Chronological close series implied by ``returns`` (first close is ``start``)."""
    out = [start]
    for r in returns:
        out.append(out[-1] * (1.0 + r))
    return out


# Deterministic, non-constant market path — 40 returns, so a default-window estimate is usable.
_MARKET_RETURNS = [0.01, -0.005, 0.02, -0.015] * 10


class TestEstimateBeta(unittest.TestCase):
    def test_recovers_known_beta_from_a_pure_two_times_series(self):
        market = _closes(_MARKET_RETURNS)
        stock = _closes([2.0 * r for r in _MARKET_RETURNS])

        est = mb.estimate_beta(stock, market)

        self.assertAlmostEqual(est.beta, 2.0, places=6)
        self.assertEqual(est.source, mb.BETA_ESTIMATED)
        self.assertEqual(est.n_observations, len(_MARKET_RETURNS))

    def test_falls_back_to_one_below_min_observations(self):
        market = _closes(_MARKET_RETURNS[:5])
        stock = _closes([2.0 * r for r in _MARKET_RETURNS[:5]])

        est = mb.estimate_beta(stock, market)

        self.assertEqual(est.beta, 1.0)
        self.assertEqual(est.source, mb.BETA_FALLBACK_THIN)
        self.assertEqual(est.n_observations, 5)

    def test_falls_back_when_the_market_never_moves(self):
        market = [100.0] * (len(_MARKET_RETURNS) + 1)
        stock = _closes(_MARKET_RETURNS)

        est = mb.estimate_beta(stock, market)

        self.assertEqual(est.beta, 1.0)
        self.assertEqual(est.source, mb.BETA_FALLBACK_DEGENERATE)

    def test_falls_back_when_the_stock_never_moves(self):
        # A stale ticker prints the same close every session. Regressing that on a moving
        # market gives beta 0, which would strip the market adjustment out entirely --
        # worse than the beta=1 baseline this variant exists to improve on.
        market = _closes(_MARKET_RETURNS)
        stock = [100.0] * len(market)

        est = mb.estimate_beta(stock, market)

        self.assertEqual(est.beta, 1.0)
        self.assertEqual(est.source, mb.BETA_FALLBACK_DEGENERATE)

    def test_a_thin_window_and_a_degenerate_one_are_tagged_differently(self):
        market = _closes(_MARKET_RETURNS)

        thin = mb.estimate_beta(_closes(_MARKET_RETURNS[:5]), _closes(_MARKET_RETURNS[:5]))
        degenerate = mb.estimate_beta([100.0] * len(market), market)

        self.assertNotEqual(thin.source, degenerate.source)

    def test_zero_return_sessions_are_counted_so_partial_staleness_is_visible(self):
        # The degeneracy guard only catches a perfectly flat stock. A half-stale one still
        # estimates, so the count of flat sessions is reported instead of silently ignored.
        returns = list(_MARKET_RETURNS)
        stale = [0.0 if i % 2 else 2.0 * r for i, r in enumerate(returns)]
        market = _closes(returns)

        est = mb.estimate_beta(_closes(stale), market)

        self.assertEqual(est.source, mb.BETA_ESTIMATED)
        self.assertEqual(est.n_zero_returns, sum(1 for r in stale if r == 0.0))
        self.assertGreater(est.n_zero_returns, 0)

    def test_a_move_below_the_float_noise_floor_counts_as_flat(self):
        returns = list(_MARKET_RETURNS)
        # A move this small is a rounding artefact, not a session in which the stock traded.
        stale = [mb.FLAT_RETURN_TOL / 2.0 if i % 2 else 2.0 * r for i, r in enumerate(returns)]
        market = _closes(returns)

        est = mb.estimate_beta(_closes(stale), market)

        self.assertEqual(est.n_zero_returns, sum(1 for i in range(len(returns)) if i % 2))

    def test_a_missing_close_drops_both_returns_that_span_it(self):
        market = _closes(_MARKET_RETURNS)
        stock = _closes([2.0 * r for r in _MARKET_RETURNS])
        stock[20] = None  # neither r_20 nor r_21 spans a single session any more

        est = mb.estimate_beta(stock, market)

        self.assertEqual(est.n_observations, len(_MARKET_RETURNS) - 2)
        self.assertAlmostEqual(est.beta, 2.0, places=6)
        self.assertEqual(est.source, mb.BETA_ESTIMATED)

    def test_non_positive_close_is_treated_as_missing(self):
        market = _closes(_MARKET_RETURNS)
        stock = _closes([2.0 * r for r in _MARKET_RETURNS])
        stock[20] = 0.0

        est = mb.estimate_beta(stock, market)

        self.assertEqual(est.n_observations, len(_MARKET_RETURNS) - 2)

    def test_mismatched_series_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            mb.estimate_beta([100.0, 101.0], [100.0])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for fixed-horizon CAR + bootstrap (pure, no I/O)."""

from __future__ import annotations

import unittest

from alphalens_research.diagnostics import fixed_horizon as fh


class TestCarForEvent(unittest.TestCase):
    def test_market_adjusted_bhar(self):
        # stock +10%, SPY +4% -> CAR +6%.
        car = fh.car_for_event(
            stock_anchor=100.0, stock_horizon=110.0, spy_anchor=100.0, spy_horizon=104.0
        )
        self.assertAlmostEqual(car, 0.06)

    def test_none_on_missing_or_nonpositive(self):
        self.assertIsNone(
            fh.car_for_event(
                stock_anchor=None, stock_horizon=110.0, spy_anchor=100.0, spy_horizon=104.0
            )
        )
        self.assertIsNone(
            fh.car_for_event(
                stock_anchor=0.0, stock_horizon=110.0, spy_anchor=100.0, spy_horizon=104.0
            )
        )


class TestBootstrapCi(unittest.TestCase):
    def test_deterministic_and_brackets_mean(self):
        vals = [0.01, -0.02, 0.05, 0.03, -0.01, 0.04]
        lo, mean, hi = fh.bootstrap_ci(vals, n_resamples=2000, ci=0.90, seed=42)
        self.assertAlmostEqual(mean, sum(vals) / len(vals))
        self.assertLessEqual(lo, mean)
        self.assertLessEqual(mean, hi)
        # reproducible
        lo2, _, hi2 = fh.bootstrap_ci(vals, n_resamples=2000, ci=0.90, seed=42)
        self.assertEqual((lo, hi), (lo2, hi2))

    def test_empty_and_singleton(self):
        self.assertEqual(fh.bootstrap_ci([], n_resamples=100, seed=1), (None, None, None))
        self.assertEqual(fh.bootstrap_ci([0.07], n_resamples=100, seed=1), (0.07, 0.07, 0.07))

    def test_filters_none(self):
        _lo, mean, _hi = fh.bootstrap_ci([0.02, None, 0.04], n_resamples=500, seed=3)
        self.assertAlmostEqual(mean, 0.03)


class TestDayBlockBootstrapCi(unittest.TestCase):
    def test_empty_returns_none_triple(self):
        self.assertEqual(fh.day_block_bootstrap_ci({}), (None, None, None))

    def test_all_none_values_returns_none_triple(self):
        self.assertEqual(
            fh.day_block_bootstrap_ci({"d1": [None, None], "d2": [None]}),
            (None, None, None),
        )

    def test_single_non_empty_day_is_degenerate(self):
        # n_eff = 1 because resampling 1 day always draws the same day
        lo, mean, hi = fh.day_block_bootstrap_ci(
            {"d1": [0.01, 0.02, 0.03, 0.04, 0.05]}, n_resamples=1000, seed=7
        )
        expected = (0.01 + 0.02 + 0.03 + 0.04 + 0.05) / 5
        import math

        self.assertTrue(math.isclose(lo, expected))
        self.assertTrue(math.isclose(mean, expected))
        self.assertTrue(math.isclose(hi, expected))

    def test_grand_mean_equals_bootstrap_ci_mean(self):
        import math

        d1 = [0.01, 0.02, 0.03]
        d2 = [0.05, 0.06]
        flat = d1 + d2
        _, db_mean, _ = fh.day_block_bootstrap_ci({"d1": d1, "d2": d2}, n_resamples=500, seed=0)
        _, bs_mean, _ = fh.bootstrap_ci(flat, n_resamples=500, seed=0)
        # grand mean must be equal (NOT mean-of-day-means)
        self.assertTrue(math.isclose(db_mean, bs_mean))

    def test_ci_width_contrast_single_day_vs_multi_row(self):
        # 5 rows all in ONE day: day_block is degenerate; bootstrap_ci is not
        vals = [0.01, 0.02, 0.03, 0.04, 0.05]
        db_lo, _, db_hi = fh.day_block_bootstrap_ci({"d1": vals}, n_resamples=5000, seed=0)
        bs_lo, _, bs_hi = fh.bootstrap_ci(vals, n_resamples=5000, seed=0)
        # day_block degenerate
        import math

        self.assertTrue(math.isclose(db_lo, db_hi))
        # bootstrap_ci is non-degenerate
        self.assertLess(bs_lo, bs_hi)

    def test_two_single_row_days_gives_real_ci(self):
        # 2 days, each 1 row — resampling draws either day, so CI is non-degenerate
        lo, _mean, hi = fh.day_block_bootstrap_ci(
            {"d1": [0.10], "d2": [-0.10]}, n_resamples=5000, seed=0
        )
        self.assertLess(lo, hi)

    def test_determinism_same_seed(self):
        data = {"d1": [0.01, 0.02], "d2": [0.03, 0.04, 0.05]}
        r1 = fh.day_block_bootstrap_ci(data, n_resamples=1000, seed=42)
        r2 = fh.day_block_bootstrap_ci(data, n_resamples=1000, seed=42)
        self.assertEqual(r1, r2)

    def test_different_seeds_differ(self):
        # Use 5 days with distinct mean values so the CI endpoints are seed-sensitive.
        data = {
            "d1": [0.10, 0.12],
            "d2": [-0.05, -0.08],
            "d3": [0.20, 0.22],
            "d4": [-0.15, -0.12],
            "d5": [0.30, 0.28],
        }
        r1 = fh.day_block_bootstrap_ci(data, n_resamples=1000, seed=1)
        r2 = fh.day_block_bootstrap_ci(data, n_resamples=1000, seed=2)
        self.assertNotEqual(r1, r2)

    def test_none_dropped_within_day(self):
        import math

        # day with [1.0, None, 3.0] should contribute 1.0 and 3.0 (mean=2.0)
        # single day → degenerate (lo==mean==hi==2.0)
        lo, mean, hi = fh.day_block_bootstrap_ci({"d1": [1.0, None, 3.0]}, n_resamples=500, seed=0)
        self.assertTrue(math.isclose(mean, 2.0))
        self.assertTrue(math.isclose(lo, 2.0))
        self.assertTrue(math.isclose(hi, 2.0))


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

        est = fh.estimate_beta(stock, market)

        self.assertAlmostEqual(est.beta, 2.0, places=6)
        self.assertEqual(est.source, fh.BETA_ESTIMATED)
        self.assertEqual(est.n_observations, len(_MARKET_RETURNS))

    def test_falls_back_to_one_below_min_observations(self):
        market = _closes(_MARKET_RETURNS[:5])
        stock = _closes([2.0 * r for r in _MARKET_RETURNS[:5]])

        est = fh.estimate_beta(stock, market)

        self.assertEqual(est.beta, 1.0)
        self.assertEqual(est.source, fh.BETA_FALLBACK_THIN)
        self.assertEqual(est.n_observations, 5)

    def test_falls_back_when_the_market_never_moves(self):
        market = [100.0] * (len(_MARKET_RETURNS) + 1)
        stock = _closes(_MARKET_RETURNS)

        est = fh.estimate_beta(stock, market)

        self.assertEqual(est.beta, 1.0)
        self.assertEqual(est.source, fh.BETA_FALLBACK_DEGENERATE)

    def test_falls_back_when_the_stock_never_moves(self):
        # A stale ticker prints the same close every session. Regressing that on a moving
        # market gives beta 0, which would strip the market adjustment out entirely --
        # worse than the beta=1 baseline this variant exists to improve on.
        market = _closes(_MARKET_RETURNS)
        stock = [100.0] * len(market)

        est = fh.estimate_beta(stock, market)

        self.assertEqual(est.beta, 1.0)
        self.assertEqual(est.source, fh.BETA_FALLBACK_DEGENERATE)

    def test_a_thin_window_and_a_degenerate_one_are_tagged_differently(self):
        market = _closes(_MARKET_RETURNS)

        thin = fh.estimate_beta(_closes(_MARKET_RETURNS[:5]), _closes(_MARKET_RETURNS[:5]))
        degenerate = fh.estimate_beta([100.0] * len(market), market)

        self.assertNotEqual(thin.source, degenerate.source)

    def test_zero_return_sessions_are_counted_so_partial_staleness_is_visible(self):
        # The degeneracy guard only catches a perfectly flat stock. A half-stale one still
        # estimates, so the count of flat sessions is reported instead of silently ignored.
        returns = list(_MARKET_RETURNS)
        stale = [0.0 if i % 2 else 2.0 * r for i, r in enumerate(returns)]
        market = _closes(returns)

        est = fh.estimate_beta(_closes(stale), market)

        self.assertEqual(est.source, fh.BETA_ESTIMATED)
        self.assertEqual(est.n_zero_returns, sum(1 for r in stale if r == 0.0))
        self.assertGreater(est.n_zero_returns, 0)

    def test_a_move_below_the_float_noise_floor_counts_as_flat(self):
        returns = list(_MARKET_RETURNS)
        # A move this small is a rounding artefact, not a session in which the stock traded.
        stale = [fh.FLAT_RETURN_TOL / 2.0 if i % 2 else 2.0 * r for i, r in enumerate(returns)]
        market = _closes(returns)

        est = fh.estimate_beta(_closes(stale), market)

        self.assertEqual(est.n_zero_returns, sum(1 for i in range(len(returns)) if i % 2))

    def test_a_missing_close_drops_both_returns_that_span_it(self):
        market = _closes(_MARKET_RETURNS)
        stock = _closes([2.0 * r for r in _MARKET_RETURNS])
        stock[20] = None  # neither r_20 nor r_21 spans a single session any more

        est = fh.estimate_beta(stock, market)

        self.assertEqual(est.n_observations, len(_MARKET_RETURNS) - 2)
        self.assertAlmostEqual(est.beta, 2.0, places=6)
        self.assertEqual(est.source, fh.BETA_ESTIMATED)

    def test_non_positive_close_is_treated_as_missing(self):
        market = _closes(_MARKET_RETURNS)
        stock = _closes([2.0 * r for r in _MARKET_RETURNS])
        stock[20] = 0.0

        est = fh.estimate_beta(stock, market)

        self.assertEqual(est.n_observations, len(_MARKET_RETURNS) - 2)

    def test_mismatched_series_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            fh.estimate_beta([100.0, 101.0], [100.0])


class TestCarForEventMarketModel(unittest.TestCase):
    def test_high_beta_name_on_an_up_market_day_has_no_abnormal_return(self):
        # A beta-2 name that returns exactly 2x a +4% market has earned nothing abnormal.
        kwargs = {
            "stock_anchor": 100.0,
            "stock_horizon": 108.0,
            "spy_anchor": 100.0,
            "spy_horizon": 104.0,
        }

        self.assertAlmostEqual(fh.car_for_event_market_model(beta=2.0, **kwargs), 0.0)
        # The beta=1 form calls the same move a +4% win — the bias this variant removes.
        self.assertAlmostEqual(fh.car_for_event(**kwargs), 0.04)

    def test_beta_one_reproduces_the_market_adjusted_form(self):
        kwargs = {
            "stock_anchor": 100.0,
            "stock_horizon": 110.0,
            "spy_anchor": 100.0,
            "spy_horizon": 104.0,
        }
        self.assertAlmostEqual(
            fh.car_for_event_market_model(beta=1.0, **kwargs), fh.car_for_event(**kwargs)
        )

    def test_none_on_missing_or_nonpositive(self):
        self.assertIsNone(
            fh.car_for_event_market_model(
                beta=2.0,
                stock_anchor=None,
                stock_horizon=108.0,
                spy_anchor=100.0,
                spy_horizon=104.0,
            )
        )
        self.assertIsNone(
            fh.car_for_event_market_model(
                beta=2.0,
                stock_anchor=100.0,
                stock_horizon=108.0,
                spy_anchor=0.0,
                spy_horizon=104.0,
            )
        )


if __name__ == "__main__":
    unittest.main()

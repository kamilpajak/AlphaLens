"""The O'Neil 0-100 score — gating, clips, renormalization, coverage shrink.

The defining behaviour vs Buffett: N (proximity to the 52w high) is MANDATORY —
the score is ``None`` whenever N is absent (missing or split-contaminated), even
when trend + earnings are present. The two optional terms partial-credit and are
renormalized out of the weighting when absent.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.experts.oneil import score as score_mod
from alphalens_pipeline.experts.oneil.comparison import ONeilPanel
from alphalens_pipeline.experts.oneil.score import compute_oneil_score


def _panel(
    *,
    pct_off_52w_high: float | None = -5.0,
    ma200_slope_pct_per_day: float | None = None,
    earnings_growth_yoy_pct: float | None = None,
    near_zero_base: bool | None = False,
    split_suspected: bool | None = False,
    data_coverage: float = 0.0,
    ma200_distance_pct: float | None = None,
    rs_approx: float | None = None,
) -> ONeilPanel:
    return ONeilPanel(
        ticker="AAA",
        theme="t",
        pct_off_52w_high=pct_off_52w_high,
        ma200_slope_pct_per_day=ma200_slope_pct_per_day,
        ma200_distance_pct=ma200_distance_pct,
        earnings_growth_yoy_pct=earnings_growth_yoy_pct,
        earnings_growth_near_zero_base=near_zero_base,
        new_high_split_suspected=split_suspected,
        data_coverage=data_coverage,
        oneil_rs_approx_pct=rs_approx,
    )


class TestNewHighGate(unittest.TestCase):
    def test_score_none_when_new_high_absent(self):
        # N column absent => None even with both optional terms present.
        panel = _panel(
            pct_off_52w_high=None,
            ma200_slope_pct_per_day=0.10,
            earnings_growth_yoy_pct=50.0,
            data_coverage=1.0,
        )
        self.assertIsNone(compute_oneil_score(panel))

    def test_score_none_when_split_suspected(self):
        # N present but split-contaminated => None (the peak is unreliable).
        panel = _panel(
            pct_off_52w_high=-2.0,
            split_suspected=True,
            ma200_slope_pct_per_day=0.10,
            earnings_growth_yoy_pct=50.0,
            data_coverage=1.0,
        )
        self.assertIsNone(compute_oneil_score(panel))


class TestCoverageShrink(unittest.TestCase):
    def test_score_n_only_is_halved(self):
        # N present (0% off high => f_nh=1.0), both optional absent =>
        # raw = 100, coverage 0.0 => shrink 0.5 => 50.0.
        panel = _panel(pct_off_52w_high=0.0, data_coverage=0.0)
        self.assertAlmostEqual(compute_oneil_score(panel), 50.0)

    def test_score_full_coverage_unchanged(self):
        # All three full credit, coverage 1.0 => shrink 1.0 => raw 100.
        panel = _panel(
            pct_off_52w_high=0.0,
            ma200_slope_pct_per_day=0.10,
            earnings_growth_yoy_pct=50.0,
            data_coverage=1.0,
        )
        self.assertAlmostEqual(compute_oneil_score(panel), 100.0)

    def test_renormalized_sum_no_silent_deflation(self):
        # N + trend present (both full), earnings absent. Renormalized over
        # {N=0.40, trend=0.30} => raw 100, NOT deflated by the missing 0.30
        # earnings weight. coverage 0.5 => shrink 0.75 => 75.0.
        panel = _panel(
            pct_off_52w_high=0.0,
            ma200_slope_pct_per_day=0.10,
            earnings_growth_yoy_pct=None,
            data_coverage=0.5,
        )
        self.assertAlmostEqual(compute_oneil_score(panel), 75.0)


class TestClips(unittest.TestCase):
    def test_new_high_clip_boundaries(self):
        # f_nh: 0.0 => 1.0, -25.0 => 0.0, -40.0 => clipped 0.0, +2.0 => clipped 1.0.
        self.assertAlmostEqual(score_mod._new_high_credit(0.0), 1.0)
        self.assertAlmostEqual(score_mod._new_high_credit(-25.0), 0.0)
        self.assertAlmostEqual(score_mod._new_high_credit(-40.0), 0.0)
        self.assertAlmostEqual(score_mod._new_high_credit(2.0), 1.0)
        self.assertAlmostEqual(score_mod._new_high_credit(-12.5), 0.5)

    def test_trend_floor_no_credit_when_falling(self):
        self.assertAlmostEqual(score_mod._trend_credit(-0.05), 0.0)
        self.assertAlmostEqual(score_mod._trend_credit(0.0), 0.0)
        self.assertAlmostEqual(score_mod._trend_credit(0.10), 1.0)
        self.assertAlmostEqual(score_mod._trend_credit(0.20), 1.0)
        self.assertAlmostEqual(score_mod._trend_credit(0.05), 0.5)

    def test_earnings_clip(self):
        self.assertAlmostEqual(score_mod._earnings_credit(50.0), 1.0)
        self.assertAlmostEqual(score_mod._earnings_credit(200.0), 1.0)
        self.assertAlmostEqual(score_mod._earnings_credit(-10.0), 0.0)
        self.assertAlmostEqual(score_mod._earnings_credit(25.0), 0.5)


class TestTrendPresentZeroCredit(unittest.TestCase):
    def test_falling_trend_present_in_coverage_zero_credit(self):
        # Trend present but <=0 => 0 credit, but still a present term. R + earnings
        # absent. N full (1.0). 4-term weights: N=0.35, trend=0.20.
        # raw = 100 * (0.35*1.0 + 0.20*0.0)/(0.35+0.20); shrink = 0.5 + 0.5*0.5 = 0.75.
        panel = _panel(
            pct_off_52w_high=0.0,
            ma200_slope_pct_per_day=-0.02,
            earnings_growth_yoy_pct=None,
            data_coverage=0.5,
        )
        raw = 100.0 * (0.35 * 1.0) / (0.35 + 0.20)
        self.assertAlmostEqual(compute_oneil_score(panel), raw * 0.75)


class TestRsTerm(unittest.TestCase):
    """R (relative strength) — the 4th term: optional, never gated, renormalized."""

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(
            score_mod._W_NEW_HIGH + score_mod._W_RS + score_mod._W_TREND + score_mod._W_EARNINGS,
            1.0,
        )

    def test_rs_credit_boundaries(self):
        self.assertAlmostEqual(score_mod._rs_credit(0.0), 0.0)
        self.assertAlmostEqual(score_mod._rs_credit(50.0), 0.5)
        self.assertAlmostEqual(score_mod._rs_credit(100.0), 1.0)
        self.assertAlmostEqual(score_mod._rs_credit(150.0), 1.0)  # clipped

    def test_n_plus_rs_renormalized(self):
        # N full (0% off high) + R full (100 pct), trend + earnings absent.
        # raw = 100 * (0.35*1.0 + 0.25*1.0)/(0.35+0.25) = 100; coverage 1/3 -> shrink
        # 0.5 + 0.5*(1/3) = 0.6667.
        panel = _panel(pct_off_52w_high=0.0, rs_approx=100.0, data_coverage=1 / 3)
        raw = 100.0 * (0.35 + 0.25) / (0.35 + 0.25)
        self.assertAlmostEqual(compute_oneil_score(panel), raw * (0.5 + 0.5 * (1 / 3)))

    def test_rs_absent_drops_out_of_weighting(self):
        # R None: N-only score is unchanged from the pre-R behaviour (N renorms to 100).
        panel = _panel(pct_off_52w_high=0.0, rs_approx=None, data_coverage=0.0)
        self.assertAlmostEqual(compute_oneil_score(panel), 100.0 * 0.5)  # shrink 0.5

    def test_rs_does_not_bypass_the_n_gate(self):
        # N still the SOLE hard gate: split-suspected OR pct_off None => None even with R.
        self.assertIsNone(
            compute_oneil_score(_panel(pct_off_52w_high=None, rs_approx=80.0, data_coverage=1 / 3))
        )
        self.assertIsNone(
            compute_oneil_score(
                _panel(
                    pct_off_52w_high=-2.0, split_suspected=True, rs_approx=80.0, data_coverage=1 / 3
                )
            )
        )


if __name__ == "__main__":
    unittest.main()

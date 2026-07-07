"""Pure feature-function tests for the options telemetry (spec 2026-07-07 §4)."""

from __future__ import annotations

import datetime as dt
import unittest

import pandas as pd
from alphalens_pipeline.thematic.options_telemetry import features as f

ASOF = dt.date(2026, 7, 6)


def _chain(strikes, ivs, bids=None, asks=None, oi=None, vol=None) -> pd.DataFrame:
    n = len(strikes)
    return pd.DataFrame(
        {
            "strike": strikes,
            "impliedVolatility": ivs,
            "bid": bids or [1.0] * n,
            "ask": asks or [1.1] * n,
            "openInterest": oi or [100] * n,
            "volume": vol or [10] * n,
        }
    )


class TestExpirySelection(unittest.TestCase):
    def test_brackets_30d(self):
        expiries = [ASOF + dt.timedelta(days=d) for d in (3, 17, 45, 170)]
        near, far = f.select_bracketing_expiries(expiries, ASOF)
        self.assertEqual(near, ASOF + dt.timedelta(days=17))
        self.assertEqual(far, ASOF + dt.timedelta(days=45))

    def test_near_leg_must_have_dte_ge_7(self):
        # Only a 3-DTE and a 45-DTE listed: gamma-week 3d leg is skipped.
        expiries = [ASOF + dt.timedelta(days=d) for d in (3, 45)]
        near, far = f.select_bracketing_expiries(expiries, ASOF)
        self.assertIsNone(near)
        self.assertEqual(far, ASOF + dt.timedelta(days=45))

    def test_no_chain_returns_none_pair(self):
        self.assertEqual(f.select_bracketing_expiries([], ASOF), (None, None))

    def test_term_leg_closest_to_180_within_band(self):
        expiries = [ASOF + dt.timedelta(days=d) for d in (17, 45, 130, 200, 400)]
        self.assertEqual(f.select_term_expiry(expiries, ASOF), ASOF + dt.timedelta(days=200))

    def test_term_leg_none_outside_band(self):
        expiries = [ASOF + dt.timedelta(days=d) for d in (17, 45, 400)]
        self.assertIsNone(f.select_term_expiry(expiries, ASOF))


class TestIvSanityAndAtm(unittest.TestCase):
    def test_sane_iv_band(self):
        self.assertTrue(f.sane_iv(0.5))
        self.assertFalse(f.sane_iv(0.001))  # stale/broken near-zero quote
        self.assertFalse(f.sane_iv(7.0))  # zero-bid inversion blow-up
        self.assertFalse(f.sane_iv(None))
        self.assertFalse(f.sane_iv(float("nan")))

    def test_atm_strike_needs_both_legs(self):
        calls = _chain([95.0, 100.0, 105.0], [0.5, 0.5, 0.5])
        puts = _chain([95.0, 105.0], [0.5, 0.5])  # 100 missing on the put side
        self.assertEqual(f.atm_strike(calls, puts, spot=101.0), 105.0)

    def test_expiry_atm_iv_midpoint(self):
        calls = _chain([100.0], [0.50])
        puts = _chain([100.0], [0.54])
        self.assertAlmostEqual(f.expiry_atm_iv(calls, puts, spot=100.0), 0.52)

    def test_expiry_atm_iv_single_sane_leg(self):
        calls = _chain([100.0], [0.0001])  # insane vendor IV
        puts = _chain([100.0], [0.54])
        self.assertAlmostEqual(f.expiry_atm_iv(calls, puts, spot=100.0), 0.54)

    def test_expiry_atm_iv_both_insane_is_none(self):
        calls = _chain([100.0], [0.0001])
        puts = _chain([100.0], [9.9])
        self.assertIsNone(f.expiry_atm_iv(calls, puts, spot=100.0))


class TestInterpolationAndSkew(unittest.TestCase):
    def test_linear_interpolation_at_30(self):
        # 20 DTE @ 0.60, 40 DTE @ 0.40 -> 30 DTE @ 0.50
        self.assertAlmostEqual(f.interpolate_iv30(0.60, 20, 0.40, 40), 0.50)

    def test_single_leg_is_flat(self):
        self.assertAlmostEqual(f.interpolate_iv30(None, None, 0.40, 45), 0.40)
        self.assertAlmostEqual(f.interpolate_iv30(0.60, 20, None, None), 0.60)

    def test_no_legs_is_none(self):
        self.assertIsNone(f.interpolate_iv30(None, None, None, None))

    def test_skew_xzz(self):
        # spot 100: OTM put window [80, 95] closest to 95 -> strike 94 @ 0.62;
        # ATM call window [95, 105] closest to 100 -> strike 101 @ 0.50.
        puts = _chain([75.0, 90.0, 94.0], [0.70, 0.65, 0.62])
        calls = _chain([96.0, 101.0, 110.0], [0.52, 0.50, 0.48])
        self.assertAlmostEqual(f.skew_xzz(calls, puts, spot=100.0), 0.12)

    def test_skew_none_when_no_otm_put_in_window(self):
        puts = _chain([50.0], [0.70])  # moneyness 0.5, outside [0.80, 0.95]
        calls = _chain([100.0], [0.50])
        self.assertIsNone(f.skew_xzz(calls, puts, spot=100.0))


class TestQuoteAndTotals(unittest.TestCase):
    def test_atm_quote_spread_pct(self):
        calls = _chain([100.0], [0.5], bids=[3.0], asks=[3.2])
        puts = _chain([100.0], [0.5])
        strike, mid, spread_pct = f.atm_quote(calls, puts, spot=100.0)
        self.assertEqual(strike, 100.0)
        self.assertAlmostEqual(mid, 3.1)
        self.assertAlmostEqual(spread_pct, 0.2 / 3.1)

    def test_atm_quote_falls_back_to_put_on_zero_bid_call(self):
        calls = _chain([100.0], [0.5], bids=[0.0], asks=[0.4])
        puts = _chain([100.0], [0.5], bids=[2.0], asks=[2.2])
        _, mid, _ = f.atm_quote(calls, puts, spot=100.0)
        self.assertAlmostEqual(mid, 2.1)

    def test_atm_quote_none_when_both_unusable(self):
        calls = _chain([100.0], [0.5], bids=[0.0], asks=[0.4])
        puts = _chain([100.0], [0.5], bids=[3.0], asks=[2.0])  # ask < bid
        self.assertIsNone(f.atm_quote(calls, puts, spot=100.0))

    def test_chain_totals_sum_both_legs(self):
        e1 = (_chain([100.0], [0.5], vol=[10], oi=[100]), _chain([100.0], [0.5], vol=[4], oi=[40]))
        e2 = (_chain([100.0], [0.5], vol=[6], oi=[60]), _chain([100.0], [0.5], vol=[1], oi=[10]))
        totals = f.chain_totals([e1, e2])
        self.assertEqual(totals["call_vol"], 16.0)
        self.assertEqual(totals["put_vol"], 5.0)
        self.assertEqual(totals["call_oi"], 160.0)
        self.assertEqual(totals["put_oi"], 50.0)


class TestChainQuality(unittest.TestCase):
    def _ok_kwargs(self):
        return {
            "has_chain": True,
            "near": ASOF + dt.timedelta(days=17),
            "far": ASOF + dt.timedelta(days=45),
            "atm": 100.0,
            "atm_call_oi": 60.0,
            "atm_put_oi": 55.0,
            "atm_vol_total": 3.0,
            "spread_pct": 0.05,
        }

    def test_ok(self):
        self.assertEqual(f.classify_chain_quality(**self._ok_kwargs()), f.CHAIN_QUALITY_OK)

    def test_none_when_no_chain(self):
        kw = self._ok_kwargs()
        kw.update(has_chain=False, near=None, far=None, atm=None)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_NONE)

    def test_thin_on_single_expiry(self):
        kw = self._ok_kwargs()
        kw.update(near=None)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_THIN)

    def test_thin_on_low_oi(self):
        kw = self._ok_kwargs()
        kw.update(atm_put_oi=10.0)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_THIN)

    def test_thin_on_zero_volume(self):
        kw = self._ok_kwargs()
        kw.update(atm_vol_total=0.0)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_THIN)

    def test_thin_on_wide_spread(self):
        kw = self._ok_kwargs()
        kw.update(spread_pct=0.25)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_THIN)


if __name__ == "__main__":
    unittest.main()

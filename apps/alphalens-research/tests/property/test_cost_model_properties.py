"""Property-based tests for the transaction-cost model.

``attribution/cost_model.py`` turns a gross return stream into a net-of-cost
one — the number every doctrine verdict is measured against. The arithmetic is
simple but load-bearing: a sign slip or a dropped ``turnover`` factor silently
inflates net alpha (the exact Layer-2b failure the ``RealisticCostModel`` was
written to prevent). These properties pin the algebra with independent recomputes
and metamorphic relations.

Heaviest properties:
  * ``CostModel.apply`` — the deducted amount is EXACTLY ``turnover × drag``
    (independent recompute); shifting gross by k shifts net by exactly k.
  * ``calibrate_k`` — noise-free recovery: feeding ``y = k·x`` back returns ``k``
    (inverts the least-squares fit; ``x`` built from an inline formula so a
    mutation to the production predictor breaks recovery).
  * ``secondary_market_impact_bps`` — sqrt scaling in trade size
    (``impact(4·size) == 2·impact(size)``), linearity in vol / k, monotonicity.
"""

from __future__ import annotations

import itertools
import math

import pandas as pd
from alphalens_research.attribution.cost_model import (
    _PROFILE_BPS,
    CostModel,
    RealisticCostModel,
    calibrate_k,
    cost_sensitivity_table,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from .base import PropertyTestCase

_RET = st.floats(-0.2, 0.2, allow_nan=False, allow_infinity=False, allow_subnormal=False)
_TURN = st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False, allow_subnormal=False)
_BPS = st.floats(0.0, 500.0, allow_nan=False, allow_infinity=False, allow_subnormal=False)
_PPY = st.sampled_from([12, 52, 252])
_POS = st.floats(1e-3, 1e6, allow_nan=False, allow_infinity=False, allow_subnormal=False)
_VOL = st.floats(0.05, 2.0, allow_nan=False, allow_infinity=False, allow_subnormal=False)
_SPREAD = st.floats(0.0, 100.0, allow_nan=False, allow_infinity=False, allow_subnormal=False)

_START = pd.Timestamp("2021-01-04")


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([_START + pd.Timedelta(days=i) for i in range(n)])


@st.composite
def _returns_and_turnover(draw: st.DrawFn) -> tuple[pd.Series, list[float], float, int]:
    n = draw(st.integers(0, 30))
    rets = draw(st.lists(_RET, min_size=n, max_size=n))
    turns = draw(st.lists(_TURN, min_size=n, max_size=n))
    bps = draw(_BPS)
    ppy = draw(_PPY)
    return pd.Series(rets, index=_idx(n), dtype=float, name="portfolio"), turns, bps, ppy


class TestPerPeriodDrag(PropertyTestCase):
    @settings(max_examples=200)
    @given(bps=_BPS, ppy=_PPY)
    def test_annualises_back_and_is_nonneg(self, bps: float, ppy: int) -> None:
        cm = CostModel(annual_drag_bps=bps)
        drag = cm.per_period_drag(periods_per_year=ppy)
        self.assertGreaterEqual(drag, 0.0)
        # A per-period drag times periods-per-year returns the annual fraction.
        self.assert_close(drag * ppy, bps / 10_000.0, rel_tol=1e-9, abs_tol=1e-15)

    @settings(max_examples=200)
    @given(bps=_BPS, ppy=_PPY, k=st.floats(1.0, 50.0, allow_nan=False, allow_subnormal=False))
    def test_linear_in_bps(self, bps: float, ppy: int, k: float) -> None:
        base = CostModel(annual_drag_bps=bps).per_period_drag(periods_per_year=ppy)
        scaled = CostModel(annual_drag_bps=bps * k).per_period_drag(periods_per_year=ppy)
        self.assert_close(scaled, base * k, rel_tol=1e-9, abs_tol=1e-15)

    @given(bps=_BPS)
    def test_nonpositive_ppy_floors_to_one(self, bps: float) -> None:
        # max(1, ppy) means ppy<=0 collapses to the annual fraction itself.
        cm = CostModel(annual_drag_bps=bps)
        self.assert_close(cm.per_period_drag(periods_per_year=0), bps / 10_000.0)
        self.assert_close(cm.per_period_drag(periods_per_year=-5), bps / 10_000.0)


class TestApply(PropertyTestCase):
    @settings(max_examples=250)
    @given(data=_returns_and_turnover())
    def test_deducted_amount_is_exactly_turnover_times_drag(
        self, data: tuple[pd.Series, list[float], float, int]
    ) -> None:
        gross, turns, bps, ppy = data
        cm = CostModel(annual_drag_bps=bps)
        net = cm.apply(gross, daily_turnover=turns, periods_per_year=ppy)
        drag = cm.per_period_drag(periods_per_year=ppy)
        for i in range(len(gross)):
            self.assert_close(
                gross.iloc[i] - net.iloc[i], turns[i] * drag, rel_tol=1e-9, abs_tol=1e-15
            )

    @settings(max_examples=200)
    @given(data=_returns_and_turnover())
    def test_none_turnover_equals_all_ones(
        self, data: tuple[pd.Series, list[float], float, int]
    ) -> None:
        gross, _turns, bps, ppy = data
        cm = CostModel(annual_drag_bps=bps)
        default = cm.apply(gross, daily_turnover=None, periods_per_year=ppy)
        ones = cm.apply(gross, daily_turnover=[1.0] * len(gross), periods_per_year=ppy)
        for i in range(len(gross)):
            self.assert_close(default.iloc[i], ones.iloc[i], rel_tol=1e-12, abs_tol=1e-15)

    @settings(max_examples=200)
    @given(data=_returns_and_turnover())
    def test_zero_drag_or_zero_turnover_is_identity(
        self, data: tuple[pd.Series, list[float], float, int]
    ) -> None:
        gross, _turns, _bps, ppy = data
        # gross profile (0 bps): net == gross.
        net0 = CostModel(annual_drag_bps=0.0).apply(
            gross, daily_turnover=None, periods_per_year=ppy
        )
        # zero turnover with any drag: net == gross.
        netz = CostModel(annual_drag_bps=250.0).apply(
            gross, daily_turnover=[0.0] * len(gross), periods_per_year=ppy
        )
        for i in range(len(gross)):
            self.assertEqual(net0.iloc[i], gross.iloc[i])
            self.assertEqual(netz.iloc[i], gross.iloc[i])

    @settings(max_examples=200)
    @given(data=_returns_and_turnover(), c=_RET)
    def test_shifting_gross_shifts_net_by_the_same_amount(
        self, data: tuple[pd.Series, list[float], float, int], c: float
    ) -> None:
        """Cost is independent of the gross level -> net is affine in gross."""
        gross, turns, bps, ppy = data
        cm = CostModel(annual_drag_bps=bps)
        net = cm.apply(gross, daily_turnover=turns, periods_per_year=ppy)
        net_shift = cm.apply(gross + c, daily_turnover=turns, periods_per_year=ppy)
        for i in range(len(gross)):
            self.assert_close(net_shift.iloc[i] - net.iloc[i], c, rel_tol=1e-9, abs_tol=1e-12)

    @settings(max_examples=200)
    @given(data=_returns_and_turnover(), extra=_BPS)
    def test_more_drag_never_increases_net(
        self, data: tuple[pd.Series, list[float], float, int], extra: float
    ) -> None:
        gross, turns, bps, ppy = data
        lo = CostModel(annual_drag_bps=bps).apply(gross, daily_turnover=turns, periods_per_year=ppy)
        hi = CostModel(annual_drag_bps=bps + extra).apply(
            gross, daily_turnover=turns, periods_per_year=ppy
        )
        for i in range(len(gross)):
            self.assertLessEqual(hi.iloc[i], lo.iloc[i] + 1e-12)

    @settings(max_examples=150)
    @given(data=_returns_and_turnover())
    def test_index_is_preserved(self, data: tuple[pd.Series, list[float], float, int]) -> None:
        gross, turns, bps, ppy = data
        net = CostModel(annual_drag_bps=bps).apply(
            gross, daily_turnover=turns, periods_per_year=ppy
        )
        self.assertTrue(net.index.equals(gross.index))

    @given(bps=_BPS)
    def test_turnover_length_mismatch_raises(self, bps: float) -> None:
        gross = pd.Series([0.01, 0.02, -0.01], index=_idx(3), dtype=float)
        with self.assertRaises(ValueError):
            CostModel(annual_drag_bps=bps).apply(gross, daily_turnover=[0.5, 0.5])


class TestApplyScalarToSharpe(PropertyTestCase):
    @settings(max_examples=200)
    @given(
        gross_sharpe=st.floats(-3.0, 3.0, allow_nan=False, allow_subnormal=False),
        vol=st.floats(-1.0, 0.5, allow_nan=False, allow_subnormal=False),
        bps=_BPS,
        ppy=_PPY,
    )
    def test_drag_never_raises_sharpe_and_zero_vol_passthrough(
        self, gross_sharpe: float, vol: float, bps: float, ppy: int
    ) -> None:
        cm = CostModel(annual_drag_bps=bps)
        out = cm.apply_scalar_to_sharpe(gross_sharpe, volatility_daily=vol, periods_per_year=ppy)
        if vol <= 0:
            self.assertEqual(out, gross_sharpe)  # non-positive vol -> untouched
        else:
            self.assertLessEqual(out, gross_sharpe + 1e-12)  # drag >= 0 lowers Sharpe

    @settings(max_examples=200)
    @given(
        gross_sharpe=st.floats(-3.0, 3.0, allow_nan=False, allow_subnormal=False),
        vol=st.floats(1e-3, 0.5, allow_nan=False, allow_subnormal=False),
        bps=_BPS,
        extra=_BPS,
        ppy=_PPY,
    )
    def test_monotone_decreasing_in_drag(
        self, gross_sharpe: float, vol: float, bps: float, extra: float, ppy: int
    ) -> None:
        lo = CostModel(annual_drag_bps=bps).apply_scalar_to_sharpe(gross_sharpe, vol, ppy)
        hi = CostModel(annual_drag_bps=bps + extra).apply_scalar_to_sharpe(gross_sharpe, vol, ppy)
        self.assertLessEqual(hi, lo + 1e-12)


class TestRealisticPrimary(PropertyTestCase):
    @settings(max_examples=200)
    @given(
        half=_SPREAD,
        adverse=st.floats(0.0, 20.0, allow_nan=False, allow_subnormal=False),
        turn=_TURN,
    )
    def test_round_trip_and_period_drag_decomposition(
        self, half: float, adverse: float, turn: float
    ) -> None:
        m = RealisticCostModel(adverse_selection_bps=adverse)
        one_way = m.primary_one_way_bps(half)
        self.assert_close(one_way, half + adverse, rel_tol=1e-12, abs_tol=1e-15)
        self.assert_close(
            m.primary_round_trip_bps(half), 2.0 * one_way, rel_tol=1e-12, abs_tol=1e-15
        )
        self.assert_close(
            m.primary_period_drag_bps(half, turn), 2.0 * one_way * turn, rel_tol=1e-9, abs_tol=1e-15
        )

    @settings(max_examples=150)
    @given(half=_SPREAD, turn=_TURN)
    def test_period_drag_linear_in_turnover(self, half: float, turn: float) -> None:
        # period_drag scales linearly with turnover: halving turnover halves it.
        m = RealisticCostModel()
        base = m.primary_period_drag_bps(half, turn)
        self.assert_close(
            m.primary_period_drag_bps(half, turn * 0.5), base * 0.5, rel_tol=1e-9, abs_tol=1e-15
        )


class TestRealisticSecondary(PropertyTestCase):
    @settings(max_examples=200)
    @given(
        size=_POS,
        adv=_POS,
        vol=_VOL,
        horizon=st.floats(1.0, 60.0, allow_nan=False, allow_subnormal=False),
        k=st.floats(0.0, 1.0, allow_nan=False, allow_subnormal=False),
    )
    def test_nonneg_and_sqrt_scaling_in_size(
        self, size: float, adv: float, vol: float, horizon: float, k: float
    ) -> None:
        m = RealisticCostModel(k=k)
        base = m.secondary_market_impact_bps(
            trade_size=size, adv=adv, annual_vol=vol, horizon_days=horizon
        )
        self.assertGreaterEqual(base, 0.0)
        quad = m.secondary_market_impact_bps(
            trade_size=size * 4.0, adv=adv, annual_vol=vol, horizon_days=horizon
        )
        # impact ~ sqrt(size) -> quadrupling size doubles impact.
        self.assert_close(quad, base * 2.0, rel_tol=1e-9, abs_tol=1e-12)

    @given(adv=_POS, vol=_VOL)
    def test_zero_size_or_nonpositive_adv_is_zero(self, adv: float, vol: float) -> None:
        m = RealisticCostModel()
        self.assertEqual(
            m.secondary_market_impact_bps(
                trade_size=0.0, adv=adv, annual_vol=vol, horizon_days=21.0
            ),
            0.0,
        )
        self.assertEqual(
            m.secondary_market_impact_bps(
                trade_size=100.0, adv=0.0, annual_vol=vol, horizon_days=21.0
            ),
            0.0,
        )

    @settings(max_examples=200)
    @given(
        size=_POS,
        adv=_POS,
        vol=_VOL,
        horizon=st.floats(1.0, 60.0, allow_nan=False, allow_subnormal=False),
        c=st.floats(1.0, 20.0, allow_nan=False, allow_subnormal=False),
    )
    def test_linear_in_k_and_vol(
        self, size: float, adv: float, vol: float, horizon: float, c: float
    ) -> None:
        base = RealisticCostModel(k=0.05).secondary_market_impact_bps(
            trade_size=size, adv=adv, annual_vol=vol, horizon_days=horizon
        )
        kx = RealisticCostModel(k=0.05 * c).secondary_market_impact_bps(
            trade_size=size, adv=adv, annual_vol=vol, horizon_days=horizon
        )
        vx = RealisticCostModel(k=0.05).secondary_market_impact_bps(
            trade_size=size, adv=adv, annual_vol=vol * c, horizon_days=horizon
        )
        self.assert_close(kx, base * c, rel_tol=1e-9, abs_tol=1e-12)
        self.assert_close(vx, base * c, rel_tol=1e-9, abs_tol=1e-12)

    @settings(max_examples=200)
    @given(
        half=_SPREAD,
        adverse=st.floats(0.0, 20.0, allow_nan=False, allow_subnormal=False),
        size=_POS,
        adv=_POS,
        vol=_VOL,
        horizon=st.floats(1.0, 60.0, allow_nan=False, allow_subnormal=False),
    )
    def test_one_way_is_additive_decomposition(
        self, half: float, adverse: float, size: float, adv: float, vol: float, horizon: float
    ) -> None:
        m = RealisticCostModel(adverse_selection_bps=adverse)
        impact = m.secondary_market_impact_bps(
            trade_size=size, adv=adv, annual_vol=vol, horizon_days=horizon
        )
        one_way = m.secondary_one_way_bps(
            half, trade_size=size, adv=adv, annual_vol=vol, horizon_days=horizon
        )
        self.assert_close(one_way, half + adverse + impact, rel_tol=1e-9, abs_tol=1e-12)


@st.composite
def _calib_case(draw: st.DrawFn) -> tuple[list[float], list[float], list[float], float, float]:
    n = draw(st.integers(1, 8))
    sizes = draw(st.lists(_POS, min_size=n, max_size=n))
    advs = draw(st.lists(_POS, min_size=n, max_size=n))
    vols = draw(st.lists(_VOL, min_size=n, max_size=n))
    horizon = draw(st.floats(1.0, 60.0, allow_nan=False, allow_subnormal=False))
    k_true = draw(st.floats(0.01, 2.0, allow_nan=False, allow_subnormal=False))
    return sizes, advs, vols, horizon, k_true


class TestCalibrateK(PropertyTestCase):
    @settings(max_examples=200)
    @given(case=_calib_case())
    def test_noise_free_recovery(
        self, case: tuple[list[float], list[float], list[float], float, float]
    ) -> None:
        sizes, advs, vols, horizon, k_true = case
        # Independent inline predictor (NOT the production impact fn) so a mutation
        # to the production predictor breaks recovery instead of cancelling.
        xs = [
            math.sqrt(s / a) * v * math.sqrt(horizon / 252.0) * 10_000.0
            for s, a, v in zip(sizes, advs, vols, strict=True)
        ]
        ys = [k_true * x for x in xs]
        k_hat = calibrate_k(
            ys, trade_sizes=sizes, advs=advs, annual_vols=vols, horizon_days=horizon
        )
        self.assert_close(k_hat, k_true, rel_tol=1e-9, abs_tol=1e-12)

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            calibrate_k([], trade_sizes=[], advs=[], annual_vols=[])

    def test_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            calibrate_k([1.0, 2.0], trade_sizes=[1.0], advs=[1.0], annual_vols=[1.0])

    def test_all_rows_dropped_raises(self) -> None:
        # Every row has adv<=0 or size<=0 -> no usable rows.
        with self.assertRaises(ValueError):
            calibrate_k(
                [10.0, 20.0], trade_sizes=[0.0, 5.0], advs=[100.0, 0.0], annual_vols=[1.0, 1.0]
            )


class TestRealisticApplyAnnualDrag(PropertyTestCase):
    @settings(max_examples=200)
    @given(rets=st.lists(_RET, min_size=0, max_size=30), bps=_BPS, ppy=_PPY)
    def test_subtracts_uniform_daily_drag_and_matches_costmodel(
        self, rets: list[float], bps: float, ppy: int
    ) -> None:
        gross = pd.Series(rets, index=_idx(len(rets)), dtype=float, name="portfolio")
        net = RealisticCostModel().apply_annual_drag_bps(
            gross, annual_drag_bps=bps, periods_per_year=ppy
        )
        daily = (bps / 10_000.0) / max(1, ppy)  # independent recompute, not per_period_drag
        # Deducted amount is exactly the uniform daily drag on every bar.
        for i in range(len(gross)):
            self.assert_close(gross.iloc[i] - net.iloc[i], daily, rel_tol=1e-9, abs_tol=1e-15)
        # ... which is the same net as CostModel.apply with turnover=None.
        cm_net = CostModel(annual_drag_bps=bps).apply(
            gross, daily_turnover=None, periods_per_year=ppy
        )
        for i in range(len(gross)):
            self.assert_close(net.iloc[i], cm_net.iloc[i], rel_tol=1e-9, abs_tol=1e-15)


class TestSensitivityTable(PropertyTestCase):
    # deadline=None: cost_sensitivity_table lazily imports backtest.metrics.sharpe
    # on first call (~350ms one-off), which trips the frozen 200ms default before
    # the ci profile's deadline=None loads in setUpClass.
    @settings(max_examples=120, deadline=None)
    @given(
        rets=st.lists(_RET, min_size=3, max_size=30),
        ppy=_PPY,
    )
    def test_sorted_by_drag_and_return_monotone(self, rets: list[float], ppy: int) -> None:
        table = cost_sensitivity_table(rets, daily_turnover=None, periods_per_year=ppy)
        self.assertEqual(len(table), len(_PROFILE_BPS))
        drags = table["drag_bps"].tolist()
        self.assertEqual(drags, sorted(drags))  # ascending by construction
        self.assertEqual(drags[0], 0.0)  # gross first
        # Uniform-turnover drag is a constant subtraction per bar, so a higher
        # drag can only lower (never raise) the compounded annual return.
        returns = table["annual_return"].tolist()
        for a, b in itertools.pairwise(returns):  # higher drag -> non-increasing return
            self.assertGreaterEqual(a + 1e-12, b)

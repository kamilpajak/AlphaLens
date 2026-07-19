"""Property-based tests for the volatility-targeting overlay's causality.

``overlays/vol_target.py`` scales exposure by ``target_vol / realized_vol``,
where the realised-vol estimate must use ONLY past returns:
``scale[t]`` is a function of ``returns[< t]`` (enforced by
``rolling(L).std().shift(1)``). A dropped or mis-aligned ``.shift(1)`` is a
look-ahead bug — the overlay would size today off today's own return — so the
causality contract is the flagship invariant here.

Two heavy properties:
  * **Causality (metamorphic)** — changing ``returns`` at positions ``>= k``
    must not change any ``scale`` at positions ``<= k``. A ``.shift(0)`` /
    ``.shift(-1)`` mutant makes ``scale[k]`` depend on ``returns[k]`` (or the
    future) and is caught immediately.
  * **Differential oracle** — the vectorised ``scale_series`` must equal the
    single-point ``scale_factor`` at every timestamp. The two code paths derive
    the same window a DIFFERENT way (strict ``index < asof`` slice vs
    ``rolling().shift(1)``), so a shift/threshold mutation in either diverges.

Plus: bounds + finiteness, warmup neutrality, the constant-returns (zero-vol)
and NaN-in-window fallbacks, and the ``apply_vol_target`` composition identity.
"""

from __future__ import annotations

import math

import pandas as pd
from alphalens_research.overlays.vol_target import VolTargeter, apply_vol_target
from hypothesis import given, settings
from hypothesis import strategies as st

from .base import PropertyTestCase

_START = pd.Timestamp("2021-01-04")  # a fixed Monday; index identity only
_RETURN = st.floats(-0.2, 0.2, allow_nan=False, allow_infinity=False, allow_subnormal=False)


def _index(n: int) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([_START + pd.Timedelta(days=i) for i in range(n)])


def _series(vals: list[float]) -> pd.Series:
    return pd.Series(vals, index=_index(len(vals)), dtype=float, name="portfolio")


def _independent_annualised_std(vals: list[float], i: int, tg: VolTargeter) -> tuple[float, str]:
    """Pure-Python realised-vol oracle for ``scale[i]`` — no pandas.

    Returns ``(value, kind)``: ``kind`` is ``"warmup"`` / ``"nan"`` (both ->
    neutral 1.0 in production), or ``"std"`` with the annualised sample std of
    the strict-history window ``vals[i-L:i]`` (the returns BEFORE position i).
    Computed with a textbook two-pass formula so it shares no code with
    production's ``rolling().std()`` / ``.iloc[-L:].std()`` — a genuine oracle.
    """
    L = tg._estimator.lookback
    ppy = tg._estimator.periods_per_year
    if i < L:
        return 1.0, "warmup"
    window = vals[i - L : i]
    if any(math.isnan(x) for x in window):
        return 1.0, "nan"
    mean = sum(window) / L
    var = sum((x - mean) ** 2 for x in window) / (L - 1)
    return math.sqrt(var) * math.sqrt(ppy), "std"


@st.composite
def _targeter(draw: st.DrawFn) -> VolTargeter:
    return VolTargeter(
        target_vol=draw(st.floats(0.05, 0.5, allow_nan=False, allow_subnormal=False)),
        lookback=draw(st.integers(2, 8)),
        periods_per_year=draw(st.sampled_from([52, 252])),
        max_leverage=draw(st.floats(0.5, 3.0, allow_nan=False, allow_subnormal=False)),
    )


@st.composite
def _returns_and_targeter(draw: st.DrawFn) -> tuple[pd.Series, VolTargeter]:
    tg = draw(_targeter())
    n = draw(st.integers(0, 40))
    vals = draw(st.lists(_RETURN, min_size=n, max_size=n))
    return _series(vals), tg


@st.composite
def _causality_case(draw: st.DrawFn) -> tuple[pd.Series, pd.Series, int, VolTargeter]:
    """A series ``r`` and a twin ``r2`` identical on ``[0, k)`` but replaced from ``k`` on."""
    tg = draw(_targeter())
    n = draw(st.integers(1, 40))
    base = draw(st.lists(_RETURN, min_size=n, max_size=n))
    k = draw(st.integers(0, n))
    tail = draw(st.lists(_RETURN, min_size=n - k, max_size=n - k))
    idx = _index(n)
    r = pd.Series(base, index=idx, dtype=float, name="portfolio")
    r2 = pd.Series(base[:k] + tail, index=idx, dtype=float, name="portfolio")
    return r, r2, k, tg


class TestCausality(PropertyTestCase):
    @settings(max_examples=250)
    @given(case=_causality_case())
    def test_future_returns_do_not_affect_past_scales(
        self, case: tuple[pd.Series, pd.Series, int, VolTargeter]
    ) -> None:
        r, r2, k, tg = case
        n = len(r)
        sa = tg.scale_series(r)
        sb = tg.scale_series(r2)
        # scale[i] uses returns[< i]; r and r2 agree on positions [0, k), so
        # every scale at position i <= k is identical. rolling() is causal, so
        # the changed tail cannot leak backward — assert exact equality.
        upto = min(k, n - 1) + 1
        for i in range(upto):
            self.assert_close(sa.iloc[i], sb.iloc[i], rel_tol=1e-12, abs_tol=1e-12)


class TestScaleSeriesVsScaleFactor(PropertyTestCase):
    @settings(max_examples=250)
    @given(data=_returns_and_targeter())
    def test_both_paths_match_independent_oracle(self, data: tuple[pd.Series, VolTargeter]) -> None:
        """Both production paths must equal an independent pure-Python oracle.

        ``scale_series`` (rolling+shift) and ``scale_factor`` (``index < asof``
        slice) derive the window two different ways; a shift or threshold mutant
        makes one use the wrong returns, diverging from the oracle's correct
        window ``vals[i-L:i]``. Warmup / NaN windows are pinned to exactly 1.0.
        The exact numeric scale is asserted only when the oracle std is
        comfortably non-degenerate (> 1e-6): within ~1e6x of the zero-vol
        tolerance, rolling-std and slice-std can straddle the threshold so one
        path returns 1.0 and the other clips to the cap — a benign FP artifact
        (delta ~1.0), NOT a causality error, so it must not be asserted on.
        """
        s, tg = data
        vals = list(s)
        scales = tg.scale_series(s)
        for i in range(len(s)):
            ss = scales.iloc[i]
            sf = tg.scale_factor(s, s.index[i])
            std, kind = _independent_annualised_std(vals, i, tg)
            if kind in ("warmup", "nan"):
                self.assertEqual(ss, 1.0)
                self.assertEqual(sf, 1.0)
                continue
            if std <= 1e-6:  # zero-vol knife-edge: FP-dominated, skip numeric assert
                continue
            expected = min(tg.target_vol / std, tg.max_leverage)
            self.assert_close(ss, expected, rel_tol=1e-6, abs_tol=1e-9)
            self.assert_close(sf, expected, rel_tol=1e-6, abs_tol=1e-9)


class TestBoundsAndNeutrality(PropertyTestCase):
    @settings(max_examples=200)
    @given(data=_returns_and_targeter())
    def test_scales_are_finite_positive_and_capped(
        self, data: tuple[pd.Series, VolTargeter]
    ) -> None:
        s, tg = data
        scales = tg.scale_series(s)
        cap = max(tg.max_leverage, 1.0)  # warmup/fallback = 1.0 may exceed a <1 cap
        for v in scales:
            self.assertTrue(math.isfinite(v))
            self.assertGreater(v, 0.0)
            self.assertLessEqual(v, cap + 1e-9)

    @settings(max_examples=200)
    @given(data=_returns_and_targeter())
    def test_warmup_positions_are_neutral(self, data: tuple[pd.Series, VolTargeter]) -> None:
        s, tg = data
        scales = tg.scale_series(s)
        # scale[i] needs `lookback` prior returns, so the first `lookback`
        # positions (0..L-1) are always the neutral 1.0.
        warm = scales.iloc[: tg._estimator.lookback]
        for v in warm:
            self.assertEqual(v, 1.0)

    @settings(max_examples=150)
    @given(c=_RETURN, n=st.integers(0, 40), tg=_targeter())
    def test_constant_returns_are_neutral(self, c: float, n: int, tg: VolTargeter) -> None:
        # Zero realised vol (a constant return path) is the degenerate state:
        # every scale must fall back to 1.0, never amplify toward max_leverage.
        scales = tg.scale_series(_series([c] * n))
        for v in scales:
            self.assertEqual(v, 1.0)

    def test_nan_in_window_falls_back_to_one(self) -> None:
        # A NaN anywhere in the lookback window taints the estimate -> 1.0.
        tg = VolTargeter(target_vol=0.1, lookback=3, periods_per_year=252, max_leverage=2.0)
        vals = [0.01, -0.02, 0.03, float("nan"), 0.02, -0.01, 0.015, 0.02, -0.03, 0.01]
        scales = tg.scale_series(_series(vals))
        p = 3  # NaN position; windows ending at p..p+look. scale[i] uses r[i-L..i-1]
        # scale at positions p+1..p+lookback include the NaN -> must be 1.0.
        for i in range(p + 1, p + 1 + tg._estimator.lookback):
            self.assertEqual(scales.iloc[i], 1.0)


class TestApplyComposition(PropertyTestCase):
    @settings(max_examples=200)
    @given(data=_returns_and_targeter())
    def test_apply_is_scale_times_return(self, data: tuple[pd.Series, VolTargeter]) -> None:
        s, tg = data
        scaled = apply_vol_target(s, tg)
        scales = tg.scale_series(s)
        self.assertEqual(len(scaled), len(s))
        for i in range(len(s)):
            self.assert_close(
                scaled.iloc[i], scales.iloc[i] * s.iloc[i], rel_tol=1e-9, abs_tol=1e-12
            )

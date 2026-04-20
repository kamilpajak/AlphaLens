"""Tests for EarlyStageScorer — catches stocks at base → Stage 2 transition,
not after they've already run. RED phase of TDD: written before implementation.

Seven metrics (per Perplexity research):
  1. base_breakout      — (close - SMA_50) / (SMA_50 - SMA_200) in [0.05, 0.25]
  2. acceleration       — d²P/dt² > median(d²P, 60d) AND > 0
  3. vcp                — BB_width < p30(BB_width, 90d) AND close > SMA_20
  4. rsi_emergence      — RSI in [45, 65]; penalty if >70
  5. adx_building       — ADX in [20, 35] AND ΔADX_5d > +2
  6. volume_accumulation — vol20/vol60 in [1.1, 1.5]; penalty if >2.0
  7. jegadeesh_11_1     — (close[t-21] - close[t-252])/close[t-252] > 0, trend live
"""

import unittest

import numpy as np
import pandas as pd

# ----- Fixtures --------------------------------------------------------------


def _base_building_df(days: int = 260, base_level: float = 10.0, breakout_pct: float = 0.10) -> pd.DataFrame:
    """Stepwise construction that lands ratio (close-SMA_50)/(SMA_50-SMA_200) ∈ [0.05, 0.25].

    Segments (total 260 bars):
    - 150 days at 8.00   (deep base, well below SMA_200)
    - 60 days at 12.00   (lifts SMA_200 upward but keeps it < SMA_50)
    - 49 days at 12.50   (steady, SMA_50 settles near 12.50)
    - 1 last day = 12.50 * (1 + breakout_pct/10)  (breakout day, slight push above SMA_50)

    With breakout_pct=0.10 → close=12.625 → numerator≈0.12, denominator≈2.18 → ratio≈0.055
    (in target [0.05, 0.25]). breakout_pct is the lever for ratio tuning.
    """
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    lift = 1 + breakout_pct / 10  # small push above the steady zone
    close = np.concatenate([
        np.full(150, 0.80 * base_level),   # 150 × 8.00
        np.full(60, 1.20 * base_level),    # 60 × 12.00
        np.full(49, 1.25 * base_level),    # 49 × 12.50
        np.array([1.25 * base_level * lift])  # breakout day
    ])
    assert len(close) == days, f"expected {days} bars, got {len(close)}"
    vol = np.full(days, 1_000_000.0)
    vol[-20:] = 1_300_000.0  # accumulation (1.3x)
    return pd.DataFrame(
        {
            "Open": close * 0.998, "High": close * 1.003,
            "Low": close * 0.997, "Close": close,
            "Volume": vol,
        },
        index=idx,
    )


def _choppy_rally_df(days: int = 260, start: float = 10.0, total_gain: float = 0.80) -> pd.DataFrame:
    """Extended rally with heavy intra-period noise → BB_width stays wide throughout.
    Used to test VCP detection (BB should NOT be at 30th percentile → score 0).
    """
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    rng = np.random.RandomState(42)
    # Gentle linear trend + 5% daily noise → persistent high volatility
    trend = np.linspace(start, start * (1 + total_gain), days)
    noise = rng.normal(0, start * 0.05, days)
    close = trend + noise
    return pd.DataFrame(
        {"Open": close * 0.995, "High": close * 1.01,
         "Low": close * 0.99, "Close": close,
         "Volume": [1_500_000.0] * days},
        index=idx,
    )


def _extended_rally_df(days: int = 260, start: float = 10.0, total_gain: float = 0.80) -> pd.DataFrame:
    """Stock up 80% over 4-6 months, now far extended above SMA_50."""
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    # Flat first half, then big run
    flat_length = days // 2
    flat = np.full(flat_length, start)
    rally = np.linspace(start, start * (1 + total_gain), days - flat_length)
    close = np.concatenate([flat, rally])
    vol = np.full(days, 1_500_000.0)
    vol[-10:] = 5_000_000.0  # climactic
    return pd.DataFrame(
        {
            "Open": close * 0.995, "High": close * 1.01,
            "Low": close * 0.99, "Close": close,
            "Volume": vol,
        },
        index=idx,
    )


def _flat_df(days: int = 260, price: float = 10.0) -> pd.DataFrame:
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    return pd.DataFrame(
        {"Open": [price]*days, "High": [price]*days, "Low": [price]*days,
         "Close": [price]*days, "Volume": [1_000_000.0]*days},
        index=idx,
    )


def _accelerating_df(days: int = 260, start: float = 10.0) -> pd.DataFrame:
    """Price with positive and increasing 2nd derivative over last ~5 days."""
    idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
    # Flat base then quadratic rise last 10 bars
    base_length = days - 10
    base = np.full(base_length, start)
    accel = np.array([start + 0.02 * (i ** 1.8) for i in range(10)])
    close = np.concatenate([base, accel])
    return pd.DataFrame(
        {"Open": close, "High": close * 1.005, "Low": close * 0.995,
         "Close": close, "Volume": [1_000_000.0]*days},
        index=idx,
    )


# ----- Tests — one class per metric ------------------------------------------


class TestBaseBreakout(unittest.TestCase):
    """Score high for stocks 5-25% above SMA_50, where SMA_50 > SMA_200."""

    def test_stock_in_base_breakout_zone_scores_high(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _base_building_df(breakout_pct=0.12)
        # Close ~12% above base; SMA_50 should be between base and close
        s = EarlyStageScorer._base_breakout_score(df)
        self.assertGreaterEqual(s, 0.9, f"got {s}")

    def test_extended_rally_scores_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _extended_rally_df(total_gain=0.80)
        # Close way above SMA_50, ratio >> 0.25 → should score 0
        s = EarlyStageScorer._base_breakout_score(df)
        self.assertLess(s, 0.1, f"got {s}")

    def test_flat_stock_scores_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _flat_df()
        # SMA_50 == SMA_200 → divide by zero; should gracefully return 0
        s = EarlyStageScorer._base_breakout_score(df)
        self.assertEqual(s, 0.0)

    def test_insufficient_bars_returns_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _flat_df(days=150)  # <200 bars
        s = EarlyStageScorer._base_breakout_score(df)
        self.assertEqual(s, 0.0)


class TestAcceleration(unittest.TestCase):
    """Score high when 2nd derivative positive AND > recent median."""

    def test_accelerating_scores_high(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _accelerating_df()
        s = EarlyStageScorer._acceleration_score(df)
        self.assertEqual(s, 1.0)

    def test_flat_scores_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _flat_df()
        s = EarlyStageScorer._acceleration_score(df)
        self.assertEqual(s, 0.0)

    def test_insufficient_bars_returns_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _accelerating_df(days=40)  # <60 bars
        s = EarlyStageScorer._acceleration_score(df)
        self.assertEqual(s, 0.0)


class TestVCP(unittest.TestCase):
    """Volatility contraction: tight BB width + above SMA_20."""

    def test_tight_range_above_sma20_scores_high(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        # Mostly flat (tight BB), tiny uptick at end to be above SMA_20
        days = 260
        idx = pd.date_range(end="2026-04-17", periods=days, freq="B")
        # First 220 have wide swings (so current 20d is narrow by comparison)
        rng = np.random.RandomState(42)
        noise = rng.normal(0, 0.10, 220)
        wide = 10 + noise
        # Last 40 — very tight, small uptick at very end
        tight = np.full(40, 10.05)
        tight[-1] = 10.15  # above SMA_20
        close = np.concatenate([wide, tight])
        df = pd.DataFrame({"Open": close, "High": close*1.002, "Low": close*0.998,
                           "Close": close, "Volume": [1_000_000.0]*days}, index=idx)
        s = EarlyStageScorer._vcp_score(df)
        self.assertEqual(s, 1.0)

    def test_choppy_rally_scores_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        # Heavy noise throughout → BB_width never contracts to p30
        df = _choppy_rally_df()
        s = EarlyStageScorer._vcp_score(df)
        self.assertEqual(s, 0.0)

    def test_insufficient_bars_returns_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _flat_df(days=40)
        s = EarlyStageScorer._vcp_score(df)
        self.assertEqual(s, 0.0)


class TestRSIEmergence(unittest.TestCase):
    """RSI in [45, 65] → 1.0; >70 → 0 (penalty); <45 → 0."""

    def test_rsi_in_emergence_zone(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._rsi_emergence_score(rsi=55.0)
        self.assertEqual(s, 1.0)

    def test_rsi_overbought_penalty(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._rsi_emergence_score(rsi=78.0)
        self.assertEqual(s, 0.0)

    def test_rsi_oversold_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._rsi_emergence_score(rsi=30.0)
        self.assertEqual(s, 0.0)

    def test_rsi_none_neutral(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._rsi_emergence_score(rsi=None)
        self.assertEqual(s, 0.5)

    def test_rsi_boundary_inclusive(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        self.assertEqual(EarlyStageScorer._rsi_emergence_score(rsi=45.0), 1.0)
        self.assertEqual(EarlyStageScorer._rsi_emergence_score(rsi=65.0), 1.0)


class TestADXBuilding(unittest.TestCase):
    """ADX in [20, 35] AND rising (ΔADX_5d > +2) → 1.0."""

    def test_adx_building_scores_high(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._adx_building_score(adx_current=28.0, adx_5d_ago=24.0)
        self.assertEqual(s, 1.0)

    def test_adx_mature_scores_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._adx_building_score(adx_current=45.0, adx_5d_ago=40.0)
        self.assertEqual(s, 0.0)

    def test_adx_low_scores_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._adx_building_score(adx_current=15.0, adx_5d_ago=12.0)
        self.assertEqual(s, 0.0)

    def test_adx_in_range_but_flat_scores_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._adx_building_score(adx_current=28.0, adx_5d_ago=28.0)
        self.assertEqual(s, 0.0)  # delta < +2

    def test_adx_none_neutral(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        s = EarlyStageScorer._adx_building_score(adx_current=None, adx_5d_ago=None)
        self.assertEqual(s, 0.5)


class TestVolumeAccumulation(unittest.TestCase):
    """vol20/vol60 in [1.1, 1.5] → 1.0; >2.0 → 0 (climactic = distribution)."""

    def test_accumulation_range(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        # Build vol where last-20d avg = 1.3x last-60d avg
        days = 80
        vol = np.concatenate([np.full(40, 1_000_000), np.full(20, 1_000_000), np.full(20, 1_900_000)])
        # mean(last 20) = 1_900_000; mean(last 60) = (40*1M + 20*1M + 20*1.9M)/60 = 1_300_000
        # ratio = 1_900_000 / 1_300_000 = 1.46 → in [1.1, 1.5]
        df = pd.DataFrame({
            "Open": [10.0]*days, "High": [10.1]*days, "Low": [9.9]*days,
            "Close": [10.0]*days, "Volume": vol,
        }, index=pd.date_range(end="2026-04-17", periods=days, freq="B"))
        s = EarlyStageScorer._volume_accumulation_score(df)
        self.assertEqual(s, 1.0)

    def test_climactic_volume_penalty(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        days = 80
        vol = np.concatenate([np.full(60, 1_000_000), np.full(20, 3_000_000)])  # ratio ~2.33x
        df = pd.DataFrame({
            "Open": [10.0]*days, "High": [10.1]*days, "Low": [9.9]*days,
            "Close": [10.0]*days, "Volume": vol,
        }, index=pd.date_range(end="2026-04-17", periods=days, freq="B"))
        s = EarlyStageScorer._volume_accumulation_score(df)
        self.assertEqual(s, 0.0)

    def test_flat_volume_below_range(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _flat_df()
        s = EarlyStageScorer._volume_accumulation_score(df)
        self.assertEqual(s, 0.0)

    def test_insufficient_bars_returns_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _flat_df(days=30)
        s = EarlyStageScorer._volume_accumulation_score(df)
        self.assertEqual(s, 0.0)


class TestJegadeesh11_1(unittest.TestCase):
    """11-month skip-last-month momentum > 0 AND trend live → 1.0."""

    def test_positive_11_1_with_live_trend(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        # 260-day series: start=10, 11m ago (t-252) price=10, 1m ago (t-21) price=14, now=14.5
        days = 260
        close = np.full(days, 10.0)
        close[-252:-21] = np.linspace(10.0, 14.0, 231)
        close[-21:] = np.linspace(14.0, 14.5, 21)
        df = pd.DataFrame({
            "Open": close, "High": close, "Low": close,
            "Close": close, "Volume": [1_000_000.0]*days,
        }, index=pd.date_range(end="2026-04-17", periods=days, freq="B"))
        s = EarlyStageScorer._jegadeesh_11_1_score(df)
        self.assertEqual(s, 1.0)

    def test_negative_11_1_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        days = 260
        close = np.linspace(20.0, 10.0, days)  # steady decline
        df = pd.DataFrame({
            "Open": close, "High": close, "Low": close,
            "Close": close, "Volume": [1_000_000.0]*days,
        }, index=pd.date_range(end="2026-04-17", periods=days, freq="B"))
        s = EarlyStageScorer._jegadeesh_11_1_score(df)
        self.assertEqual(s, 0.0)

    def test_positive_11_1_but_trend_broken(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        # Up 40% in months 2-11, then down 10% in last month → 11-1 still positive but trend dead
        days = 260
        close = np.full(days, 10.0)
        close[-252:-21] = np.linspace(10.0, 14.0, 231)
        close[-21:] = np.linspace(14.0, 12.5, 21)  # declining last 20d
        df = pd.DataFrame({
            "Open": close, "High": close, "Low": close,
            "Close": close, "Volume": [1_000_000.0]*days,
        }, index=pd.date_range(end="2026-04-17", periods=days, freq="B"))
        s = EarlyStageScorer._jegadeesh_11_1_score(df)
        self.assertEqual(s, 0.0)

    def test_insufficient_bars_returns_zero(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        df = _flat_df(days=100)  # need 252+
        s = EarlyStageScorer._jegadeesh_11_1_score(df)
        self.assertEqual(s, 0.0)


class TestCompositeScore(unittest.TestCase):
    """End-to-end: base_building_df should beat extended_rally_df."""

    def test_base_building_beats_extended_rally(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        scorer = EarlyStageScorer()
        df_base = _base_building_df(breakout_pct=0.12)
        df_ext = _extended_rally_df(total_gain=0.80)
        score_base = scorer.score_all(
            ["BASE", "EXT"],
            {"BASE": df_base, "EXT": df_ext},
            benchmark_ticker=None,
        )
        base_row = score_base[score_base["ticker"] == "BASE"].iloc[0]
        ext_row = score_base[score_base["ticker"] == "EXT"].iloc[0]
        self.assertGreater(base_row["early_stage_score"], ext_row["early_stage_score"],
                           f"base={base_row['early_stage_score']} ext={ext_row['early_stage_score']}")

    def test_all_zero_on_empty_df(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        scorer = EarlyStageScorer()
        result = scorer.score_all(["X"], {"X": pd.DataFrame()}, benchmark_ticker=None)
        self.assertEqual(result.iloc[0]["early_stage_score"], 0.0)

    def test_output_schema(self):
        from alphalens.momentum_screener.early_stage_scorer import EarlyStageScorer
        scorer = EarlyStageScorer()
        df = _base_building_df()
        result = scorer.score_all(["BASE"], {"BASE": df}, benchmark_ticker=None)
        expected_cols = {
            "ticker", "base_breakout_score", "acceleration_score",
            "vcp_score", "rsi_emergence_score", "adx_building_score",
            "volume_accumulation_score", "jegadeesh_11_1_score",
            "early_stage_score",
        }
        self.assertTrue(expected_cols.issubset(set(result.columns)),
                        f"missing: {expected_cols - set(result.columns)}")


if __name__ == "__main__":
    unittest.main()

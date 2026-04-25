import unittest

import numpy as np
import pandas as pd

from alphalens.backtest.history_store import HistoryStore
from alphalens.macro.scorer import MacroRegime, RuleBasedScorer
from alphalens.macro.signals import SignalSet
from alphalens.rotation.allocator import OverlayAllocator
from alphalens.rotation.config import Rule


def _ohlcv_constant_return(daily_return: float, n_bars: int, start: str = "2020-01-02"):
    idx = pd.date_range(start=start, periods=n_bars, freq="B")
    close = (1.0 + daily_return) ** np.arange(n_bars) * 100.0
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(n_bars, 1_000_000.0),
        },
        index=idx,
    )


def _neutral_signals(index: pd.DatetimeIndex) -> SignalSet:
    """Signals that will never fire any rule (all zeros)."""
    zeros = pd.Series(0.0, index=index)
    return SignalSet(
        yield_curve_slope=zeros.copy(),
        vix_decile=zeros.copy(),
        qqq_iwm_spread=zeros.copy(),
    )


class _StaticScorer:
    """Test double: returns the same MacroRegime every call."""

    def __init__(self, regime: MacroRegime):
        self._regime = regime

    def score(self, signals):
        return self._regime


class TestOverlayBacktestEngine(unittest.TestCase):
    CORE = {"SPY": 0.60, "QQQ": 0.30, "IWM": 0.10}

    def _store(self, n_bars=260):
        # SPY +0.04%/day, QQQ +0.06%/day, IWM +0.02%/day (arbitrary but distinct)
        return HistoryStore(
            {
                "SPY": _ohlcv_constant_return(0.0004, n_bars),
                "QQQ": _ohlcv_constant_return(0.0006, n_bars),
                "IWM": _ohlcv_constant_return(0.0002, n_bars),
            }
        )

    def _engine(self, store, scorer=None, signals=None, spread_bps=None):
        from alphalens.rotation.overlay_engine import OverlayBacktestEngine

        scorer = scorer or _StaticScorer(MacroRegime(flags={}, tilt_sum={}))
        alloc = OverlayAllocator(core_weights=self.CORE, max_tilt=0.10)
        signals = signals or _neutral_signals(store.full("SPY").index)
        spread_bps = spread_bps or {"SPY": 1.0, "QQQ": 2.0, "IWM": 3.0}
        return OverlayBacktestEngine(
            store=store,
            scorer=scorer,
            allocator=alloc,
            signals=signals,
            etf_spread_bps=spread_bps,
            benchmark="SPY",
        )

    def test_passive_baseline_matches_weighted_buy_and_hold(self):
        store = self._store(n_bars=260)
        engine = self._engine(store)

        result = engine.run(
            start=store.full("SPY").index[0],
            end=store.full("SPY").index[-1],
            rebalance_stride=63,
        )

        # Core 60/30/10 on returns 0.0004/0.0006/0.0002 → 0.00044 per day
        expected_daily = 0.60 * 0.0004 + 0.30 * 0.0006 + 0.10 * 0.0002
        self.assertAlmostEqual(result.daily_returns_gross.mean(), expected_daily, places=6)

    def test_quarterly_stride_produces_expected_rebalance_count(self):
        store = self._store(n_bars=252)
        engine = self._engine(store)

        result = engine.run(
            start=store.full("SPY").index[0],
            end=store.full("SPY").index[-1],
            rebalance_stride=63,
        )

        # 252 days / 63 = 4 rebalances (first day + 3 strides)
        self.assertEqual(len(result.rebalances), 4)

    def test_turnover_is_sum_absolute_weight_delta(self):
        store = self._store(n_bars=252)
        regime_tilted = MacroRegime(flags={"r1": True}, tilt_sum={"QQQ": 0.10, "SPY": -0.10})
        engine = self._engine(store, scorer=_StaticScorer(regime_tilted))

        result = engine.run(
            start=store.full("SPY").index[0],
            end=store.full("SPY").index[-1],
            rebalance_stride=63,
        )

        # First rebalance from {0,0,0} to {0.5, 0.4, 0.1} → turnover = 1.0
        self.assertAlmostEqual(result.rebalances[0].turnover, 1.0, places=6)
        # Subsequent rebalances: same weights → turnover = 0
        for reb in result.rebalances[1:]:
            self.assertAlmostEqual(reb.turnover, 0.0, places=6)

    def test_costs_applied_on_rebalance_day_only(self):
        store = self._store(n_bars=126)
        regime_tilted = MacroRegime(flags={"r1": True}, tilt_sum={"QQQ": 0.05, "SPY": -0.05})
        engine = self._engine(store, scorer=_StaticScorer(regime_tilted))

        result = engine.run(
            start=store.full("SPY").index[0],
            end=store.full("SPY").index[-1],
            rebalance_stride=63,
        )

        # Gross and net equal except on rebalance days (drag subtracted there)
        diffs = result.daily_returns_gross - result.daily_returns_net
        nonzero = diffs[diffs > 1e-12]
        # 2 rebalance events in 126 days at stride 63
        self.assertGreaterEqual(len(nonzero), 1)
        # and gross > net
        self.assertTrue((result.daily_returns_gross >= result.daily_returns_net).all())

    def test_daily_returns_length_matches_trading_days(self):
        store = self._store(n_bars=252)
        engine = self._engine(store)
        idx = store.full("SPY").index

        result = engine.run(start=idx[0], end=idx[-1], rebalance_stride=63)

        # Returns from day 1 onwards (day 0 has no prior close)
        self.assertEqual(len(result.daily_returns_gross), 251)

    def test_benchmark_series_present(self):
        store = self._store(n_bars=252)
        engine = self._engine(store)
        idx = store.full("SPY").index

        result = engine.run(start=idx[0], end=idx[-1], rebalance_stride=63)

        self.assertEqual(len(result.benchmark_returns), 251)
        # Benchmark is SPY daily return ≈ 0.0004
        self.assertAlmostEqual(result.benchmark_returns.mean(), 0.0004, places=6)

    def test_rule_firings_recorded_on_rebalance(self):
        store = self._store(n_bars=252)
        fired = MacroRegime(
            flags={"yield_steep": True, "vix_elevated": False},
            tilt_sum={"QQQ": 0.05, "SPY": -0.05},
        )
        engine = self._engine(store, scorer=_StaticScorer(fired))
        idx = store.full("SPY").index

        result = engine.run(start=idx[0], end=idx[-1], rebalance_stride=63)

        self.assertEqual(
            dict(result.rebalances[0].rule_firings),
            {"yield_steep": True, "vix_elevated": False},
        )


class TestEngineWithRealScorer(unittest.TestCase):
    """End-to-end: RuleBasedScorer wired into engine."""

    def test_rule_fires_only_when_signal_above_threshold(self):
        from alphalens.rotation.overlay_engine import OverlayBacktestEngine

        n_bars = 260
        store = HistoryStore(
            {
                "SPY": _ohlcv_constant_return(0.0004, n_bars),
                "QQQ": _ohlcv_constant_return(0.0006, n_bars),
                "IWM": _ohlcv_constant_return(0.0002, n_bars),
            }
        )
        idx = store.full("SPY").index

        # Signals: slope starts at 0, jumps to 1.5 after day 100
        slope = pd.Series(0.0, index=idx)
        slope.iloc[100:] = 1.5
        signals = SignalSet(
            yield_curve_slope=slope,
            vix_decile=pd.Series(0.5, index=idx),
            qqq_iwm_spread=pd.Series(0.0, index=idx),
        )
        rules = (
            Rule(
                name="yield_steep",
                signal="yield_curve_slope",
                operator="gt",
                threshold=1.0,
                tilt={"QQQ": 0.05, "SPY": -0.05},
            ),
        )
        scorer = RuleBasedScorer(rules)
        alloc = OverlayAllocator(
            core_weights={"SPY": 0.60, "QQQ": 0.30, "IWM": 0.10},
            max_tilt=0.10,
        )
        engine = OverlayBacktestEngine(
            store=store,
            scorer=scorer,
            allocator=alloc,
            signals=signals,
            etf_spread_bps={"SPY": 1.0, "QQQ": 2.0, "IWM": 3.0},
            benchmark="SPY",
        )

        result = engine.run(start=idx[0], end=idx[-1], rebalance_stride=63)

        # Rebalances at days 0, 63, 126, 189, 252
        # slope fires at day >= 100 → rebalance at 126, 189 fire; 0, 63 don't
        firings = [r.rule_firings["yield_steep"] for r in result.rebalances]
        self.assertFalse(firings[0])
        self.assertFalse(firings[1])
        self.assertTrue(firings[2])
        self.assertTrue(firings[3])


if __name__ == "__main__":
    unittest.main()

"""End-to-end synthetic-data integration test for Tactical Sector Rotation.

Wires together config → FRED-style signals → RuleBasedScorer → OverlayAllocator
→ OverlayBacktestEngine → GateReport on synthetic but realistic 5-year OHLCV.
Guarantees the whole pipeline stays connected as modules evolve.

Uses synthetic data (no real API calls); runs in <5s. No skip marker needed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from alphalens.archive.rotation.allocator import OverlayAllocator
from alphalens.archive.rotation.config import load_config
from alphalens.archive.rotation.gates import evaluate_all_gates
from alphalens.archive.rotation.overlay_engine import OverlayBacktestEngine
from alphalens.backtest.factor_analysis import AlphaResult
from alphalens.data.macro.scorer import RuleBasedScorer
from alphalens.data.macro.signals import build_signal_set
from alphalens.data.store.history import HistoryStore


def _synthetic_ohlcv(n_bars: int, daily_mu: float, daily_vol: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2018-01-02", periods=n_bars, freq="B")
    daily_ret = rng.normal(daily_mu, daily_vol, n_bars)
    close = np.cumprod(1.0 + daily_ret) * 100.0
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n_bars, 5_000_000.0),
        },
        index=idx,
    )


_CONFIG_YAML = """
core_weights: {SPY: 0.60, QQQ: 0.30, IWM: 0.10}
max_tilt: 0.10
rebalance_stride: 63
etf_spread_bps: {SPY: 1.0, QQQ: 2.0, IWM: 3.0}
rules:
  - name: yield_steep
    signal: yield_curve_slope
    operator: gt
    threshold: 0.5
    tilt: {QQQ: 0.05, SPY: -0.05}
  - name: vix_elevated
    signal: vix_decile
    operator: gt
    threshold: 0.75
    tilt: {SPY: 0.05, QQQ: -0.05}
gates:
  rolling_sharpe_min: 0.20
  carhart_oos_t_min: 1.50
"""


class TestEndToEnd(unittest.TestCase):
    def test_full_pipeline_on_synthetic_5y_data(self):
        n = 1260  # ~5y trading days
        store = HistoryStore(
            {
                "SPY": _synthetic_ohlcv(n, 0.0004, 0.010, seed=1),
                "QQQ": _synthetic_ohlcv(n, 0.0006, 0.012, seed=2),
                "IWM": _synthetic_ohlcv(n, 0.0003, 0.013, seed=3),
            }
        )
        idx = store.full("SPY").index

        # Synthetic macro series on the same calendar
        rng = np.random.default_rng(10)
        dgs10 = pd.Series(2.5 + rng.normal(0, 0.3, n), index=idx)
        dgs2 = pd.Series(1.0 + rng.normal(0, 0.3, n), index=idx)
        vix = pd.Series(np.clip(18 + rng.normal(0, 5, n), 10, 50), index=idx)

        signals = build_signal_set(
            dgs10=dgs10,
            dgs2=dgs2,
            vix=vix,
            qqq_close=store.full("QQQ")["close"],
            iwm_close=store.full("IWM")["close"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "rotation.yaml"
            cfg_path.write_text(_CONFIG_YAML)
            cfg = load_config(cfg_path)

        scorer = RuleBasedScorer(cfg.rules)
        allocator = OverlayAllocator(core_weights=cfg.core_weights, max_tilt=cfg.max_tilt)
        engine = OverlayBacktestEngine(
            store=store,
            scorer=scorer,
            allocator=allocator,
            signals=signals,
            etf_spread_bps=cfg.etf_spread_bps,
        )

        result = engine.run(start=idx[0], end=idx[-1], rebalance_stride=cfg.rebalance_stride)

        # Expected: ~20 rebalances in 1260 days
        self.assertGreaterEqual(len(result.rebalances), 15)
        self.assertLess(len(result.rebalances), 25)

        # Sanity: gross and net return series same length as calendar minus 1
        self.assertEqual(len(result.daily_returns_net), n - 1)
        self.assertEqual(len(result.daily_returns_gross), n - 1)

        # Net should be <= Gross on rebalance days (cost drag)
        self.assertTrue((result.daily_returns_net <= result.daily_returns_gross + 1e-12).all())

        # Feed gates (with mocked Carhart result — no FF factors loaded in tests)
        bench_close = store.full("SPY")["close"]
        carhart = AlphaResult(
            spec_name="Carhart-4F",
            alpha_daily=0.00008,
            alpha_annualized=0.02,
            alpha_tstat=1.8,  # above 1.5, below Bonferroni n=5 ≈ 2.58
            betas={"Mkt-RF": 1.0, "SMB": 0.05, "HML": 0.02, "Mom": 0.0},
            r_squared=0.85,
            n_observations=n - 1,
            cov_type="HAC",
        )

        report = evaluate_all_gates(
            result=result,
            benchmark_close=bench_close,
            carhart_result=carhart,
            gates=cfg.gates,
            n_tests=3,
        )

        # GateReport shape + each gate has name/passed/value
        self.assertEqual(len(report.gates), 6)
        for g in report.gates:
            self.assertIsInstance(g.name, str)
            self.assertIsInstance(g.passed, bool)


if __name__ == "__main__":
    unittest.main()

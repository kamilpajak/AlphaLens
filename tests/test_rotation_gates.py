import unittest

import numpy as np
import pandas as pd

from alphalens.archive.rotation.config import GateConfig
from alphalens.archive.rotation.overlay_engine import OverlayBacktestResult, RebalanceEvent
from alphalens.backtest.factor_analysis import AlphaResult


def _result(
    *,
    gross_mean: float = 0.0005,
    net_mean: float = 0.0004,
    bench_mean: float = 0.0003,
    n: int = 1000,
    seed: int = 42,
) -> OverlayBacktestResult:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-02", periods=n, freq="B")
    gross = pd.Series(rng.normal(gross_mean, 0.01, n), index=idx)
    net = pd.Series(gross.values - (gross_mean - net_mean), index=idx)
    bench = pd.Series(rng.normal(bench_mean, 0.01, n), index=idx)
    rebs = [
        RebalanceEvent(
            date=idx[0],
            target_weights={"SPY": 0.60, "QQQ": 0.30, "IWM": 0.10},
            prev_weights={"SPY": 0.0, "QQQ": 0.0, "IWM": 0.0},
            turnover=1.0,
            rule_firings={},
            cost_bps=2.0,
        )
    ]
    return OverlayBacktestResult(
        daily_returns_gross=gross,
        daily_returns_net=net,
        benchmark_returns=bench,
        rebalances=rebs,
    )


def _benchmark_close(n: int = 1000, trend: float = 0.0003) -> pd.Series:
    idx = pd.date_range("2020-01-02", periods=n, freq="B")
    rng = np.random.default_rng(1)
    daily_ret = rng.normal(trend, 0.01, n)
    return pd.Series(np.cumprod(1.0 + daily_ret) * 100.0, index=idx)


class TestRegimeDecompGate(unittest.TestCase):
    def test_passes_when_alpha_positive_in_every_regime(self):
        from alphalens.archive.rotation.gates import gate_regime_decomp

        # Deterministic constant-positive series → every regime slice has mean > 0
        n = 1000
        idx = pd.date_range("2020-01-02", periods=n, freq="B")
        const = pd.Series(0.001, index=idx)
        result = OverlayBacktestResult(
            daily_returns_gross=const,
            daily_returns_net=const,
            benchmark_returns=const,
            rebalances=[],
        )
        bench_close = _benchmark_close(n=n, trend=0.0)

        gate = gate_regime_decomp(result, bench_close)

        self.assertTrue(gate.passed)

    def test_fails_when_alpha_negative_in_some_regime(self):
        from alphalens.archive.rotation.gates import gate_regime_decomp

        # Strategy loses money (net mean < benchmark mean)
        result = _result(gross_mean=-0.0005, net_mean=-0.0005, bench_mean=0.0005, n=1000)
        bench_close = _benchmark_close(n=1000, trend=0.0005)

        gate = gate_regime_decomp(result, bench_close)

        self.assertFalse(gate.passed)


class TestBootstrapCIGate(unittest.TestCase):
    def test_passes_when_clearly_positive(self):
        from alphalens.archive.rotation.gates import gate_bootstrap_ci

        result = _result(gross_mean=0.002, net_mean=0.002, bench_mean=0.0, n=1000)

        gate = gate_bootstrap_ci(result, n_bootstrap=500, block_size=21, seed=7)

        self.assertTrue(gate.passed)
        self.assertGreater(gate.value, 0.0)

    def test_fails_when_ci_includes_zero(self):
        from alphalens.archive.rotation.gates import gate_bootstrap_ci

        result = _result(gross_mean=0.0, net_mean=0.0, bench_mean=0.0, n=500)

        gate = gate_bootstrap_ci(result, n_bootstrap=500, block_size=21, seed=7)

        self.assertFalse(gate.passed)


class TestCostDragGate(unittest.TestCase):
    def test_passes_when_drag_below_threshold(self):
        from alphalens.archive.rotation.gates import gate_cost_drag

        # gross annualised 252 × 0.0005 ≈ 12.6%; net 12.0% → drag ≈ 0.6 pp → 5% of gross
        result = _result(gross_mean=0.0005, net_mean=0.000475, n=1000)

        gate = gate_cost_drag(result, max_drag_ratio=0.5)

        self.assertTrue(gate.passed)

    def test_fails_when_drag_exceeds_half_of_alpha(self):
        from alphalens.archive.rotation.gates import gate_cost_drag

        # gross 0.001, net 0.0001 → drag ratio = 0.9
        result = _result(gross_mean=0.001, net_mean=0.0001, n=1000)

        gate = gate_cost_drag(result, max_drag_ratio=0.5)

        self.assertFalse(gate.passed)


class TestRollingSharpeGate(unittest.TestCase):
    def test_passes_when_all_windows_above_threshold(self):
        from alphalens.archive.rotation.gates import gate_rolling_sharpe

        # High persistent mean → rolling Sharpe high everywhere
        rng = np.random.default_rng(2)
        idx = pd.date_range("2018-01-02", periods=800, freq="B")
        strong = pd.Series(rng.normal(0.0015, 0.005, 800), index=idx)
        result = OverlayBacktestResult(
            daily_returns_gross=strong,
            daily_returns_net=strong,
            benchmark_returns=pd.Series(0.0, index=idx),
            rebalances=[],
        )

        gate = gate_rolling_sharpe(result, window=252, min_sharpe=0.30)

        self.assertTrue(gate.passed)

    def test_fails_when_any_window_below_threshold(self):
        from alphalens.archive.rotation.gates import gate_rolling_sharpe

        # Flip sign halfway → one rolling window will have negative Sharpe
        rng = np.random.default_rng(3)
        idx = pd.date_range("2018-01-02", periods=800, freq="B")
        data = np.concatenate([rng.normal(0.0015, 0.005, 400), rng.normal(-0.0015, 0.005, 400)])
        s = pd.Series(data, index=idx)
        result = OverlayBacktestResult(
            daily_returns_gross=s,
            daily_returns_net=s,
            benchmark_returns=pd.Series(0.0, index=idx),
            rebalances=[],
        )

        gate = gate_rolling_sharpe(result, window=252, min_sharpe=0.30)

        self.assertFalse(gate.passed)


class TestCarhartAlphaTGate(unittest.TestCase):
    def test_passes_when_t_above_oos_threshold(self):
        from alphalens.archive.rotation.gates import gate_carhart_alpha_t

        carhart = AlphaResult(
            spec_name="Carhart-4F",
            alpha_daily=0.0003,
            alpha_annualized=0.076,
            alpha_tstat=2.0,
            betas={"Mkt-RF": 1.0, "SMB": 0.0, "HML": 0.0, "Mom": 0.0},
            r_squared=0.8,
            n_observations=1200,
            cov_type="HAC",
        )

        gate = gate_carhart_alpha_t(carhart, min_t=1.5)

        self.assertTrue(gate.passed)
        self.assertAlmostEqual(gate.value, 2.0)

    def test_fails_when_t_below_threshold(self):
        from alphalens.archive.rotation.gates import gate_carhart_alpha_t

        carhart = AlphaResult(
            spec_name="Carhart-4F",
            alpha_daily=0.0001,
            alpha_annualized=0.025,
            alpha_tstat=0.8,
            betas={"Mkt-RF": 1.0, "SMB": 0.0, "HML": 0.0, "Mom": 0.0},
            r_squared=0.8,
            n_observations=1200,
            cov_type="HAC",
        )

        gate = gate_carhart_alpha_t(carhart, min_t=1.5)

        self.assertFalse(gate.passed)


class TestBonferroniGate(unittest.TestCase):
    def test_passes_when_t_exceeds_bonferroni_critical(self):
        from alphalens.archive.rotation.gates import gate_bonferroni

        carhart = AlphaResult(
            spec_name="Carhart-4F",
            alpha_daily=0.0,
            alpha_annualized=0.0,
            alpha_tstat=3.5,  # clearly above n=5 Bonferroni critical ≈ 2.58
            betas={"Mkt-RF": 1.0, "SMB": 0.0, "HML": 0.0, "Mom": 0.0},
            r_squared=0.8,
            n_observations=1200,
            cov_type="HAC",
        )

        gate = gate_bonferroni(carhart, n_tests=5, alpha=0.05)

        self.assertTrue(gate.passed)

    def test_fails_when_t_below_bonferroni_critical(self):
        from alphalens.archive.rotation.gates import gate_bonferroni

        carhart = AlphaResult(
            spec_name="Carhart-4F",
            alpha_daily=0.0,
            alpha_annualized=0.0,
            alpha_tstat=2.0,
            betas={"Mkt-RF": 1.0, "SMB": 0.0, "HML": 0.0, "Mom": 0.0},
            r_squared=0.8,
            n_observations=1200,
            cov_type="HAC",
        )

        gate = gate_bonferroni(carhart, n_tests=10, alpha=0.05)

        self.assertFalse(gate.passed)


class TestEvaluateAllGates(unittest.TestCase):
    def test_aggregator_returns_pass_false_if_any_gate_fails(self):
        from alphalens.archive.rotation.gates import GateReport, evaluate_all_gates

        result = _result(gross_mean=0.0, net_mean=-0.001, bench_mean=0.0, n=600)
        bench_close = _benchmark_close(n=600, trend=0.0)
        carhart = AlphaResult(
            spec_name="Carhart-4F",
            alpha_daily=0.0,
            alpha_annualized=0.0,
            alpha_tstat=0.5,  # fails both simple and Bonferroni
            betas={"Mkt-RF": 1.0, "SMB": 0.0, "HML": 0.0, "Mom": 0.0},
            r_squared=0.5,
            n_observations=600,
            cov_type="HAC",
        )
        gates_cfg = GateConfig(rolling_sharpe_min=0.30, carhart_oos_t_min=1.5)

        report = evaluate_all_gates(
            result=result,
            benchmark_close=bench_close,
            carhart_result=carhart,
            gates=gates_cfg,
            n_tests=5,
        )

        self.assertIsInstance(report, GateReport)
        self.assertFalse(report.passed)
        self.assertGreaterEqual(len(report.gates), 5)


if __name__ == "__main__":
    unittest.main()

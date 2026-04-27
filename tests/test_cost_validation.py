"""Unit tests for `alphalens.backtest.cost_validation`.

All tests on synthetic in-memory fixtures — no Polygon, no real
BacktestEngine runs. Covers the 12 critical behaviours in the plan,
including the two zen-flagged invariants: **no-lookahead per-date
tier assignment** and **zero-volume/halted-day fallback**.
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd

from alphalens.backtest.cost_validation import (
    DEFAULT_TIERS,
    ScalePathSummary,
    apply_tiered_cost,
    build_per_date_tiers,
    classify_tier_as_of,
    compare_cost_scenarios,
    evaluate_cost_gate,
    rolling_dollar_adv,
    run_scale_path,
)
from alphalens.backtest.engine import BacktestReport, RebalanceSnapshot
from alphalens.backtest.history_store import HistoryStore


def _history(close: list[float], volume: list[int], start: str = "2022-01-03") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(close), freq="B")
    return pd.DataFrame(
        {
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": volume,
        },
        index=idx,
    )


def _daily(d: pd.Timestamp, tickers: list[str], ret: float = 0.001) -> RebalanceSnapshot:
    return RebalanceSnapshot(
        date=d,
        scored_count=10,
        top_n_tickers=list(tickers),
        top_n_scores=[1.0 - 0.1 * i for i in range(len(tickers))],
        top_n_forward_returns=[ret] * len(tickers),
        portfolio_return=ret,
        portfolio_return_holding=ret * 5,
        universe_median_return=0.0,
        ic=0.05,
    )


def _report(
    daily_specs: list[tuple[pd.Timestamp, list[str]]],
    ret: float = 0.001,
) -> BacktestReport:
    daily = [_daily(d, tks, ret) for d, tks in daily_specs]
    return BacktestReport(
        scorer_config={},
        holding_period=5,
        top_n=max(len(s[1]) for s in daily_specs) if daily_specs else 5,
        start=daily_specs[0][0].date() if daily_specs else date(2022, 1, 3),
        end=daily_specs[-1][0].date() if daily_specs else date(2022, 1, 3),
        benchmark="SPY",
        universe_ticker_count=10,
        rebalance_results=daily,
    )


class TestRollingDollarAdv(unittest.TestCase):
    def test_21d_window_mean(self):
        # 40-day constant close=100, volume=1000 → dollar_volume=100k per day
        close = [100.0] * 40
        volume = [1000] * 40
        store = HistoryStore({"FOO": _history(close, volume)})
        adv = rolling_dollar_adv(store, ["FOO"], window_days=21)
        series = adv["FOO"]
        # Value on day 30 uses trailing 21 bars strictly before day 30 → all 100k
        self.assertAlmostEqual(float(series.iloc[30]), 100_000.0, places=3)

    def test_zero_volume_fallback(self):
        # Day 10 has zero volume → imputed from day 9
        close = [100.0] * 30
        volume = [1000] * 30
        volume[10] = 0
        store = HistoryStore({"FOO": _history(close, volume)})
        adv = rolling_dollar_adv(store, ["FOO"], window_days=21)
        series = adv["FOO"]
        # Day 15 trailing window [5,14] — if day 10 hadn't been ffilled we'd see
        # a dip. Assert all values are exactly $100k (no dip).
        for i in range(21, 30):
            self.assertAlmostEqual(
                float(series.iloc[i]),
                100_000.0,
                places=3,
                msg=f"zero-vol day not ffilled at index {i}",
            )


class TestClassifyTierAsOf(unittest.TestCase):
    def test_quintile_boundaries(self):
        # 100 tickers with ADV = rank (so linear distribution)
        adv = {f"T{i:03d}": float(i + 1) for i in range(100)}
        tiers = classify_tier_as_of(adv, DEFAULT_TIERS)
        counts: dict[str, int] = {}
        for t in tiers.values():
            counts[t] = counts.get(t, 0) + 1
        # Each tier should have ~20 (allow ±2 for boundary/tie handling)
        for tier_name in ("mega", "large", "mid", "small", "micro"):
            self.assertTrue(
                18 <= counts.get(tier_name, 0) <= 22,
                f"{tier_name} count = {counts.get(tier_name, 0)}, expected ~20",
            )


class TestBuildPerDateTiersLookahead(unittest.TestCase):
    def test_no_lookahead(self):
        """zen-critical: ticker whose ADV jumps mid-period should get
        different tiers on before/after dates — never seeing future data.
        """
        # Ticker FOO: low volume for first 30 days, high volume for next 30
        close = [100.0] * 60
        low_vol = [100] * 30
        high_vol = [10_000] * 30
        vol = low_vol + high_vol
        # Universe: FOO + 10 stable tickers with medium volume to have quintiles
        histories = {"FOO": _history(close, vol)}
        for i in range(10):
            histories[f"STABLE{i:02d}"] = _history(close, [1_000] * 60)
        store = HistoryStore(histories)
        adv = rolling_dollar_adv(store, list(histories.keys()), window_days=21)

        calendar = list(adv["FOO"].index)
        per_date_tiers = build_per_date_tiers(adv, calendar, DEFAULT_TIERS)

        # Day 22: FOO has just transitioned from 0-vol to low-vol imputation;
        # its 21-day trailing ADV is all from low_vol period → LOWER tier
        # than STABLE tickers
        early_tier = per_date_tiers[calendar[22]].get("FOO", "?")
        # Day 58: trailing 21 days are from high_vol period → HIGHER tier
        late_tier = per_date_tiers[calendar[58]].get("FOO", "?")

        # Must be different tiers — that proves the rolling window actually
        # changes with time, not fixed from end-of-backtest
        self.assertNotEqual(
            early_tier,
            late_tier,
            f"tier unchanged across volume regime: {early_tier} vs {late_tier}",
        )


class TestRunScalePathSynthetic(unittest.TestCase):
    def _common_fixture(self):
        # 5 days, top-3 picks, 3 distinct tickers all day.
        # Portfolio $1M, linear weighting [0.50, 0.33, 0.17] approx.
        start = pd.Timestamp("2022-01-03")
        daily_specs = [(start + pd.Timedelta(days=i), ["A", "B", "C"]) for i in range(5)]
        baseline = _report(daily_specs)

        # Fake ADV: A=$10M, B=$1M, C=$100k (mega/mid/micro-ish)
        idx = pd.date_range(start, periods=30, freq="B")
        rolling_adv = {
            "A": pd.Series(10_000_000.0, index=idx),
            "B": pd.Series(1_000_000.0, index=idx),
            "C": pd.Series(100_000.0, index=idx),
        }
        per_date_tiers: dict[pd.Timestamp, dict[str, str]] = {}
        for ts in idx:
            per_date_tiers[ts] = {"A": "mega", "B": "mid", "C": "micro"}
        return baseline, rolling_adv, per_date_tiers

    def test_participation_formula(self):
        baseline, rolling_adv, per_date_tiers = self._common_fixture()
        summary = run_scale_path(
            baseline,
            rolling_adv,
            per_date_tiers,
            portfolio_value=1_000_000,
            threshold_pct=15.0,
            max_threshold_pct=20.0,
        )
        # 5 days × 3 picks = 15, but first day's turnover = 100% (no prior);
        # subsequent days: same picks → turnover 0% → participation 0
        # So worst offenders are on day 1 only.
        # Linear weights for n=3: sum to 1.0, rank1 = 2.0/3.2, rank2 = 1.1/3.2, rank3 = 0.2/3.2
        # Actually reach from linspace(2.0, 0.2, 3): [2.0, 1.1, 0.2] → sum 3.3 → [0.606, 0.333, 0.061]
        # Day 1 rank 1 (ticker A): $1M × 0.606 × 1.0 / $10M = 6.06%
        # Day 1 rank 3 (ticker C): $1M × 0.061 × 1.0 / $100k = 60.6% — worst offender!
        self.assertEqual(summary.n_pick_days, 15)
        self.assertGreater(summary.max_participation, 0.50)  # C on day 1

    def test_flags_worst_offender(self):
        baseline, rolling_adv, per_date_tiers = self._common_fixture()
        summary = run_scale_path(
            baseline,
            rolling_adv,
            per_date_tiers,
            portfolio_value=1_000_000,
            threshold_pct=15.0,
            max_threshold_pct=20.0,
            n_worst=5,
        )
        # Worst offender should be ticker C (lowest ADV) on day 1 (full turnover)
        self.assertEqual(summary.worst_offenders[0].ticker, "C")
        self.assertEqual(summary.worst_offenders[0].tier, "micro")


class TestApplyTieredCost(unittest.TestCase):
    def test_heterogeneous_tiers(self):
        # 2 days, 3 picks in different tiers
        start = pd.Timestamp("2022-01-03")
        daily_specs = [(start + pd.Timedelta(days=i), ["A", "B", "C"]) for i in range(2)]
        baseline = _report(daily_specs, ret=0.0)

        per_date_tiers = {d: {"A": "mega", "B": "mid", "C": "micro"} for d, _ in daily_specs}
        bps_per_tier = {"mega": 3, "mid": 25, "micro": 100}

        returns = baseline.portfolio_returns
        top_n_lists = [snap.top_n_tickers for snap in baseline.rebalance_results]
        dates = [snap.date for snap in baseline.rebalance_results]

        net = apply_tiered_cost(
            returns,
            top_n_lists,
            dates,
            per_date_tiers,
            bps_per_tier,
            daily_turnover=None,
            weighting="linear",
        )
        # Weighted bps = linear([2.0, 1.1, 0.2]/3.3) · [3, 25, 100]
        # = [0.606, 0.333, 0.061] · [3, 25, 100] = 1.82 + 8.33 + 6.1 = ~16.25 bps
        # Daily drag = 16.25 / 10_000 / 252 ≈ 6.45e-6
        # Net = 0 - 6.45e-6 per day
        net_mean = float(net.mean())
        self.assertLess(net_mean, 0.0)
        self.assertGreater(net_mean, -0.001)  # within reasonable range

    def test_per_date_tiers_used(self):
        """Same ticker with different tier on different dates → cost uses
        date-correct tier assignment.
        """
        start = pd.Timestamp("2022-01-03")
        daily_specs = [(start + pd.Timedelta(days=i), ["A"]) for i in range(2)]
        baseline = _report(daily_specs, ret=0.0)
        returns = baseline.portfolio_returns
        top_n_lists = [snap.top_n_tickers for snap in baseline.rebalance_results]
        dates = [snap.date for snap in baseline.rebalance_results]

        # Day 1: A is mega (cheap). Day 2: A is micro (expensive).
        per_date_tiers = {
            dates[0]: {"A": "mega"},
            dates[1]: {"A": "micro"},
        }
        bps = {"mega": 3, "micro": 100}
        net = apply_tiered_cost(returns, top_n_lists, dates, per_date_tiers, bps)
        # Day 2 should have larger drag than Day 1
        self.assertLess(float(net.iloc[1]), float(net.iloc[0]))


class TestCompareCostScenarios(unittest.TestCase):
    def test_monotone(self):
        """Sharpe ordering: gross > tiered > flat_100bps when tiered avg < 100bps.

        Needs return variance for Sharpe to be non-degenerate; use
        deterministic alternating returns with positive drift.
        """
        start = pd.Timestamp("2022-01-03")
        rng = np.random.default_rng(42)
        noisy_returns = rng.normal(0.001, 0.02, size=100)
        daily = []
        for i, r in enumerate(noisy_returns):
            daily.append(_daily(start + pd.Timedelta(days=i), ["A", "B"], ret=float(r)))
        baseline = BacktestReport(
            scorer_config={},
            holding_period=5,
            top_n=2,
            start=daily[0].date.date(),
            end=daily[-1].date.date(),
            benchmark="SPY",
            universe_ticker_count=10,
            rebalance_results=daily,
        )
        per_date_tiers = {snap.date: {"A": "mega", "B": "mid"} for snap in daily}
        bps_per_tier = {"mega": 3, "mid": 25}

        comparison = compare_cost_scenarios(
            baseline, per_date_tiers, bps_per_tier, weighting="linear"
        )
        # Sharpe ordering: gross > tiered > flat_100bps (tiered < 100bps avg)
        self.assertGreater(comparison.sharpe_gross, comparison.sharpe_tiered)
        self.assertGreater(comparison.sharpe_tiered, comparison.sharpe_flat_100bps)


def _summary(
    *,
    fraction: float = 0.02,
    max_part: float = 0.10,
    threshold_pct: float = 15.0,
    max_threshold_pct: float = 20.0,
) -> ScalePathSummary:
    return ScalePathSummary(
        n_pick_days=1000,
        threshold_pct=threshold_pct,
        max_threshold_pct=max_threshold_pct,
        fraction_exceeding_threshold=fraction,
        max_participation=max_part,
        q95_participation=0.12,
        median_participation=0.02,
        worst_offenders=(),
        per_tier_max={},
        per_tier_median={},
    )


class TestEvaluateCostGate(unittest.TestCase):
    def test_pass(self):
        v = evaluate_cost_gate(_summary(fraction=0.02, max_part=0.10))
        self.assertTrue(v.scale_path_pass)
        self.assertTrue(v.fraction_pass)
        self.assertTrue(v.max_pass)
        self.assertEqual(v.overall, "PASS")

    def test_fail_fraction(self):
        v = evaluate_cost_gate(_summary(fraction=0.08, max_part=0.10))
        self.assertFalse(v.fraction_pass)
        self.assertTrue(v.max_pass)
        self.assertEqual(v.overall, "FAIL")

    def test_fail_max(self):
        v = evaluate_cost_gate(_summary(fraction=0.02, max_part=0.22))
        self.assertTrue(v.fraction_pass)
        self.assertFalse(v.max_pass)
        self.assertEqual(v.overall, "FAIL")


if __name__ == "__main__":
    unittest.main()

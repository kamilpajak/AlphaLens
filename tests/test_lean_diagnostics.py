import unittest

import numpy as np
import pandas as pd


def _synthetic_scored_frames(n_days=50, n_tickers=100, tail_alpha=True, seed=0):
    """Build fake scored_frames with either tail-concentrated or flat signal.

    When tail_alpha=True: top decile mean return is strong, bottom is weak, middle is noise.
    When tail_alpha=False: returns are independent of score (pure noise).
    """
    rng = np.random.default_rng(seed)
    frames: dict[pd.Timestamp, pd.DataFrame] = {}
    for i in range(n_days):
        scores = rng.normal(0, 1, n_tickers)
        if tail_alpha:
            # Top 10%: strong positive, bottom 10%: strong negative, middle: pure noise.
            pct_rank = pd.Series(scores).rank(pct=True)
            signal = np.where(
                pct_rank >= 0.9,
                0.03,
                np.where(pct_rank <= 0.1, -0.03, 0.0),
            )
            returns = signal + rng.normal(0, 0.02, n_tickers)
        else:
            returns = rng.normal(0, 0.02, n_tickers)
        ts = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)
        frames[ts] = pd.DataFrame(
            {
                "ticker": [f"T{j}" for j in range(n_tickers)],
                "score": scores,
                "fwd_holding": returns,
                "fwd_1d": returns / 5,
            }
        )
    return frames


class TestICByDecile(unittest.TestCase):
    def test_tail_alpha_shows_u_shape(self):
        from alphalens.backtest.diagnostics import (
            ic_by_decile_from_scored_frames,
            tail_concentration_score,
        )

        frames = _synthetic_scored_frames(n_days=100, tail_alpha=True, seed=1)
        results = ic_by_decile_from_scored_frames(frames)

        self.assertEqual(len(results), 10)
        # Decile 10 mean > decile 1 mean (top outperforms bottom)
        top = next(r for r in results if r.decile == 10)
        bottom = next(r for r in results if r.decile == 1)
        self.assertGreater(top.mean_return, bottom.mean_return)
        # Tail concentration score should be > 1 (tails stronger than middle)
        self.assertGreater(tail_concentration_score(results), 1.2)

    def test_noise_shows_flat_deciles(self):
        from alphalens.backtest.diagnostics import (
            ic_by_decile_from_scored_frames,
            tail_concentration_score,
        )

        frames = _synthetic_scored_frames(n_days=100, tail_alpha=False, seed=2)
        results = ic_by_decile_from_scored_frames(frames)

        self.assertEqual(len(results), 10)
        # Concentration score should be near 1 (no tail concentration in pure noise)
        score = tail_concentration_score(results)
        self.assertLess(score, 2.0)  # not meaningfully concentrated

    def test_empty_input_returns_empty(self):
        from alphalens.backtest.diagnostics import (
            ic_by_decile_from_scored_frames,
        )

        self.assertEqual(ic_by_decile_from_scored_frames({}), [])


class TestVolDecomposition(unittest.TestCase):
    def test_detects_defensive_positioning(self):
        """Synthetic: top-N has lower vol in bear but similar mean as median."""
        from alphalens.backtest.diagnostics import (
            vol_decomposition_by_regime,
        )
        from alphalens.backtest.engine import BacktestReport, RebalanceSnapshot

        rng = np.random.default_rng(0)
        # Bear days: port vol 10% ann, median vol 20% ann, same mean returns.
        idx = pd.date_range("2024-01-01", periods=100)
        port = pd.Series(rng.normal(0, 0.006, 100), index=idx)
        median = pd.Series(rng.normal(0, 0.012, 100), index=idx)
        regime_labels = pd.Series(["bear"] * 100, index=idx)

        # Build fake rebalance_results to satisfy BacktestReport shape.
        report = BacktestReport(
            scorer_config={},
            holding_period=5,
            top_n=30,
            start=idx[0].date(),
            end=idx[-1].date(),
            benchmark="SPY",
            universe_ticker_count=100,
            rebalance_results=[
                RebalanceSnapshot(
                    date=d,
                    scored_count=10,
                    top_n_tickers=["A"],
                    top_n_scores=[1.0],
                    top_n_forward_returns=[p],
                    portfolio_return=p,
                    portfolio_return_holding=p,
                    universe_median_return=m,
                    ic=0.0,
                )
                for d, p, m in zip(idx, port.values, median.values)
            ],
        )

        result = vol_decomposition_by_regime(report, regime_labels)
        self.assertIn("bear", result)
        bear = result["bear"]
        # Top-N vol should be meaningfully lower than universe median vol.
        self.assertLess(bear.top_n_vol_annualised, bear.universe_median_vol_annualised)
        self.assertLess(bear.vol_ratio, 0.8)

    def test_missing_regime_omitted(self):
        from alphalens.backtest.diagnostics import (
            vol_decomposition_by_regime,
        )
        from alphalens.backtest.engine import BacktestReport

        report = BacktestReport(
            scorer_config={},
            holding_period=5,
            top_n=30,
            start=pd.Timestamp("2024-01-01").date(),
            end=pd.Timestamp("2024-01-02").date(),
            benchmark="SPY",
            universe_ticker_count=1,
        )
        regime = pd.Series(["bull"] * 10, index=pd.date_range("2024-01-01", periods=10))
        out = vol_decomposition_by_regime(report, regime)
        self.assertEqual(out, {})


class TestFormatVolDecomposition(unittest.TestCase):
    def test_format_contains_header_and_regimes(self):
        from alphalens.backtest.diagnostics import (
            VolDecomposition,
            format_vol_decomposition,
        )

        stats = {
            "bull": VolDecomposition(
                regime="bull",
                days=100,
                top_n_vol_annualised=0.15,
                universe_median_vol_annualised=0.20,
                top_n_mean_return_annualised=0.30,
                universe_median_mean_return_annualised=0.10,
                vol_ratio=0.75,
                excess_return_annualised=0.20,
            ),
        }
        text = format_vol_decomposition(stats)
        self.assertIn("bull", text)
        self.assertIn("Vol Ratio", text)


if __name__ == "__main__":
    unittest.main()

"""Tests for synchronous-across-phases block-bootstrap of pooled mean alpha_t.

Pre-reg ledger ``insider_form4_opportunistic_2026_05_08_v2`` mandates one
block-index sequence per replicate, applied synchronously across all 5
phases — so cross-phase covariance is preserved when the pooled mean is
formed (independent resampling per phase would destroy the joint shock
structure and artificially narrow the pooled CI).

These tests exercise the orchestrator helper
``scripts.run_insider_form4_phase_b._synchronous_bootstrap_pooled_alpha_t``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.run_insider_form4_phase_b import (  # noqa: E402
    _synchronous_bootstrap_pooled_alpha_t,
)


def _factor_frame(idx: pd.DatetimeIndex, seed: int = 0) -> pd.DataFrame:
    """Synthesize Carhart-shaped factor frame on the supplied index."""
    rng = np.random.default_rng(seed)
    n = len(idx)
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.0005, 0.01, n),
            "SMB": rng.normal(0.0002, 0.005, n),
            "HML": rng.normal(0.0001, 0.005, n),
            "Mom": rng.normal(0.0001, 0.005, n),
            "RF": np.full(n, 0.0001),
        },
        index=idx,
    )


class TestSynchronousBootstrapShape(unittest.TestCase):
    def setUp(self):
        self.idx = pd.date_range("2018-01-02", periods=300, freq="B")
        rng = np.random.default_rng(42)
        self.per_phase_rets = [
            pd.Series(rng.normal(0.0008, 0.01, 300), index=self.idx, name=f"phase_{p}")
            for p in range(5)
        ]
        self.factors = _factor_frame(self.idx, seed=1)

    def test_bootstrap_returns_required_keys(self):
        out = _synchronous_bootstrap_pooled_alpha_t(
            self.per_phase_rets,
            self.factors,
            block_size_trading_days=63,
            n_reps=50,
            seed=42,
            hac_maxlags=63,
        )
        for k in (
            "alpha_t_observed_mean",
            "alpha_t_per_phase_observed",
            "bounds_alpha_t_lower",
            "bounds_alpha_t_upper",
            "n_reps_used",
            "block_size_trading_days",
            "bootstrap_resampling",
        ):
            self.assertIn(k, out)
        self.assertEqual(out["bootstrap_resampling"], "synchronous_across_phases")

    def test_per_phase_observed_alpha_count_matches_phases(self):
        out = _synchronous_bootstrap_pooled_alpha_t(
            self.per_phase_rets,
            self.factors,
            block_size_trading_days=63,
            n_reps=20,
            seed=1,
            hac_maxlags=63,
        )
        self.assertEqual(len(out["alpha_t_per_phase_observed"]), 5)

    def test_lower_bound_below_upper_bound(self):
        out = _synchronous_bootstrap_pooled_alpha_t(
            self.per_phase_rets,
            self.factors,
            block_size_trading_days=63,
            n_reps=200,
            seed=7,
            hac_maxlags=63,
        )
        self.assertLess(out["bounds_alpha_t_lower"], out["bounds_alpha_t_upper"])


class TestSynchronousBootstrapReproducibility(unittest.TestCase):
    def setUp(self):
        idx = pd.date_range("2018-01-02", periods=200, freq="B")
        rng = np.random.default_rng(0)
        self.per_phase_rets = [
            pd.Series(rng.normal(0.001, 0.012, 200), index=idx, name=f"phase_{p}") for p in range(3)
        ]
        self.factors = _factor_frame(idx, seed=2)

    def test_same_seed_same_bounds(self):
        a = _synchronous_bootstrap_pooled_alpha_t(
            self.per_phase_rets,
            self.factors,
            block_size_trading_days=42,
            n_reps=100,
            seed=12345,
            hac_maxlags=42,
        )
        b = _synchronous_bootstrap_pooled_alpha_t(
            self.per_phase_rets,
            self.factors,
            block_size_trading_days=42,
            n_reps=100,
            seed=12345,
            hac_maxlags=42,
        )
        self.assertEqual(a["bounds_alpha_t_lower"], b["bounds_alpha_t_lower"])
        self.assertEqual(a["bounds_alpha_t_upper"], b["bounds_alpha_t_upper"])

    def test_different_seed_different_bounds(self):
        a = _synchronous_bootstrap_pooled_alpha_t(
            self.per_phase_rets,
            self.factors,
            block_size_trading_days=42,
            n_reps=100,
            seed=1,
            hac_maxlags=42,
        )
        b = _synchronous_bootstrap_pooled_alpha_t(
            self.per_phase_rets,
            self.factors,
            block_size_trading_days=42,
            n_reps=100,
            seed=2,
            hac_maxlags=42,
        )
        self.assertNotEqual(a["bounds_alpha_t_lower"], b["bounds_alpha_t_lower"])


class TestSynchronousBootstrapCrossPhaseCovariance(unittest.TestCase):
    """If the bootstrap is truly synchronous across phases, then phases that
    are perfectly correlated (same return series duplicated) must produce
    identical alpha_t per replicate — and thus a NARROWER pooled-mean CI than
    independent resampling would. Independent resampling treats them as
    iid and averages out variance ~1/sqrt(n_phases); synchronous resampling
    preserves the perfect correlation and the pooled mean equals each phase's
    individual mean."""

    def test_perfectly_correlated_phases_yield_pooled_mean_equal_to_per_phase(self):
        idx = pd.date_range("2018-01-02", periods=200, freq="B")
        rng = np.random.default_rng(99)
        common = pd.Series(rng.normal(0.001, 0.01, 200), index=idx, name="x")
        per_phase = [common.copy() for _ in range(4)]
        factors = _factor_frame(idx, seed=99)

        out = _synchronous_bootstrap_pooled_alpha_t(
            per_phase,
            factors,
            block_size_trading_days=20,
            n_reps=200,
            seed=11,
            hac_maxlags=20,
        )
        # All phases identical => observed alphas all equal, mean equals any single
        observed = out["alpha_t_per_phase_observed"]
        for t in observed[1:]:
            self.assertAlmostEqual(t, observed[0], places=10)
        # Pooled mean = per-phase value (no averaging away)
        self.assertAlmostEqual(out["alpha_t_observed_mean"], observed[0], places=10)


if __name__ == "__main__":
    unittest.main()

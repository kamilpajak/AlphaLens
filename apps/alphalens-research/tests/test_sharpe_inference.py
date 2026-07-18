"""Tests for block-bootstrap Sharpe-difference inference.

Used by overlay-class success metric per ADR 0007. Covers:
  - reproducibility under fixed seed
  - degenerate-input handling (NaN, length mismatch, zero std)
  - block-bootstrap correctness on synthetic IID data (recovers analytic Sharpe)
  - paired sampling preserves cross-correlation
  - sanity: identical input series → diff exactly 0, p ~ 0.5
"""

from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

import numpy as np
import pandas as pd
from alphalens_research.backtest import sharpe_inference
from alphalens_research.backtest.sharpe_inference import (
    SharpeDiffResult,
    _circular_block_bootstrap_indices,
    _sharpe,
    block_bootstrap_sharpe_diff,
)


class InputValidationTests(unittest.TestCase):
    def test_rejects_length_mismatch(self):
        a = np.zeros(100)
        b = np.zeros(99)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52)

    def test_rejects_2d_input(self):
        a = np.zeros((10, 2))
        b = np.zeros((10, 2))
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52)

    def test_rejects_too_short(self):
        a = np.array([0.01])
        b = np.array([0.02])
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52)

    def test_rejects_nans(self):
        a = np.array([0.01, np.nan, 0.02])
        b = np.array([0.01, 0.01, 0.01])
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52)

    def test_rejects_invalid_block_size(self):
        a = np.zeros(50)
        b = np.zeros(50)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, block_size=0)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, block_size=51)

    def test_rejects_low_n_bootstrap(self):
        a = np.random.default_rng(0).normal(size=100)
        b = np.random.default_rng(1).normal(size=100)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, n_bootstrap=50)

    def test_rejects_invalid_confidence(self):
        a = np.random.default_rng(0).normal(size=100)
        b = np.random.default_rng(1).normal(size=100)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, confidence=0.5)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, confidence=1.0)


class ReproducibilityTests(unittest.TestCase):
    def test_same_seed_same_result(self):
        rng = np.random.default_rng(0)
        a = rng.normal(loc=0.001, scale=0.01, size=300)
        b = rng.normal(loc=0.0, scale=0.01, size=300)
        r1 = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=42
        )
        r2 = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=42
        )
        self.assertEqual(r1.t_stat, r2.t_stat)
        self.assertEqual(r1.p_value_one_sided, r2.p_value_one_sided)
        self.assertEqual(r1.bootstrap_se, r2.bootstrap_se)


class IdenticalSeriesTests(unittest.TestCase):
    def test_zero_diff_exactly_when_a_equals_b(self):
        rng = np.random.default_rng(7)
        a = rng.normal(loc=0.001, scale=0.01, size=300)
        b = a.copy()
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertEqual(result.sharpe_diff, 0.0)
        # se should be ~0 because diffs are identically 0 across bootstraps
        # (same indices for both → same diff every resample). t_stat is NaN.
        self.assertEqual(result.bootstrap_se, 0.0)
        self.assertTrue(math.isnan(result.t_stat))

    def test_identical_series_p_value_is_exactly_one(self):
        # Identical inputs make every bootstrap diff exactly 0.0, so the
        # fraction with diff <= 0 must be exactly 1.0. Kills `<=`->`<` on
        # the p-value threshold (strict < would give 0.0 instead).
        rng_data = np.random.default_rng(7)
        a = rng_data.normal(0.001, 0.01, size=20)
        result = block_bootstrap_sharpe_diff(
            a,
            a.copy(),
            periods_per_year=252,
            block_size=4,
            n_bootstrap=100,
            seed=0,
        )
        self.assertEqual(result.sharpe_diff, 0.0)
        self.assertEqual(result.p_value_one_sided, 1.0)


class SharpeRecoveryTests(unittest.TestCase):
    def test_observed_sharpe_matches_analytic(self):
        """Analytic Sharpe of a synthetic series should match the result's
        sharpe_a/sharpe_b fields exactly."""
        rng = np.random.default_rng(0)
        a = rng.normal(loc=0.001, scale=0.01, size=500)
        b = rng.normal(loc=0.0, scale=0.01, size=500)
        ppy = 252
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=ppy, block_size=21, n_bootstrap=200, seed=0
        )
        analytic_a = a.mean() / a.std(ddof=1) * math.sqrt(ppy)
        analytic_b = b.mean() / b.std(ddof=1) * math.sqrt(ppy)
        self.assertAlmostEqual(result.sharpe_a, analytic_a, places=10)
        self.assertAlmostEqual(result.sharpe_b, analytic_b, places=10)
        self.assertAlmostEqual(result.sharpe_diff, analytic_a - analytic_b, places=10)


class PairedSamplingTests(unittest.TestCase):
    def test_strongly_positive_diff_yields_low_p(self):
        """If a strictly dominates b, p-value should be very small."""
        rng = np.random.default_rng(0)
        n = 500
        # b is just IID noise; a = b + significant positive drift.
        b = rng.normal(loc=0.0, scale=0.01, size=n)
        a = b + 0.005  # +0.5% deterministic edge per period (huge)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertGreater(result.sharpe_diff, 0)
        self.assertLess(result.p_value_one_sided, 0.01)
        self.assertGreater(result.t_stat, 2.0)

    def test_strongly_negative_diff_yields_high_p(self):
        """If b dominates a, p-value for HA: a > b should be high."""
        rng = np.random.default_rng(0)
        n = 500
        b = rng.normal(loc=0.001, scale=0.01, size=n)
        a = b - 0.005  # a is worse
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertLess(result.sharpe_diff, 0)
        self.assertGreater(result.p_value_one_sided, 0.9)


class CIBoundsTests(unittest.TestCase):
    def test_ci_brackets_zero_when_diff_indistinguishable(self):
        """Two IID series with same mu/sigma — 95% CI should bracket 0."""
        rng = np.random.default_rng(0)
        a = rng.normal(loc=0.0005, scale=0.01, size=300)
        b = rng.normal(loc=0.0005, scale=0.01, size=300)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        # CI should bracket 0 (since true diff = 0).
        self.assertLessEqual(result.ci_lower, 0.0)
        self.assertGreaterEqual(result.ci_upper, 0.0)

    def test_ci_excludes_zero_when_diff_strong(self):
        rng = np.random.default_rng(0)
        n = 500
        b = rng.normal(loc=0.0, scale=0.01, size=n)
        a = b + 0.003  # strong positive edge
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertGreater(result.ci_lower, 0.0)


class ResultShapeTests(unittest.TestCase):
    def test_returns_dataclass_with_expected_fields(self):
        rng = np.random.default_rng(0)
        a = rng.normal(size=100)
        b = rng.normal(size=100)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=5, n_bootstrap=200, seed=0
        )
        self.assertIsInstance(result, SharpeDiffResult)
        self.assertEqual(result.n_obs, 100)
        self.assertEqual(result.block_size, 5)
        self.assertGreater(result.n_bootstrap, 0)


class PandasSeriesAcceptanceTests(unittest.TestCase):
    def test_accepts_pd_series(self):
        rng = np.random.default_rng(0)
        idx = pd.date_range("2020-01-01", periods=100, freq="W-MON")
        a = pd.Series(rng.normal(size=100), index=idx)
        b = pd.Series(rng.normal(size=100), index=idx)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=5, n_bootstrap=200, seed=0
        )
        self.assertIsInstance(result, SharpeDiffResult)


class BoundaryValidationHardeningTests(unittest.TestCase):
    """Pin the EXACT accept/reject edge of every validation guard so a
    `<`<->`<=` mutation (which shifts the boundary by one) is caught. The
    existing tests only reject well past the edge (n=1, block_size=0/51,
    n_bootstrap=50, confidence=0.5/1.0), leaving the boundary itself untested.
    """

    def _ab(self, n: int):
        rng = np.random.default_rng(0)
        return rng.normal(size=n), rng.normal(size=n)

    def test_exactly_two_observations_is_accepted(self):
        # `n < 2` raises: n=2 must be ACCEPTED (kills `<`->`<=`).
        a, b = self._ab(2)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=1, n_bootstrap=100, seed=0
        )
        self.assertEqual(result.n_obs, 2)

    def test_block_size_one_is_accepted(self):
        # `1 <= block_size`: block_size=1 must be accepted (kills `<=`->`<`).
        a, b = self._ab(50)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=1, n_bootstrap=100, seed=0
        )
        self.assertEqual(result.block_size, 1)

    def test_block_size_equal_to_n_is_accepted(self):
        # `block_size <= n`: block_size==n must be accepted (kills `<=`->`<`).
        a, b = self._ab(40)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=40, n_bootstrap=100, seed=0
        )
        self.assertEqual(result.block_size, 40)

    def test_block_size_one_past_n_is_rejected(self):
        a, b = self._ab(40)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, block_size=41, n_bootstrap=100)

    def test_shorter_first_series_is_rejected(self):
        # `a.shape != b.shape`: a SHORTER than b must raise. Tuple `>` would
        # read (99,) > (100,) as False and silently proceed on mismatched
        # lengths — the paired-sampling contract needs exact equality.
        a, b = self._ab(99)[0], self._ab(100)[1]
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, block_size=5, n_bootstrap=100)

    def test_signature_defaults_are_applied_when_omitted(self):
        # block_size / n_bootstrap defaults (21 / 10_000) are never exercised
        # when every call passes them explicitly. Call with only the required
        # args and assert the defaults took effect (kills NumberReplacer on the
        # signature defaults).
        a, b = self._ab(60)
        result = block_bootstrap_sharpe_diff(a, b, periods_per_year=252)
        self.assertEqual(result.block_size, 21)
        # non-degenerate input → every default resample is valid.
        self.assertEqual(result.n_bootstrap, 10_000)

    def test_n_bootstrap_exactly_100_is_accepted(self):
        # `n_bootstrap < 100` raises: exactly 100 must be accepted (kills `<`->`<=`).
        a, b = self._ab(50)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=5, n_bootstrap=100, seed=0
        )
        # valid resamples ≤ requested; with non-degenerate input all 100 are valid.
        self.assertEqual(result.n_bootstrap, 100)

    def test_n_bootstrap_ninety_nine_is_rejected(self):
        a, b = self._ab(50)
        with self.assertRaises(ValueError):
            block_bootstrap_sharpe_diff(a, b, periods_per_year=52, block_size=5, n_bootstrap=99)

    def test_confidence_just_inside_bounds_is_accepted(self):
        # `0.5 < confidence < 1.0`: values just inside must be accepted (kills
        # the strict `<` becoming `<=` or the bound flipping).
        a, b = self._ab(80)
        for conf in (0.5001, 0.9999):
            result = block_bootstrap_sharpe_diff(
                a, b, periods_per_year=52, block_size=5, n_bootstrap=100, confidence=conf, seed=0
            )
            self.assertLessEqual(result.ci_lower, result.ci_upper)


class SharpeGuardHardeningTests(unittest.TestCase):
    """Pin the zero/degenerate-std guards in _sharpe and the bootstrap valid
    mask — the sigma<=0 branch and the all-degenerate RuntimeError were never
    exercised (recovery/paired tests all use non-degenerate input)."""

    def test_sharpe_of_constant_series_is_nan(self):
        # Direct unit test of the _sharpe helper: a constant series has std
        # exactly 0 (0.5 is exact in binary float) → the `sigma <= 0.0` guard
        # must return NaN, not divide by ~0 into a huge value. Kills `<=`->`<`
        # and dropping the isfinite/guard branch. (This path is unreachable via
        # the public fn — a constant series makes the whole bootstrap degenerate
        # and raises before the observed Sharpe is reported.)
        self.assertTrue(math.isnan(_sharpe(np.full(50, 0.5), 252)))

    def test_sharpe_matches_closed_form_exactly(self):
        # Pin the exact formula mean/std(ddof=1)*sqrt(ppy) so operator mutations
        # (*->/ on the annualiser, ddof change, mean/std swap) die deterministically.
        r = np.array([0.01, -0.02, 0.03, 0.00, 0.015])
        ppy = 252
        expected = float(np.mean(r)) / float(np.std(r, ddof=1)) * math.sqrt(ppy)
        self.assertAlmostEqual(_sharpe(r, ppy), expected, places=12)

    def test_all_degenerate_resamples_raise_runtime_error(self):
        # Both series constant → every resample has zero std for a and b →
        # valid mask empty → RuntimeError (kills the `valid.any()` guard and
        # the `(std_a > 0) & (std_b > 0)` `>`->`>=` mutation).
        a = np.full(200, 0.5)  # exact-float constants → std exactly 0
        b = np.full(200, 1.0)
        with self.assertRaises(RuntimeError):
            block_bootstrap_sharpe_diff(
                a, b, periods_per_year=252, block_size=21, n_bootstrap=200, seed=0
            )

    def test_sharpe_nan_guard_covers_negative_sigma_not_only_exact_zero(self):
        """The sigma guard is `sigma <= 0.0`, not `sigma == 0.0`.

        np.std on a real float array is mathematically non-negative, so the
        negative branch is pinned by forcing np.std (as seen by the module)
        to return a finite negative value: the original guard must map it to
        NaN, while an `== 0.0` mutant would return a finite Sharpe
        (mu / -1.0 * sqrt(252) = -0.3174901573277509 for this input).
        Kills `<=`->`==` on the sigma guard; the exact-zero boundary is
        pinned by test_sharpe_of_constant_series_is_nan above.
        """
        returns = np.array([0.01, 0.02, 0.03])
        with mock.patch.object(sharpe_inference.np, "std", return_value=-1.0):
            result = _sharpe(returns, 252)
        self.assertTrue(math.isnan(result))


class ValidMaskConjunctionTests(unittest.TestCase):
    """Pin the per-resample valid mask `(std_a > 0) & (std_b > 0)` — it must
    be a strict-positivity CONJUNCTION over BOTH series. One-sided degenerate
    inputs (only a constant, or only b constant) and NaN-std inputs (inf
    poisoning a resample) discriminate `&`->`|`/`+`, per-side `>`->`>=`, and
    `>`->`!=` mutations that the both-sides-constant test above cannot see."""

    def test_constant_a_with_varying_b_still_raises_runtime_error(self):
        # A constant returns_a gives std exactly 0 in EVERY resample while
        # returns_b keeps positive std in most rows. The whole call must be
        # degenerate and raise. Kills `&`->`|`/`+` on the mask and
        # `std_a > 0` -> `std_a >= 0` (which would admit the b-only-valid
        # rows and return a garbage result instead of raising).
        a = np.full(4, 0.01)
        b = np.array([0.01, 0.02, -0.01, 0.03])
        with self.assertRaises(RuntimeError):
            block_bootstrap_sharpe_diff(
                a, b, periods_per_year=252, block_size=1, n_bootstrap=100, seed=0
            )

    def test_constant_b_with_varying_a_still_raises_runtime_error(self):
        # Mirror of the constant-a case: a constant returns_b gives std
        # exactly 0 in EVERY resample, so no resample may count as valid
        # regardless of returns_a. Kills `std_b > 0` -> `std_b >= 0`.
        a = np.array([0.01, 0.02, -0.01, 0.03])
        b = np.full(4, 0.01)
        with self.assertRaises(RuntimeError):
            block_bootstrap_sharpe_diff(
                a, b, periods_per_year=252, block_size=1, n_bootstrap=100, seed=0
            )

    def test_inf_in_a_never_counts_as_valid_resample(self):
        # np.inf passes the NaN input guard but poisons every resample that
        # contains it: its std is NaN, which must FAIL the `std_a > 0` check
        # (NaN > 0 is False). With n=2 and one inf, every resample has std
        # of either 0 (constant row) or NaN, so the call must raise. Kills
        # `std_a > 0` -> `std_a != 0` (NaN != 0 is True, admitting NaN-std
        # rows and returning a garbage result instead of raising).
        a = np.array([0.01, np.inf])
        b = np.array([0.01, 0.02])
        # errstate: inf-inf inside np.std legitimately warns before the raise.
        with np.errstate(invalid="ignore"), self.assertRaises(RuntimeError):
            block_bootstrap_sharpe_diff(
                a, b, periods_per_year=252, block_size=1, n_bootstrap=100, seed=0
            )

    def test_inf_in_b_never_counts_as_valid_resample(self):
        # Mirror of the inf-in-a case for the `std_b > 0` term: np.inf in
        # returns_b passes the NaN input guard, every resample std for b is
        # 0 or NaN, and the call must raise. Kills `std_b > 0` -> `std_b != 0`.
        a = np.array([0.01, 0.02])
        b = np.array([0.01, np.inf])
        # errstate: inf-inf inside np.std legitimately warns before the raise.
        with np.errstate(invalid="ignore"), self.assertRaises(RuntimeError):
            block_bootstrap_sharpe_diff(
                a, b, periods_per_year=252, block_size=1, n_bootstrap=100, seed=0
            )


class BootstrapIndexMatrixTests(unittest.TestCase):
    """Pin the exact circular-block index matrix `_circular_block_bootstrap_indices`
    builds for a fixed seed — kills ceil-div operator, integers lower-bound,
    offset-grid add, and modulo-wrap mutations that a downstream statistical
    assertion could absorb."""

    def test_index_matrix_exact_for_seed_zero(self):
        # Reconstructs the documented algorithm independently: ceil(n_obs /
        # block_size) blocks per row (hard-coded 4 for 10/3), starts drawn
        # from integers[0, n_obs), consecutive offsets added, modulo-n_obs
        # wrap, truncation to n_obs columns. n_obs=10 with block_size=3 is
        # deliberately non-divisible so the ceil-div matters; n_bootstrap>1
        # desynchronises the RNG stream on later rows for mutants that draw
        # a different number of start positions.
        n_obs, block_size, n_bootstrap = 10, 3, 8
        idx = _circular_block_bootstrap_indices(
            n_obs, block_size, n_bootstrap, np.random.default_rng(0)
        )
        # Independent reconstruction: ceil(10 / 3) == 4 blocks per row.
        n_blocks = 4
        starts = np.random.default_rng(0).integers(low=0, high=n_obs, size=(n_bootstrap, n_blocks))
        expected = (starts[:, :, None] + np.arange(block_size)[None, None, :]) % n_obs
        expected = expected.reshape(n_bootstrap, n_blocks * block_size)[:, :n_obs]
        self.assertEqual(idx.shape, (n_bootstrap, n_obs))
        np.testing.assert_array_equal(idx, expected)


class SignatureContractTests(unittest.TestCase):
    """Pin the public signature contract of block_bootstrap_sharpe_diff — the
    keyword-only `*` marker and the seed default — which no behavioural test
    exercises (every existing call passes seed positionally-by-keyword)."""

    @staticmethod
    def _ab():
        rng = np.random.default_rng(42)
        a = rng.normal(loc=0.001, scale=0.01, size=60)
        b = rng.normal(loc=0.0005, scale=0.01, size=60)
        return a, b

    def test_signature_keyword_only_marker(self):
        # returns_a/returns_b accept keyword form and every parameter after
        # the `*` marker is keyword-only. A mutation replacing the
        # keyword-only marker `*` with the positional-only marker `/` flips
        # both behaviors.
        a, b = self._ab()
        result = block_bootstrap_sharpe_diff(
            returns_a=a,
            returns_b=b,
            periods_per_year=252,
            block_size=5,
            n_bootstrap=200,
            seed=0,
        )
        self.assertTrue(math.isfinite(result.sharpe_diff))
        # periods_per_year is keyword-only: positional form must be rejected.
        with self.assertRaises(TypeError):
            block_bootstrap_sharpe_diff(a, b, 252)  # type: ignore[misc]

    def test_default_seed_is_zero(self):
        # Omitting `seed` must reproduce an explicit `seed=0` run exactly.
        # A mutated default (e.g. seed=1) consumes a different rng stream and
        # produces a different result object, which the cross-seed inequality
        # below confirms is detectable (kills NumberReplacer on the default).
        a, b = self._ab()
        common = {"periods_per_year": 252, "block_size": 5, "n_bootstrap": 200}
        r_default = block_bootstrap_sharpe_diff(a, b, **common)
        r_seed0 = block_bootstrap_sharpe_diff(a, b, **common, seed=0)
        r_seed1 = block_bootstrap_sharpe_diff(a, b, **common, seed=1)
        self.assertEqual(r_default, r_seed0)
        self.assertNotEqual(r_seed0, r_seed1)


class GuardMessageSpecificityTests(unittest.TestCase):
    """Pin WHICH guard fires (by its own message) on inputs engineered so no
    later guard can mask a mutated comparison — the broad assertRaises tests
    above accept any ValueError from any guard, letting comparison-operator
    mutants slip through when a downstream guard raises instead."""

    def test_rejects_2d_input_with_guard_message(self):
        # Same-shape 2-D input passes the shape-mismatch guard, so the 1-D
        # guard must fire with its OWN message. 30 rows with block_size=5
        # ensures no later guard (block_size <= n) can mask a mutant that
        # skips the ndim check — such a mutant returns a result instead.
        # Kills `!=`->`<` on the ndim comparison.
        rng = np.random.default_rng(0)
        a = rng.normal(size=(30, 2))
        b = rng.normal(size=(30, 2))
        with self.assertRaisesRegex(ValueError, "1-D"):
            block_bootstrap_sharpe_diff(
                a, b, periods_per_year=252, block_size=5, n_bootstrap=100, seed=0
            )

    def test_rejects_scalar_zero_dim_input(self):
        # 0-D scalars share shape () so they pass the shape-mismatch guard;
        # the ndim guard must reject them with ValueError — not let them
        # fall through to a TypeError from len() of an unsized object.
        # Kills `!=`->`>` on the ndim comparison.
        with self.assertRaisesRegex(ValueError, "1-D"):
            block_bootstrap_sharpe_diff(0.01, 0.02, periods_per_year=252)  # type: ignore[arg-type]

    def test_single_observation_rejected_by_n_guard_itself(self):
        # n=1 must be rejected by the n<2 guard with its OWN message.
        # block_size=1 and n_bootstrap=100 are otherwise valid so no later
        # ValueError guard can mask a mutant that lets n=1 through (which
        # would instead die downstream as a degenerate-std RuntimeError).
        # Kills NumberReplacer on the `n < 2` bound.
        with self.assertRaisesRegex(ValueError, "observations"):
            block_bootstrap_sharpe_diff(
                np.array([0.01]),
                np.array([0.02]),
                periods_per_year=252,
                block_size=1,
                n_bootstrap=100,
                seed=0,
            )

    def test_nan_in_only_one_series_is_rejected(self):
        # NaN in exactly ONE of the two series must raise: the guard is OR,
        # not AND. All other parameters are valid so nothing downstream can
        # mask a mutant that requires NaN in both series (kills `or`->`and`).
        clean = np.random.default_rng(0).normal(size=30)
        dirty = clean.copy()
        dirty[7] = np.nan
        with self.assertRaisesRegex(ValueError, "NaN"):
            block_bootstrap_sharpe_diff(
                dirty, clean, periods_per_year=252, block_size=5, n_bootstrap=100, seed=0
            )
        with self.assertRaisesRegex(ValueError, "NaN"):
            block_bootstrap_sharpe_diff(
                clean, dirty, periods_per_year=252, block_size=5, n_bootstrap=100, seed=0
            )

    def test_confidence_outside_both_ends_rejected_with_guard_message(self):
        # confidence=0.3 (below the open lower bound) and confidence=1.5
        # (above the open upper bound) must each be rejected by the
        # confidence guard ITSELF (its own message) — not accepted, and not
        # rejected later by np.quantile's out-of-range ValueError whose
        # message differs. Kills `<`->`!=` on either leg of the chained
        # comparison `0.5 < confidence < 1.0`.
        rng = np.random.default_rng(0)
        a = rng.normal(size=80)
        b = rng.normal(size=80)
        for conf in (0.3, 1.5):
            with self.assertRaisesRegex(ValueError, "confidence must be"):
                block_bootstrap_sharpe_diff(
                    a,
                    b,
                    periods_per_year=252,
                    block_size=5,
                    n_bootstrap=100,
                    confidence=conf,
                    seed=0,
                )


class IndependentReplicationTests(unittest.TestCase):
    """Replicate the whole bootstrap pipeline independently (same seed, same
    index machinery) and pin every derived statistic bit-exactly — kills
    arithmetic-operator and constant mutations on the SE, t-stat, p-value
    threshold, and CI quantile levels that range/ordering invariants absorb."""

    def test_bootstrap_se_and_t_stat_match_independent_replication(self):
        # Pins bootstrap_se == np.std(diffs, ddof=1) and t_stat ==
        # sharpe_diff / se exactly. Kills ddof=0/2 mutations on the SE and
        # `/`->`-`/`+`/`//` mutations on the t-stat.
        rng_data = np.random.default_rng(42)
        a = rng_data.normal(loc=0.001, scale=0.01, size=16)
        b = rng_data.normal(loc=0.0005, scale=0.012, size=16)
        ppy = 252
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=ppy, block_size=3, n_bootstrap=100, seed=7
        )
        rng = np.random.default_rng(7)
        idx = _circular_block_bootstrap_indices(16, 3, 100, rng)
        a_res, b_res = a[idx], b[idx]
        ann = math.sqrt(ppy)
        std_a = a_res.std(axis=1, ddof=1)
        std_b = b_res.std(axis=1, ddof=1)
        # Continuous data: every resample is valid, so no rows are dropped.
        self.assertTrue(((std_a > 0) & (std_b > 0)).all())
        diffs = (a_res.mean(axis=1) / std_a) * ann - (b_res.mean(axis=1) / std_b) * ann
        expected_se = float(np.std(diffs, ddof=1))
        expected_t = (_sharpe(a, ppy) - _sharpe(b, ppy)) / expected_se
        self.assertAlmostEqual(result.bootstrap_se, expected_se, places=12)
        self.assertAlmostEqual(result.t_stat, expected_t, places=12)

    def test_p_value_and_ci_match_independent_reference(self):
        # Pins the p_one threshold `(diffs <= 0.0)` and both percentile-CI
        # quantile levels exactly. Kills mutants that shift the p-value
        # cutoff (diffs <= 1.0 / <= -1.0) or the CI quantile levels
        # (alpha/2 mutated to alpha, alpha**2, alpha//2, alpha/3, alpha%2
        # on either tail).
        rng_data = np.random.default_rng(42)
        a = rng_data.normal(0.001, 0.01, size=30)
        b = rng_data.normal(0.0, 0.012, size=30)
        result = block_bootstrap_sharpe_diff(
            a,
            b,
            periods_per_year=252,
            block_size=5,
            n_bootstrap=100,
            confidence=0.95,
            seed=0,
        )

        # Independent reference recompute mirroring the documented pipeline.
        rng = np.random.default_rng(0)
        idx = _circular_block_bootstrap_indices(30, 5, 100, rng)
        a_res, b_res = a[idx], b[idx]
        std_a = a_res.std(axis=1, ddof=1)
        std_b = b_res.std(axis=1, ddof=1)
        valid = (std_a > 0) & (std_b > 0)
        ann = math.sqrt(252)
        diffs = (a_res.mean(axis=1)[valid] / std_a[valid]) * ann - (
            b_res.mean(axis=1)[valid] / std_b[valid]
        ) * ann

        alpha = 1.0 - 0.95
        self.assertEqual(result.p_value_one_sided, float((diffs <= 0.0).mean()))
        self.assertEqual(result.ci_lower, float(np.quantile(diffs, alpha / 2.0)))
        self.assertEqual(result.ci_upper, float(np.quantile(diffs, 1.0 - alpha / 2.0)))
        # Guard: this seed puts bootstrap diffs on both sides of 0 within
        # (-1, 1), so shifted thresholds (<= 1.0, <= -1.0) cannot coincide
        # with <= 0.0.
        self.assertTrue(((diffs > 0.0) & (diffs <= 1.0)).any())
        self.assertTrue(((diffs > -1.0) & (diffs <= 0.0)).any())


class CIAndPValueOrderingTests(unittest.TestCase):
    """Pin the CI quantile ordering + p-value range invariants that hold
    regardless of the RNG draw, catching quantile-arg and threshold mutations
    that break the ordering or push the statistic out of range."""

    def test_ci_lower_never_exceeds_ci_upper(self):
        # `alpha/2` vs `1 - alpha/2`: swapping or mutating the quantile args
        # would invert the bounds. This invariant holds for any resample draw.
        rng = np.random.default_rng(3)
        a = rng.normal(loc=0.0008, scale=0.012, size=400)
        b = rng.normal(loc=0.0002, scale=0.011, size=400)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=800, seed=1
        )
        self.assertLessEqual(result.ci_lower, result.ci_upper)

    def test_tighter_confidence_gives_narrower_interval(self):
        # `alpha = 1 - confidence`: a higher confidence must widen the CI.
        # Kills mutations that drop the `1 - confidence` dependency (e.g.
        # a constant alpha) — the 99% band must contain the 80% band's width.
        rng = np.random.default_rng(5)
        a = rng.normal(loc=0.0008, scale=0.012, size=400)
        b = rng.normal(loc=0.0002, scale=0.011, size=400)
        common = {"periods_per_year": 252, "block_size": 21, "n_bootstrap": 1500, "seed": 2}
        wide = block_bootstrap_sharpe_diff(a, b, confidence=0.99, **common)
        narrow = block_bootstrap_sharpe_diff(a, b, confidence=0.80, **common)
        self.assertGreater(wide.ci_upper - wide.ci_lower, narrow.ci_upper - narrow.ci_lower)

    def test_p_value_is_a_probability_in_unit_interval(self):
        # `(diffs <= 0.0).mean()` is a fraction: must stay in [0, 1]. A
        # threshold mutation that inverts the comparison could push it out or
        # to a constant; pin the range + that a dominant edge lands near 0.
        rng = np.random.default_rng(0)
        n = 500
        b = rng.normal(loc=0.0, scale=0.01, size=n)
        a = b + 0.004
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=252, block_size=21, n_bootstrap=500, seed=0
        )
        self.assertGreaterEqual(result.p_value_one_sided, 0.0)
        self.assertLessEqual(result.p_value_one_sided, 1.0)
        self.assertLess(result.p_value_one_sided, 0.05)


class ResultImmutabilityTests(unittest.TestCase):
    def test_result_is_frozen(self):
        rng = np.random.default_rng(0)
        a, b = rng.normal(size=100), rng.normal(size=100)
        result = block_bootstrap_sharpe_diff(
            a, b, periods_per_year=52, block_size=5, n_bootstrap=100, seed=0
        )
        with self.assertRaises(FrozenInstanceError):
            result.p_value_one_sided = 0.0  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

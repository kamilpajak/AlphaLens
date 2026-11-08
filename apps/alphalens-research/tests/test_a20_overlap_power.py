"""The overlap-aware DGP and the size-before-power harness.

Hermetic: every case feeds synthetic arrays to pure functions.

The load-bearing case is ``TestTheOverlapIsActuallySimulated``. The defect this
whole module exists to fix is a simulator that draws one independent effect per
arrival session and then tests with a bootstrap clustered on arrival session —
it generates and tests under the same assumption, so no inference method applied
to its output can reveal the assumption is wrong. A new simulator that silently
failed to produce the overlap would repeat exactly that, and would look just as
healthy. So the sharing fraction has a zero control (reproduces the old
behaviour) and a one-session-horizon control (nothing left to share), and the
lag profile is asserted against a closed form rather than against "not zero".

Since 2026-11-07 this file carries a second load-bearing group, for the
feature-aligned noise the module now simulates:

- ``TestTheDefaultNumericPathDoesNotMove`` is the "a published figure moved"
  detector. It hashes TWO consecutive panels from one generator, because a
  single panel from a fresh generator is blind to a trailing extra draw —
  measured: inserting ``rng.normal(0.0, 1.0)`` at the end of the function leaves
  the first-panel digests green and turns all six second-panel digests red.
- ``TestThePhiZeroEquivalence`` replaces a merged test that FORBADE an
  ``alignment`` parameter here. The no-fork concern it answered is unchanged;
  the enforcement is stronger. The mechanism is imported from ``a20_power`` and
  the equivalence is pinned on the aligned path too, so an actual divergence
  between the two copies of this DGP goes red rather than a parameter name.
- ``TestTheAlignmentFixtureIsNotDegenerate`` pins the shapes on which alignment
  is an exact no-op, because every lag fixture here passes a CONSTANT signal
  column — where ``_z`` returns zeros and both levels scale by exactly 1.
"""

from __future__ import annotations

import contextlib
import io
import re
import unittest
from typing import Any

import numpy as np
from scripts.ml import a20_overlap_power as ov
from scripts.ml import a20_power
from scripts.ml.a20_power import _z

# The on-disk fixture pair is the merged preflight's, reused rather than copied:
# both drivers read the same two stores, and a second copy would drift.
from tests.test_a20_power_preflight import _Store

#: The pinned fixture for every bit-identity case below. Shared so a digest and
#: the generator-state check cannot drift onto two different panels.
_PIN_SIGNALS = np.random.default_rng(0).normal(0.0, 1.0, (50, 3))
_PIN_EFFECTS = {"atr": 0.2, "ma50": -0.1, "press": 0.05}
_PIN_SIZES = [3, 4, 2, 5]
_PIN_OFFSETS = [0, 1, 2, 3]
_PIN_ICC = 0.4
_PIN_SEED = 42
_PIN_HORIZON = 20

#: ``sha256(y.tobytes()).hexdigest()[:32]`` for the FIRST and the SECOND panel
#: drawn from one ``default_rng(42)``, keyed by ``(shared_fraction, kappa)``.
#:
#: Computed from the UNMODIFIED module at 53ed1171, BEFORE the alignment
#: mechanism was written. A digest taken from the edited module would pin the new
#: behaviour to itself and prove nothing.
#:
#: The SECOND panel is the assertion that matters. One panel read from a fresh
#: generator cannot see a trailing extra draw: inserting a spare
#: ``rng.normal(0.0, 1.0)`` after every real draw left all 47 merged tests green.
#: Only the next panel from the same generator moves.
#:
#: The two phi = 0 rows coincide because at phi = 0 the shared term is zero
#: whatever kappa does. That is a fact about the DGP, not a copy-paste.
_BASELINE_DIGESTS = {
    (0.0, 0.0): ("581c66776e7979a6eef206ce4467e47e", "a70fa62e091a887b6e470a25d2bea5e3"),
    (0.0, 0.142): ("581c66776e7979a6eef206ce4467e47e", "a70fa62e091a887b6e470a25d2bea5e3"),
    (0.5, 0.0): ("bc7fae3b8a7d0e9075059efe12604f45", "6fb28e589153df2469a4b2f9817b0149"),
    (0.5, 0.142): ("97db8ec198b30a7af6098882cc9e51ad", "195db6c329127889e45d412dad2b07f4"),
    (1.0, 0.0): ("111cd69c99d0076f9bc98f639a1afa87", "beedb3f549b3509717df6c7cae21bf02"),
    (1.0, 0.142): ("d414689d339662be02f751aca143383e", "b98f1f45c351964017eecf50aabe7eca"),
}

#: Large enough that the row arm's attenuation separates from an unattenuated
#: ``icc`` by far more than the Monte Carlo error a test can afford. The
#: CALIBRATED pair is what ``report`` runs and is pinned in the grid case; the
#: one exact form that survives alignment — the session level leaving the
#: within-cluster correlation at the ``icc`` — is tested at this magnitude in
#: ``TestTheClosedFormsUnderAlignment``. The ROW level has no closed form at any
#: magnitude; what is tested there is a DIRECTION.
_TEST_GAM = 0.6


def _digest(values: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(np.ascontiguousarray(values).tobytes()).hexdigest()[:32]


def _pin_panel(rng, *, shared_fraction, signal_loading=0.0, alignment=None, icc=_PIN_ICC):
    return ov.simulate_overlapping_panel(
        burnt_signals=_PIN_SIGNALS,
        arrival_offsets=_PIN_OFFSETS,
        cluster_sizes=_PIN_SIZES,
        effects=_PIN_EFFECTS,
        sd_y=1.0,
        icc=icc,
        shared_fraction=shared_fraction,
        horizon=_PIN_HORIZON,
        rng=rng,
        signal_loading=signal_loading,
        alignment=alignment,
    )


def _episode_pairs(
    *,
    lag: int,
    icc: float,
    shared_fraction: float,
    horizon: int = 20,
    n_sims: int = 2000,
    cluster_size: int = 4,
    n_clusters: int | None = None,
    seed: int = 7,
    signals: np.ndarray | None = None,
    alignment=None,
) -> float:
    """Correlation between the first episode of cluster 0 and of cluster ``lag``.

    Measured across independent simulations, which is the only way to see a
    cross-cluster covariance: within one panel the two draws are a single
    realisation.

    The default signal column is CONSTANT, where both alignment levels are exact
    no-ops (``_z`` returns zeros). Any case that wants alignment to do something
    must pass its own varying column; ``TestTheAlignmentFixtureIsNotDegenerate``
    pins that trap.
    """
    rng = np.random.default_rng(seed)
    if signals is None:
        signals = np.zeros((32, 1))
    left, right = [], []
    count = n_clusters if n_clusters is not None else lag + 2
    sizes = [cluster_size] * count
    offsets = list(range(count))
    for _ in range(n_sims):
        y, _x, _c = ov.simulate_overlapping_panel(
            burnt_signals=signals,
            arrival_offsets=offsets,
            cluster_sizes=sizes,
            effects={"atr": 0.0},
            sd_y=1.0,
            icc=icc,
            shared_fraction=shared_fraction,
            horizon=horizon,
            rng=rng,
            alignment=alignment,
        )
        left.append(y[0])
        right.append(y[cluster_size * lag])
    return float(np.corrcoef(left, right)[0, 1])


class TestTheOverlapIsActuallySimulated(unittest.TestCase):
    def test_no_sharing_reproduces_the_independent_cluster_simulator(self):
        # The control. shared_fraction = 0 is the DGP the merged preflight used,
        # and it must come back with no cross-cluster dependence at all.
        self.assertAlmostEqual(_episode_pairs(lag=1, icc=0.5, shared_fraction=0.0), 0.0, delta=0.05)

    def test_a_one_session_horizon_leaves_nothing_to_share(self):
        # The second control. At horizon 1 adjacent clusters share no session,
        # so even full sharing must produce independence. A simulator that
        # correlated clusters by arrival distance rather than by window overlap
        # would pass the profile test above and fail here.
        self.assertAlmostEqual(
            _episode_pairs(lag=1, icc=0.5, shared_fraction=1.0, horizon=1), 0.0, delta=0.05
        )

    def test_clusters_further_apart_than_the_horizon_do_not_share(self):
        self.assertAlmostEqual(
            _episode_pairs(lag=5, icc=0.5, shared_fraction=1.0, horizon=4), 0.0, delta=0.05
        )

    def test_gaps_in_the_arrival_calendar_are_respected(self):
        # Offsets are calendar session indices, not positions in a list. Two
        # arrivals 30 sessions apart share nothing even if they are adjacent
        # entries, which is what makes the real (gappy) arrival list usable.
        rng = np.random.default_rng(3)
        left, right = [], []
        for _ in range(2000):
            y, _x, _c = ov.simulate_overlapping_panel(
                burnt_signals=np.zeros((8, 1)),
                arrival_offsets=[0, 30],
                cluster_sizes=[2, 2],
                effects={"atr": 0.0},
                sd_y=1.0,
                icc=0.5,
                shared_fraction=1.0,
                horizon=20,
                rng=rng,
            )
            left.append(y[0])
            right.append(y[2])
        self.assertAlmostEqual(float(np.corrcoef(left, right)[0, 1]), 0.0, delta=0.05)


class TestTheCalibrationSurvivesTheChange(unittest.TestCase):
    """Only the cross-cluster structure may move; the marginals must not."""

    def _population_spread(self, *, shared_fraction, signal_loading, n_panels=20000):
        """Spread of ONE episode taken from each of many independent panels.

        Pooling episodes inside a panel cannot measure this: with a shared
        window the cluster effects are correlated, so the pooled sample spread
        is shrunk by the dependence itself. That shrinkage is real and is
        asserted separately below; it is not a variance leak, and reading it as
        one would send someone hunting a bug that is not there.
        """
        signals = _z(np.random.default_rng(0).normal(0.0, 1.0, 300)).reshape(-1, 1)
        drawn = []
        for seed in range(n_panels):
            rng = np.random.default_rng(seed)
            y, _x, _c = ov.simulate_overlapping_panel(
                burnt_signals=signals,
                arrival_offsets=[0],
                cluster_sizes=[1],
                effects={"atr": 0.0},
                sd_y=2.0,
                icc=0.4,
                shared_fraction=shared_fraction,
                horizon=20,
                rng=rng,
                signal_loading=signal_loading,
            )
            drawn.append(y[0])
        return float(np.std(drawn))

    def test_the_population_spread_is_sd_y_for_every_sharing_and_loading(self):
        # The real invariant. The kappa arm renormalises by sqrt(1 + kappa**2)
        # precisely so that loading the shared shock onto the signal moves the
        # DEPENDENCE and not the marginal variance. Dropping the cluster-local
        # component instead would land near 1.55 here, far outside the band.
        for phi, kappa in ((0.0, 0.0), (1.0, 0.0), (1.0, 0.5), (1.0, 1.0)):
            with self.subTest(shared_fraction=phi, signal_loading=kappa):
                self.assertAlmostEqual(
                    self._population_spread(shared_fraction=phi, signal_loading=kappa),
                    2.0,
                    # 20,000 panels put the Monte Carlo error near 0.010, so this
                    # is about 4 standard errors and catches a 2% bias in the
                    # spread. The previous 0.12 was 40x the noise: wide enough to
                    # hide exactly the kind of systematic narrowing that cost this
                    # memo's predecessor four points of power.
                    delta=0.04,
                )

    def test_the_pooled_sample_spread_shrinks_as_sharing_rises(self):
        # A positive control for the dependence, built out of the thing that
        # first looked like a defect. Correlated clusters shrink the POOLED
        # sample spread even though the population variance is untouched, so a
        # simulator that only pretended to share would not produce this slope.
        spreads = []
        for phi in (0.0, 0.5, 1.0):
            per_panel = []
            for seed in range(60):
                rng = np.random.default_rng(seed)
                y, _x, _c = ov.simulate_overlapping_panel(
                    burnt_signals=np.zeros((64, 1)),
                    arrival_offsets=list(range(35)),
                    cluster_sizes=[12] * 35,
                    effects={"atr": 0.0},
                    sd_y=2.0,
                    icc=0.4,
                    shared_fraction=phi,
                    horizon=20,
                    rng=rng,
                )
                per_panel.append(float(np.std(y)))
            spreads.append(float(np.mean(per_panel)))
        self.assertGreater(spreads[0], spreads[1])
        self.assertGreater(spreads[1], spreads[2])
        self.assertGreater(spreads[0] - spreads[2], 0.10)

    def test_no_sharing_is_bit_identical_to_the_simulator_it_audits(self):
        # The load-bearing claim of the whole module, pinned exactly rather than
        # statistically: with nothing shared, this simulator must BE the one it
        # is auditing, down to the random stream. If it drifts, every "phi = 0
        # reproduces the merged preflight" sentence in the memo becomes false.
        signals = np.random.default_rng(0).normal(0.0, 1.0, (50, 3))
        effects = {"atr": 0.2, "ma50": -0.1, "press": 0.05}
        sizes = [3, 4, 2, 5]
        old = a20_power.simulate_panel(
            burnt_signals=signals,
            cluster_sizes=sizes,
            effects=effects,
            sd_y=1.0,
            icc=0.4,
            rng=np.random.default_rng(42),
        )
        new = ov.simulate_overlapping_panel(
            burnt_signals=signals,
            arrival_offsets=[0, 1, 2, 3],
            cluster_sizes=sizes,
            effects=effects,
            sd_y=1.0,
            icc=0.4,
            shared_fraction=0.0,
            horizon=20,
            rng=np.random.default_rng(42),
        )
        for label, a, b in zip(("y", "X", "clusters"), old, new, strict=True):
            with self.subTest(array=label):
                self.assertTrue(np.array_equal(a, b))


class TestTheScoreDiagnosticRefusesADegeneratePanel(unittest.TestCase):
    def test_a_panel_with_no_score_variation_is_refused_not_called_independent(self):
        y = np.zeros(8)
        x_all = np.column_stack([np.ones(8), np.zeros(8)])
        with self.assertRaises(ValueError) as caught:
            ov.score_autocovariance(
                y=y, x_all=x_all, clusters=np.array(list("aabbccdd")), coef_index=1, max_lag=2
            )
        self.assertIn("degenerate", str(caught.exception))


class TestBlockClusterLabels(unittest.TestCase):
    def test_a_block_of_one_session_is_the_arrival_session_itself(self):
        labels = ov.block_clusters(
            arrival_offsets=[0, 1, 2], cluster_sizes=[2, 1, 2], block_sessions=1
        )
        self.assertEqual(len(set(labels)), 3)

    def test_consecutive_sessions_fall_into_one_block(self):
        labels = ov.block_clusters(
            arrival_offsets=[0, 1, 2, 3], cluster_sizes=[1, 1, 1, 1], block_sessions=2
        )
        self.assertEqual(list(labels), ["0", "0", "1", "1"])

    def test_blocks_are_cut_on_the_calendar_not_on_the_arrival_index(self):
        # Sessions 0 and 9 are nine sessions apart, so a block of 5 must not
        # put them together even though they are neighbours in the list.
        labels = ov.block_clusters(arrival_offsets=[0, 9], cluster_sizes=[1, 1], block_sessions=5)
        self.assertNotEqual(labels[0], labels[1])

    def test_a_block_shorter_than_one_session_is_refused(self):
        with self.assertRaises(ValueError):
            ov.block_clusters(arrival_offsets=[0], cluster_sizes=[1], block_sessions=0)


class TestTheFittedSignalLoading(unittest.TestCase):
    """The one channel through which an overlapping window can bias the slope."""

    @staticmethod
    def _panel(outcome, atr, ma50, press):
        import pandas as pd

        return pd.DataFrame(
            {
                ov.OUTCOME: outcome,
                ov.SIGNALS["atr"]: atr,
                ov.SIGNALS["ma50"]: ma50,
                ov.SIGNALS["press"]: press,
            }
        )

    def test_a_panel_with_no_spread_left_to_explain_loads_at_zero(self):
        # Four rows and four parameters fit the panel, so the residuals are
        # numerical zeros (about 1e-17) rather than true zeros. An `intercept
        # <= 0` guard lets those through and returns a ratio of two of them as
        # if it were a loading - this panel measured 0.0046 that way before the
        # guard was made scale-aware.
        panel = self._panel(
            [0.1, -0.2, 0.3, -0.4],
            [1.0, 2.0, 3.0, 5.0],
            [0.5, -1.0, 2.0, 0.25],
            [1.0, 4.0, 2.0, 3.0],
        )
        self.assertEqual(ov.estimate_signal_loading(panel, signal="atr"), 0.0)

    def test_a_negative_loading_is_clamped_rather_than_simulated(self):
        # High-signal names reacting LESS to a common move is not a direction
        # worth simulating, and letting it through would make the overlap look
        # harmless for the wrong reason.
        rng = np.random.default_rng(11)
        atr = rng.normal(size=200)
        # Residual spread SHRINKS with the signal, the opposite of the loading.
        noise = rng.normal(size=200) * np.exp(-0.6 * _z(atr))
        panel = self._panel(noise, atr, rng.normal(size=200), rng.normal(size=200))
        self.assertEqual(ov.estimate_signal_loading(panel, signal="atr"), 0.0)

    def test_a_signal_that_widens_the_residual_loads_above_zero(self):
        # The positive control: without it the two cases above would pass on an
        # estimator that always returns zero.
        rng = np.random.default_rng(12)
        atr = rng.normal(size=400)
        noise = rng.normal(size=400) * np.exp(0.6 * _z(atr))
        panel = self._panel(noise, atr, rng.normal(size=400), rng.normal(size=400))
        self.assertGreater(ov.estimate_signal_loading(panel, signal="atr"), 0.0)


class TestVarianceSplit(unittest.TestCase):
    def test_the_pieces_add_back_to_the_residual_variance(self):
        daily, local, idio = ov.variance_split(
            var_resid=4.0, icc=0.25, shared_fraction=0.5, horizon=20
        )
        self.assertAlmostEqual(daily * 20 + local + idio, 4.0)

    def test_no_sharing_puts_the_whole_cluster_component_in_the_local_draw(self):
        daily, local, _idio = ov.variance_split(
            var_resid=4.0, icc=0.25, shared_fraction=0.0, horizon=20
        )
        self.assertEqual(daily, 0.0)
        self.assertAlmostEqual(local, 1.0)

    def test_a_sharing_fraction_outside_the_unit_interval_is_refused(self):
        with self.assertRaises(ValueError):
            ov.variance_split(var_resid=1.0, icc=0.2, shared_fraction=1.5, horizon=20)

    def test_a_horizon_shorter_than_one_session_is_refused(self):
        # A zero horizon would divide the shared variance by zero and return
        # infinities rather than refusing, so the guard has to come first.
        with self.assertRaises(ValueError):
            ov.variance_split(var_resid=1.0, icc=0.2, shared_fraction=0.5, horizon=0)

    def test_an_icc_outside_the_unit_interval_is_refused(self):
        with self.assertRaises(ValueError):
            ov.variance_split(var_resid=1.0, icc=1.4, shared_fraction=0.5, horizon=20)


class TestTheScoreDiagnostic(unittest.TestCase):
    """The replacement for the withdrawn session-mean-residual diagnostic."""

    #: A demeaned autocorrelation over G sessions sits at -1/(G-1) under
    #: independence, not at 0. With 35 sessions that is -0.029, and forgetting it
    #: would read a healthy panel as mildly negatively dependent.
    BIAS_LINE = -1.0 / 34.0

    def _lag1(self, *, shared_fraction: float, signal_loading: float, n_panels: int = 120) -> float:
        """Mean lag-1 score autocorrelation over independent panels.

        120 panels puts the standard error near 0.015. One panel of 35 sessions
        has a standard error around 0.17, so a single realisation cannot tell
        these cases apart and averaging is not optional here.
        """
        out = []
        for seed in range(n_panels):
            rng = np.random.default_rng(seed)
            y, x_all, clusters = ov.simulate_overlapping_panel(
                burnt_signals=rng.normal(0.0, 1.0, (200, 1)),
                arrival_offsets=list(range(35)),
                cluster_sizes=[6] * 35,
                effects={"atr": 0.0},
                sd_y=1.0,
                icc=0.5,
                shared_fraction=shared_fraction,
                horizon=20,
                rng=rng,
                signal_loading=signal_loading,
            )
            out.append(
                ov.score_autocovariance(
                    y=y, x_all=x_all, clusters=clusters, coef_index=1, max_lag=3
                )[0]
            )
        return float(np.mean(out))

    def test_a_level_shift_shared_window_barely_reaches_the_coefficient(self):
        # The substantive finding, pinned. A shared shock that moves every name
        # on a date by the SAME amount is orthogonal to a mean-zero regressor, so
        # full window sharing leaves the score autocorrelation near zero
        # (measured +0.005 +/- 0.014 at 150 panels) instead of near the +0.475
        # the OUTCOMES themselves correlate at. Overlap alone does not bias this
        # coefficient. It is not exactly the bias line -- sharing lifts it by
        # about 0.03 -- so this asserts smallness, not equality.
        self.assertLess(abs(self._lag1(shared_fraction=1.0, signal_loading=0.0)), 0.05)

    def test_it_does_see_the_dependence_once_the_shock_loads_on_the_signal(self):
        # The refutation control, and the reason the test above means anything.
        # If high-ATR names take more of the common move, the overlap DOES reach
        # the score (measured +0.170). A diagnostic that could not produce this
        # observation would have tested nothing.
        self.assertGreater(self._lag1(shared_fraction=1.0, signal_loading=0.6), 0.10)

    def test_the_loaded_channel_still_needs_a_shared_window(self):
        # Loading without sharing is not a time-series problem at all.
        self.assertAlmostEqual(
            self._lag1(shared_fraction=0.0, signal_loading=0.6), self.BIAS_LINE, delta=0.03
        )


class TestTheRejectionHarness(unittest.TestCase):
    def _common(self, method: str, phi: float):
        return {
            "burnt_signals": np.random.default_rng(1).normal(0.0, 1.0, (200, 1)),
            "arrival_offsets": list(range(35)),
            "cluster_sizes": [6] * 35,
            "sd_y": 1.0,
            "icc": 0.5,
            "shared_fraction": phi,
            "horizon": 20,
            "method": method,
            "coef_name": "atr",
            "n_sims": 120,
            "wcb_boot": 199,
            "seed": 4,
        }

    def test_an_independent_panel_rejects_at_about_the_bar(self):
        # The harness sanity check: with nothing shared and no effect, the
        # arrival-session bootstrap must sit near its nominal 5%. If this drifts
        # far, the measurement below says nothing about overlap.
        size = ov.rejection_rate(effects={"atr": 0.0}, **self._common("arrival", 0.0))
        self.assertLess(size, 0.15)

    def test_a_real_effect_rejects_more_often_than_none(self):
        args = self._common("arrival", 0.0)
        null = ov.rejection_rate(effects={"atr": 0.0}, **args)
        real = ov.rejection_rate(effects={"atr": 0.35}, **args)
        self.assertGreater(real, null)

    def test_a_method_that_leaves_one_cluster_is_refused(self):
        # A block longer than the whole calendar collapses to one group, where
        # CR1 divides by zero. Refusing beats returning a confident number.
        with self.assertRaises(ValueError) as caught:
            ov.rejection_rate(
                effects={"atr": 0.0},
                **{
                    **self._common("block5", 0.0),
                    "arrival_offsets": [0, 1],
                    "cluster_sizes": [3, 3],
                },
            )
        self.assertIn("at least two", str(caught.exception))

    def test_an_unknown_method_is_refused(self):
        with self.assertRaises(ValueError):
            ov.labels_for_method(method="newey", arrival_offsets=[0, 1], cluster_sizes=[1, 1])

    def test_a_coefficient_outside_the_simulated_effects_is_refused(self):
        with self.assertRaises(ValueError):
            ov.rejection_rate(
                effects={"atr": 0.0}, **{**self._common("arrival", 0.0), "coef_name": "ma50"}
            )


class TestProgressReporting(unittest.TestCase):
    """An hour-long run that prints nothing forces the caller to guess."""

    def _args(self):
        return {
            "burnt_signals": np.random.default_rng(1).normal(0.0, 1.0, (120, 1)),
            "arrival_offsets": list(range(12)),
            "cluster_sizes": [4] * 12,
            "effects": {"atr": 0.0},
            "sd_y": 1.0,
            "icc": 0.3,
            "shared_fraction": 0.5,
            "horizon": 20,
            "method": "arrival",
            "coef_name": "atr",
            "n_sims": 20,
            "wcb_boot": 49,
            "seed": 9,
        }

    def test_progress_goes_to_stderr_and_counts_simulations(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ov.rejection_rate(progress_every=5, **self._args())
        lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 4)
        self.assertIn("20/20", lines[-1])

    def test_progress_never_reports_the_running_rejection_count(self):
        # Deliberate. The running count is a partial estimate of the very number
        # the run exists to produce at high precision; seeing it mid-run and then
        # deciding whether to continue is optional stopping.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ov.rejection_rate(progress_every=5, **self._args())
        self.assertNotIn("hits", err.getvalue())
        self.assertNotIn("reject", err.getvalue().lower())

    def test_reporting_progress_does_not_change_the_answer(self):
        # The counter must not touch the random stream. If it ever did, every
        # recorded run in the memo would stop reproducing.
        quiet = ov.rejection_rate(**self._args())
        with contextlib.redirect_stderr(io.StringIO()):
            loud = ov.rejection_rate(progress_every=1, **self._args())
        self.assertEqual(quiet, loud)

    def test_the_pass_name_tells_a_size_run_from_a_power_run(self):
        # Every cell runs twice. Two identical progress lines would leave the
        # reader unable to tell which half of a long run they are watching.
        args = self._args()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ov.rejection_rate(progress_every=20, **{**args, "effects": {"atr": 0.0}})
        self.assertIn("size", err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ov.rejection_rate(progress_every=20, **{**args, "effects": {"atr": 0.4}})
        self.assertIn("power", err.getvalue())

    def test_the_arm_name_tells_the_three_corners_apart(self):
        # With --alignment-size a cell runs SIX times, not twice. A line naming
        # only size-or-power would leave the reader unable to tell which sixth of
        # a long run they are watching.
        args = self._args()
        seen = []
        for alignment in (
            None,
            ov.NoiseAlignment(_TEST_GAM, ov.ALIGNMENT_SESSION),
            ov.NoiseAlignment(_TEST_GAM, ov.ALIGNMENT_ROW),
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                ov.rejection_rate(progress_every=20, alignment=alignment, **args)
            seen.append(err.getvalue().strip())
        self.assertEqual(len(set(seen)), len(ov.BRACKET_ARMS))
        for arm, line in zip(ov.BRACKET_ARMS, seen, strict=True):
            self.assertIn(arm, line)

    def test_silent_by_default(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ov.rejection_rate(**self._args())
        self.assertEqual(err.getvalue(), "")


class TestArrivalOffsets(unittest.TestCase):
    def test_the_first_arrival_is_the_origin(self):
        self.assertEqual(ov.arrival_offsets_from_dates(["2026-07-06", "2026-07-07"])[0], 0)

    def test_a_weekend_is_not_two_sessions(self):
        # Friday to Monday is one session, not three days. Counting calendar
        # days would inflate every gap and destroy sharing that really exists.
        friday_to_monday = ov.arrival_offsets_from_dates(["2026-07-10", "2026-07-13"])
        self.assertEqual(friday_to_monday[1], 1)


class TestTheWholeDriverRuns(unittest.TestCase):
    """End to end over the fixture stores, at simulation counts a test can afford.

    The numbers are meaningless at four simulations; what this pins is that the
    driver assembles a complete answer from disk, that the two reads of the
    held-out side agree with each other, and that the size-before-power rule is
    applied rather than merely documented.

    The held-out arm is 45 sessions rather than the fixture default, because
    ``block20`` needs more than one block to exist on the calendar. At the
    default 12 sessions every episode lands in one group and the coarsest
    method would never be exercised at all.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = _Store(self.root, n_held=45)

    def _report(self, **overrides):
        args = {
            "labels_dir": self.store.labels,
            "briefs_dir": self.store.briefs,
            "population": ov.POPULATION_BRIEFED,
            "n_sims": 4,
            "wcb_boot": 9,
            "seed": 99,
        }
        return ov.report(**{**args, **overrides})

    def test_the_report_carries_every_field_the_memo_quotes(self):
        r = self._report()
        for key in (
            "population",
            "burnt_episodes",
            "held_out_clusters",
            "held_out_episodes",
            "calendar_span_sessions",
            "sd_y",
            "icc",
            "signal_loading",
            "fitted_arm_omitted_as_duplicate",
            "size_tolerance",
            "rows",
        ):
            self.assertIn(key, r)
        self.assertGreater(r["burnt_episodes"], 0)
        self.assertGreater(r["held_out_clusters"], 0)
        self.assertGreaterEqual(r["calendar_span_sessions"], r["held_out_clusters"])

    def test_the_grid_is_the_pre_specified_one(self):
        r = self._report()
        arms = 1 if r["fitted_arm_omitted_as_duplicate"] else 2
        self.assertEqual(len(r["rows"]), len(ov.SHARED_FRACTION_GRID) * arms * len(ov.METHODS))
        self.assertEqual(
            {row["shared_fraction"] for row in r["rows"]}, set(ov.SHARED_FRACTION_GRID)
        )
        self.assertEqual({row["method"] for row in r["rows"]}, set(ov.METHODS))

    def test_a_method_that_misses_the_size_bar_gets_no_power_figure(self):
        # The two-stage rule is the point of the module, so it is asserted on
        # the driver's own output rather than trusted to the docstring: power
        # is present exactly when size held, and absent exactly when it did not.
        r = self._report()
        for row in r["rows"]:
            self.assertEqual(row["size_held"], row["size"] <= r["size_tolerance"])
            self.assertEqual(row["power"] is None, not row["size_held"])

    def test_the_fitted_arm_is_omitted_only_when_it_would_repeat_the_control(self):
        r = self._report()
        loadings = {row["loading"] for row in r["rows"]}
        if r["fitted_arm_omitted_as_duplicate"]:
            self.assertEqual(r["signal_loading"], 0.0)
            self.assertEqual(loadings, {"kappa=0"})
        else:
            self.assertGreater(r["signal_loading"], 0.0)
            self.assertEqual(loadings, {"kappa=0", "kappa=fitted"})

    def test_the_calendar_and_the_cluster_sizes_describe_one_panel(self):
        # These used to be two passes over the same rows guarded by a
        # consistency check. Now they are one read, so the property to pin is
        # that the panel the simulation sees is the COLLAPSED one: ledger rule
        # 5 counts ticker-episodes, and the fixture repeats a ticker inside the
        # 5-session window on purpose.
        from alphalens_research.diagnostics.options_retro import ticker_episode_dedup

        r = self._report()
        rows = ov.load_held_out(self.store.labels)
        briefed = rows[rows["briefed_any_theme"].fillna(False).astype(bool)]
        resolved = briefed[briefed["sel_label_status_20"].astype(str) == "ok"]
        collapsed = ticker_episode_dedup(
            resolved[["brief_date", "ticker", "anchor_session"]]
            .astype({"brief_date": str})
            .drop_duplicates(subset=["brief_date", "ticker"])
        )

        self.assertEqual(r["held_out_episodes"], len(collapsed))
        self.assertEqual(r["held_out_clusters"], collapsed["anchor_session"].nunique())

    def test_the_collapse_is_actually_exercised_by_this_fixture(self):
        # Positive control for the case above: without a chained repeat on
        # disk, the collapsed and uncollapsed panels would be identical and the
        # assertions could not have failed.
        r = self._report()
        rows = ov.load_held_out(self.store.labels)
        briefed = rows[rows["briefed_any_theme"].fillna(False).astype(bool)]
        uncollapsed = len(
            briefed[briefed["sel_label_status_20"].astype(str) == "ok"].drop_duplicates(
                subset=["brief_date", "ticker"]
            )
        )
        self.assertLess(r["held_out_episodes"], uncollapsed)

    def test_main_prints_the_grid_and_writes_its_json(self):
        import json

        out_json = self.root / "out.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ov.main(
                [
                    "--labels-dir",
                    str(self.store.labels),
                    "--briefs-dir",
                    str(self.store.briefs),
                    "--n-sims",
                    "4",
                    "--wcb-boot",
                    "9",
                    "--out-json",
                    str(out_json),
                ]
            )
        self.assertEqual(code, 0)
        printed = buffer.getvalue()
        for method in ov.METHODS:
            self.assertIn(method, printed)
        self.assertIn("size bar", printed)

        written = json.loads(out_json.read_text())
        self.assertEqual(written["population"], ov.POPULATION_BRIEFED)
        # Every row of the answer must reach the screen, not only the file: the
        # table is what a reader of a two-hour run actually sees.
        table = [line for line in printed.splitlines() if re.match(r"^ *\d\.\d{2} +kappa=", line)]
        self.assertEqual(len(table), len(written["rows"]))


class TestThePhiZeroEquivalence(unittest.TestCase):
    """At phi = 0 this module must BE the simulator it audits, on BOTH paths.

    This module is a second copy of the DGP, and the merged version of this class
    answered that risk by FORBIDDING an ``alignment`` parameter here. The risk is
    the same and the answer is now stronger: the mechanism is imported from
    ``a20_power`` rather than re-implemented, and the equivalence is pinned on the
    aligned path too. A prohibition stops a parameter existing; this catches an
    actual divergence between the two copies.

    The equivalence holds for ``0 < icc < 1`` only, and that condition is
    measured below rather than claimed. This module guards its cluster-local and
    idiosyncratic draws on a POSITIVE variance (``phi = 1`` has no cluster-local
    component and is two published rows); the sibling draws both
    unconditionally. The real panel's icc is 0.061, so nothing merged is
    affected.
    """

    def _overlap_panel(self, *, icc=_PIN_ICC, **extra):
        return _pin_panel(np.random.default_rng(_PIN_SEED), shared_fraction=0.0, icc=icc, **extra)

    def _power_panel(self, *, icc=_PIN_ICC, **extra):
        return a20_power.simulate_panel(
            burnt_signals=_PIN_SIGNALS,
            cluster_sizes=_PIN_SIZES,
            effects=_PIN_EFFECTS,
            sd_y=1.0,
            icc=icc,
            rng=np.random.default_rng(_PIN_SEED),
            **extra,
        )

    def test_the_two_simulators_still_agree_on_the_default_path(self):
        self.assertTrue(np.array_equal(self._power_panel()[0], self._overlap_panel()[0]))

    def test_they_agree_on_the_aligned_path_as_well(self):
        # The replacement anti-divergence pin. The composition here must be the
        # sibling's own - `sig + (noise) * s`, one scale for the WHOLE residual -
        # or this equality breaks.
        for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
            with self.subTest(level=level):
                alignment = a20_power.NoiseAlignment(_TEST_GAM, level)
                self.assertTrue(
                    np.array_equal(
                        self._power_panel(alignment=alignment)[0],
                        self._overlap_panel(alignment=alignment)[0],
                    )
                )

    def test_the_streams_part_at_the_degenerate_ends_of_the_icc(self):
        # The condition the merged docstring stated unconditionally. At icc 0
        # there is no cluster-local variance and at icc 1 no idiosyncratic one,
        # and this module skips the draw the sibling still makes.
        for icc in (0.0, 1.0):
            with self.subTest(icc=icc):
                self.assertFalse(
                    np.array_equal(self._power_panel(icc=icc)[0], self._overlap_panel(icc=icc)[0])
                )


class TestTheDefaultNumericPathDoesNotMove(unittest.TestCase):
    """The "default path moved" detector for every published overlap figure."""

    def test_the_first_panel_still_hashes_to_the_pre_change_digest(self):
        for (phi, kappa), (first, _second) in _BASELINE_DIGESTS.items():
            with self.subTest(shared_fraction=phi, signal_loading=kappa):
                rng = np.random.default_rng(_PIN_SEED)
                y, _x, _c = _pin_panel(rng, shared_fraction=phi, signal_loading=kappa)
                self.assertEqual(_digest(y), first)

    def test_the_second_panel_from_the_same_generator_also_does(self):
        # The only one of the three that can see a trailing extra draw.
        for (phi, kappa), (_first, second) in _BASELINE_DIGESTS.items():
            with self.subTest(shared_fraction=phi, signal_loading=kappa):
                rng = np.random.default_rng(_PIN_SEED)
                _pin_panel(rng, shared_fraction=phi, signal_loading=kappa)
                y, _x, _c = _pin_panel(rng, shared_fraction=phi, signal_loading=kappa)
                self.assertEqual(_digest(y), second)

    def test_turning_alignment_on_consumes_no_extra_randomness(self):
        # The only one of the three that can see a per-cluster scale VARIATE: the
        # mechanism must be a deterministic rescaling of noise already drawn.
        off = np.random.default_rng(_PIN_SEED)
        _pin_panel(off, shared_fraction=0.5)
        for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
            with self.subTest(level=level):
                on = np.random.default_rng(_PIN_SEED)
                _pin_panel(
                    on,
                    shared_fraction=0.5,
                    alignment=ov.NoiseAlignment(_TEST_GAM, level),
                )
                self.assertEqual(on.bit_generator.state, off.bit_generator.state)


class TestTheClosedFormsHoldWithAlignmentOff(unittest.TestCase):
    """The docstring's two closed forms, stated for the OFF path only.

    They are no longer unconditional: the row level attenuates both of them, by
    an amount the class below MEASURES rather than predicts. Keeping the
    assertions unlabelled would have left the docstring claiming more than the
    code does.
    """

    def test_the_lag_profile_follows_the_shared_window_fraction(self):
        # Two clusters `lag` sessions apart share `horizon - lag` of their
        # `horizon` outcome sessions, so the closed form is
        # shared_fraction * icc * (horizon - lag) / horizon.
        for lag in (1, 5, 10):
            with self.subTest(lag=lag):
                got = _episode_pairs(lag=lag, icc=0.5, shared_fraction=1.0)
                self.assertAlmostEqual(got, 0.5 * (20 - lag) / 20, delta=0.05)

    def test_the_within_cluster_correlation_is_the_icc_whatever_is_shared(self):
        # Same-cluster episodes share the whole window AND the cluster-local
        # draw, so their correlation is the icc for every split between the two.
        for phi in (0.0, 0.5, 1.0):
            with self.subTest(shared_fraction=phi):
                self.assertAlmostEqual(
                    _within_cluster_correlation(shared_fraction=phi, icc=0.4), 0.4, delta=0.05
                )


def _within_cluster_correlation(
    *,
    shared_fraction: float,
    icc: float,
    alignment=None,
    signals: np.ndarray | None = None,
    n_clusters: int = 1,
    n_sims: int = 2000,
    seed: int = 5,
) -> float:
    """Correlation of the first two episodes of cluster 0 across panels."""
    column = np.zeros((8, 1)) if signals is None else signals
    rng = np.random.default_rng(seed)
    a, b = [], []
    for _ in range(n_sims):
        y, _x, _c = ov.simulate_overlapping_panel(
            burnt_signals=column,
            arrival_offsets=list(range(n_clusters)),
            cluster_sizes=[2] * n_clusters,
            effects={"atr": 0.0},
            sd_y=1.0,
            icc=icc,
            shared_fraction=shared_fraction,
            horizon=20,
            rng=rng,
            alignment=alignment,
        )
        a.append(y[0])
        b.append(y[1])
    return float(np.corrcoef(a, b)[0, 1])


class TestTheClosedFormsUnderAlignment(unittest.TestCase):
    """What replaces the two closed forms once the noise aligns with the feature.

    One exact form survives (the session level leaves the within-cluster
    correlation at the icc) and the other three statements are MEASURED
    DIRECTIONS. See :class:`TestThereIsNoClosedFormForTheRowLevel` for why a
    fourth form is not available to assert.
    """

    #: Eight clusters so the session arm's cross-cluster z-score exists, two rows
    #: each so a within-cluster correlation exists, and a non-constant gaussian
    #: column so neither level is a no-op.
    _COLUMN = _z(np.random.default_rng(31).normal(0.0, 1.0, 400)).reshape(-1, 1)
    #: The SKEWED column this class's stated reason for existing names. The
    #: deleted closed form was 38.65x wrong on a lognormal sigma 0.6 draw and
    #: 1.04x wrong on the gaussian one, so a class that only ever ran gaussian
    #: fixtures was testing the shape where the error hides. sigma 0.4 rather
    #: than 0.6 because at 0.6 one row of 400 sets the panel normaliser and the
    #: session form's Monte Carlo error at 2 000 panels widens past the delta.
    _SKEWED_COLUMN = _z(np.random.default_rng(31).lognormal(0.0, 0.4, 400)).reshape(-1, 1)
    _COLUMNS = {"gaussian": _COLUMN, "lognormal s=0.4": _SKEWED_COLUMN}
    _N_CLUSTERS = 8
    _ICC = 0.4
    #: Every attenuation case must read below this FRACTION of the OFF-PATH value
    #: measured on its own fixture. The threshold used to be a theoretical
    #: constant minus a flat 0.02, which puts the margin against an exact no-op
    #: wherever that fixture's Monte Carlo error happens to land. Measured
    #: 2026-11-08, same fixtures, same helpers:
    #:
    #: | case                    | no-op  | threshold | margin | real effect |
    #: |-------------------------|--------|-----------|--------|-------------|
    #: | within-cluster, old     | 0.3796 | 0.3800    | -0.0004 | 0.1107     |
    #: | within-cluster, now     | 0.3796 | 0.3417    | +0.0380 | 0.1107     |
    #: | lag profile, old        | 0.3872 | 0.3550    | +0.0322 | 0.1258     |
    #: | lag profile, now        | 0.3872 | 0.3485    | +0.0387 | 0.1258     |
    #:
    #: The lag rows catch a no-op in BOTH forms today, which is luck about which
    #: way the Monte Carlo error went rather than a designed margin. Measured
    #: over 20 generator seeds, same fixture, 2 000 panels each: the off-path lag
    #: profile has mean 0.3758 and sd 0.0155, spanning 0.3478 to 0.4082 around a
    #: theoretical 0.3750. Three of those 20 seeds read BELOW the old 0.3550
    #: threshold, so on those the threshold sits above the no-op value and an
    #: exact no-op passes. On a threshold that is a fraction of the SAME
    #: fixture's off-path reading, zero of the 20 do - the margin moves with the
    #: measurement instead of against it.
    _ATTENUATED_FRACTION = 0.9

    def _corr(self, *, level, shared_fraction=0.0, column=None):
        return _within_cluster_correlation(
            shared_fraction=shared_fraction,
            icc=self._ICC,
            alignment=ov.NoiseAlignment(_TEST_GAM, level),
            signals=self._COLUMN if column is None else column,
            n_clusters=self._N_CLUSTERS,
        )

    def _off_corr(self, column):
        return _within_cluster_correlation(
            shared_fraction=0.0,
            icc=self._ICC,
            signals=column,
            n_clusters=self._N_CLUSTERS,
        )

    #: The lag-profile fixture both cases below share, so the attenuated reading
    #: and the off-path reading it is measured against come off ONE construction.
    _LAG = 5
    _LAG_HORIZON = 20
    _LAG_ICC = 0.5

    def _lag_profile(self, *, alignment=None):
        return _episode_pairs(
            lag=self._LAG,
            icc=self._LAG_ICC,
            shared_fraction=1.0,
            horizon=self._LAG_HORIZON,
            signals=self._COLUMN,
            alignment=alignment,
            n_clusters=self._N_CLUSTERS,
        )

    def test_the_session_level_leaves_the_within_cluster_correlation_at_the_icc(self):
        # The scale is constant inside a cluster, so it factors out of the
        # numerator and the denominator alike. On BOTH column shapes: this is the
        # one exact form that survives alignment, and a form asserted only on a
        # gaussian column is the mistake the deleted row-level form was.
        #
        # This case confirms the exact FORM and cannot distinguish the mechanism
        # from a no-op, which is inherent to what it asserts rather than a gap:
        # the form says the session level leaves the correlation UNCHANGED, so a
        # no-op satisfies it by definition. Measured 2026-11-08 on this fixture,
        # the no-op reads 0.3796 against the session level's 0.3726 and a target
        # of 0.4 - the mechanism's own effect here is 0.0070, smaller than the
        # no-op's own 0.0204 distance from the target, so no threshold on this
        # quantity can separate them. The case that DOES separate them is
        # `test_the_session_level_attenuates_the_lag_profile_too`.
        for name, column in self._COLUMNS.items():
            with self.subTest(column=name):
                self.assertAlmostEqual(
                    self._corr(level=ov.ALIGNMENT_SESSION, column=column),
                    self._ICC,
                    delta=0.04,
                )

    def test_the_row_level_attenuates_the_within_cluster_correlation(self):
        # A DIRECTION, not a closed form, and for the same reason the session
        # case below is a direction: the normaliser in `_alignment_scale` is
        # `sqrt(mean(s**2))` over the PANEL'S OWN rows, so it is random and
        # correlated with those rows and no function of the column alone
        # expresses the result. This module published such a function until
        # 2026-11-08; `TestThereIsNoClosedFormForTheRowLevel` records what
        # measuring it cost.
        #
        # Measured against the OFF-path correlation on the same fixture, not
        # against `icc - 0.02`: the latter sat 0.0004 below the no-op value.
        for name, column in self._COLUMNS.items():
            with self.subTest(column=name):
                off = self._off_corr(column)
                got = self._corr(level=ov.ALIGNMENT_ROW, column=column)
                self.assertLess(got, off * self._ATTENUATED_FRACTION)
                self.assertGreater(got, 0.0)

    def test_the_off_path_lag_profile_still_follows_the_theoretical_form(self):
        # The control the two cases below need, stated as its own reading: they
        # compare against the MEASURED off-path profile, so if that measurement
        # ever stopped being the quantity the theory describes, both would be
        # measuring something else quietly. The theoretical value here is
        # `0.5 * (horizon - lag) / horizon` = 0.3750; the fixture reads 0.3872 at
        # 2 000 panels, which is the Monte Carlo error and not a disagreement.
        unattenuated = 0.5 * (self._LAG_HORIZON - self._LAG) / self._LAG_HORIZON
        self.assertAlmostEqual(self._lag_profile(), unattenuated, delta=0.03)

    def test_the_row_level_attenuates_the_lag_profile_too(self):
        # Measured against the OFF-path profile on the same fixture, not against
        # the theoretical constant minus 0.02. That threshold was 0.3550 while
        # the no-op value on this fixture is 0.3872, so it sat 0.0322 BELOW the
        # no-op - it caught an exact no-op only because the off-path Monte Carlo
        # error happened to land above the theory. Had it landed below, the
        # threshold would have sat above the no-op value and this case would have
        # passed on a mechanism that did nothing. A fraction of the measured
        # off-path value scales the margin with the effect instead.
        off = self._lag_profile()
        got = self._lag_profile(alignment=ov.NoiseAlignment(_TEST_GAM, ov.ALIGNMENT_ROW))
        self.assertLess(got, off * self._ATTENUATED_FRACTION)
        self.assertGreater(got, 0.0)

    def test_the_session_level_attenuates_the_lag_profile_too(self):
        # Asserted as a DIRECTION rather than a point: the session factor is
        # computed on z(cluster means), which `_z` standardises inside each
        # panel, so it is not a closed form of the fixture's own column. Same
        # off-path threshold as the row case, for the same reason.
        off = self._lag_profile()
        got = self._lag_profile(alignment=ov.NoiseAlignment(_TEST_GAM, ov.ALIGNMENT_SESSION))
        self.assertLess(got, off * self._ATTENUATED_FRACTION)
        self.assertGreater(got, 0.0)

    def test_alignment_on_differs_from_alignment_off_on_the_same_fixture(self):
        # The positive control. Without it every case above would pass on a
        # mechanism that quietly did nothing.
        off = _within_cluster_correlation(
            shared_fraction=0.0,
            icc=self._ICC,
            signals=self._COLUMN,
            n_clusters=self._N_CLUSTERS,
        )
        self.assertGreater(abs(off - self._corr(level=ov.ALIGNMENT_ROW)), 0.02)


class TestThereIsNoClosedFormForTheRowLevel(unittest.TestCase):
    """The deleted ``alignment_attenuation``, kept as a measurement.

    This module published ``E[exp(gam*x)]**2 / E[exp(2*gam*x)]`` on the feature
    column as the ROW-level factor, on the stated grounds that "the panel-wide
    normaliser cancels". It does not cancel: :func:`a20_power._alignment_scale`
    divides by ``c = sqrt(mean(s**2))`` computed on the PANEL'S OWN rows, so ``c``
    is random and correlated with the rows whose covariance the form describes.

    CORRECTED 2026-11-08. The first version of this table did not reproduce from
    its own stated construction: its gaussian row did, to four decimals, but its
    three lognormal rows named no column SEED, and the "claimed" side is a sample
    mean over a heavy right tail whose value is set by the draw. The three
    lognormal "claimed" figures published here were 0.2464 / 0.1239 / 0.1380,
    which belong to other draws, and the worst ratio read 1.72x where the stated
    recipe gives 38.65x - a 22x understatement of the error the table exists to
    record, and non-monotone in sigma where the mechanism is monotone. The table
    below is recomputed from the recipe, at the seed the recipe now names.

    Read 2026-11-08. Column: ``_z`` of a 400-point draw from
    ``default_rng(31)`` - the SAME seed and length the fixture above uses, named
    here because the quantity is not a function of the distribution alone.
    Panels: 40 000 at panel seed 5, gam 0.6, eight clusters of two rows, icc 0.4,
    phi 0, comparing the simulated within-cluster correlation against the form's
    ``icc * attenuation``:

    | column          | claimed | simulated | ratio  |
    |-----------------|---------|-----------|--------|
    | gaussian        | 0.2760  | 0.2874    | 1.04x  |
    | lognormal s=0.2 | 0.1895  | 0.2714    | 1.43x  |
    | lognormal s=0.4 | 0.0517  | 0.2614    | 5.06x  |
    | lognormal s=0.6 | 0.0067  | 0.2604    | 38.65x |

    The claimed side is DETERMINISTIC given the column, so it carries no Monte
    Carlo error and is pinned exactly below. The simulated side does carry one.
    Measured 2026-11-08 at this table's own 40 000 panels over ten independent
    panel seeds, its standard deviation is 0.0051 on every column, which puts the
    headline 38.65x at about plus or minus 0.8x per standard deviation and the
    1.04x gaussian row at plus or minus 0.02x. A later reader recomputing the
    s=0.6 row and landing on 38.2x has reproduced it, not contradicted it.

    These AGREE with the adversarial finding that prompted the deletion, which
    reported 1.45x / 5.10x / 38.8x on the lognormal columns. The first published
    table did not, and did not say so.

    A single ratio is not the honest form, because the "claimed" side swings with
    the draw (see the spread table below). The same measurement over 200
    INDEPENDENT column draws, seeds 0..199, 4 000 panels each:

    | column          | min   | median | max     | spread |
    |-----------------|-------|--------|---------|--------|
    | gaussian        | 0.91x | 1.02x  | 1.13x   | 1x     |
    | lognormal s=0.2 | 0.93x | 1.12x  | 1.97x   | 2x     |
    | lognormal s=0.4 | 0.95x | 1.43x  | 16.43x  | 17x    |
    | lognormal s=0.6 | 1.01x | 2.37x  | 132.60x | 131x   |

    So seed 31 is NOT a typical sigma 0.6 draw and the headline must not be read
    as a central estimate: at 4 000 panels it reads 36.61x, which ranks 195th of
    those 200 draws - the 97.5th percentile, with five draws worse - against a
    median draw of 2.37x. The argument does not need seed 31 to be typical. It
    needs the gaussian column to be SAFE at every draw, and it is: over the same
    200 gaussian draws every one of the 200 gaps between the simulated and the
    claimed correlation falls inside the old assertion's ``delta=0.04``, the
    largest being 0.0350 at seed 137. (An earlier version of this sentence said
    the gaussian form is right "to within the Monte Carlo error at EVERY draw".
    That is checkable and false: against a simulated-side Monte Carlo standard
    deviation of 0.0116 at 4 000 panels, 13 of the 200 gaps exceed two standard
    deviations and one exceeds three. The delta=0.04 statement is the one that
    carries the argument, and it holds at 200 of 200.)

    That is the whole shape of the problem: the form can be arbitrarily wrong and
    the suite's gaussian fixture can never see it.

    The suite's own fixture is gaussian, where the gap is 1.04x and sits inside
    the old assertion's ``delta=0.04``. That is why the wrong form passed. The
    function's own docstring named "ATR is skewed" as its reason for existing,
    which is the column shape where it is most wrong.

    It is DELETED rather than replaced by a Monte Carlo version, for two reasons.

    The quantity depends on the panel SHAPE and not only on the column: the same
    four columns measured on a single cluster of two rows read 1.13x / 1.66x /
    6.18x / 48.78x instead, a different answer for the same column. So no
    function of the column alone can express it.

    And the form itself is not stable even as a function of the column.
    Evaluated at gam 0.6 over the SAME 200 independent 400-point draws, seeds
    0..199, the attenuation factor alone spans:

    | column          | min    | median | max    | spread |
    |-----------------|--------|--------|--------|--------|
    | gaussian        | 0.6385 | 0.7015 | 0.7580 | 1x     |
    | lognormal s=0.2 | 0.3537 | 0.6014 | 0.7058 | 2x     |
    | lognormal s=0.4 | 0.0414 | 0.4396 | 0.6438 | 16x    |
    | lognormal s=0.6 | 0.0052 | 0.2524 | 0.5712 | 109x   |

    ``E[exp(2*gam*x)]`` is a sample mean over a heavy right tail, so one extreme
    row sets it. The simulated correlation it was supposed to predict does not
    move that way at all (0.2614 at s=0.4 against 0.2604 at s=0.6). A quantity
    whose published value swings 109x with the draw is not a diagnostic, and a
    second simulator maintained for a diagnostic nobody consumes is worse than
    no diagnostic. The ON-path behaviour is stated as a measured direction by the
    two cases above instead.

    WHAT THE GUARDS BELOW REACH, AND WHAT THEY DO NOT. The structural guard pins
    a SPELLING of the formula, not the claim: it asserts that no occurrence of
    ``_FORMULA`` sits outside the paragraph that withdraws it. A reader who
    rebuilt the same prediction in different algebra would not be caught by it.
    It is whitespace-insensitive and bounded by the paragraph's own blank lines
    rather than by a line count, because a line-distance anchor rots - an earlier
    version used a 4-line window and could be evaded by spacing the formula out
    or by duplicating the anchor, while going red on two editorial sentences
    added inside the refuting paragraph with the refutation intact.

    The RATIO guard is the one that reaches a reader. The three places this
    correction is published - this docstring's first table, the module header and
    the overlap memo ``_MEMO`` names - all quote the same four ratios, every one of
    them is DERIVED here from the recipe rather than copied: ``_TABLE_ROWS``
    carries the claimed and simulated columns, the ratio is computed from the
    recipe's exact (unrounded) claimed value, and the published sentences are
    checked against the result. The withdrawn wording ("2.00x on a lognormal
    one", the 22x understatement) cannot be restored at any of the three without
    one of those cases going red.
    """

    #: The formula the deleted function computed, as it appears in source. The
    #: module still contains it ON PURPOSE, in the one paragraph that refutes it,
    #: so the guard below is structural rather than a plain absence check.
    _FORMULA = "E[exp(2*gam*x)]"
    #: A string inside the refuting paragraph. The guard anchors on this and then
    #: takes the WHOLE paragraph the anchor sits in, bounded by blank lines, so
    #: the paragraph can be reflowed or expanded freely. A line-distance window
    #: rots; this does not.
    _REFUTING_ANCHOR = "did publish a closed form for the ROW level"
    #: The other half of the refuted claim. The formula alone is algebra; this is
    #: the sentence that turned it into a PREDICTION, so it is forbidden outside
    #: the refuting paragraph too.
    _REFUTING_JUSTIFICATION = "panel-wide normaliser cancels"

    #: The recipe the first table above states, as code. The "claimed" side is
    #: DETERMINISTIC - a function of the column alone, with no Monte Carlo error -
    #: so it can be pinned exactly, which is what the cases below do.
    _TABLE_GAM = 0.6
    _TABLE_ICC = 0.4
    _TABLE_POINTS = 400
    _TABLE_COLUMN_SEED = 31
    _TABLE_PANELS = 40000
    _TABLE_PANEL_SEED = 5
    _TABLE_PHI = 0
    #: The panel SHAPE, in the words the three published sentences use. The
    #: quantity is not a function of the column alone - the same columns on one
    #: cluster of two rows read a different answer - so the shape is part of the
    #: construction and not decoration.
    _TABLE_SHAPE = "eight clusters of two rows"
    #: ``(row label, sigma or None for gaussian, claimed, simulated, ratio)`` as
    #: the first table prints them. Read 2026-11-08. The ratio is stored rather
    #: than formatted from the two columns beside it, because those columns are
    #: ROUNDED to the four decimals the table prints: at s=0.6 the exact claimed
    #: value is 0.006738, and 0.2604 / 0.0067 is 38.87x where the recipe gives
    #: 38.65x. :meth:`test_the_published_ratio_is_the_one_the_recipe_gives`
    #: checks the stored string against the recipe to exactly the precision the
    #: inputs are printed at, so it is a pinned reading and not a hand-copy.
    _TABLE_ROWS = (
        ("gaussian", None, 0.2760, 0.2874, "1.04x"),
        ("lognormal s=0.2", 0.2, 0.1895, 0.2714, "1.43x"),
        ("lognormal s=0.4", 0.4, 0.0517, 0.2614, "5.06x"),
        ("lognormal s=0.6", 0.6, 0.0067, 0.2604, "38.65x"),
    )
    #: The decimals the table prints its claimed and simulated columns at, and
    #: the decimals it prints the ratio at. Both feed the tolerance above.
    _TABLE_DECIMALS = 4
    _RATIO_DECIMALS = 2
    #: Standard deviation of the simulated side at ``_TABLE_PANELS``, over ten
    #: independent panel seeds. Read 2026-11-08.
    _SIMULATED_MC_SD = 0.0051
    #: Where the headline draw sits in the 200-draw spread: its own ratio at
    #: 4 000 panels, its rank, the number of draws, and the median draw.
    _SEED31_RATIO = "36.61x"
    _SEED31_RANK = 195
    _SPREAD_DRAWS = 200
    _SPREAD_MEDIAN_S06 = "2.37x"
    #: Panels per draw in the 200-draw spread.
    _SPREAD_PANELS = 4000
    #: The third published copy of these figures, under ``docs/research/``. It is
    #: prose for a reader outside this repo, and it is checked here because the
    #: alternative - naming it as a copy that has to be edited by hand - is what
    #: let the module header drift in the first place.
    _MEMO = "a20_overlap_inference_2026_09.md"

    @classmethod
    def _claimed_exactly(cls, sigma):
        """The form's own value on the recipe's column, UNROUNDED.

        The published ratio has to be derived from this and not from the table's
        four-decimal ``claimed`` column: at s=0.6 the claimed value is 0.006738,
        and 0.2604 / 0.0067 is 38.87x where 0.2604 / 0.006738 is 38.65x, which is
        the figure all three published sentences quote.
        """
        x = cls._table_column(sigma)
        return cls._TABLE_ICC * float(
            np.mean(np.exp(cls._TABLE_GAM * x)) ** 2 / np.mean(np.exp(2 * cls._TABLE_GAM * x))
        )

    @classmethod
    def _ratios(cls):
        return [row[4] for row in cls._TABLE_ROWS]

    @staticmethod
    def _squeeze(text):
        """All whitespace removed, so neither spacing nor line wrapping can hide a match."""
        return "".join(text.split())

    @classmethod
    def _table_column(cls, sigma):
        rng = np.random.default_rng(cls._TABLE_COLUMN_SEED)
        raw = (
            rng.normal(0.0, 1.0, cls._TABLE_POINTS)
            if sigma is None
            else rng.lognormal(0.0, sigma, cls._TABLE_POINTS)
        )
        return _z(raw)

    @classmethod
    def _memo_text(cls):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        return (repo_root / "docs" / "research" / cls._MEMO).read_text()

    @classmethod
    def _module_paragraphs(cls):
        """The refuting paragraph of the module source, and the two halves around it.

        The paragraph is the maximal run of non-blank source lines containing
        ``_REFUTING_ANCHOR``, so it can be reflowed, shortened or expanded without
        moving its ends. Each piece is returned whitespace-squeezed.
        """
        import inspect

        lines = inspect.getsource(ov).splitlines()
        anchors = [i for i, line in enumerate(lines) if cls._REFUTING_ANCHOR in line]
        if len(anchors) != 1:
            return None, anchors
        start = end = anchors[0]
        while start > 0 and lines[start - 1].strip():
            start -= 1
        while end + 1 < len(lines) and lines[end + 1].strip():
            end += 1
        return (
            cls._squeeze("\n".join(lines[start : end + 1])),
            [
                cls._squeeze("\n".join(lines[:start])),
                cls._squeeze("\n".join(lines[end + 1 :])),
            ],
        )

    def test_the_module_publishes_no_row_level_closed_form(self):
        self.assertFalse(hasattr(ov, "alignment_attenuation"))

    def test_the_claimed_column_reproduces_from_the_stated_recipe(self):
        # The first version of this table published a "claimed" column that did
        # not reproduce from its own construction: the gaussian row did, to four
        # decimals, but the three lognormal rows named no column seed and their
        # figures belonged to other draws, so the worst ratio read 1.72x where
        # the recipe gives 38.65x. The quantity is deterministic given the
        # column, so the table is pinned to the recipe here rather than taken on
        # trust. Four decimals, because that is the precision the table prints.
        for label, sigma, claimed, _simulated, _ratio in self._TABLE_ROWS:
            with self.subTest(column=label):
                self.assertAlmostEqual(
                    self._claimed_exactly(sigma), claimed, places=self._TABLE_DECIMALS
                )

    def test_the_published_ratio_is_the_one_the_recipe_gives(self):
        # The ratio column is what every published sentence quotes, so it is the
        # column that must not be a hand-copy. It is checked against the recipe
        # rather than formatted from the table, because the table's own inputs
        # are rounded: the tolerance is the rounding of the simulated side
        # divided through by the claimed side, plus the ratio's own last digit.
        # At s=0.6 that is 0.012, and 38.65x against the recipe's 38.6450x is
        # inside it; a wrong ratio is not.
        for label, sigma, _claimed, simulated, ratio in self._TABLE_ROWS:
            with self.subTest(column=label):
                exact = self._claimed_exactly(sigma)
                tolerance = 0.5 * 10.0**-self._TABLE_DECIMALS / exact + (
                    0.5 * 10.0**-self._RATIO_DECIMALS
                )
                self.assertAlmostEqual(
                    float(ratio.removesuffix("x")), simulated / exact, delta=tolerance
                )

    def test_the_first_table_prints_the_ratio_it_derives(self):
        # The table's own four rows, rebuilt from the constants and matched
        # against the docstring. Without this the claimed and simulated columns
        # would be pinned and the RATIO column - the number every published
        # sentence quotes - would be a hand-copy.
        doc = self._squeeze(type(self).__doc__)
        for label, _sigma, claimed, simulated, ratio in self._TABLE_ROWS:
            with self.subTest(column=label):
                row = (
                    f"| {label} | {claimed:.{self._TABLE_DECIMALS}f} "
                    f"| {simulated:.{self._TABLE_DECIMALS}f} | {ratio} |"
                )
                self.assertIn(self._squeeze(row), doc)

    def test_the_docstring_states_the_construction_it_was_measured_at(self):
        # G3.4's shape: the recipe is PROSE, and prose beside a constant does not
        # check the constant. Rewriting the paragraph to a different seed, gam,
        # icc, shape, panel count or phi used to leave everything green, so each
        # constant's printed form is required to be in the docstring.
        doc = self._squeeze(type(self).__doc__)
        for phrase in self._construction_phrases():
            with self.subTest(phrase=phrase):
                self.assertIn(self._squeeze(phrase), doc)

    @classmethod
    def _construction_phrases(cls):
        """Every piece of the construction, in the form the published sentences print it.

        A number is a claim and ships with its construction, so these are
        required in all three published places rather than in one.
        """
        return [
            f"default_rng({cls._TABLE_COLUMN_SEED})",
            f"{cls._TABLE_POINTS}-point",
            f"{cls._TABLE_PANELS:,}".replace(",", " "),
            f"panel seed {cls._TABLE_PANEL_SEED}",
            f"gam {cls._TABLE_GAM}",
            cls._TABLE_SHAPE,
            f"icc {cls._TABLE_ICC}",
            f"phi {cls._TABLE_PHI}",
        ]

    def test_the_module_header_quotes_the_ratios_this_class_derives(self):
        # The defect this whole chain exists to correct lived in the HEADER
        # sentence, not in a test fixture: restoring "1.04x on a gaussian column
        # and 2.00x on a lognormal one" - the 22x understatement - left the whole
        # glob green (measured 2026-11-08), because the pin covered the
        # `_TABLE_ROWS` constants only. A reader reads the header.
        paragraph, _rest = self._module_paragraphs()
        self.assertIsNotNone(paragraph, "the refuting paragraph was not located")
        for ratio in self._ratios():
            with self.subTest(ratio=ratio):
                self.assertIn(ratio, paragraph)

    def test_the_module_header_states_the_construction_too(self):
        paragraph, _rest = self._module_paragraphs()
        self.assertIsNotNone(paragraph, "the refuting paragraph was not located")
        for phrase in self._construction_phrases():
            with self.subTest(phrase=phrase):
                self.assertIn(self._squeeze(phrase), paragraph)

    def test_the_memo_quotes_the_same_ratios_and_the_same_construction(self):
        # The memo is the third published copy and the one a reader outside this
        # repo reads. It writes the ratios with the multiplication sign, which is
        # math notation and stays, so the comparison substitutes it. This is a
        # PRESENCE check over the whole memo rather than a paragraph check: the
        # memo is prose under its own editorial rules, and what has to be caught
        # is a withdrawn figure reappearing, which needs a real figure to go.
        memo = self._squeeze(self._memo_text())
        for ratio in self._ratios():
            with self.subTest(ratio=ratio):
                self.assertIn(ratio.replace("x", "×"), memo)
        for phrase in self._construction_phrases():
            with self.subTest(phrase=phrase):
                self.assertIn(self._squeeze(phrase), memo)

    def test_the_memo_states_the_spread_with_its_own_construction(self):
        # A spread is a claim too: "1.01x to 132.60x" without its seeds, its
        # panels per draw or its median is not reproducible. The median is the
        # part that stops the headline reading as a central estimate.
        memo = self._squeeze(self._memo_text())
        panels = f"{self._SPREAD_PANELS:,}".replace(",", " ")
        median = self._SPREAD_MEDIAN_S06.replace("x", "×")
        for phrase in (
            f"{self._SPREAD_DRAWS} independent",
            f"seeds 0..{self._SPREAD_DRAWS - 1}",
            f"{panels} panels each",
            f"median {median}",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(self._squeeze(phrase), memo)

    def test_the_claimed_column_falls_with_the_skew_as_the_mechanism_says(self):
        # The internal-consistency check the first table failed: it rose from
        # sigma 0.4 (0.1239) to sigma 0.6 (0.1380) while the form is monotone
        # decreasing in sigma on this draw. A table that is not monotone here is
        # not a reading of this recipe, whatever its numbers.
        claimed = [row[2] for row in self._TABLE_ROWS if row[1] is not None]
        self.assertEqual(claimed, sorted(claimed, reverse=True))

    def test_the_docstring_places_the_headline_draw_in_its_own_spread(self):
        # G3.10: "a bad draw but not an extreme one" understated it. Seed 31 is
        # the 97.5th percentile of the class's own 200-draw spread, five draws
        # from the worst, against a median of 2.37x. The percentile costs nothing
        # to state and stops the headline being read as a central estimate.
        doc = self._squeeze(type(self).__doc__)
        for phrase in (
            self._SEED31_RATIO,
            f"ranks {self._SEED31_RANK}th of",
            f"median draw of {self._SPREAD_MEDIAN_S06}",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(self._squeeze(phrase), doc)

    def test_the_docstring_states_the_monte_carlo_error_of_the_simulated_side(self):
        # G3.9: the simulated column and every ratio built from it carry a Monte
        # Carlo error, and at 40 000 panels it is 0.0051 on the correlation -
        # about plus or minus 0.8x on the headline. Published without it, a later
        # reader's 38.2x reads as a contradiction instead of a reproduction.
        doc = self._squeeze(type(self).__doc__)
        self.assertIn(self._squeeze(f"{self._SIMULATED_MC_SD:.4f}"), doc)

    def test_no_docstring_still_promises_one(self):
        # The matching LINES, not the whole module: a failure here has to name
        # which sentence still promises the form, and 1300 lines of source in an
        # assertion message names nothing.
        import inspect

        promises = [
            line.strip()
            for line in inspect.getsource(ov).splitlines()
            if "alignment_attenuation" in line
        ]
        self.assertEqual(promises, [])

    def _refuted_claim_sites_outside_the_refuting_paragraph(self):
        """Where the formula or its justification appears away from the withdrawal.

        Both halves are checked, because the formula on its own is algebra: the
        sentence that says the normaliser cancels is what makes it a prediction,
        and it is the cheaper half to pin.
        """
        paragraph, rest = self._module_paragraphs()
        if paragraph is None:
            return [f"the anchor is not unique: {len(rest)} occurrence(s)"]
        found = []
        for needle in (self._FORMULA, self._REFUTING_JUSTIFICATION):
            squeezed = self._squeeze(needle)
            for half in rest:
                at = half.find(squeezed)
                if at >= 0:
                    found.append(f"{needle!r} at ...{half[max(0, at - 60) : at + 80]}...")
        return found

    def test_the_refuted_claim_appears_only_in_the_paragraph_that_refutes_it(self):
        # The NAME guard above is not enough on its own: the exact wrong closed
        # form, restated verbatim as a predictive docstring sentence without the
        # deleted identifier, left the whole ``test_a20*.py`` glob green
        # (measured 2026-11-08). A plain absence check is not available either,
        # because the module legitimately contains the formula in the paragraph
        # that withdraws it. So the guard is STRUCTURAL: every occurrence of the
        # formula, and of the justification that made it a prediction, must sit
        # inside that paragraph.
        #
        # Located by a unique anchor string and the paragraph's own blank-line
        # ends, and matched with all whitespace removed. The previous version
        # used a 4-line window after the anchor and could be evaded two ways
        # with the glob green (measured 2026-11-08): the same sentence with the
        # formula spaced out, and the formula verbatim under a DUPLICATED copy of
        # the anchor. It also went red when two editorial sentences were added
        # inside the refuting paragraph with the refutation intact.
        self.assertEqual(self._refuted_claim_sites_outside_the_refuting_paragraph(), [])

    def test_the_anchor_the_guard_depends_on_is_really_there_exactly_once(self):
        # The positive control. Without it, deleting or rewording the refuting
        # paragraph's anchor would make the guard above vacuous instead of red,
        # and a SECOND copy of the anchor would legalise a second site for the
        # formula - which is how the previous version was evaded. It also pins
        # that the formula and its justification ARE still in the module, which
        # is what makes the structural form necessary.
        import inspect

        lines = inspect.getsource(ov).splitlines()
        self.assertEqual(len([line for line in lines if self._REFUTING_ANCHOR in line]), 1)
        paragraph, _rest = self._module_paragraphs()
        self.assertIsNotNone(paragraph, "the refuting paragraph was not located")
        self.assertIn(self._squeeze(self._FORMULA), paragraph)
        self.assertIn(self._squeeze(self._REFUTING_JUSTIFICATION), paragraph)


class TestTheAlignmentFixtureIsNotDegenerate(unittest.TestCase):
    """Three shapes on which alignment is an EXACT no-op, pinned as measurements.

    Every existing lag fixture in this file passes ``np.zeros((32, 1))`` as the
    signal column, where ``_z`` returns zeros and both levels scale by exactly 1.
    Extending a calibration case onto one of those shapes would measure nothing
    and look healthy, so the traps get a test of their own.
    """

    _VARYING = _z(np.random.default_rng(17).normal(0.0, 1.0, 200)).reshape(-1, 1)
    _CONSTANT = np.full((200, 1), 0.7)

    def _panels(self, *, sizes, signals, level):
        off, _x, _c = ov.simulate_overlapping_panel(
            burnt_signals=signals,
            arrival_offsets=list(range(len(sizes))),
            cluster_sizes=sizes,
            effects={"atr": 0.0},
            sd_y=1.0,
            icc=0.4,
            shared_fraction=0.5,
            horizon=20,
            rng=np.random.default_rng(3),
        )
        on, _x, _c = ov.simulate_overlapping_panel(
            burnt_signals=signals,
            arrival_offsets=list(range(len(sizes))),
            cluster_sizes=sizes,
            effects={"atr": 0.0},
            sd_y=1.0,
            icc=0.4,
            shared_fraction=0.5,
            horizon=20,
            rng=np.random.default_rng(3),
            alignment=ov.NoiseAlignment(_TEST_GAM, level),
        )
        return off, on

    def test_one_cluster_of_one_row_is_a_no_op_at_both_levels(self):
        for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
            with self.subTest(level=level):
                off, on = self._panels(sizes=[1], signals=self._VARYING, level=level)
                self.assertTrue(np.array_equal(off, on))

    def test_one_cluster_of_many_rows_is_a_no_op_only_at_the_session_level(self):
        off, on = self._panels(sizes=[6], signals=self._VARYING, level=ov.ALIGNMENT_SESSION)
        self.assertTrue(np.array_equal(off, on))
        off, on = self._panels(sizes=[6], signals=self._VARYING, level=ov.ALIGNMENT_ROW)
        self.assertFalse(np.array_equal(off, on))

    def test_a_constant_feature_column_is_a_no_op_at_both_levels(self):
        for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
            with self.subTest(level=level):
                off, on = self._panels(sizes=[6] * 8, signals=self._CONSTANT, level=level)
                self.assertTrue(np.array_equal(off, on))

    def test_many_clusters_with_a_varying_column_are_a_no_op_at_neither_level(self):
        for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
            with self.subTest(level=level):
                off, on = self._panels(sizes=[6] * 8, signals=self._VARYING, level=level)
                self.assertFalse(np.array_equal(off, on))


class TestTheMarginalSpreadSurvivesAlignment(unittest.TestCase):
    """The normaliser holds ``sd_y``, and it is PANEL-WIDE for a reason."""

    _COLUMN = _z(np.random.default_rng(23).normal(0.0, 1.0, 300)).reshape(-1, 1)
    _SIZES = [3] * 4
    _SD_Y = 2.0

    def _pooled_spread(self, *, alignment, n_panels=1500):
        """Spread of EVERY row of many panels, pooled without per-panel demeaning.

        The merged spread case draws one episode from a panel of
        ``cluster_sizes=[1]``, where every per-cluster normaliser is
        arithmetically 1 — so it cannot tell a panel-wide normaliser from a
        per-cluster one. Pooling the rows is what makes the normaliser visible.
        """
        drawn = []
        for seed in range(n_panels):
            y, _x, _c = ov.simulate_overlapping_panel(
                burnt_signals=self._COLUMN,
                arrival_offsets=list(range(len(self._SIZES))),
                cluster_sizes=self._SIZES,
                effects={"atr": 0.0},
                sd_y=self._SD_Y,
                icc=0.4,
                shared_fraction=0.5,
                horizon=20,
                rng=np.random.default_rng(seed),
                alignment=alignment,
            )
            drawn.append(y)
        return float(np.sqrt(np.mean(np.concatenate(drawn) ** 2)))

    def test_the_spread_is_still_sd_y_with_alignment_on(self):
        for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
            with self.subTest(level=level):
                self.assertAlmostEqual(
                    self._pooled_spread(alignment=ov.NoiseAlignment(_TEST_GAM, level)),
                    self._SD_Y,
                    delta=0.08,
                )

    def test_the_normaliser_is_what_holds_it(self):
        # The refutation control: the same exponential scale WITHOUT the
        # normaliser has a mean square well above 1, so it WOULD move the
        # spread. Without this the case above could pass on a no-op.
        xs = [self._COLUMN[: self._SIZES[0]]] * len(self._SIZES)
        scaled = ov._alignment_scale(
            xs, alignment=ov.NoiseAlignment(_TEST_GAM, ov.ALIGNMENT_ROW), names=["atr"]
        )
        raw = np.concatenate([np.exp(_TEST_GAM * x[:, 0]) for x in xs])
        self.assertAlmostEqual(float(np.mean(np.concatenate(scaled) ** 2)), 1.0, places=9)
        self.assertGreater(float(np.mean(raw**2)), 1.1)


class TestTheLoadingColumnFollowsTheFeatureName(unittest.TestCase):
    """kappa loaded ``x[:, 0]`` positionally while alignment resolves a NAME.

    The moment alignment lands, two mechanisms in one DGP disagree about which
    column the feature is unless kappa resolves it by name too.
    """

    _COLUMN = np.column_stack(
        [
            _z(np.random.default_rng(41).normal(0.0, 1.0, 400)),
            _z(np.random.default_rng(42).normal(0.0, 1.0, 400)),
        ]
    )
    #: ``atr`` deliberately SECOND, so a positional loading picks the wrong one.
    _EFFECTS = {"ma50": 0.0, "atr": 0.0}
    _KAPPA = 0.8

    def _squared_outcome_against_columns(self):
        y_all, atr_all, ma50_all = [], [], []
        for seed in range(80):
            y, x_all, _c = ov.simulate_overlapping_panel(
                burnt_signals=self._COLUMN,
                arrival_offsets=list(range(20)),
                cluster_sizes=[6] * 20,
                effects=self._EFFECTS,
                sd_y=1.0,
                icc=0.6,
                shared_fraction=1.0,
                horizon=20,
                rng=np.random.default_rng(seed),
                signal_loading=self._KAPPA,
            )
            y_all.append(y**2)
            ma50_all.append(x_all[:, 1])
            atr_all.append(x_all[:, 2])
        squared = np.concatenate(y_all)
        return (
            float(np.corrcoef(squared, np.concatenate(atr_all))[0, 1]),
            float(np.corrcoef(squared, np.concatenate(ma50_all))[0, 1]),
        )

    def test_the_loading_reaches_the_named_column(self):
        atr_corr, _ = self._squared_outcome_against_columns()
        self.assertGreater(atr_corr, 0.05)

    def test_the_loading_leaves_the_other_column_alone(self):
        _, ma50_corr = self._squared_outcome_against_columns()
        self.assertLess(abs(ma50_corr), 0.02)

    def test_a_loading_feature_outside_the_effects_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            ov.simulate_overlapping_panel(
                burnt_signals=self._COLUMN,
                arrival_offsets=[0, 1],
                cluster_sizes=[2, 2],
                effects=self._EFFECTS,
                sd_y=1.0,
                icc=0.4,
                shared_fraction=1.0,
                horizon=20,
                rng=np.random.default_rng(1),
                signal_loading=self._KAPPA,
                loading_feature="press",
            )
        self.assertIn("press", str(caught.exception))

    def test_a_zero_loading_does_not_require_the_feature_to_exist(self):
        # Zero-kappa fixtures whose effects lack `atr` must keep working: with no
        # loading there is no column to resolve.
        y, _x, _c = ov.simulate_overlapping_panel(
            burnt_signals=np.zeros((8, 1)),
            arrival_offsets=[0, 1],
            cluster_sizes=[2, 2],
            effects={"ma50": 0.0},
            sd_y=1.0,
            icc=0.4,
            shared_fraction=1.0,
            horizon=20,
            rng=np.random.default_rng(1),
        )
        self.assertEqual(len(y), 4)


class TestTheSimulatorRefusesAnImpossibleCalendar(unittest.TestCase):
    def test_a_negative_arrival_offset_is_refused(self):
        # A negative offset clips or wraps the numpy slice and corrupts both
        # closed forms SILENTLY: at offsets [-1, 0] the first cluster loses its
        # whole shared component, and at [-30, 0] two clusters 30 sessions apart
        # come back correlated where the closed form says zero. The FIGURES and
        # their construction are in `simulate_overlapping_panel`'s refusal
        # comment and are deliberately not restated here - this comment used to
        # carry a second, different number for the same quantity (0.29 against
        # the module's +0.262), with no panel seed on either, so neither copy
        # reproduced.
        with self.assertRaises(ValueError) as caught:
            ov.simulate_overlapping_panel(
                burnt_signals=np.zeros((8, 1)),
                arrival_offsets=[-1, 0],
                cluster_sizes=[2, 2],
                effects={"atr": 0.0},
                sd_y=1.0,
                icc=0.4,
                shared_fraction=1.0,
                horizon=20,
                rng=np.random.default_rng(1),
            )
        self.assertIn("arrival_offsets", str(caught.exception))


class TestTheScoreDiagnosticOrdersSessionsNumerically(unittest.TestCase):
    """A lag over lexicographically sorted labels is not a lag.

    With ten or more clusters ``sorted(set(...))`` over string labels puts "10"
    next to "1", so "lag 1" pairs session 1 with session 10 — and it under-reports
    in the direction that makes the overlap look harmless.
    """

    _N_SESSIONS = 35
    #: How much of the real dependence the lexicographic order must lose. The
    #: deterministic ramp below reads +0.914 in calendar order and +0.527 when
    #: the labels are sorted as strings.
    _MIN_UNDER_REPORT = 0.25

    @classmethod
    def _panel(cls):
        """A design whose OLS fit is exactly zero, so the score is known.

        Two rows per session with ``x = (+1, -1)`` and ``y = (t, -t)`` makes
        ``X'y`` the zero vector, so the residual IS ``y`` and the per-session
        score contribution is exactly ``2 * t``.

        ``t`` rises monotonically across the calendar: a deterministic sequence
        with near-maximal lag-1 dependence, so the discrimination below does not
        depend on a lucky seed. A random AR(1) was tried first and the gap it
        produced swung between -0.14 and +0.28 across three seeds.
        """
        t = np.arange(cls._N_SESSIONS, dtype=float)
        t = t - t.mean()
        y = np.concatenate([[v, -v] for v in t])
        x = np.tile([1.0, -1.0], cls._N_SESSIONS)
        x_all = np.column_stack([np.ones(len(y)), x])
        labels = np.array([str(j) for j in range(cls._N_SESSIONS) for _ in range(2)])
        return y, x_all, labels, t

    @staticmethod
    def _lag1(series: np.ndarray) -> float:
        s = series - series.mean()
        return float(np.dot(s[:-1], s[1:]) / np.dot(s, s))

    def test_the_lag_one_autocorrelation_is_the_one_the_calendar_has(self):
        y, x_all, labels, t = self._panel()
        got = ov.score_autocovariance(y=y, x_all=x_all, clusters=labels, coef_index=1, max_lag=1)[0]
        self.assertAlmostEqual(got, self._lag1(t), places=6)

    def test_the_lexicographic_order_would_have_under_reported_it(self):
        # The refutation control, so the case above is a discrimination rather
        # than a restatement.
        _y, _x, _labels, t = self._panel()
        lexicographic = t[np.argsort([str(j) for j in range(self._N_SESSIONS)], kind="stable")]
        self.assertGreater(self._lag1(t) - self._lag1(lexicographic), self._MIN_UNDER_REPORT)

    def test_a_non_integer_session_label_is_refused(self):
        # The same panel, relabelled. The scores still vary, so the refusal the
        # case meets is the ordinal one and not the degeneracy one.
        y, x_all, _labels, _t = self._panel()
        labels = np.array([f"s{j}" for j in range(self._N_SESSIONS) for _ in range(2)])
        with self.assertRaises(ValueError) as caught:
            ov.score_autocovariance(y=y, x_all=x_all, clusters=labels, coef_index=1, max_lag=1)
        self.assertIn("integer", str(caught.exception))


class TestTheSizeExperimentGrid(unittest.TestCase):
    """Three noise corners per cell, paired draw for draw."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = _Store(Path(self._tmp.name), n_held=45)
        self.report = ov.report(
            labels_dir=self.store.labels,
            briefs_dir=self.store.briefs,
            population=ov.POPULATION_BRIEFED,
            n_sims=2,
            wcb_boot=5,
            seed=99,
            alignment_size=True,
        )

    def test_the_grid_is_still_the_pre_specified_one(self):
        arms = 1 if self.report["fitted_arm_omitted_as_duplicate"] else 2
        self.assertEqual(
            len(self.report["rows"]), len(ov.SHARED_FRACTION_GRID) * arms * len(ov.METHODS)
        )

    def test_every_row_carries_exactly_the_three_corners(self):
        for row in self.report["rows"]:
            self.assertEqual(set(row["arms"]), set(ov.BRACKET_ARMS))

    def test_each_aligned_corner_runs_at_its_own_calibrated_magnitude(self):
        expected = {
            ov.ALIGNMENT_SESSION: ov.GAM_AT_REALIZED_SE_SESSION,
            ov.ALIGNMENT_ROW: ov.GAM_AT_REALIZED_SE_ROW,
        }
        for row in self.report["rows"]:
            for level, gam in expected.items():
                self.assertEqual(row["arms"][level]["alignment"].gam, gam)

    def test_the_homoskedastic_corner_has_no_alignment_at_all(self):
        for row in self.report["rows"]:
            self.assertIsNone(row["arms"]["homoskedastic"]["alignment"])

    def test_the_three_corners_of_a_cell_share_one_seed(self):
        # Paired draw for draw, so a difference between corners is the mechanism
        # and not three independent Monte Carlo draws.
        for row in self.report["rows"]:
            seeds = {row["arms"][arm]["seed"] for arm in ov.BRACKET_ARMS}
            self.assertEqual(len(seeds), 1)

    def test_a_corner_that_missed_the_size_bar_withholds_the_span(self):
        for row in self.report["rows"]:
            missed = [arm for arm in ov.BRACKET_ARMS if not row["arms"][arm]["size_held"]]
            if missed:
                self.assertIsNone(row["power_span"])
                self.assertIsNotNone(row["power_span_withheld_reason"])
            else:
                self.assertIsNotNone(row["power_span"])
                self.assertIsNone(row["power_span_withheld_reason"])

    def test_the_verdict_travels_with_the_report(self):
        self.assertIn("size_verdict", self.report)
        self.assertIn("verdict_line", self.report["size_verdict"])

    def test_a_default_run_carries_no_corners_and_says_so(self):
        plain = ov.report(
            labels_dir=self.store.labels,
            briefs_dir=self.store.briefs,
            population=ov.POPULATION_BRIEFED,
            n_sims=2,
            wcb_boot=5,
            seed=99,
        )
        self.assertFalse(plain["alignment_size"])
        self.assertIsNone(plain["size_verdict"])
        for row in plain["rows"]:
            self.assertIsNone(row["arms"])


class TestEachCornerIsSimulatedAtItsOwnCorner(unittest.TestCase):
    """What ``size_under_alignment`` PASSES, not what it writes down beside it.

    Every assertion on the recorded ``arms`` dict is blind to the call. The cell
    records the :class:`NoiseAlignment` it BUILT and the ``seed`` LITERAL beside
    the call, so a mutation that drops the alignment on its way into the
    simulator, or gives each arm its own random stream, leaves every recorded
    field correct. Measured 2026-11-08 against the ``test_a20*.py`` glob as it
    stood before this class existed, each mutation applied alone. The column is
    the RESULT and not a count: an absolute suite count in a docstring was wrong
    in two consecutive rounds here, which is the rot this repo already recorded
    about line anchors in instruction files, so counts are no longer published
    in prose at all. Re-read the glob yourself if you need its size.

    | mutation                                               | glob, pre-fix |
    |--------------------------------------------------------|---------------|
    | size call's ``alignment=alignments[arm]`` -> ``None``  | all green     |
    | power call's ``alignment=alignments[arm]`` -> ``None`` | all green     |
    | both aligned arms built at ``ALIGNMENT_SESSION``       | all green     |
    | each arm given ``seed + 1000 * index``                 | all green     |

    The only test that looked at the corners ran the driver at ``n_sims=2``,
    ``wcb_boot=5``, where every corner reads size 0.0 and the three are
    indistinguishable. A spy is deterministic and costs one cell; a separation
    test needs ``n_sims=120`` to make the corners part at all.
    """

    @classmethod
    def setUpClass(cls):
        from unittest import mock

        cls.calls: list[dict] = []
        real = ov.rejection_rate

        def spy(**kwargs):
            cls.calls.append(kwargs)
            return real(**kwargs)

        with mock.patch.object(ov, "rejection_rate", spy):
            cls.cell = ov.size_under_alignment(
                null_effects={"atr": 0.0, "ma50": 0.0},
                gate_effects={"atr": 0.4, "ma50": 0.0},
                gam=_TEST_GAM,
                **_two_signal_cell(),
            )

    def _pass(self, name: str) -> list[dict]:
        want = name == "power"
        return [c for c in self.calls if bool(any(c["effects"].values())) is want]

    def _recorded_alignments(self) -> list:
        return [self.cell["arms"][arm]["alignment"] for arm in ov.BRACKET_ARMS]

    def test_both_passes_run_for_every_arm_on_this_fixture(self):
        # The positive control. If a corner missed the size bar here, its power
        # call would never happen and the power case below would be vacuous.
        self.assertEqual((len(self._pass("size")), len(self._pass("power"))), (3, 3))

    def test_the_size_pass_runs_each_arm_at_its_own_alignment(self):
        self.assertEqual([c["alignment"] for c in self._pass("size")], self._recorded_alignments())

    def test_the_power_pass_runs_each_arm_at_the_same_alignment(self):
        # Without this the published span would be three copies of the
        # optimistic corner while printing as if it spanned the identified set.
        self.assertEqual([c["alignment"] for c in self._pass("power")], self._recorded_alignments())

    def test_each_aligned_corner_carries_both_its_magnitude_and_its_LEVEL(self):
        # The magnitude alone does not pin the mechanism: building both aligned
        # corners at the session level collapses two mechanisms to one at two
        # magnitudes, and no assertion on `.gam` can see it.
        passed = [c["alignment"] for c in self._pass("size")]
        self.assertEqual(
            [(a.gam, a.level) for a in passed if a is not None],
            [(_TEST_GAM, ov.ALIGNMENT_SESSION), (_TEST_GAM, ov.ALIGNMENT_ROW)],
        )

    def test_the_seed_each_arm_RECEIVES_is_the_one_recorded_beside_it(self):
        # The pairing claim rests on the seed PASSED, and the recorded field is
        # a literal written next to the call rather than read off it.
        recorded = {self.cell["arms"][arm]["seed"] for arm in ov.BRACKET_ARMS}
        self.assertEqual({c["seed"] for c in self._pass("size")}, recorded)

    def test_the_power_pass_is_paired_on_one_seed_of_its_own(self):
        self.assertEqual(len({c["seed"] for c in self._pass("power")}), 1)


#: Panels per corner in the pairing fixture. FIVE, not two, so "every panel" is
#: more than "the first and the last": de-pairing every panel AFTER the first is
#: a plausible shape (a guard added inside the simulation loop), and at
#: n_sims = 2 a case that skipped panel 1 would still only have one panel left.
#: Raised from three when the class grew to cover the power pass as well, so a
#: break conditioned on a panel index is visible over a wider range of indices.
_PAIRING_SIMS = 5

#: The gate magnitudes the pairing fixture's POWER pass runs at. Named here
#: because the noise case has to rebuild the signal term from them: on the power
#: pass ``y = signal + noise * scale``, so the scale cannot be divided out of
#: ``y`` until the signal is subtracted.
_PAIRING_GATE_EFFECTS = {"atr": 0.4, "ma50": 0.0}
_PAIRING_NULL_EFFECTS = {"atr": 0.0, "ma50": 0.0}


class TestThePairingReachesTheSimulatedPanels(unittest.TestCase):
    """Two corners of one cell draw the SAME panel; the scale is the only difference.

    Pairing is what makes a corner-to-corner difference the MECHANISM rather
    than three independent Monte Carlo draws, so it is asserted on the panels
    THEMSELVES - every panel of the run, as ``rejection_rate`` actually simulated
    them - and on the bootstrap calls it actually made.

    The first version of this class spied on ``rejection_rate``, captured its
    ``seed`` kwarg and then REBUILT one panel per arm from
    ``default_rng(call["seed"])``. That moved the assertion one level closer to
    the panels, not all the way: anything that de-paired the arms INSIDE
    ``rejection_rate``, after the seed was received, was invisible. Measured
    2026-11-08, each mutation applied alone to ``rejection_rate``, against the
    ``test_a20*.py`` glob as it stood that day:

    | mutation                                                     | suite |
    |--------------------------------------------------------------|-------|
    | ``if alignment is not None: rng.normal()`` after the seed     | OK    |
    | per-arm offset on the ``wild_cluster_bootstrap_p`` seed       | OK    |
    | ``if done > 1 and alignment is not None: rng.normal()``       | OK    |

    The second of those matters most for the published figures, because they ARE
    bootstrap p-value rejection rates: the bootstrap draws are half of the
    docstring's "PAIRED draw for draw", and that half was entirely unpinned.

    The second version spied on both seams but recorded a panel only when the
    effects were all zero, so it observed the SIZE pass and never the POWER
    pass. That is the half that matters most: ``power_span`` comes off the power
    pass and the span is the figure this module publishes. Measured the same
    day, each mutation applied alone and scoped to the power pass by
    ``any(effects.values())``, both left the whole glob green:

    | mutation (power pass only)                                   | suite |
    |--------------------------------------------------------------|-------|
    | ``if alignment is not None: rng.normal()`` after the seed     | OK    |
    | ``+ 7`` on the ``wild_cluster_bootstrap_p`` seed              | OK    |

    So this version records BOTH passes, keyed by ``(pass, arm)``, and asserts
    pairing on each separately. The seams are :func:`simulate_overlapping_panel`,
    for the panels, and ``wild_cluster_bootstrap_p``, for the inference stream;
    the bootstrap spy keeps the WHOLE kwargs dict rather than the seed alone, so
    a per-arm ``n_boot`` is caught too.

    Two bounds on what this class can see, so a later reader does not have to
    rediscover them. Pairing is asserted over ``_PAIRING_SIMS`` panels of a
    ``_two_signal_cell``, so a break conditioned on a LARGER panel index, or on
    an ``n_sims`` larger than this fixture's, is outside its reach. And the
    noise case RECONSTRUCTS the scale by calling
    :func:`a20_power._alignment_scale`, the same function the simulator calls,
    so it cannot observe a change to the scale itself - both sides move
    together. What pins the scale is
    ``TestTheMarginalSpreadSurvivesAlignment`` and
    ``TestTheAlignmentFixtureIsNotDegenerate``; this class pins the pairing and
    the composition.
    """

    @classmethod
    def setUpClass(cls):
        from unittest import mock

        import alphalens_research.diagnostics.options_retro as retro

        cls.args = _two_signal_cell(n_sims=_PAIRING_SIMS)
        # Panels per (pass, arm), in simulation order. Both passes, because the
        # power pass is where `power_span` comes from.
        cls.panels: dict[tuple[str, str | None], list] = {}
        # Whole bootstrap kwargs per (pass, arm), in the order they were made.
        cls.boot_calls: dict[tuple[str, str | None], list[dict]] = {}
        # The effects each pass ran at, read off the call rather than restated,
        # so the noise case rebuilds the signal the simulator really injected.
        cls.pass_effects: dict[str, dict[str, float]] = {}
        current: dict[str, Any] = {"key": ("size", None)}

        real_sim = ov.simulate_overlapping_panel
        real_boot = retro.wild_cluster_bootstrap_p

        def sim_spy(**kwargs):
            out = real_sim(**kwargs)
            alignment = kwargs["alignment"]
            arm = None if alignment is None else alignment.level
            pass_name = "power" if any(kwargs["effects"].values()) else "size"
            current["key"] = (pass_name, arm)
            cls.pass_effects[pass_name] = dict(kwargs["effects"])
            cls.panels.setdefault(current["key"], []).append(out)
            return out

        def boot_spy(*args, **kwargs):
            cls.boot_calls.setdefault(current["key"], []).append(dict(kwargs))
            return real_boot(*args, **kwargs)

        with (
            mock.patch.object(ov, "simulate_overlapping_panel", sim_spy),
            mock.patch.object(retro, "wild_cluster_bootstrap_p", boot_spy),
        ):
            cls.cell = ov.size_under_alignment(
                null_effects=dict(_PAIRING_NULL_EFFECTS),
                gate_effects=dict(_PAIRING_GATE_EFFECTS),
                gam=_TEST_GAM,
                **cls.args,
            )

    def _signal(self, pass_name: str, x_all: np.ndarray) -> np.ndarray:
        # The signal term as the simulator composed it: a left fold over the
        # effects in their own order, against the feature columns of `x_all`
        # (column 0 is the intercept). `x_all` is asserted equal across arms by
        # `test_every_corner_draws_the_same_design_matrix_in_every_panel`, so a
        # signal rebuilt from it is the same for all three arms of a pass.
        signal = np.zeros(len(x_all), dtype=float)
        for i, name in enumerate(self.pass_effects[pass_name]):
            signal = signal + self.pass_effects[pass_name][name] * x_all[:, i + 1]
        return signal

    def test_the_spy_saw_every_panel_of_both_passes_of_every_corner(self):
        # The positive control on the instrument itself. Without it, a spy that
        # captured nothing past panel 1, or nothing on the power pass, would
        # make every case below vacuous - which is exactly how the previous two
        # versions of this class failed.
        self.assertEqual(
            {key: len(panels) for key, panels in self.panels.items()},
            {
                (pass_name, arm): _PAIRING_SIMS
                for pass_name in ("size", "power")
                for arm in (None, ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW)
            },
        )

    def test_the_power_pass_really_ran_at_the_gate_magnitudes(self):
        # The power pass exists only for a corner whose size held the bar, so
        # this is the control that the power cases below are not vacuous for a
        # second reason: the fixture's corners DO reach the power pass, and they
        # reach it at the effects this class rebuilds the signal from.
        self.assertEqual(self.pass_effects["power"], _PAIRING_GATE_EFFECTS)
        self.assertEqual(self.pass_effects["size"], _PAIRING_NULL_EFFECTS)
        self.assertEqual(
            [self.cell["arms"][arm]["power"] is None for arm in ov.BRACKET_ARMS],
            [False, False, False],
        )

    def test_every_corner_draws_the_same_design_matrix_in_every_panel(self):
        for pass_name in ("size", "power"):
            home = [panel[1] for panel in self.panels[(pass_name, None)]]
            for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
                for i, (got, want) in enumerate(
                    zip(self.panels[(pass_name, level)], home, strict=True)
                ):
                    with self.subTest(pass_name=pass_name, level=level, panel=i):
                        self.assertTrue(np.array_equal(got[1], want))

    def test_the_underlying_noise_draws_are_the_same_up_to_the_scale_in_every_panel(self):
        sizes = self.args["cluster_sizes"]
        bounds = np.cumsum([0, *sizes])
        for pass_name in ("size", "power"):
            for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
                for i, (aligned, home) in enumerate(
                    zip(
                        self.panels[(pass_name, level)],
                        self.panels[(pass_name, None)],
                        strict=True,
                    )
                ):
                    with self.subTest(pass_name=pass_name, level=level, panel=i):
                        home_y, home_x, _c = home
                        signal = self._signal(pass_name, home_x)
                        xs = [home_x[bounds[j] : bounds[j + 1], 1:] for j in range(len(sizes))]
                        scale = np.concatenate(
                            a20_power._alignment_scale(
                                xs,
                                alignment=ov.NoiseAlignment(_TEST_GAM, level),
                                names=list(self.pass_effects[pass_name]),
                            )
                        )
                        # `y = signal + noise * scale`, so the scale divides out
                        # of the NOISE and not out of `y`. On the size pass the
                        # signal is zero and this is the plain `y * scale`.
                        self.assertTrue(
                            np.allclose(aligned[0] - signal, (home_y - signal) * scale, atol=1e-12)
                        )

    def test_the_bootstrap_stream_is_paired_too(self):
        # The published figures are bootstrap p-value rejection rates, so the
        # bootstrap draws are half of "paired draw for draw". A per-arm bootstrap
        # seed leaves the panels identical and the rejection rates independent.
        # The WHOLE kwargs dict, not the seed alone, so a per-arm `n_boot` -
        # which changes the inference and leaves every seed intact - is caught.
        for pass_name in ("size", "power"):
            home = self.boot_calls[(pass_name, None)]
            for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
                with self.subTest(pass_name=pass_name, level=level):
                    self.assertEqual(self.boot_calls[(pass_name, level)], home)

    def test_the_bootstrap_stream_really_advances_between_panels(self):
        # The control for the case above: a constant seed would make it pass
        # while telling nothing about the sequence.
        for pass_name in ("size", "power"):
            seeds = [call["seed"] for call in self.boot_calls[(pass_name, None)]]
            with self.subTest(pass_name=pass_name):
                self.assertEqual((len(seeds), len(set(seeds))), (_PAIRING_SIMS, _PAIRING_SIMS))

    def test_the_two_passes_are_not_the_same_stream(self):
        # The control that keys the two passes apart: if `sim_spy` ever filed
        # both passes under one key, or the module ran the power pass on the
        # size pass's seed, the cases above would be asserting one stream twice.
        for arm in (None, ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
            with self.subTest(arm=arm):
                self.assertNotEqual(
                    [call["seed"] for call in self.boot_calls[("size", arm)]],
                    [call["seed"] for call in self.boot_calls[("power", arm)]],
                )

    def test_the_scale_is_not_itself_the_identity(self):
        # The refutation control: without it both cases above would pass on a
        # mechanism that quietly did nothing, which is the shape this whole
        # class exists to catch.
        for pass_name in ("size", "power"):
            for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
                for i, (aligned, home) in enumerate(
                    zip(
                        self.panels[(pass_name, level)],
                        self.panels[(pass_name, None)],
                        strict=True,
                    )
                ):
                    with self.subTest(pass_name=pass_name, level=level, panel=i):
                        self.assertFalse(np.array_equal(aligned[0], home[0]))


def _two_signal_cell(**overrides):
    """A two-column panel small enough for a refusal case to be instant."""
    args = {
        "burnt_signals": np.random.default_rng(1).normal(0.0, 1.0, (60, 2)),
        "arrival_offsets": list(range(6)),
        "cluster_sizes": [3] * 6,
        "sd_y": 1.0,
        "icc": 0.4,
        "shared_fraction": 0.5,
        "horizon": 20,
        "method": "arrival",
        "coef_name": "atr",
        "n_sims": 2,
        "wcb_boot": 5,
        "seed": 1,
    }
    return {**args, **overrides}


class TestTheAlignedArmsRefuseAColumnTheyAreNotCalibratedFor(unittest.TestCase):
    """The calibrated pair is a fact about the ATR slope, not about a column index."""

    def test_noise_aligned_with_another_column_is_refused(self):
        # A run testing one column while the noise aligns with another would
        # report a clean size for the wrong reason.
        with self.assertRaises(ValueError) as caught:
            ov.rejection_rate(
                effects={"atr": 0.0, "ma50": 0.0},
                alignment=ov.NoiseAlignment(_TEST_GAM, ov.ALIGNMENT_ROW, feature="ma50"),
                **_two_signal_cell(),
            )
        self.assertIn("ma50", str(caught.exception))

    def test_the_default_magnitudes_are_refused_for_another_coefficient(self):
        with self.assertRaises(ValueError) as caught:
            ov.size_under_alignment(
                null_effects={"atr": 0.0, "ma50": 0.0},
                gate_effects={"atr": 0.2, "ma50": 0.0},
                **_two_signal_cell(coef_name="ma50"),
            )
        self.assertIn("not transferable", str(caught.exception))

    def test_an_explicit_magnitude_lifts_that_refusal(self):
        # The way out the message names has to exist, or the refusal is a dead end.
        cell = ov.size_under_alignment(
            null_effects={"atr": 0.0, "ma50": 0.0},
            gate_effects={"atr": 0.0, "ma50": 0.4},
            gam=_TEST_GAM,
            **_two_signal_cell(coef_name="ma50"),
        )
        self.assertEqual(cell["arms"][ov.ALIGNMENT_ROW]["alignment"].feature, "ma50")


class TestTheSizeRefusalWinsOverTheBracket(unittest.TestCase):
    """A span missing a corner is not a span, so it is withheld with a reason."""

    def test_a_corner_without_a_size_withholds_the_whole_span(self):
        # Forced with an impossible bar rather than hoped for from a fixture:
        # the refusal WINS over the bracket, so a span missing a corner is not
        # published at all and the reason names the corners responsible.
        cell = ov.size_under_alignment(
            null_effects={"atr": 0.0, "ma50": 0.0},
            gate_effects={"atr": 0.4, "ma50": 0.0},
            size_tolerance=-1.0,
            **_two_signal_cell(),
        )
        self.assertIsNone(cell["power_span"])
        for arm in ov.BRACKET_ARMS:
            self.assertIsNone(cell["arms"][arm]["power"])
            self.assertIn(arm, cell["power_span_withheld_reason"])


#: A cell whose three corners really separate, which the cheap fixtures do not.
#: `_two_signal_cell` at n_sims=2 reads power 0.0 at every corner, so
#: `(min, max)`, `(powers[0], powers[0])` and `(max, min)` are the SAME tuple
#: there and no assertion on the span's content can tell them apart. Measured
#: 2026-11-08 on this fixture: homoskedastic 37.5%, session 45.8%, row 8.3% - so
#: the homoskedastic corner is NEITHER end of the span and a mutation is caught
#: in both coordinates.
_SEPARATING_SEED = 19
#: Panels per corner. Small on purpose: this fixture pins arithmetic, not a rate.
_SEPARATING_SIMS = 24
_SEPARATING_GAM = 0.9
#: The size gate is lifted on purpose: what is under test here is the span's
#: ARITHMETIC, and the two-stage refusal has a class of its own. Left at the real
#: bar the aligned corners of this fixture miss it and the span is withheld,
#: which is what `TestTheSizeRefusalWinsOverTheBracket` already pins.
_SPAN_FIXTURE_TOLERANCE = 1.0
#: An ATR effect strong enough that the corners land AROUND the 80% power bar
#: rather than below it, so the span's own Monte Carlo error crosses the bar and
#: `PowerBracket.is_resolved` must answer False. Measured 2026-11-08 at
#: `_SEPARATING_SIMS` panels: 75.0% / 70.8% / 41.7%, and the first two Wilson
#: intervals straddle 80%. Without a fixture on this side of the predicate the
#: assertion below cannot tell `is_resolved(...)` from the literal `True`.
_STRADDLING_GATE_EFFECT = 0.5


def _separating_cell(**overrides):
    """One cell at three measurably different corners."""
    rng = np.random.default_rng(9)
    atr = rng.lognormal(0.0, 0.6, size=200)
    ma50 = 0.5 * _z(atr) + rng.normal(size=200)
    args = {
        "burnt_signals": np.column_stack([_z(atr), _z(ma50)]),
        "arrival_offsets": list(range(10)),
        "cluster_sizes": [5] * 10,
        "null_effects": {"atr": 0.0, "ma50": 0.0},
        "gate_effects": {"atr": 0.30, "ma50": 0.0},
        "sd_y": 1.0,
        "icc": 0.2,
        "shared_fraction": 0.5,
        "horizon": 20,
        "method": "arrival",
        "coef_name": "atr",
        "n_sims": _SEPARATING_SIMS,
        "wcb_boot": 19,
        "seed": _SEPARATING_SEED,
        "gam": _SEPARATING_GAM,
        "size_tolerance": _SPAN_FIXTURE_TOLERANCE,
    }
    return ov.size_under_alignment(**{**args, **overrides})


class TestThePowerSpanIsTheSpanOfItsArms(unittest.TestCase):
    """The span's CONTENT, which nothing asserted until 2026-11-08.

    Every existing case on ``power_span`` was ``assertIsNone`` or
    ``assertIsNotNone``. Three mutations left the whole suite green: returning
    ``(powers[0], powers[0])``, returning ``(max, min)`` REVERSED so the
    optimistic end reads as the pessimistic one, and printing
    ``span[1]..span[1]``. The reversal is the worst of the three, because the
    number a reader would then quote as the bound is the corner this programme
    measured 1.36x optimistic.
    """

    @classmethod
    def setUpClass(cls):
        cls.cell = _separating_cell()

    def _powers(self):
        return [self.cell["arms"][arm]["power"] for arm in ov.BRACKET_ARMS]

    def test_the_three_corners_really_separate_on_this_fixture(self):
        # The positive control. On a fixture where every corner reads the same
        # power, not one of the cases below can fail.
        powers = self._powers()
        self.assertEqual(len(set(powers)), 3)
        self.assertNotIn(powers[0], (min(powers), max(powers)))

    def test_the_span_is_the_lowest_and_highest_corner(self):
        self.assertEqual(self.cell["power_span"], (min(self._powers()), max(self._powers())))

    def test_the_span_runs_low_to_high(self):
        low, high = self.cell["power_span"]
        self.assertLessEqual(low, high)

    def test_the_span_resolution_is_the_siblings_own_predicate(self):
        # Reused rather than re-implemented: a span whose own Monte Carlo error
        # crosses the bar is not an answer, and `PowerBracket.is_resolved` is
        # already that guard. Built here from the same arms the cell carries.
        bracket = a20_power.PowerBracket(
            arms={arm: {"atr": self.cell["arms"][arm]["power"]} for arm in ov.BRACKET_ARMS},
            alignments={arm: self.cell["arms"][arm]["alignment"] for arm in ov.BRACKET_ARMS},
            target="atr",
            shrinkage=a20_power.GATE_SHRINKAGE,
            n_sims=_SEPARATING_SIMS,
        )
        self.assertIs(
            self.cell["power_span_resolved"], bracket.is_resolved(bar=a20_power.GATE_POWER)
        )

    def test_this_fixtures_span_really_resolves(self):
        # Named rather than left implicit. The case above is an `assertIs`
        # against the predicate's value on THIS fixture, so a reader has to know
        # which value that is before the unresolved case below means anything.
        self.assertIs(self.cell["power_span_resolved"], True)

    def test_a_span_straddling_the_gate_bar_does_not_resolve(self):
        # The other side of the predicate. Replacing the whole
        # `PowerBracket(...).is_resolved(bar=GATE_POWER)` expression with the
        # literal `True` left the whole ``test_a20*.py`` glob green (measured
        # 2026-11-08), because the fixture above
        # happens to resolve: `assertIs(cell[...], bracket.is_resolved(...))`
        # cannot tell the predicate from the constant when both read True. This
        # fixture makes the False value reachable, and False is the direction
        # that matters - it is the one that turns an unresolved span into an
        # answer.
        cell = _separating_cell(
            gate_effects={"atr": _STRADDLING_GATE_EFFECT, "ma50": 0.0},
        )
        self.assertIs(cell["power_span_resolved"], False)

    def test_the_resolution_is_asked_of_the_sibling_at_the_sibling_s_bar(self):
        # The predicate is REUSED, not re-implemented, and it is asked at
        # `GATE_POWER`. A second copy of the arithmetic, or the same call at a
        # different bar, is the thing that would drift.
        from unittest import mock

        with mock.patch.object(
            a20_power.PowerBracket, "is_resolved", autospec=True, return_value=False
        ) as spy:
            cell = _separating_cell()
        self.assertEqual(
            [call.kwargs["bar"] for call in spy.call_args_list], [a20_power.GATE_POWER]
        )
        self.assertIs(cell["power_span_resolved"], False)

    def test_a_withheld_span_has_no_resolution_either(self):
        withheld = _separating_cell(size_tolerance=-1.0)
        self.assertIsNone(withheld["power_span"])
        self.assertIsNone(withheld["power_span_resolved"])


class TestThePrintedSpanIsTheSpan(unittest.TestCase):
    """The printed form, on both of its branches.

    ``span[0]..span[1]`` -> ``span[1]..span[1]`` left the suite green: no case
    read the two numbers off the screen. The renderer is extracted from
    :func:`a20_overlap_power.main` for a second reason - inline, the only
    reachable fixture was one where every span RESOLVES, so the unresolved
    rendering could not be asserted at all.
    """

    _SPAN = (0.083, 0.458)

    def test_a_resolved_span_prints_both_ends(self):
        self.assertEqual(ov.format_power_span(self._SPAN, resolved=True), "8.3-45.8%")

    def test_an_unresolved_span_carries_the_mark(self):
        self.assertEqual(
            ov.format_power_span(self._SPAN, resolved=False),
            f"8.3-45.8%{ov.UNRESOLVED_SPAN_MARK}",
        )

    def test_a_missing_span_prints_as_withheld(self):
        self.assertEqual(ov.format_power_span(None, resolved=None), ov.WITHHELD_SPAN)


class TestTheTwoBarsOnScreenAreNamedApart(unittest.TestCase):
    """Two different quantities used to be printed two lines apart, both as "the gate".

    ``size bar ... ; power at the 50% gate`` carried a hardcoded ``50%``
    duplicating :data:`a20_power.GATE_SHRINKAGE` - the SHRINKAGE of the
    alternative - beside two interpolated constants on the same line, while the
    next block named :data:`a20_power.GATE_POWER` (80%) as "the gate bar". The
    literal could drift from the constant, and the two labels read as each other.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path

        cls._tmp = tempfile.TemporaryDirectory()
        store = _Store(Path(cls._tmp.name), n_held=45)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ov.main(
                [
                    "--labels-dir",
                    str(store.labels),
                    "--briefs-dir",
                    str(store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "5",
                    "--alignment-size",
                ]
            )
        cls.printed = buffer.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_shrinkage_is_interpolated_from_the_siblings_constant(self):
        self.assertIn(f"{a20_power.GATE_SHRINKAGE:.0%} shrinkage", self.printed)

    def test_the_power_bar_is_interpolated_from_the_siblings_constant(self):
        self.assertIn(f"{a20_power.GATE_POWER:.0%} POWER bar", self.printed)

    def test_neither_quantity_is_printed_as_the_gate(self):
        # The defect was the WORD, not the number: both lines said "gate".
        self.assertNotIn("gate", self.printed)


class TestTheScreenShowsTheSpanTheArtifactRecords(unittest.TestCase):
    """Every span column on screen is :func:`format_power_span` of that row's fields."""

    @classmethod
    def setUpClass(cls):
        import json
        import tempfile
        from pathlib import Path

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        store = _Store(root, n_held=45)
        out_json = root / "span.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ov.main(
                [
                    "--labels-dir",
                    str(store.labels),
                    "--briefs-dir",
                    str(store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "5",
                    "--alignment-size",
                    "--out-json",
                    str(out_json),
                ]
            )
        cls.printed = buffer.getvalue()
        cls.written = json.loads(out_json.read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_last_column_of_every_table_row_is_the_rendered_span(self):
        columns = [
            line.rsplit("|", 1)[1].strip()
            for line in self.printed.splitlines()
            if re.match(r"^ *\d\.\d{2} +kappa=", line)
        ]
        self.assertEqual(
            columns,
            [
                ov.format_power_span(
                    None if row["power_span"] is None else tuple(row["power_span"]),
                    resolved=row["power_span_resolved"],
                )
                for row in self.written["rows"]
            ],
        )

    def test_this_fixture_really_prints_a_span_rather_than_withheld(self):
        # The positive control: with every span withheld the case above would
        # compare two lists of the word "withheld".
        self.assertTrue([row["power_span"] for row in self.written["rows"] if row["power_span"]])


class TestTheArtifactKeepsTheAlignmentMagnitudes(unittest.TestCase):
    """``json.dumps(default=str)`` would record a dataclass repr, not a number."""

    @classmethod
    def setUpClass(cls):
        import json
        import tempfile
        from pathlib import Path

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        store = _Store(root, n_held=45)
        out_json = root / "aligned.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ov.main(
                [
                    "--labels-dir",
                    str(store.labels),
                    "--briefs-dir",
                    str(store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "5",
                    "--alignment-size",
                    "--out-json",
                    str(out_json),
                ]
            )
        cls.printed = buffer.getvalue()
        cls.written = json.loads(out_json.read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_each_aligned_corner_records_its_magnitude_as_a_number(self):
        for row in self.written["rows"]:
            for level in (ov.ALIGNMENT_SESSION, ov.ALIGNMENT_ROW):
                gam = row["arms"][level]["alignment"]["gam"]
                self.assertIsInstance(gam, float)

    def test_the_verdict_line_reaches_the_screen(self):
        self.assertIn(self.written["size_verdict"]["verdict_line"], self.printed)

    def test_the_aligned_run_does_not_claim_the_arms_were_skipped(self):
        self.assertNotIn("aligned arms were NOT run", self.printed)

    def test_the_aligned_run_carries_no_default_run_notice_in_the_artifact_either(self):
        # The control on the field: a notice written unconditionally would say
        # the aligned arms were skipped on the run that ran them.
        self.assertIsNone(self.written["default_run_notice"])

    def test_both_surfaces_say_kappa_and_gam_are_not_substitutes(self):
        # A reader who only ever sees the JSON would otherwise take the two
        # mechanisms for alternatives, which the fitted-kappa arm invites.
        self.assertEqual(self.written["composed_mechanisms_note"], ov.COMPOSED_MECHANISMS_NOTE)
        self.assertIn(ov.COMPOSED_MECHANISMS_NOTE, self.printed)


class TestTheOverlapAlignmentIsNotASecondCopy(unittest.TestCase):
    """The no-fork rule, carried measurably instead of by a prohibition.

    The merged version of this file forbade an ``alignment`` parameter here
    outright, on the grounds that a forked mechanism in two modules is how the two
    silently diverge. That reasoning is kept; the enforcement is stronger. The
    mechanism is IMPORTED, and the phi = 0 aligned path is pinned bit-identical to
    the sibling's aligned path — a prohibition stops a parameter existing, this
    catches an actual divergence.
    """

    def test_the_scale_function_is_the_siblings_own_object(self):
        self.assertIs(ov._alignment_scale, a20_power._alignment_scale)

    def test_the_alignment_type_is_the_siblings_own_class(self):
        self.assertIs(ov.NoiseAlignment, a20_power.NoiseAlignment)

    def test_this_module_defines_no_scale_function_of_its_own(self):
        import inspect

        self.assertNotIn("def _alignment_scale", inspect.getsource(ov))


#: One synthetic size for every arm of a cell, well inside the tolerance.
_SAFE_SIZE = 0.05
#: A synthetic size far enough above ``SIZE_TOLERANCE`` that 4 000 draws resolve it.
_CROSSING_SIZE = 0.14
#: Simulation counts that make the Wilson interval around the two sizes above
#: narrower (4 000) or wider (40) than the distance to the tolerance.
_RESOLVING_SIMS = 4000
_STRADDLING_SIMS = 40


def _arm_cell(size: float) -> dict:
    """One arm of one synthetic cell. Only ``size`` drives the verdict."""
    return {"size": size, "power": 0.5}


#: The calibrated pair, as `report` records it. Passed to `size_verdict` so the
#: verdict line can name the magnitudes that produced its figures; without it
#: the only gam on screen came from the precision disclosure, which quotes the
#: CALIBRATED pair whatever `--alignment-gam` was set to.
_MAGNITUDES = {
    ov.ALIGNMENT_SESSION: ov.GAM_AT_REALIZED_SE_SESSION,
    ov.ALIGNMENT_ROW: ov.GAM_AT_REALIZED_SE_ROW,
}


def _synthetic_rows(
    *,
    session_size: float,
    row_size: float,
    home_size: float = _SAFE_SIZE,
    loading: str = ov.COMPOSED_LOADING,
    shared_fraction: float = 1.0,
) -> list:
    """One grid cell, three arms, nothing simulated.

    The verdict must be testable without a simulation: a verdict function that
    could only be exercised through 72 cells of bootstrap would never be pinned
    in both directions.
    """
    return [
        {
            "shared_fraction": shared_fraction,
            "loading": loading,
            "method": "arrival",
            "size": home_size,
            "arms": {
                "homoskedastic": _arm_cell(home_size),
                ov.ALIGNMENT_SESSION: _arm_cell(session_size),
                ov.ALIGNMENT_ROW: _arm_cell(row_size),
            },
        }
    ]


def _verdict(rows, *, n_sims=_RESOLVING_SIMS, magnitudes=_MAGNITUDES):
    return ov.size_verdict(
        rows, size_tolerance=ov.SIZE_TOLERANCE, n_sims=n_sims, magnitudes=magnitudes
    )


class TestTheSizeVerdictLine(unittest.TestCase):
    """The claim under test is reversible, so the verdict is pinned both ways."""

    def test_no_crossing_and_matching_holding_sets_let_the_claim_survive(self):
        verdict = _verdict(_synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE))
        self.assertIs(verdict["claim_survives"], True)

    def test_the_surviving_line_says_so(self):
        verdict = _verdict(_synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE))
        self.assertIn("survives", verdict["verdict_line"])

    def test_a_resolved_crossing_refutes_the_claim(self):
        verdict = _verdict(_synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE))
        self.assertIs(verdict["claim_survives"], False)

    def test_the_refuting_crossing_is_listed_with_its_cell(self):
        verdict = _verdict(_synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE))
        self.assertEqual(
            [
                (c["shared_fraction"], c["loading"], c["method"], c["arm"])
                for c in verdict["crosses_only_under_alignment"]
            ],
            [(1.0, "kappa=fitted", "arrival", ov.ALIGNMENT_SESSION)],
        )

    def test_the_refuting_line_names_the_arm_that_crossed(self):
        verdict = _verdict(_synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE))
        self.assertIn(ov.ALIGNMENT_SESSION, verdict["verdict_line"])

    def test_an_unresolved_crossing_is_no_verdict_at_all(self):
        verdict = _verdict(
            _synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE),
            n_sims=_STRADDLING_SIMS,
        )
        self.assertIsNone(verdict["claim_survives"])

    def test_an_unresolved_crossing_never_becomes_the_finding(self):
        # Monte Carlo noise must not be able to produce the result. The cell is
        # reported as unresolved and is absent from the crossing list.
        verdict = _verdict(
            _synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE),
            n_sims=_STRADDLING_SIMS,
        )
        self.assertEqual(verdict["crosses_only_under_alignment"], [])
        self.assertTrue(verdict["unresolved"])


class TestTheVerdictCarriesBothReadings(unittest.TestCase):
    """A composed-mechanism cell cannot refute silently.

    The refutation this instrument produced sits in the cell the module itself
    calls double-counted. On an independent 2000-panel run at the memo's
    conservative corner (phi 1.00, kappa fitted, method arrival, bar 7.5%), the
    arrival-session bootstrap reads 5.90% [4.95, 7.02] homoskedastic against
    9.70% [8.48, 11.08] row-aligned - disjoint, a resolved crossing. At kappa = 0
    nothing crosses (4.05 / 5.20 / 5.90%, all resolved held), and at the
    burnt-panel MEASURED magnitudes the same cell reads 8.20% [7.08, 9.48] and
    resolves nothing. So the refutation holds at the CALIBRATED magnitudes, in
    the cell that applies the feature-dependence twice - and the verdict reported
    it flat.

    The difference between "the claim is refuted" and "the claim is refuted at
    these magnitudes in this cell" is the whole content of this class.
    """

    def _composed_only(self):
        # Refuted in the kappa=fitted cell, held in the kappa=0 cell.
        return _synthetic_rows(
            session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE, loading=ov.COMPOSED_LOADING
        ) + _synthetic_rows(
            session_size=_SAFE_SIZE, row_size=_SAFE_SIZE, loading=ov.SINGLE_MECHANISM_LOADING
        )

    def _both_loadings_cross(self):
        return _synthetic_rows(
            session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE, loading=ov.COMPOSED_LOADING
        ) + _synthetic_rows(
            session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE, loading=ov.SINGLE_MECHANISM_LOADING
        )

    def test_the_verdict_carries_a_second_single_mechanism_reading(self):
        self.assertIsNotNone(_verdict(self._composed_only())["single_mechanism"])

    def test_the_second_reading_is_the_kappa_zero_rows_alone(self):
        # Computed on the single-mechanism rows only, so it must agree with the
        # verdict of a table that holds nothing else.
        rows = self._composed_only()
        alone = [r for r in rows if r["loading"] == ov.SINGLE_MECHANISM_LOADING]
        self.assertEqual(
            _verdict(rows)["single_mechanism"]["crosses_only_under_alignment"],
            _verdict(alone)["crosses_only_under_alignment"],
        )

    def test_a_refutation_only_in_a_composed_cell_is_flagged_composed(self):
        verdict = _verdict(self._composed_only())
        self.assertIs(verdict["claim_survives"], False)
        self.assertIs(verdict["refutation_is_composed"], True)

    def test_the_verdict_line_says_the_refutation_is_composed(self):
        # The rule this class exists for: a composed cell may set
        # claim_survives False only if the OUTPUT says the refutation is
        # composed.
        self.assertIn("COMPOSED", _verdict(self._composed_only())["verdict_line"])

    def test_the_verdict_line_says_what_the_single_mechanism_rows_do(self):
        self.assertIn(ov.SINGLE_MECHANISM_LOADING, _verdict(self._composed_only())["verdict_line"])

    def test_a_refutation_the_single_mechanism_rows_share_is_not_composed(self):
        verdict = _verdict(self._both_loadings_cross())
        self.assertIs(verdict["claim_survives"], False)
        self.assertIs(verdict["refutation_is_composed"], False)

    def test_a_surviving_verdict_has_no_composition_to_report(self):
        # THREE states, not two. `False` used to mean both "there was no
        # refutation" and "there was a single-mechanism refutation", so the
        # screen printed "refutation is composed: False" about a refutation that
        # does not exist. `None` is the third answer, and `refuting_branch` says
        # which branch produced the refutation when there is one.
        verdict = _verdict(_synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE))
        self.assertIs(verdict["claim_survives"], True)
        self.assertIsNone(verdict["refuting_branch"])
        self.assertIsNone(verdict["refutation_is_composed"])

    def test_a_composed_crossing_beside_a_single_mechanism_ordering_change(self):
        # Every `or` branch measured rather than assumed. A crossing in the
        # kappa=0 subset is also a crossing in the full grid, so `composed` and
        # "the single rows refute it too" look mutually exclusive - but the
        # single reading can refute through the ORDERING branch instead, which
        # does not need a crossing. Built here: the composed cell crosses, and
        # the kappa=0 cell has the homoskedastic corner MISSING the bar while
        # both aligned corners hold it, so the holding sets differ with no
        # crossing at all.
        rows = _synthetic_rows(
            session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE, loading=ov.COMPOSED_LOADING
        ) + _synthetic_rows(
            session_size=_SAFE_SIZE,
            row_size=_SAFE_SIZE,
            home_size=_CROSSING_SIZE,
            loading=ov.SINGLE_MECHANISM_LOADING,
        )
        verdict = _verdict(rows)
        self.assertIs(verdict["refutation_is_composed"], True)
        self.assertIs(verdict["single_mechanism"]["claim_survives"], False)
        self.assertIn("on their own as well", verdict["verdict_line"])

    def test_a_table_with_no_single_mechanism_rows_has_no_second_reading(self):
        # A `--single-mechanism-only` run is the mirror case and must not
        # manufacture a reading it does not have.
        rows = _synthetic_rows(
            session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE, loading=ov.COMPOSED_LOADING
        )
        self.assertIsNone(_verdict(rows)["single_mechanism"])


#: An ORDERING-branch refutation: no corner crosses the bar, but the
#: homoskedastic corner MISSES it while both aligned corners hold it, so the
#: holding sets differ with zero crossing cells.
def _ordering_rows(*, loading=ov.COMPOSED_LOADING):
    return _synthetic_rows(
        session_size=_SAFE_SIZE,
        row_size=_SAFE_SIZE,
        home_size=_CROSSING_SIZE,
        loading=loading,
    )


class TestEachRefutingBranchNamesOnlyWhatItMeasured(unittest.TestCase):
    """The composition sentence belongs to the branch that refuted, not to one of them.

    ``claim_survives`` goes ``False`` on TWO branches. Composition used to be
    read off ``crosses_only_under_alignment`` on both, and on the ORDERING branch
    that list is EMPTY by construction - that branch only runs when nothing
    crossed. So ``composed`` was forced ``False`` there and the verdict printed,
    in one block: "no corner crosses the bar", then "the refutation is NOT
    composed: at least one crossing cell is a kappa=0 cell", then a second
    reading saying the kappa=0 rows SURVIVE. Three mutually contradictory
    sentences about a table with no crossing cell at all.

    The ordering branch now decides from the cells whose HOLDING status differs
    between corners, which is the quantity that branch measured, and the sentence
    names that quantity by name.
    """

    _CROSSING_NOUN = "crossing cell"
    _ORDERING_NOUN = "holding status differs between corners"

    def _crossing(self, *, loading=ov.COMPOSED_LOADING):
        return _verdict(
            _synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE, loading=loading)
        )

    def test_the_crossing_branch_is_the_one_this_table_takes(self):
        # The positive control on the fixture, so the wording cases below are
        # about the branch they claim to be about.
        verdict = self._crossing()
        self.assertEqual(verdict["refuting_branch"], ov.REFUTING_BRANCH_CROSSING)
        self.assertTrue(verdict["crosses_only_under_alignment"])

    def test_the_ordering_branch_is_the_one_that_table_takes(self):
        verdict = _verdict(_ordering_rows())
        self.assertEqual(verdict["refuting_branch"], ov.REFUTING_BRANCH_ORDERING)
        self.assertEqual(verdict["crosses_only_under_alignment"], [])

    def test_the_crossing_branch_names_the_crossing_cells(self):
        self.assertIn(self._CROSSING_NOUN, self._crossing()["verdict_line"])

    def test_the_ordering_branch_never_names_a_crossing_cell(self):
        # The defect, stated as the assertion: zero crossing cells, so the
        # sentence must not claim one either way.
        self.assertNotIn(self._CROSSING_NOUN, _verdict(_ordering_rows())["verdict_line"])

    def test_the_ordering_branch_names_the_quantity_it_measured(self):
        self.assertIn(self._ORDERING_NOUN, _verdict(_ordering_rows())["verdict_line"])

    def test_the_line_and_the_flag_cannot_disagree_on_any_branch(self):
        # The sentence and the recorded flag are built from the same cells, so
        # "COMPOSED" on screen and `refutation_is_composed: false` in the
        # artifact must not be able to ship together. Both refuting branches and
        # both loadings, because the pre-fix code could produce exactly that
        # pair on the ordering branch.
        for name, rows in (
            ("crossing", _synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE)),
            ("ordering", _ordering_rows()),
            (
                "crossing-single",
                _synthetic_rows(
                    session_size=_CROSSING_SIZE,
                    row_size=_SAFE_SIZE,
                    loading=ov.SINGLE_MECHANISM_LOADING,
                ),
            ),
            ("ordering-single", _ordering_rows(loading=ov.SINGLE_MECHANISM_LOADING)),
        ):
            with self.subTest(table=name):
                verdict = _verdict(rows)
                says_composed = "is COMPOSED" in verdict["verdict_line"]
                self.assertIs(says_composed, verdict["refutation_is_composed"])

    def test_the_ordering_branch_calls_a_composed_only_refutation_composed(self):
        # Every differing cell is a kappa=fitted cell, so the refutation IS
        # composed. It used to be reported as NOT composed, with a sentence about
        # a kappa=0 crossing cell in a table that had neither.
        verdict = _verdict(_ordering_rows(loading=ov.COMPOSED_LOADING))
        self.assertIs(verdict["claim_survives"], False)
        self.assertIs(verdict["refutation_is_composed"], True)

    def test_the_ordering_branch_calls_a_single_mechanism_refutation_not_composed(self):
        # The other direction, so the case above is not passing on a constant.
        verdict = _verdict(_ordering_rows(loading=ov.SINGLE_MECHANISM_LOADING))
        self.assertIs(verdict["claim_survives"], False)
        self.assertIs(verdict["refutation_is_composed"], False)
        self.assertIn(ov.SINGLE_MECHANISM_LOADING, verdict["verdict_line"])

    def test_an_ordering_refutation_from_a_composed_cell_alone_reads_coherently(self):
        # The verifier's own probe: the ordering difference comes only from the
        # kappa=fitted cell, while the kappa=0 rows hold identically at all three
        # corners. All three sentences in the block must agree now.
        rows = _ordering_rows(loading=ov.COMPOSED_LOADING) + _synthetic_rows(
            session_size=_SAFE_SIZE, row_size=_SAFE_SIZE, loading=ov.SINGLE_MECHANISM_LOADING
        )
        verdict = _verdict(rows)
        self.assertIs(verdict["refutation_is_composed"], True)
        self.assertIs(verdict["single_mechanism"]["claim_survives"], True)
        self.assertNotIn(self._CROSSING_NOUN, verdict["verdict_line"])

    def test_a_surviving_verdict_names_no_refuting_branch(self):
        verdict = _verdict(_synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE))
        self.assertIsNone(verdict["refuting_branch"])

    def test_a_refuting_branch_always_has_a_cell_to_name(self):
        # The invariant that lets `_which_reading_refuted` have no "no cell to
        # name" case, so the function carries no unreachable branch. An
        # unresolved method sends the table to the UNRESOLVED branch, which takes
        # precedence, so in the ordering branch the holding sets can differ only
        # through a per-row hold difference. Checked exhaustively off-suite over
        # all 3**12 held / over / unresolved assignments; pinned here on the two
        # fixtures the suite has.
        for name, rows in (
            ("crossing", _synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE)),
            ("ordering", _ordering_rows()),
        ):
            with self.subTest(branch=name):
                verdict = _verdict(rows)
                key = ov._DECIDING_CELLS[verdict["refuting_branch"]]
                self.assertTrue(verdict[key])


class TestAnEmptyMeasurementIsNoVerdict(unittest.TestCase):
    """Zero methods entitled to a power figure is not the claim surviving.

    The refusal above catches an empty TABLE. This is an empty MEASUREMENT: a
    grid where no method holds the size bar at ANY corner used to return
    ``claim_survives: True`` with "the methods holding size are the same at all
    three corners (none)". The claim under test is about WHICH method wins under
    alignment, so with no winner anywhere there is nothing for alignment to leave
    unaffected.
    """

    def _nothing_holds(self):
        return _synthetic_rows(
            session_size=_CROSSING_SIZE, row_size=_CROSSING_SIZE, home_size=_CROSSING_SIZE
        )

    def test_a_table_where_nothing_holds_is_no_verdict(self):
        self.assertIsNone(_verdict(self._nothing_holds())["claim_survives"])

    def test_that_table_really_has_no_crossing_and_no_ordering_change(self):
        # The positive control: without it this could be passing through the
        # crossing or the unresolved branch and say nothing about the new one.
        verdict = _verdict(self._nothing_holds())
        self.assertEqual(verdict["crosses_only_under_alignment"], [])
        self.assertEqual(verdict["unresolved"], [])
        self.assertIs(verdict["ordering_changed"], False)

    def test_the_line_says_no_method_holds_at_any_corner(self):
        self.assertIn(
            "NO method holds the size bar", _verdict(self._nothing_holds())["verdict_line"]
        )

    def test_this_branch_reports_its_holding_sets_as_the_measurements_they_are(self):
        # The other half of the branch: every corner here IS resolved against
        # the bar, so the empty holding sets and `ordering_changed: False` are
        # real readings and belong in the payload. The unresolved branch is the
        # one that withholds them.
        verdict = _verdict(self._nothing_holds())
        self.assertEqual(verdict["no_verdict_cause"], ov.NO_VERDICT_NOTHING_HOLDS)
        self.assertIs(verdict["holding_sets_are_measurements"], True)
        self.assertEqual(verdict["methods_holding_size"], dict.fromkeys(ov.BRACKET_ARMS, ()))
        self.assertIs(verdict["ordering_changed"], False)

    def test_one_holding_method_is_enough_for_the_claim_to_survive(self):
        # The positive control on the other side: the same table shape with a
        # method that holds must still return True, or the branch above would be
        # swallowing real surviving verdicts.
        verdict = _verdict(_synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE))
        self.assertIs(verdict["claim_survives"], True)
        self.assertEqual(verdict["methods_holding_size"][ov.HOMOSKEDASTIC_ARM], ("arrival",))


#: A size that is not a measurement at all. Both reach the comparisons as
#: `nan <= tolerance` -> False ("did not hold") and `verdict_is_resolved(nan, n)`
#: -> True ("resolved"), which is how an all-NaN table reported the claim
#: SURVIVING with zero unresolved cells.
_NOT_A_SIZE = float("nan")


class TestTheUnresolvedSentenceCountsWhatItNames(unittest.TestCase):
    """ "N cell(s)" has to be a count of cells.

    ``unresolved`` pools two SHAPES: a cell-shaped entry with four coordinates
    (phi, loading, method, arm), and a method-corner entry with two (arm, method)
    that speaks about a method's status over the WHOLE grid. Printed as "N
    cell(s) it depends on are unresolved", a four-cell table reported 12 - which
    was 3 arms x 4 methods and not a count of cells at all. A reader told to
    "Re-run with more than --n-sims 2000" was given a number that does not
    describe the grid.
    """

    def _straddling(self):
        # One cell-shaped entry and three method-corner ones, so the two counts
        # DIFFER: a fixture where they agreed could not tell them apart, and
        # their sum (4) is a third number again.
        return _verdict(
            _synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE),
            n_sims=_STRADDLING_SIMS,
        )

    def test_this_fixture_really_produces_both_shapes_and_they_differ(self):
        verdict = self._straddling()
        self.assertEqual(verdict["no_verdict_cause"], ov.NO_VERDICT_UNRESOLVED)
        self.assertEqual(
            (len(verdict["unresolved_cells"]), len(verdict["unresolved_method_corners"])), (1, 3)
        )

    def test_the_two_shapes_really_are_different_shapes(self):
        # The control on the split itself: a cell-shaped entry carries the four
        # grid coordinates, a method-corner entry carries two.
        verdict = self._straddling()
        for cell in verdict["unresolved_cells"]:
            self.assertIn("shared_fraction", cell)
        for corner in verdict["unresolved_method_corners"]:
            self.assertNotIn("shared_fraction", corner)

    def test_the_sentence_prints_both_counts_and_neither_is_their_sum(self):
        verdict = self._straddling()
        line = verdict["verdict_line"]
        self.assertIn(
            f"{len(verdict['unresolved_cells'])} cell(s) and "
            f"{len(verdict['unresolved_method_corners'])} method-corner(s)",
            line,
        )
        self.assertNotIn(f"{len(verdict['unresolved'])} cell(s)", line)

    def test_the_pooled_list_is_still_the_union_of_the_two(self):
        # Every existing consumer reads `unresolved` as "everything the verdict
        # could not resolve", so the split adds two lists rather than replacing
        # one.
        verdict = self._straddling()
        self.assertEqual(
            verdict["unresolved"],
            verdict["unresolved_cells"] + verdict["unresolved_method_corners"],
        )


class TestTheBracketNamesTheGridThatWasMeasured(unittest.TestCase):
    """A construction bracket is a claim about what was run.

    ``size_verdict``'s refusal is ``not any(row.get("arms"))``, so ONE measured
    row among 24 passes it - and the bracket was then built from ALL the rows,
    advertising a phi/loading/method grid that was never run at three corners.
    The second reading's bracket on the same table was already honest, because
    ``single_rows`` filters on ``arms``.
    """

    def _mixed(self):
        measured = _synthetic_rows(
            session_size=_SAFE_SIZE,
            row_size=_SAFE_SIZE,
            loading=ov.SINGLE_MECHANISM_LOADING,
            shared_fraction=0.0,
        )
        unmeasured = []
        for phi in (0.5, 1.0):
            for loading in (ov.SINGLE_MECHANISM_LOADING, ov.COMPOSED_LOADING):
                for method in ("block5", "block10"):
                    unmeasured.append(
                        {
                            "shared_fraction": phi,
                            "loading": loading,
                            "method": method,
                            "size": None,
                            "arms": None,
                        }
                    )
        return measured + unmeasured

    def test_the_bracket_names_only_the_measured_coordinates(self):
        line = _verdict(self._mixed())["verdict_line"]
        self.assertIn("phi 0.00;", line)
        self.assertIn(f"loading {ov.SINGLE_MECHANISM_LOADING};", line)
        self.assertIn("method arrival]", line)

    def test_the_bracket_names_none_of_the_unmeasured_ones(self):
        # The direction that matters: an unmeasured phi, loading or method in
        # the bracket is the instrument advertising a grid it did not run.
        line = _verdict(self._mixed())["verdict_line"]
        for absent in ("0.50", "1.00", ov.COMPOSED_LOADING, "block5", "block10"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, line)

    def test_the_mixed_table_is_not_refused(self):
        # The positive control: the refusal catches an empty TABLE, and this
        # table is not empty, so the bracket is reached.
        self.assertIs(_verdict(self._mixed())["claim_survives"], True)


class TestTheVerdictRefusesASizeThatIsNotAMeasurement(unittest.TestCase):
    """A missing corner, a non-numeric size and a NaN size are all the same refusal.

    Measured 2026-11-08 on the pre-fix module: a table whose nine sizes were all
    NaN returned ``claim_survives: True`` with zero unresolved cells; a table
    whose two aligned corners alone were NaN returned ``claim_survives: False``
    with ``refutation_is_composed: True`` and two crossing cells, printed as
    ``nan%`` on screen and written to ``--out-json`` with no NaN marker on it. A
    row carrying only some corners raised a bare ``KeyError: 'row'`` and a size
    of ``None`` a bare ``TypeError``, neither naming the cell or a remedy.
    """

    def _rows(self, **sizes):
        return _synthetic_rows(**sizes)

    def test_an_all_nan_table_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            _verdict(
                self._rows(home_size=_NOT_A_SIZE, session_size=_NOT_A_SIZE, row_size=_NOT_A_SIZE)
            )
        self.assertIn("not a rejection rate", str(caught.exception))

    def test_a_nan_in_one_aligned_corner_is_refused_rather_than_crossing(self):
        # The direction that mattered: this table used to produce a RESOLVED
        # COMPOSED refutation out of two corners that were never measured.
        with self.assertRaises(ValueError) as caught:
            _verdict(
                self._rows(home_size=_SAFE_SIZE, session_size=_NOT_A_SIZE, row_size=_NOT_A_SIZE)
            )
        self.assertIn(ov.ALIGNMENT_SESSION, str(caught.exception))

    def test_the_refusal_names_the_cell(self):
        rows = self._rows(home_size=_SAFE_SIZE, session_size=_NOT_A_SIZE, row_size=_SAFE_SIZE)
        with self.assertRaises(ValueError) as caught:
            _verdict(rows)
        message = str(caught.exception)
        for coordinate in (ov.COMPOSED_LOADING, "arrival", "1.0"):
            with self.subTest(coordinate=coordinate):
                self.assertIn(coordinate, message)

    def test_a_row_missing_a_corner_is_refused_by_name(self):
        rows = self._rows(home_size=_SAFE_SIZE, session_size=_SAFE_SIZE, row_size=_SAFE_SIZE)
        del rows[0]["arms"][ov.ALIGNMENT_ROW]
        with self.assertRaises(ValueError) as caught:
            _verdict(rows)
        self.assertIn(f"carries no {ov.ALIGNMENT_ROW!r} corner", str(caught.exception))

    def test_a_non_numeric_size_is_refused_too(self):
        rows = self._rows(home_size=_SAFE_SIZE, session_size=_SAFE_SIZE, row_size=_SAFE_SIZE)
        rows[0]["arms"][ov.ALIGNMENT_ROW]["size"] = None
        with self.assertRaises(ValueError) as caught:
            _verdict(rows)
        self.assertIn("not a rejection rate", str(caught.exception))

    def test_a_negative_size_is_refused_rather_than_holding_the_bar(self):
        # Finiteness is not the predicate the message claimed. A size of -0.50
        # has a Wilson interval of (-0.4990, -0.4971), so it read as RESOLVED
        # and as HOLDING the bar, and the table produced "the published claim
        # survives" with all four methods in every holding set.
        rows = self._rows(home_size=-0.50, session_size=_SAFE_SIZE, row_size=_SAFE_SIZE)
        with self.assertRaises(ValueError) as caught:
            _verdict(rows)
        self.assertIn("PROPORTION in [0, 1]", str(caught.exception))

    def test_a_size_above_one_is_refused_rather_than_crossing(self):
        # The other end, and the direction that invents a finding: 1.50 used to
        # produce a resolved refutation printed as "rejects 150.0% [95% 149.7%,
        # 149.9%]" with `claim_survives: false` and one crossing cell.
        rows = self._rows(home_size=_SAFE_SIZE, session_size=1.50, row_size=_SAFE_SIZE)
        with self.assertRaises(ValueError) as caught:
            _verdict(rows)
        message = str(caught.exception)
        self.assertIn("PROPORTION in [0, 1]", message)
        self.assertIn(ov.ALIGNMENT_SESSION, message)

    def test_a_power_that_is_not_a_rejection_rate_is_refused_too(self):
        # The same argument holds verbatim for the OTHER measurement in the same
        # dict: a non-finite power prints as `nan-nan%` with no `?` mark and
        # `PowerBracket.is_resolved` returns True for it, so an absent power
        # read as a resolved answer.
        rows = self._rows(home_size=_SAFE_SIZE, session_size=_SAFE_SIZE, row_size=_SAFE_SIZE)
        rows[0]["arms"][ov.ALIGNMENT_SESSION]["power"] = _NOT_A_SIZE
        with self.assertRaises(ValueError) as caught:
            _verdict(rows)
        message = str(caught.exception)
        self.assertIn("power", message)
        self.assertIn(ov.ALIGNMENT_SESSION, message)

    def test_a_power_of_None_is_legitimate_and_is_not_refused(self):
        # The control on the case above. A corner whose size missed the bar
        # carries NO power by design, so a guard that refused `None` there would
        # refuse the module's own output.
        rows = self._rows(home_size=_SAFE_SIZE, session_size=_SAFE_SIZE, row_size=_SAFE_SIZE)
        for row in rows:
            for arm in ov.BRACKET_ARMS:
                row["arms"][arm]["power"] = None
        self.assertIs(_verdict(rows)["claim_survives"], True)

    def test_a_table_of_real_sizes_is_not_refused(self):
        # The positive control. A refusal that fired on every table would pass
        # every case above and measure nothing.
        self.assertIs(
            _verdict(self._rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE))["claim_survives"],
            True,
        )


class TestTheVerdictCannotBeAskedWithoutItsMagnitudes(unittest.TestCase):
    """``magnitudes`` is a required keyword, so no verdict can be built without it.

    With a default of ``None`` the library surface handed back an affirmative
    verdict whose construction bracket read "magnitudes NOT RECORDED". The label
    was honest, but a verdict nobody can attribute to a magnitude is not worth
    printing, and the one on-path caller always has them.
    """

    def test_omitting_the_magnitudes_is_a_type_error(self):
        with self.assertRaises(TypeError):
            ov.size_verdict(
                _synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE),
                size_tolerance=ov.SIZE_TOLERANCE,
                n_sims=_RESOLVING_SIMS,
            )

    def test_passing_them_explicitly_as_none_still_works(self):
        # A run whose aligned arms did not go has no magnitudes to name, so
        # `None` stays a legal VALUE - what is removed is the DEFAULT.
        verdict = _verdict(
            _synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE), magnitudes=None
        )
        self.assertIn("NOT RECORDED", verdict["verdict_line"])


class TestTheSingleMechanismGridRunsOnItsOwn(unittest.TestCase):
    """The memo pre-authorises restricting the claim to the kappa = 0 rows.

    So the flag that produces exactly that grid has to exist, or the
    authorisation is unreachable and the composed reading is the only one anyone
    can quote.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path

        cls._tmp = tempfile.TemporaryDirectory()
        cls.store = _Store(Path(cls._tmp.name), n_held=45)
        cls.single = ov.report(
            labels_dir=cls.store.labels,
            briefs_dir=cls.store.briefs,
            population=ov.POPULATION_BRIEFED,
            n_sims=2,
            wcb_boot=5,
            seed=99,
            alignment_size=True,
            single_mechanism_only=True,
        )
        cls.both = ov.report(
            labels_dir=cls.store.labels,
            briefs_dir=cls.store.briefs,
            population=ov.POPULATION_BRIEFED,
            n_sims=2,
            wcb_boot=5,
            seed=99,
            alignment_size=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_restricted_grid_holds_only_the_single_mechanism_rows(self):
        self.assertEqual(
            {row["loading"] for row in self.single["rows"]}, {ov.SINGLE_MECHANISM_LOADING}
        )

    def test_the_unrestricted_grid_on_this_fixture_really_holds_both(self):
        # The positive control: on a fixture whose fitted kappa is zero the
        # composed rows are omitted anyway and the case above proves nothing.
        self.assertEqual(
            {row["loading"] for row in self.both["rows"]},
            {ov.SINGLE_MECHANISM_LOADING, ov.COMPOSED_LOADING},
        )

    def test_the_restriction_is_recorded_in_the_artifact(self):
        self.assertIs(self.single["single_mechanism_only"], True)
        self.assertIs(self.both["single_mechanism_only"], False)

    def test_a_restricted_run_has_no_composed_refutation_to_report(self):
        # The name was already right and the assertion was not: with no
        # refutation there is nothing to report, which is `None`.
        self.assertIsNone(self.single["size_verdict"]["refuting_branch"])
        self.assertIsNone(self.single["size_verdict"]["refutation_is_composed"])


class TestTheVerdictRefusesATableWithNoCorners(unittest.TestCase):
    """Zero measurement must not produce an affirmative verdict.

    ``size_verdict([])`` returned ``claim_survives: True`` with the sentence
    "the published claim survives: no method crosses the size bar 7.5% only
    under alignment, and the methods holding size are the same at all three
    corners (none)". The rows of a DEFAULT run, which carry ``arms: None``,
    returned the same thing: the loop skipped every row and the surviving branch
    is the fall-through.
    """

    def test_an_empty_table_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            _verdict([])
        self.assertIn("no aligned corners", str(caught.exception))

    def test_a_default_run_table_is_refused_too(self):
        rows = [
            {
                "shared_fraction": 1.0,
                "loading": ov.SINGLE_MECHANISM_LOADING,
                "method": "arrival",
                "size": _SAFE_SIZE,
                "arms": None,
            }
        ]
        with self.assertRaises(ValueError) as caught:
            _verdict(rows)
        self.assertIn("no aligned corners", str(caught.exception))

    def test_the_refusal_names_the_flag_that_produces_corners(self):
        with self.assertRaises(ValueError) as caught:
            _verdict([])
        self.assertIn("--alignment-size", str(caught.exception))


class TestTheVerdictLineCarriesItsConstruction(unittest.TestCase):
    """Every branch names the magnitudes, the panel count and the grid.

    The surviving branch named NONE of them while the refuting branch named its
    cell and its ``n_sims``, and that asymmetry ran the wrong way: a surviving
    verdict is the one a reader is most likely to quote without its
    construction. The magnitudes were not even available to the function.
    """

    def _lines(self):
        surviving = _verdict(_synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE))
        refuting = _verdict(_synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE))
        no_verdict = _verdict(
            _synthetic_rows(session_size=_CROSSING_SIZE, row_size=_SAFE_SIZE),
            n_sims=_STRADDLING_SIMS,
        )
        return {
            "surviving": surviving["verdict_line"],
            "refuting": refuting["verdict_line"],
            "no_verdict": no_verdict["verdict_line"],
        }

    def test_every_branch_names_both_magnitudes(self):
        for branch, line in self._lines().items():
            with self.subTest(branch=branch):
                self.assertIn(f"{ov.GAM_AT_REALIZED_SE_SESSION:.3f}", line)
                self.assertIn(f"{ov.GAM_AT_REALIZED_SE_ROW:.3f}", line)

    def test_every_branch_names_the_panel_count(self):
        for branch, line in self._lines().items():
            with self.subTest(branch=branch):
                self.assertIn(
                    str(_RESOLVING_SIMS if branch != "no_verdict" else _STRADDLING_SIMS), line
                )

    def test_every_branch_names_the_grid_it_ran_over(self):
        for branch, line in self._lines().items():
            with self.subTest(branch=branch):
                self.assertIn("1.00", line)
                self.assertIn(ov.COMPOSED_LOADING, line)
                self.assertIn("arrival", line)

    def test_a_non_default_magnitude_is_the_one_the_line_names(self):
        # The defect in one sentence: with `--alignment-gam 0.9` the only gam on
        # screen was the disclosure's 0.400 / 0.205, which does not describe the
        # figures beside it.
        line = _verdict(
            _synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE),
            magnitudes={ov.ALIGNMENT_SESSION: 0.9, ov.ALIGNMENT_ROW: 0.9},
        )["verdict_line"]
        self.assertIn("0.900", line)
        self.assertNotIn(f"{ov.GAM_AT_REALIZED_SE_SESSION:.3f}", line)

    def test_a_verdict_asked_without_magnitudes_says_so_rather_than_guessing(self):
        line = _verdict(
            _synthetic_rows(session_size=_SAFE_SIZE, row_size=_SAFE_SIZE), magnitudes=None
        )["verdict_line"]
        self.assertIn("magnitudes NOT RECORDED", line)


class TestTheScreenNamesTheMagnitudeThatRan(unittest.TestCase):
    """The JSON was honest and the screen was not."""

    _GAM = 0.9

    @classmethod
    def setUpClass(cls):
        import json
        import tempfile
        from pathlib import Path

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        store = _Store(root, n_held=45)
        out_json = root / "gam.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ov.main(
                [
                    "--labels-dir",
                    str(store.labels),
                    "--briefs-dir",
                    str(store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "5",
                    "--alignment-size",
                    "--alignment-gam",
                    str(cls._GAM),
                    "--out-json",
                    str(out_json),
                ]
            )
        cls.printed = buffer.getvalue()
        cls.written = json.loads(out_json.read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_table_header_names_each_aligned_column_s_magnitude(self):
        header = next(line for line in self.printed.splitlines() if "session size" in line)
        self.assertIn(f"{self._GAM:.3f}", header)

    def test_the_verdict_line_on_screen_names_the_magnitude_that_ran(self):
        verdict = next(line for line in self.printed.splitlines() if "claim" in line)
        self.assertIn(f"{self._GAM:.3f}", verdict)

    def test_the_artifact_records_the_magnitudes_at_the_top_level(self):
        self.assertEqual(
            self.written["alignment_magnitudes"],
            {ov.ALIGNMENT_SESSION: self._GAM, ov.ALIGNMENT_ROW: self._GAM},
        )

    def test_the_calibrated_pair_is_not_quoted_beside_the_figures(self):
        # It still appears in the precision disclosure, which is a statement
        # about the CALIBRATION and says so. It must not appear in the table or
        # the verdict, which describe this run.
        above = self.printed.split("PRECISION DISCLOSURE")[0]
        self.assertNotIn(f"{ov.GAM_AT_REALIZED_SE_SESSION:.3f}", above)


class TestTheNoVerdictBranchPrintsNoMeasurements(unittest.TestCase):
    """The holding set and the ordering answer are vacuous on the NO VERDICT branch.

    Standing alone under that heading they read as measurements. They are not:
    an unresolved method is DROPPED from the holding set, so an empty set and a
    set nobody could resolve print identically, and ``ordering changed: False``
    then says only that two unresolvable sets were equal.

    BOTH SURFACES, because a round that fixed the screen left the consumer. On
    this very run - the one whose passing case asserts the string "ordering
    changed: False" never reaches the screen - ``--out-json`` wrote
    ``"ordering_changed": false`` and a ``"methods_holding_size"`` with the
    unresolved methods silently dropped, and no field named the suppression.
    Suppressing both in the payload left the whole glob green, so nothing pinned
    it. A CLI's human and machine renderings carry the same facts.
    """

    @classmethod
    def setUpClass(cls):
        import json
        import tempfile
        from pathlib import Path

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        store = _Store(root, n_held=45)
        out_json = root / "unresolved.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ov.main(
                [
                    "--labels-dir",
                    str(store.labels),
                    "--briefs-dir",
                    str(store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "5",
                    "--alignment-size",
                    "--out-json",
                    str(out_json),
                ]
            )
        cls.printed = buffer.getvalue()
        cls.written = json.loads(out_json.read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _verdict(self):
        return self.written["size_verdict"]

    def test_this_fixture_really_lands_on_the_no_verdict_branch(self):
        self.assertIn("NO VERDICT", self.printed)
        self.assertEqual(self._verdict()["no_verdict_cause"], ov.NO_VERDICT_UNRESOLVED)

    def test_no_holding_set_is_printed_as_a_measurement(self):
        self.assertNotIn("methods holding size, homoskedastic: none", self.printed)

    def test_no_ordering_answer_is_printed_as_a_measurement(self):
        self.assertNotIn("ordering changed: False", self.printed)

    def test_both_are_labelled_unresolved_instead(self):
        self.assertIn(ov.NOT_RESOLVED_LABEL, self.printed)

    def test_the_artifact_withholds_the_holding_sets_the_screen_refuses(self):
        self.assertIsNone(self._verdict()["methods_holding_size"])

    def test_the_artifact_withholds_the_ordering_answer_the_screen_refuses(self):
        self.assertIsNone(self._verdict()["ordering_changed"])

    def test_the_artifact_names_the_suppression_rather_than_leaving_a_null(self):
        # A consumer must not have to infer it from a null: `None` on a field is
        # also what a missing key deserialises to.
        self.assertIs(self._verdict()["holding_sets_are_measurements"], False)

    def test_the_artifact_still_carries_what_it_could_resolve(self):
        # The positive control on the suppression: withholding two fields must
        # not withhold the run. The unresolved list, its two shapes and the
        # verdict sentence are all real readings and stay.
        verdict = self._verdict()
        self.assertEqual(
            len(verdict["unresolved"]),
            len(verdict["unresolved_cells"]) + len(verdict["unresolved_method_corners"]),
        )
        self.assertGreater(len(verdict["unresolved"]), 0)
        self.assertIn("NO VERDICT", verdict["verdict_line"])


class TestTheNothingHoldsBranchDoesNotClaimItCouldNotTell(unittest.TestCase):
    """``(not resolved)`` is FALSE on the branch that measured every corner.

    ``main`` gated that label on ``claim_survives is None``, which has two
    causes: cells unresolved against the bar, and no method holding the bar at
    any corner. On the second nothing is unresolved at all - every corner is
    resolved, ``len(unresolved) == 0`` - so the instrument reported "we could
    not tell" about a measurement it completed at all nine corners, hiding a
    real finding behind a non-answer. Gating the label on the CAUSE instead left
    the whole glob green, so nothing pinned it.

    The verdict is the REAL one, built by ``size_verdict`` on a table where no
    corner holds; only its delivery into ``main`` is substituted, because no
    fixture small enough for this suite reaches that branch through the
    simulator.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path
        from unittest import mock

        nothing_holds = _synthetic_rows(
            session_size=_CROSSING_SIZE, row_size=_CROSSING_SIZE, home_size=_CROSSING_SIZE
        )
        cls.verdict = _verdict(nothing_holds)
        cls._tmp = tempfile.TemporaryDirectory()
        store = _Store(Path(cls._tmp.name), n_held=45)
        buffer = io.StringIO()
        with (
            mock.patch.object(ov, "size_verdict", lambda *a, **k: cls.verdict),
            contextlib.redirect_stdout(buffer),
        ):
            ov.main(
                [
                    "--labels-dir",
                    str(store.labels),
                    "--briefs-dir",
                    str(store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "5",
                    "--alignment-size",
                ]
            )
        cls.printed = buffer.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_this_fixture_really_reaches_the_nothing_holds_branch(self):
        self.assertEqual(self.verdict["no_verdict_cause"], ov.NO_VERDICT_NOTHING_HOLDS)
        self.assertEqual(self.verdict["unresolved"], [])
        self.assertIn("NO method holds the size bar", self.printed)

    def test_the_screen_does_not_say_it_could_not_resolve_them(self):
        self.assertNotIn(ov.NOT_RESOLVED_LABEL, self.printed)

    def test_the_empty_sets_are_labelled_as_measurements_instead(self):
        for arm in ov.BRACKET_ARMS:
            with self.subTest(arm=arm):
                self.assertIn(
                    f"methods holding size, {arm}: {ov.MEASURED_EMPTY_LABEL}", self.printed
                )

    def test_the_ordering_answer_is_printed_because_it_was_measured(self):
        self.assertIn("ordering changed: False", self.printed)

    def test_no_composition_is_reported_for_a_refutation_that_does_not_exist(self):
        # The screen's half of the tri-state field: it used to print
        # "refutation is composed: False" on a branch with no refutation at all.
        self.assertIsNone(self.verdict["refutation_is_composed"])
        self.assertIn("refutation is composed: n/a", self.printed)
        self.assertNotIn("refutation is composed: False", self.printed)


class TestTheDisclosureReachesBothSurfaces(unittest.TestCase):
    """Every power figure in the table is conditional on it, so it is not a flag."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = _Store(self.root, n_held=45)
        self.out_json = self.root / "out.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ov.main(
                [
                    "--labels-dir",
                    str(self.store.labels),
                    "--briefs-dir",
                    str(self.store.briefs),
                    "--n-sims",
                    "4",
                    "--wcb-boot",
                    "9",
                    "--out-json",
                    str(self.out_json),
                ]
            )
        self.printed = buffer.getvalue()

    def _written(self):
        import json

        return json.loads(self.out_json.read_text())

    def test_the_human_surface_carries_the_memo_and_the_ratio(self):
        self.assertIn(ov.ALIGNMENT_MEMO, self.printed)
        self.assertIn(str(ov.SIMULATOR_OPTIMISM_RATIO), self.printed)

    def test_the_human_surface_says_the_level_is_not_identified(self):
        self.assertIn("level_not_identified", self.printed)

    def test_the_artifact_carries_the_disclosure_at_the_top_level(self):
        self.assertIn(ov.PRECISION_DISCLOSURE_KEY, self._written())

    def test_the_disclosure_is_not_nested_inside_a_row_or_the_effects(self):
        # The sibling nests it per population because its top level is keyed by
        # population. This report is one flat single-population dict, so the top
        # level IS the record and a nested copy would be unreachable.
        written = self._written()
        self.assertNotIn(ov.PRECISION_DISCLOSURE_KEY, written["effects"])
        for row in written["rows"]:
            self.assertNotIn(ov.PRECISION_DISCLOSURE_KEY, row)

    def test_the_recorded_disclosure_is_the_siblings_own_numbers(self):
        disclosure = self._written()[ov.PRECISION_DISCLOSURE_KEY]
        self.assertEqual(disclosure["memo"], a20_power.ALIGNMENT_MEMO)
        self.assertEqual(disclosure["optimism_ratio"], a20_power.SIMULATOR_OPTIMISM_RATIO)

    def test_the_projection_note_does_not_claim_the_siblings_surfaces(self):
        # Reusing the sibling's own note verbatim would write a false claim into
        # this module's artifact: neither the cluster-count search nor
        # `--alignment-bracket` exists here.
        note = self._written()[ov.PRECISION_DISCLOSURE_KEY]["projections_are_not_bracketed"]
        self.assertNotIn("--alignment-bracket", note)
        self.assertNotIn("cluster-count search", note)

    def test_the_disclosure_block_does_not_look_like_a_table_row(self):
        table = [
            line for line in self.printed.splitlines() if re.match(r"^ *\d\.\d{2} +kappa=", line)
        ]
        self.assertEqual(len(table), len(self._written()["rows"]))

    def test_the_default_run_notice_reaches_the_artifact_as_well_as_the_screen(self):
        # Same rule as `composed_mechanisms_note`, and the module's comment
        # beside that one gives the reason that applies here too. The notice
        # names the measured ratio by which the homoskedastic corner is
        # optimistic on the slope standard error; a JSON-only reader was left to
        # infer the whole gap from `alignment_size: false` plus
        # `size_verdict: null`, and the inference does not supply the ratio.
        self.assertIn(ov.DEFAULT_RUN_NOTICE, self.printed)
        self.assertEqual(self._written()["default_run_notice"], ov.DEFAULT_RUN_NOTICE)


if __name__ == "__main__":
    unittest.main()

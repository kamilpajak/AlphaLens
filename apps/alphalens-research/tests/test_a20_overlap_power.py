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
"""

from __future__ import annotations

import contextlib
import io
import re
import unittest

import numpy as np
from scripts.ml import a20_overlap_power as ov
from scripts.ml import a20_power
from scripts.ml.a20_power import _z

# The on-disk fixture pair is the merged preflight's, reused rather than copied:
# both drivers read the same two stores, and a second copy would drift.
from tests.test_a20_power_preflight import _Store


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
) -> float:
    """Correlation between the first episode of cluster 0 and of cluster ``lag``.

    Measured across independent simulations, which is the only way to see a
    cross-cluster covariance: within one panel the two draws are a single
    realisation.
    """
    rng = np.random.default_rng(seed)
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
        )
        left.append(y[0])
        right.append(y[cluster_size * lag])
    return float(np.corrcoef(left, right)[0, 1])


class TestTheOverlapIsActuallySimulated(unittest.TestCase):
    def test_the_lag_profile_follows_the_shared_window_fraction(self):
        # Two clusters `lag` sessions apart share `horizon - lag` of their
        # `horizon` outcome sessions, so the closed form is
        # shared_fraction * icc * (horizon - lag) / horizon.
        for lag in (1, 5, 10):
            with self.subTest(lag=lag):
                got = _episode_pairs(lag=lag, icc=0.5, shared_fraction=1.0)
                self.assertAlmostEqual(got, 0.5 * (20 - lag) / 20, delta=0.05)

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

    def test_the_within_cluster_correlation_is_the_icc_whatever_is_shared(self):
        # Same-cluster episodes share the whole window AND the cluster-local
        # draw, so their correlation is the icc for every split between the two.
        for phi in (0.0, 0.5, 1.0):
            with self.subTest(shared_fraction=phi):
                rng = np.random.default_rng(5)
                a, b = [], []
                for _ in range(2000):
                    y, _x, _c = ov.simulate_overlapping_panel(
                        burnt_signals=np.zeros((8, 1)),
                        arrival_offsets=[0],
                        cluster_sizes=[2],
                        effects={"atr": 0.0},
                        sd_y=1.0,
                        icc=0.4,
                        shared_fraction=phi,
                        horizon=20,
                        rng=rng,
                    )
                    a.append(y[0])
                    b.append(y[1])
                self.assertAlmostEqual(float(np.corrcoef(a, b)[0, 1]), 0.4, delta=0.05)


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


if __name__ == "__main__":
    unittest.main()

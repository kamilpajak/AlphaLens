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

import unittest

import numpy as np
from scripts.ml import a20_overlap_power as ov


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

    def _panel(self, shared_fraction: float, seed: int = 11):
        rng = np.random.default_rng(seed)
        sizes = [5] * 40
        y, _x, clusters = ov.simulate_overlapping_panel(
            burnt_signals=np.zeros((64, 1)),
            arrival_offsets=list(range(40)),
            cluster_sizes=sizes,
            effects={"atr": 0.0},
            sd_y=2.0,
            icc=0.4,
            shared_fraction=shared_fraction,
            horizon=20,
            rng=rng,
        )
        return y, clusters

    def test_total_spread_matches_sd_y_for_every_sharing_fraction(self):
        for phi in (0.0, 0.5, 1.0):
            with self.subTest(shared_fraction=phi):
                spreads = []
                for seed in range(40):
                    y, _c = self._panel(phi, seed=seed)
                    spreads.append(float(np.std(y)))
                self.assertAlmostEqual(float(np.mean(spreads)), 2.0, delta=0.25)

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


class TestArrivalOffsets(unittest.TestCase):
    def test_the_first_arrival_is_the_origin(self):
        self.assertEqual(ov.arrival_offsets_from_dates(["2026-07-06", "2026-07-07"])[0], 0)

    def test_a_weekend_is_not_two_sessions(self):
        # Friday to Monday is one session, not three days. Counting calendar
        # days would inflate every gap and destroy sharing that really exists.
        friday_to_monday = ov.arrival_offsets_from_dates(["2026-07-10", "2026-07-13"])
        self.assertEqual(friday_to_monday[1], 1)


if __name__ == "__main__":
    unittest.main()

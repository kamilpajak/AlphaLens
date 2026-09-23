"""Unit tests for the #1227 A20 power preflight (ledger amendment + power sim).

Hermetic: the socket guard forbids network, and every case here feeds synthetic
frames to the pure functions. The one thing that cannot be unit-tested is the
number the preflight finally prints — that is a single recorded run, not a
regression fixture.

The load-bearing case is ``TestHeldOutBlindness``. The held-out panel may
contribute STRUCTURE only: which arrival session an episode falls on, and
whether it is in the confirmation population. Not the label — and, the part a
``sel_ar_*`` denylist would miss, **not the signal columns either**: reading
held-out ``technical_atr_pct`` to learn how the three statistics co-move would
put the held-out dependence structure straight into a Holm power number without
touching a single label value.
"""

from __future__ import annotations

import datetime as dt
import unittest

import numpy as np
import pandas as pd
from scripts.ml import a20_power as pre


def _held_out_frame(rows: list[tuple[str, str, str, bool]]) -> pd.DataFrame:
    """(brief_date, ticker, anchor_session, briefed_any_theme) plus noise columns.

    ``briefed_any_theme`` — not ``population`` — is what separates a name the
    brief actually carried from one only the LLM proposed; measured on the real
    store, ``population`` distinguishes the provenance of the row SET
    (briefed_or_llm_proposed vs pre_open_recovered) and not the two arms.

    The noise columns are the point: a real read of this store would hand back
    labels and signals too, so the frame the tests pass in carries them.
    """
    frame = pd.DataFrame(
        rows, columns=["brief_date", "ticker", "anchor_session", "briefed_any_theme"]
    )
    frame["sel_label_status_20"] = "ok"
    frame["sel_ar_20"] = 0.123  # must never be read
    frame["sel_zar_20"] = 1.5  # must never be read
    frame["technical_atr_pct"] = 4.2  # must never be read either
    return frame


class TestHeldOutBlindness(unittest.TestCase):
    def test_the_allowlist_carries_no_outcome_and_no_signal(self):
        # Asserted against the constant rather than the call, so a future column
        # added to the read has to be argued for here first.
        self.assertEqual(
            set(pre.HELD_OUT_COLUMNS),
            {"brief_date", "ticker", "anchor_session", "briefed_any_theme", "sel_label_status_20"},
        )
        for banned in ("sel_ar_20", "sel_zar_20", "technical_atr_pct", "car_10"):
            self.assertNotIn(banned, pre.HELD_OUT_COLUMNS)

    def test_the_structure_read_touches_only_the_allowlist(self):
        frame = _held_out_frame([("2026-07-06", "AAA", "2026-07-07", True)])
        seen: list[str] = []

        class _Watched(pd.DataFrame):
            """Records every column the code asks for by name."""

            @property
            def _constructor(self):
                return _Watched

            def __getitem__(self, key):
                seen.extend([key] if isinstance(key, str) else list(key))
                return super().__getitem__(key)

        pre.held_out_structure(_Watched(frame), population=pre.POPULATION_BRIEFED)

        self.assertTrue(seen, "positive control: the watcher recorded nothing at all")
        self.assertTrue(
            set(seen) <= set(pre.HELD_OUT_COLUMNS),
            f"held-out branch read outside the allowlist: {sorted(set(seen) - set(pre.HELD_OUT_COLUMNS))}",
        )

    def test_the_watcher_can_refute(self):
        # The control for the control: if the code did read a label, the check
        # above must fail rather than pass quietly.
        seen = {"anchor_session", "sel_ar_20"}
        self.assertFalse(seen <= set(pre.HELD_OUT_COLUMNS))


class TestTheStructureMatchesTheSimulatedPopulation(unittest.TestCase):
    """Cluster sizes must come from the population the test will actually run on.

    Reading sizes from every held-out row while the confirmation runs on the
    briefed subset would inflate power by exactly the ratio between them — a
    silent overstatement that no assertion about labels would catch.
    """

    _ROWS = [
        ("2026-07-06", "AAA", "2026-07-07", True),
        ("2026-07-06", "BBB", "2026-07-07", True),
        ("2026-07-06", "CCC", "2026-07-07", False),
        ("2026-07-07", "DDD", "2026-07-08", False),
    ]

    def test_the_briefed_population_sees_only_its_own_clusters(self):
        sizes = pre.held_out_structure(
            _held_out_frame(self._ROWS), population=pre.POPULATION_BRIEFED
        )
        self.assertEqual(sorted(sizes), [2])  # one arrival session, two episodes

    def test_the_wider_population_sees_more(self):
        sizes = pre.held_out_structure(_held_out_frame(self._ROWS), population=pre.POPULATION_ALL)
        self.assertEqual(sorted(sizes), [1, 3])

    def test_an_immature_row_is_not_a_cluster(self):
        frame = _held_out_frame(self._ROWS)
        frame.loc[frame["ticker"] == "AAA", "sel_label_status_20"] = "immature"
        sizes = pre.held_out_structure(frame, population=pre.POPULATION_BRIEFED)
        self.assertEqual(sorted(sizes), [1])


class TestHolm(unittest.TestCase):
    """Holm step-down at FWER 0.05 across the three #1227 hypotheses."""

    def test_the_sorted_bars_are_alpha_over_three_two_one(self):
        self.assertEqual(pre.holm_bars(3, 0.05), [0.05 / 3, 0.05 / 2, 0.05])

    def test_all_three_clear_when_every_p_is_tiny(self):
        self.assertEqual(
            pre.holm_reject({"atr": 1e-6, "ma50": 1e-6, "press": 1e-6}), {"atr", "ma50", "press"}
        )

    def test_the_step_down_stops_at_the_first_failure(self):
        # p = 0.01 clears 0.05/3; 0.03 fails 0.05/2, and the step-down then
        # rejects nothing further EVEN THOUGH 0.04 < 0.05. That early stop is
        # the whole difference from three independent tests.
        rejected = pre.holm_reject({"atr": 0.01, "ma50": 0.03, "press": 0.04})
        self.assertEqual(rejected, {"atr"})

    def test_nothing_clears_when_the_smallest_p_misses_its_bar(self):
        self.assertEqual(pre.holm_reject({"atr": 0.02, "ma50": 0.03, "press": 0.04}), set())

    def test_a_tie_at_the_bar_is_rejected(self):
        # p <= bar, not p < bar: the boundary belongs to the rejection region.
        self.assertEqual(pre.holm_reject({"a": 0.05 / 3}, alpha=0.05), {"a"})


class TestTheGateDateArithmetic(unittest.TestCase):
    """Turning 'we need N clusters' into a date.

    The first version of this class asserted that the missing arrivals had to
    HAPPEN and then mature, and the implementation obeyed it — so the test
    passed and confirmed a bug that put the gate five weeks late. Arrivals
    accrue daily and are already in flight; what is missing is maturity, not
    the arrivals. Measured 2026-09-22: 55 arrivals existed, 34 had matured.
    """

    @staticmethod
    def _arrivals(n: int = 50, start: dt.date = dt.date(2026, 7, 6)) -> list[str]:
        """``n`` consecutive XNYS sessions — arrivals are sessions, not dates.

        Built from the real calendar rather than a date range: a fixture with
        weekends in it does not round-trip through the session arithmetic, and
        the first version of this test failed for that reason rather than for
        the behaviour it is about.
        """
        from alphalens_pipeline.paper.calendar import advance_trading_sessions

        return [advance_trading_sessions(start, i).isoformat() for i in range(n)]

    def test_an_arrival_that_already_happened_only_has_to_mature(self):
        # The 38th arrival in this list is in the past; the answer is its
        # maturity date, NOT today plus four sessions plus the lag.
        got = pre.gate_date(
            observed_arrivals=self._arrivals(),
            clusters_needed=38,
            accrual_per_session=1.0,
            maturity_lag_sessions=20,
            today=dt.date(2026, 9, 22),
        )
        nth = dt.date.fromisoformat(sorted(self._arrivals())[37])
        self.assertEqual(pre.sessions_between(nth, got), 20)
        self.assertLess(got, dt.date(2026, 10, 14))

    def test_a_count_beyond_the_observed_arrivals_falls_back_to_accrual(self):
        # The projection still exists for the case it is for.
        got = pre.gate_date(
            observed_arrivals=self._arrivals(10),
            clusters_needed=20,
            accrual_per_session=1.0,
            maturity_lag_sessions=20,
            today=dt.date(2026, 9, 22),
        )
        self.assertEqual(pre.sessions_between(dt.date(2026, 9, 22), got), 30)

    def test_a_gate_already_met_is_never_in_the_past(self):
        got = pre.gate_date(
            observed_arrivals=self._arrivals(),
            clusters_needed=5,
            accrual_per_session=1.0,
            maturity_lag_sessions=20,
            today=dt.date(2026, 9, 22),
        )
        self.assertEqual(got, dt.date(2026, 9, 22))


class TestTheSimulatedOutcomeKeepsThePanelsVariance(unittest.TestCase):
    """The injected signal must come OUT of the measured variance, not sit on top.

    The first version drew the noise with variance ``sd_y**2`` and then added
    the injected signal, so every simulated panel had MORE variance than the
    panel it was calibrated to. That inflates the residual the test statistic
    divides by, and the power number comes out too low - which is the direction
    that says "wait longer" and so never looks like a mistake.

    Measured 2026-09-22 on the burnt panel: R2 = 0.28, so at the 50% gate the
    injected signal accounts for 7% of the variance and the noise was about
    3.7% too wide. That is roughly four points of power at the gate, against a
    gap to the bar of 2.7.
    """

    @staticmethod
    def _signals(rng, n=600):
        # Two correlated columns, so the explained share is not a sum of squares.
        a = rng.normal(size=n)
        b = 0.6 * a + 0.8 * rng.normal(size=n)
        return np.column_stack([(a - a.mean()) / a.std(), (b - b.mean()) / b.std()])

    def test_the_noise_shrinks_by_exactly_what_the_signal_explains(self):
        rng = np.random.default_rng(7)
        signals = self._signals(rng)
        effects = {"x": 0.30, "y": 0.20}
        sd_y = 1.0

        explained = float(np.var(signals @ np.array([0.30, 0.20])))
        self.assertGreater(explained, 0.0, "positive control: the effects explain something")
        self.assertAlmostEqual(
            pre.residual_variance(signals, effects, sd_y), sd_y**2 - explained, places=12
        )

    def test_zero_effects_leave_the_variance_untouched(self):
        rng = np.random.default_rng(7)
        signals = self._signals(rng)
        self.assertAlmostEqual(
            pre.residual_variance(signals, {"x": 0.0, "y": 0.0}, 0.4), 0.16, places=12
        )

    def test_an_effect_that_explains_more_than_the_panel_has_is_refused(self):
        # An incoherent DGP - negative noise - must be loud, not clamped to a
        # floor that would silently simulate a panel nobody measured.
        rng = np.random.default_rng(7)
        signals = self._signals(rng)
        with self.assertRaises(ValueError):
            pre.residual_variance(signals, {"x": 5.0, "y": 5.0}, 0.1)

    def test_a_simulated_panel_has_the_variance_it_was_calibrated_to(self):
        # The behavioural form of the same check, and the one that would have
        # caught the bug: build a panel the way simulate_power does and compare
        # its spread against sd_y.
        rng = np.random.default_rng(11)
        signals = self._signals(rng)
        sd_y = 0.1653  # the measured burnt-panel value
        effects = {"x": -0.30 * sd_y, "y": -0.20 * sd_y}

        spreads = []
        for _ in range(40):
            y, _X, _cl = pre.simulate_panel(
                burnt_signals=signals,
                cluster_sizes=[12] * 40,
                effects=effects,
                sd_y=sd_y,
                icc=0.06,
                rng=rng,
            )
            spreads.append(float(np.std(y)))
        self.assertAlmostEqual(float(np.mean(spreads)), sd_y, delta=0.004)


class TestShrinkage(unittest.TestCase):
    def test_the_grid_is_the_one_1227_pre_registered(self):
        self.assertEqual(pre.SHRINKAGE_GRID, (0.25, 0.50, 0.75))

    def test_shrinking_scales_the_effect_and_keeps_its_sign(self):
        # The July ATR effect is NEGATIVE (higher ATR -> worse outcome); a
        # shrinkage that flipped the sign would simulate the wrong alternative.
        self.assertAlmostEqual(pre.shrink(-0.35, 0.50), -0.175)
        self.assertLess(pre.shrink(-0.35, 0.25), 0)


if __name__ == "__main__":
    unittest.main()


class TestTheMonteCarloGuard(unittest.TestCase):
    """A power number whose own error bar crosses the bar is not an answer.

    Both cases below are the real ones from 2026-09-22: 400 simulations gave
    79.75% and it was quoted as "80%", which reads as a pass; 4 000 gave 77.3%
    and could finally say "does not clear".
    """

    def test_the_standard_error_is_the_binomial_one(self):
        self.assertAlmostEqual(pre.monte_carlo_se(0.8, 400), (0.8 * 0.2 / 400) ** 0.5, places=12)

    def test_a_degenerate_estimate_has_no_spread(self):
        self.assertEqual(pre.monte_carlo_se(1.0, 10), 0.0)

    def test_zero_simulations_is_refused(self):
        with self.assertRaises(ValueError):
            pre.monte_carlo_se(0.5, 0)

    def test_the_400_sim_run_that_read_as_a_pass_is_unresolved(self):
        self.assertFalse(pre.verdict_is_resolved(0.7975, 400))

    def test_the_4000_sim_run_resolves(self):
        self.assertTrue(pre.verdict_is_resolved(0.773, 4000))

    def test_a_clean_sweep_of_four_sims_does_not_prove_80_percent(self):
        # The case that forces the Wilson interval: 4 of 4 gives a plain
        # standard error of exactly zero, which would claim certainty.
        self.assertEqual(pre.monte_carlo_se(1.0, 4), 0.0)
        self.assertFalse(pre.verdict_is_resolved(1.0, 4))

    def test_the_interval_keeps_a_width_at_both_boundaries(self):
        for power in (0.0, 1.0):
            low, high = pre.wilson_interval(power, 8)
            self.assertLess(low, high)
            self.assertGreaterEqual(low, 0.0 - 1e-12)
            self.assertLessEqual(high, 1.0 + 1e-12)

    def test_the_interval_refuses_zero_simulations(self):
        with self.assertRaises(ValueError):
            pre.wilson_interval(0.5, 0)


class TestPopulationMask(unittest.TestCase):
    _ROWS = [
        ("2026-07-06", "AAA", "2026-07-07", True),
        ("2026-07-06", "BBB", "2026-07-07", False),
    ]

    def test_all_keeps_every_row(self):
        mask = pre.population_mask(_held_out_frame(self._ROWS), pre.POPULATION_ALL)
        self.assertEqual(mask.tolist(), [True, True])

    def test_briefed_keeps_only_the_briefed_row(self):
        mask = pre.population_mask(_held_out_frame(self._ROWS), pre.POPULATION_BRIEFED)
        self.assertEqual(mask.tolist(), [True, False])

    def test_a_missing_flag_is_not_briefed(self):
        # The real store writes this column nullable, so a row the stamper
        # never resolved arrives as null rather than False. Treating null as
        # briefed would quietly widen the confirmation population.
        frame = _held_out_frame(self._ROWS)
        frame["briefed_any_theme"] = pd.array([None, False], dtype="boolean")
        mask = pre.population_mask(frame, pre.POPULATION_BRIEFED)
        self.assertEqual(mask.tolist(), [False, False])

    def test_an_unknown_population_is_refused(self):
        with self.assertRaises(ValueError):
            pre.population_mask(_held_out_frame(self._ROWS), "everything")


class TestMeasuredAccrual(unittest.TestCase):
    """Clusters per SESSION, measured over the window the clusters span.

    Dividing by the number of store dates instead counts arrivals that have not
    resolved and understates the rate - the 0.44 an earlier reviewer computed,
    against a true 1.00.
    """

    def test_one_cluster_per_session_reads_as_one(self):
        from alphalens_pipeline.paper.calendar import advance_trading_sessions

        start = dt.date(2026, 7, 6)
        anchors = {advance_trading_sessions(start, i).isoformat(): 3 for i in range(10)}
        self.assertAlmostEqual(pre.measured_accrual(anchors), 1.0, places=9)

    def test_every_other_session_reads_as_a_half(self):
        from alphalens_pipeline.paper.calendar import advance_trading_sessions

        start = dt.date(2026, 7, 6)
        anchors = {advance_trading_sessions(start, 2 * i).isoformat(): 1 for i in range(5)}
        self.assertAlmostEqual(pre.measured_accrual(anchors), 5 / 9, places=9)

    def test_a_single_anchor_cannot_measure_a_rate(self):
        self.assertEqual(pre.measured_accrual({"2026-07-06": 4}), 0.0)
        self.assertEqual(pre.measured_accrual({}), 0.0)


class TestTheGateDateIgnoresRepeatedArrivals(unittest.TestCase):
    def test_a_repeated_session_is_still_one_cluster(self):
        arrivals = ["2026-07-06", "2026-07-06", "2026-07-07", "2026-07-08"]
        deduped = pre.gate_date(
            observed_arrivals=arrivals,
            clusters_needed=3,
            accrual_per_session=1.0,
            maturity_lag_sessions=2,
            today=dt.date(2026, 7, 1),
        )
        unique = pre.gate_date(
            observed_arrivals=sorted(set(arrivals)),
            clusters_needed=3,
            accrual_per_session=1.0,
            maturity_lag_sessions=2,
            today=dt.date(2026, 7, 1),
        )
        self.assertEqual(deduped, unique)


class TestIccEstimator(unittest.TestCase):
    def test_identical_clusters_carry_no_between_variance(self):
        y = np.tile([1.0, 2.0, 3.0], 5)
        clusters = np.repeat(np.arange(5), 3)
        self.assertAlmostEqual(pre.estimate_icc_local(y, clusters), 0.0, places=6)

    def test_clusters_that_differ_only_by_level_saturate(self):
        y = np.repeat(np.arange(5, dtype=float) * 10.0, 3) + np.tile([0.0, 0.01, -0.01], 5)
        self.assertGreater(pre.estimate_icc_local(y, np.repeat(np.arange(5), 3)), 0.8)

    def test_it_is_clamped_to_the_unit_interval(self):
        y = np.repeat(np.arange(6, dtype=float), 4)
        icc = pre.estimate_icc_local(y, np.repeat(np.arange(6), 4))
        self.assertLessEqual(icc, 0.9)
        self.assertGreaterEqual(icc, 0.0)

    def test_too_few_clusters_to_estimate(self):
        self.assertEqual(pre.estimate_icc_local(np.array([1.0, 2.0]), np.array([0, 0])), 0.0)

    def test_it_matches_the_neighbouring_scripts_estimator(self):
        # The estimator is duplicated rather than imported (the neighbour is a
        # dated one-shot script, not a module). This is the anti-drift check
        # that makes the duplication safe to keep.
        import importlib.util
        from pathlib import Path

        path = Path(pre.__file__).with_name("2026_09_experts_last_look.py")
        self.assertTrue(path.exists(), f"positive control: the neighbour is at {path}")
        spec = importlib.util.spec_from_file_location("_neighbour_last_look", path)
        assert spec is not None and spec.loader is not None
        neighbour = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(neighbour)

        rng = np.random.default_rng(3)
        clusters = np.repeat(np.arange(12), 4)
        y = np.repeat(rng.normal(size=12), 4) + rng.normal(scale=0.5, size=48)
        self.assertAlmostEqual(
            pre.estimate_icc_local(y, clusters), neighbour.estimate_icc(y, clusters), places=9
        )


def _sessions(n: int, start: dt.date = dt.date(2026, 6, 1)) -> list[dt.date]:
    from alphalens_pipeline.paper.calendar import advance_trading_sessions

    return [advance_trading_sessions(start, i) for i in range(n)]


class _Store:
    """A pair of on-disk parquet stores shaped like the real ones.

    The preflight reads two directories of per-date parquets and joins them.
    Every case below that touches disk goes through this, because the failure
    the pure-function tests cannot reach is the join coming back EMPTY - which
    looks exactly like a clean run with no data.
    """

    def __init__(self, root, *, n_burnt: int = 14, n_held: int = 12, tickers: int = 6):
        import numpy as _np

        self.labels = root / "selection_labels"
        self.briefs = root / "thematic_briefs"
        self.labels.mkdir(parents=True)
        self.briefs.mkdir(parents=True)
        rng = _np.random.default_rng(1234)

        burnt_dates = _sessions(n_burnt, dt.date(2026, 6, 1))
        held_dates = _sessions(n_held, dt.date(2026, 7, 6))

        previous: list[str] = []
        for i, day in enumerate(burnt_dates + held_dates):
            # Mostly FRESH names per session, because the ledger-rule-5 episode
            # dedup chains a ticker that reappears within the window - a fixture
            # that repeats the same six names every day collapses to six
            # episodes and the panel stops being a panel. One deliberate repeat
            # of yesterday's first name keeps the dedup exercised.
            names = [f"T{i:02d}_{j}" for j in range(tickers - 1)]
            names.append(previous[0] if previous else f"T{i:02d}_x")
            previous = names

            held_out = day > dt.date.fromisoformat(pre.DISCOVERY_CUTOFF)
            atr = rng.normal(size=tickers)
            ma50 = 0.5 * atr + rng.normal(size=tickers)
            press = rng.integers(0, 5, size=tickers).astype(float)
            # A real negative ATR effect, so the burnt fit is not a coin flip.
            outcome = -0.06 * atr - 0.03 * ma50 + rng.normal(scale=0.12, size=tickers)

            labels = pd.DataFrame(
                {
                    "brief_date": [day] * tickers,
                    "ticker": names,
                    "anchor_session": [day.isoformat()] * tickers,
                    "briefed_any_theme": [j % 4 != 3 for j in range(tickers)],
                    "sel_label_status_20": ["ok"] * tickers,
                    "sel_ar_20": outcome,
                    # Present on disk and never read by the held-out branch.
                    "sel_zar_20": outcome * 3.0,
                    "population": ["briefed_or_llm_proposed"] * tickers,
                }
            )
            if held_out and i % 5 == 0:
                labels.loc[0, "sel_label_status_20"] = "immature"
            labels.to_parquet(self.labels / f"{day.isoformat()}.parquet", index=False)

            pd.DataFrame(
                {
                    "ticker": names,
                    "technical_atr_pct": atr,
                    "technical_ma50_distance_pct": ma50,
                    "n_gates_passed": press,
                    "unrelated_column": rng.normal(size=tickers),
                }
            ).to_parquet(self.briefs / f"{day.isoformat()}.parquet", index=False)


class TestTheStoresAreReadAsPromised(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = _Store(Path(self._tmp.name))

    def test_the_held_out_read_materialises_only_the_allowlist(self):
        # The allowlist is enforced at the parquet reader, not just in the
        # structure function: the label and the scaled label are ON DISK here,
        # and the frame that comes back must not carry them at all.
        frame = pre.load_held_out(self.store.labels)
        self.assertEqual(set(frame.columns), set(pre.HELD_OUT_COLUMNS))
        self.assertFalse(frame.empty, "positive control: the held-out side has rows")

    def test_the_held_out_read_stops_at_the_discovery_cutoff(self):
        frame = pre.load_held_out(self.store.labels)
        self.assertTrue((frame["brief_date"].astype(str) > pre.DISCOVERY_CUTOFF).all())

    def test_an_empty_directory_reads_as_an_empty_frame(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as empty:
            self.assertTrue(pre.load_held_out(Path(empty)).empty)

    def test_the_burnt_join_finds_its_rows(self):
        panel = pre.burnt_panel(
            self.store.labels, self.store.briefs, population=pre.POPULATION_BRIEFED
        )
        self.assertFalse(panel.empty, "the two stores joined to nothing")
        self.assertIn(pre.OUTCOME, panel.columns)
        for column in pre.SIGNALS.values():
            self.assertIn(column, panel.columns)
        self.assertTrue((panel["brief_date"].astype(str) <= pre.DISCOVERY_CUTOFF).all())

    def test_the_burnt_join_drops_the_unbriefed_arm(self):
        briefed = pre.burnt_panel(
            self.store.labels, self.store.briefs, population=pre.POPULATION_BRIEFED
        )
        everyone = pre.burnt_panel(
            self.store.labels, self.store.briefs, population=pre.POPULATION_ALL
        )
        self.assertLess(len(briefed), len(everyone))

    def test_an_unknown_population_is_refused_before_any_work(self):
        with self.assertRaises(ValueError):
            pre.burnt_panel(self.store.labels, self.store.briefs, population="everything")

    def test_the_episode_dedup_collapses_a_repeated_ticker(self):
        # The store repeats every ticker on consecutive sessions, so without
        # the ledger-rule-5 dedup the panel would be rows, not episodes.
        panel = pre.burnt_panel(self.store.labels, self.store.briefs, population=pre.POPULATION_ALL)
        self.assertEqual(len(panel), len(panel.drop_duplicates(subset=["ticker", "brief_date"])))
        # One name per session is yesterday's, so the dedup must drop 13 of the
        # 14 burnt sessions' repeats - fewer rows than the store holds.
        self.assertLess(len(panel), 14 * 6)
        self.assertGreater(len(panel), 14 * 4)

    def test_the_fitted_atr_effect_has_the_sign_the_fixture_built_in(self):
        panel = pre.burnt_panel(self.store.labels, self.store.briefs, population=pre.POPULATION_ALL)
        effects = pre.standardised_effects(panel)
        self.assertLess(effects["atr"], 0.0)
        self.assertEqual(set(effects), set(pre.SIGNALS))

    def test_the_effect_interval_brackets_the_point_estimate(self):
        panel = pre.burnt_panel(self.store.labels, self.store.briefs, population=pre.POPULATION_ALL)
        effects = pre.standardised_effects(panel)
        cis = pre.effect_ci(panel, n_boot=60, seed=5)
        for name, (lo, hi) in cis.items():
            self.assertLessEqual(lo, hi)
            self.assertGreater(hi - lo, 0.0, f"{name}: a zero-width interval is not an interval")


class TestTheWholeDriverRuns(unittest.TestCase):
    """End to end over the fixture stores, at simulation counts a test can afford.

    The numbers this produces are meaningless - the point is that the driver
    assembles a complete answer from disk and that its parts agree with each
    other (episodes sum to the cluster sizes, the date is not in the past).
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = _Store(self.root)

    def _report(self, population=pre.POPULATION_BRIEFED):
        return pre.report(
            labels_dir=self.store.labels,
            briefs_dir=self.store.briefs,
            population=population,
            n_sims=4,
            wcb_boot=9,
            seed=99,
            max_clusters=16,
        )

    def test_the_report_carries_every_field_the_memo_quotes(self):
        r = self._report()
        for key in (
            "population",
            "burnt_episodes",
            "held_out_clusters",
            "held_out_episodes",
            "accrual_per_session",
            "sd_y",
            "icc",
            "effects",
            "effect_ci",
            "power",
        ):
            self.assertIn(key, r)
        self.assertGreater(r["burnt_episodes"], 0)
        self.assertGreater(r["held_out_clusters"], 0)

    def test_the_power_grid_is_the_pre_registered_one(self):
        r = self._report()
        self.assertEqual(sorted(r["power"]), sorted(pre.SHRINKAGE_GRID))
        for factor in pre.SHRINKAGE_GRID:
            self.assertEqual(set(r["power"][factor]), set(pre.SIGNALS))
            for value in r["power"][factor].values():
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_the_accrual_is_measured_on_the_simulated_population(self):
        # The defect this pins: accrual read from every held-out row while the
        # simulation runs on the briefed subset. The fixture holds one arrival
        # session per date in both arms, so the RATE matches even though the
        # episode counts do not - and the episode counts are what must differ.
        briefed = self._report(pre.POPULATION_BRIEFED)
        everyone = self._report(pre.POPULATION_ALL)
        self.assertLess(briefed["held_out_episodes"], everyone["held_out_episodes"])
        self.assertGreater(briefed["accrual_per_session"], 0.0)

    def test_a_gate_date_is_never_before_today(self):
        r = self._report()
        if r["gate_date"] is not None:
            self.assertGreaterEqual(r["gate_date"], dt.date.today())

    def test_the_cluster_search_reports_a_count_at_least_as_big_as_today(self):
        r = self._report()
        if r["clusters_needed_for_gate"] is not None:
            self.assertGreaterEqual(r["clusters_needed_for_gate"], r["held_out_clusters"])

    def test_the_search_gives_up_rather_than_running_forever(self):
        # An effect of zero can never reach 80% power, so the ceiling is the
        # only thing that ends the search.
        got, _table = pre.clusters_for_power(
            burnt_signals=np.random.default_rng(2).normal(size=(40, 3)),
            observed_sizes=[4, 4, 4],
            effects={"atr": 0.0, "ma50": 0.0, "press": 0.0},
            sd_y=0.2,
            icc=0.05,
            n_sims=2,
            wcb_boot=9,
            seed=1,
            max_clusters=7,
        )
        self.assertIsNone(got)

    def test_main_prints_both_populations_and_writes_its_json(self):
        import contextlib
        import io
        import json

        out_json = self.root / "out.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pre.main(
                [
                    "--labels-dir",
                    str(self.store.labels),
                    "--briefs-dir",
                    str(self.store.briefs),
                    "--n-sims",
                    "4",
                    "--wcb-boot",
                    "9",
                    "--max-clusters",
                    "16",
                    "--out-json",
                    str(out_json),
                ]
            )
        self.assertEqual(code, 0)
        printed = buffer.getvalue()
        self.assertIn("population: briefed", printed)
        self.assertIn("population: all", printed)
        # The verdict always carries its own Monte Carlo error, so a reader
        # cannot take a whole-percent number for a decision on its own.
        self.assertIn("VERDICT", printed)
        self.assertIn("+/-", printed)

        written = json.loads(out_json.read_text())
        self.assertEqual(set(written), {pre.POPULATION_BRIEFED, pre.POPULATION_ALL})


class TestTheClusterTableIsReproducible(unittest.TestCase):
    """The memo quotes power at named cluster counts, so those numbers must
    come from a function that returns the same thing twice.

    The first implementation grew the extra clusters from an rng that advanced
    across the search, so two runs at the same count disagreed and no table
    could be built from it.
    """

    def test_growing_to_the_same_count_twice_gives_the_same_shape(self):
        observed = [5, 2, 9, 1]
        first = pre.grown_cluster_sizes(observed, 10, seed=42)
        second = pre.grown_cluster_sizes(observed, 10, seed=42)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)

    def test_the_observed_sizes_are_kept_and_only_extras_are_drawn(self):
        observed = [5, 2, 9, 1]
        grown = pre.grown_cluster_sizes(observed, 7, seed=42)
        self.assertEqual(grown[:4], observed)
        for size in grown[4:]:
            self.assertIn(size, observed)

    def test_asking_for_fewer_than_observed_truncates(self):
        self.assertEqual(pre.grown_cluster_sizes([5, 2, 9, 1], 2, seed=42), [5, 2])

    def test_different_counts_do_not_share_a_draw(self):
        # Seeded per count, so the shapes differ - otherwise every row of the
        # table would inherit the same padding and move together.
        observed = [5, 2, 9, 1]
        self.assertNotEqual(
            pre.grown_cluster_sizes(observed, 8, seed=42)[4:],
            pre.grown_cluster_sizes(observed, 12, seed=42)[4:8],
        )

    def test_the_table_answers_for_every_count_it_was_given(self):
        rng = np.random.default_rng(4)
        table = pre.power_at_cluster_counts(
            burnt_signals=rng.normal(size=(60, 3)),
            observed_sizes=[4, 3, 5],
            effects={"atr": -0.1, "ma50": -0.05, "press": 0.0},
            sd_y=0.2,
            icc=0.05,
            counts=[3, 5],
            n_sims=3,
            wcb_boot=9,
            seed=7,
        )
        self.assertEqual(sorted(table), [3, 5])
        for row in table.values():
            self.assertEqual(set(row), {"atr", "ma50", "press"})


class TestABrokenJoinIsLoud(unittest.TestCase):
    """An empty join must raise, not return a clean-looking empty panel.

    Without this the fit returns zeros, the simulation runs on them, and a
    power number comes out the other end with nothing to say it is meaningless.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = _Store(self.root)

    def test_a_ticker_that_matches_nothing_refuses(self):
        # The shape that actually happens: one store renames or re-cases its
        # keys and the inner join silently returns nothing.
        for path in sorted(self.store.briefs.glob("*.parquet")):
            frame = pd.read_parquet(path)
            frame["ticker"] = frame["ticker"] + "-XX"
            frame.to_parquet(path, index=False)
        with self.assertRaises(ValueError) as caught:
            pre.burnt_panel(self.store.labels, self.store.briefs, population=pre.POPULATION_ALL)
        self.assertIn("burnt panel has 0 episodes", str(caught.exception))

    def test_the_intact_store_is_the_positive_control(self):
        panel = pre.burnt_panel(self.store.labels, self.store.briefs, population=pre.POPULATION_ALL)
        self.assertGreaterEqual(len(panel), pre._MIN_BURNT_EPISODES)


class TestTheSearchAndTheTableAreOneComputation(unittest.TestCase):
    """The memo quotes a table beside a verdict, so they must be the same numbers.

    They used to be two passes over the same cluster counts that merely ought
    to agree - twice the work, and nothing checking that they did.
    """

    @staticmethod
    def _args():
        rng = np.random.default_rng(6)
        return {
            "burnt_signals": rng.normal(size=(80, 3)),
            "observed_sizes": [4, 6, 3],
            "effects": {"atr": -0.12, "ma50": -0.06, "press": 0.0},
            "sd_y": 0.2,
            "icc": 0.05,
            "n_sims": 4,
            "wcb_boot": 9,
            "seed": 11,
        }

    def test_the_search_reports_every_count_it_tried(self):
        needed, table = pre.clusters_for_power(**self._args(), max_clusters=9)
        self.assertTrue(table, "positive control: the search evaluated something")
        self.assertIn(3, table, "the first count tried is the observed cluster count")
        if needed is not None:
            self.assertEqual(max(table), needed)

    def test_the_table_matches_a_standalone_run_at_the_same_counts(self):
        # The equivalence the de-duplication rests on: same seed, same grown
        # sizes, same simulation - so reusing the search's numbers cannot
        # change what the memo reports.
        args = self._args()
        _needed, table = pre.clusters_for_power(**args, max_clusters=9)
        standalone = pre.power_at_cluster_counts(**args, counts=sorted(table))
        self.assertEqual(sorted(standalone), sorted(table))
        for count in table:
            for name in table[count]:
                self.assertAlmostEqual(table[count][name], standalone[count][name], places=12)

    def test_an_unreachable_target_returns_no_count_but_still_a_table(self):
        args = self._args()
        args["effects"] = {"atr": 0.0, "ma50": 0.0, "press": 0.0}
        needed, table = pre.clusters_for_power(**args, max_clusters=7)
        self.assertIsNone(needed)
        self.assertTrue(table, "a failed search must still say what it tried")

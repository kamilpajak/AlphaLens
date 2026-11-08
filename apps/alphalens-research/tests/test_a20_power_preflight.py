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

import ast
import contextlib
import dataclasses
import datetime as dt
import hashlib
import io
import math
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

        pre.held_out_episodes_by_arrival(_Watched(frame), population=pre.POPULATION_BRIEFED)

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
        by_arrival = pre.held_out_episodes_by_arrival(
            _held_out_frame(self._ROWS), population=pre.POPULATION_BRIEFED
        )
        self.assertEqual(by_arrival, {"2026-07-07": 2})  # one arrival session, two episodes

    def test_the_wider_population_sees_more(self):
        by_arrival = pre.held_out_episodes_by_arrival(
            _held_out_frame(self._ROWS), population=pre.POPULATION_ALL
        )
        self.assertEqual(by_arrival, {"2026-07-07": 3, "2026-07-08": 1})

    def test_an_immature_row_is_not_a_cluster(self):
        frame = _held_out_frame(self._ROWS)
        frame.loc[frame["ticker"] == "AAA", "sel_label_status_20"] = "immature"
        by_arrival = pre.held_out_episodes_by_arrival(frame, population=pre.POPULATION_BRIEFED)
        self.assertEqual(by_arrival, {"2026-07-07": 1})


class TestAnUnusableArrivalIsRefusedAtTheSource(unittest.TestCase):
    """A missing arrival session must be named here, not crash 40 lines later.

    The failure mode is worse than it looks. ``.str.slice`` hands a missing value
    back as float ``NaN`` even after ``astype(str)``, and ``groupby`` drops a NaN
    key by default — so an episode with no arrival session leaves the panel with
    NOTHING raised anywhere and the count quietly one lower. It does not create a
    phantom cluster, which was the first guess; it deletes an episode. A panel
    that shrinks in silence is the exact failure this whole change is a
    correction for, so the single definition of the panel refuses its own bad
    input.

    Measured on the real store 2026-09-24: 0 null anchors in 1001 held-out rows,
    every anchor string exactly 10 characters. So this guards a case that does
    not occur today, which is the point of guarding it.
    """

    _GOOD = ("2026-07-06", "AAA", "2026-07-07", True)

    def test_an_unusable_anchor_would_otherwise_vanish(self):
        # The refutation control, and the reason this guard is not decoration:
        # without it the grouping drops the row and reports a smaller panel.
        rows = pd.DataFrame({"anchor": ["2026-07-07", float("nan")], "n": [1, 1]})
        self.assertEqual(rows.groupby("anchor").size().to_dict(), {"2026-07-07": 1})

    def test_a_missing_arrival_is_named(self):
        frame = _held_out_frame([self._GOOD, ("2026-07-06", "BBB", "2026-07-07", True)])
        frame.loc[frame["ticker"] == "BBB", "anchor_session"] = None
        with self.assertRaises(ValueError) as caught:
            pre.held_out_episodes_by_arrival(frame, population=pre.POPULATION_BRIEFED)
        self.assertIn("anchor_session", str(caught.exception))

    def test_a_row_the_panel_excludes_cannot_trigger_it(self):
        # The guard must look at the panel, not the file. An unusable anchor on a
        # row that is immature, or that the brief never carried, is not this
        # panel's problem and must not block a run.
        for column, value in (("sel_label_status_20", "immature"), ("briefed_any_theme", False)):
            with self.subTest(column=column):
                frame = _held_out_frame([self._GOOD, ("2026-07-06", "BBB", "2026-07-07", True)])
                frame.loc[frame["ticker"] == "BBB", "anchor_session"] = None
                frame.loc[frame["ticker"] == "BBB", column] = value
                self.assertEqual(
                    pre.held_out_episodes_by_arrival(frame, population=pre.POPULATION_BRIEFED),
                    {"2026-07-07": 1},
                )

    def test_a_clean_panel_passes(self):
        # Positive control: the guard must not refuse the ordinary case.
        self.assertEqual(
            pre.held_out_episodes_by_arrival(
                _held_out_frame([self._GOOD]), population=pre.POPULATION_BRIEFED
            ),
            {"2026-07-07": 1},
        )


class TestTheUnitIsTheTickerEpisode(unittest.TestCase):
    """The held-out unit must be the one ledger rule 5 defines, and the one the
    burnt side already uses.

    Rule 5's unit is the ticker-EPISODE under the chained 5-session collapse, not
    the distinct ``(brief_date, ticker)`` pair. ``burnt_panel`` runs
    ``ticker_episode_dedup``; if the held-out branch does not, the simulation
    takes its effect size from collapsed episodes and its cluster sizes from
    uncollapsed rows. Every episode per cluster that is not really independent
    inflates power, and no assertion about label columns would catch it.

    Dates chosen against the real XNYS calendar: 2026-07-06 + 5 sessions is
    2026-07-13, so a 07-09 reappearance chains and a 07-16 one does not.
    """

    _ROWS = [
        ("2026-07-06", "AAA", "2026-07-07", True),
        ("2026-07-09", "AAA", "2026-07-10", True),  # 3 sessions later: same episode
        ("2026-07-06", "BBB", "2026-07-07", True),
        ("2026-07-16", "BBB", "2026-07-17", True),  # 8 sessions later: new episode
    ]

    def test_a_reappearance_inside_the_window_is_not_a_second_episode(self):
        by_arrival = pre.held_out_episodes_by_arrival(
            _held_out_frame(self._ROWS), population=pre.POPULATION_BRIEFED
        )
        # AAA and BBB both arrive on 07-07; only BBB opens a second episode.
        self.assertEqual(sorted(by_arrival.values()), [1, 2])

    def test_an_arrival_session_carrying_only_chained_repeats_is_not_a_cluster(self):
        # The other half of the same defect, and the one a size-only assertion
        # misses: 2026-07-10 holds AAA's chained reappearance and nothing else.
        # Counting it as an arrival session would overstate both the cluster
        # count and the accrual rate the Wake date is derived from.
        by_arrival = pre.held_out_episodes_by_arrival(
            _held_out_frame(self._ROWS), population=pre.POPULATION_BRIEFED
        )
        self.assertEqual(sorted(by_arrival), ["2026-07-07", "2026-07-17"])

    def test_the_mapping_is_ordered_by_arrival_date(self):
        # Not cosmetic. The overlap simulator zips cluster sizes against calendar
        # offsets by POSITION, so a mapping in some other order would give every
        # cluster someone else's arrival date while every count stayed right.
        # Rows are written newest-first here so insertion order cannot pass by
        # accident.
        rows = list(reversed(self._ROWS))
        by_arrival = pre.held_out_episodes_by_arrival(
            _held_out_frame(rows), population=pre.POPULATION_BRIEFED
        )
        self.assertEqual(list(by_arrival), sorted(by_arrival))
        self.assertEqual(list(by_arrival), ["2026-07-07", "2026-07-17"])

    def test_the_held_out_unit_equals_the_burnt_unit(self):
        # The two sides must agree by construction, not by coincidence: the
        # comparison runs the burnt-side helper over the same rows.
        from alphalens_research.diagnostics.options_retro import ticker_episode_dedup

        frame = _held_out_frame(self._ROWS)
        expected = len(ticker_episode_dedup(frame[["brief_date", "ticker"]].copy()))
        by_arrival = pre.held_out_episodes_by_arrival(frame, population=pre.POPULATION_BRIEFED)
        self.assertEqual(sum(by_arrival.values()), expected)

    def test_the_check_can_refute(self):
        # Positive control: on these rows the collapsed and uncollapsed counts
        # differ, so the cases above could have failed.
        frame = _held_out_frame(self._ROWS)
        uncollapsed = frame.drop_duplicates(subset=["brief_date", "ticker"])
        self.assertEqual((len(uncollapsed), uncollapsed["anchor_session"].nunique()), (4, 3))
        by_arrival = pre.held_out_episodes_by_arrival(frame, population=pre.POPULATION_BRIEFED)
        self.assertNotEqual(sum(by_arrival.values()), 4)
        self.assertNotEqual(len(by_arrival), 3)


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


class TestTheTestFAMILY(unittest.TestCase):
    """Which family the power figure is computed under.

    #1227 was scoped as three hypotheses under Holm, and the merged registration
    then narrowed it to ATR alone as a dated amendment. Power computed under
    Holm for a test that will run at a plain one-sided 0.05 understates it, and
    the accrual projection reads directly off that number, so the family has to
    be an argument rather than a constant.
    """

    _P = {"atr": 0.03, "ma50": 0.9, "press": 0.9}

    def test_the_default_is_holm_across_every_hypothesis(self):
        # 0.03 misses the 0.05/3 bar, so the step-down rejects nothing.
        self.assertEqual(pre.rejected_under_family(self._P, family_size=None), set())
        self.assertEqual(pre.rejected_under_family(self._P, family_size=3), set())

    def test_a_family_of_one_judges_each_hypothesis_at_plain_alpha(self):
        self.assertEqual(pre.rejected_under_family(self._P, family_size=1), {"atr"})

    def test_a_family_size_nobody_has_defined_is_refused(self):
        # A family of 2 among 3 hypotheses does not say WHICH two share the
        # bar. Guessing an answer here would put an undefined correction into a
        # registration; there are exactly two families this programme has.
        with self.assertRaises(ValueError):
            pre.rejected_under_family(self._P, family_size=2)

    def test_the_family_of_one_never_rejects_less_than_holm(self):
        # The direction is the load-bearing part: narrowing the family may only
        # make rejection easier, so a power figure can never fall by narrowing.
        for p in (0.001, 0.01, 0.02, 0.04, 0.2):
            values = dict.fromkeys(("atr", "ma50", "press"), p)
            with self.subTest(p=p):
                self.assertLessEqual(
                    len(pre.rejected_under_family(values, family_size=None)),
                    len(pre.rejected_under_family(values, family_size=1)),
                )


class TestTheSimulatorHonoursTheFamily(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(3)
        self.signals = rng.normal(size=(200, 3))
        self.sizes = [6] * 12
        self.effects = {"atr": -0.05, "ma50": -0.02, "press": 0.0}

    def _power(self, **kwargs):
        return pre.simulate_power(
            burnt_signals=self.signals,
            cluster_sizes=self.sizes,
            effects=self.effects,
            sd_y=0.165,
            icc=0.06,
            n_sims=40,
            wcb_boot=99,
            seed=101,
            **kwargs,
        )

    def test_the_default_is_unchanged(self):
        # Regression guard: the merged numbers were produced by the default
        # path, so it must stay exactly what it was.
        self.assertEqual(self._power(), self._power(family_size=None))

    def test_narrowing_the_family_cannot_lower_power(self):
        holm = self._power()
        alone = self._power(family_size=1)
        for name in holm:
            with self.subTest(name=name):
                self.assertGreaterEqual(alone[name], holm[name])

    def test_this_fixture_can_tell_the_two_apart(self):
        # Positive control: on a fixture where Holm and the family of one agree
        # everywhere, the case above would pass without testing anything.
        self.assertGreater(self._power(family_size=1)["atr"], self._power()["atr"])


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
        from alphalens_pipeline.market.calendar import advance_trading_sessions

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
        from alphalens_pipeline.market.calendar import advance_trading_sessions

        start = dt.date(2026, 7, 6)
        anchors = {advance_trading_sessions(start, i).isoformat(): 3 for i in range(10)}
        self.assertAlmostEqual(pre.measured_accrual(anchors), 1.0, places=9)

    def test_every_other_session_reads_as_a_half(self):
        from alphalens_pipeline.market.calendar import advance_trading_sessions

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
    from alphalens_pipeline.market.calendar import advance_trading_sessions

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
        # Default: Holm across all three, which is what every merged number
        # was computed under.
        self.assertEqual(written[pre.POPULATION_BRIEFED]["family_size"], 3)
        self.assertIn("family of 3", printed)

    def test_main_carries_the_requested_family_into_the_answer(self):
        # A power figure is meaningless without its family, so the flag has to
        # reach both the printed header and the recorded JSON. Quoting a
        # family-of-one number under a Holm heading is the mistake this pins.
        import contextlib
        import io
        import json

        out_json = self.root / "family.json"
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
                    "--family-size",
                    "1",
                    "--population",
                    "briefed",
                    "--out-json",
                    str(out_json),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("family of 1", buffer.getvalue())
        written = json.loads(out_json.read_text())
        self.assertEqual(written[pre.POPULATION_BRIEFED]["family_size"], 1)


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


class TestThePopulationFlag(unittest.TestCase):
    """One arm per process, so a machine with cores can run both at once.

    The two arms share no state, so this is a scheduling choice and not a
    methodological one - which is why the default stays "both".
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = _Store(self.root)

    def _run(self, extra: list[str]) -> dict:
        import contextlib
        import io
        import json

        out_json = self.root / f"out{len(extra)}.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = pre.main(
                [
                    "--labels-dir",
                    str(self.store.labels),
                    "--briefs-dir",
                    str(self.store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "9",
                    "--max-clusters",
                    "8",
                    "--out-json",
                    str(out_json),
                    *extra,
                ]
            )
        self.assertEqual(code, 0)
        return json.loads(out_json.read_text())

    def test_one_named_population_runs_only_that_one(self):
        written = self._run(["--population", pre.POPULATION_BRIEFED])
        self.assertEqual(set(written), {pre.POPULATION_BRIEFED})

    def test_the_flag_repeats(self):
        written = self._run(
            ["--population", pre.POPULATION_BRIEFED, "--population", pre.POPULATION_ALL]
        )
        self.assertEqual(set(written), {pre.POPULATION_BRIEFED, pre.POPULATION_ALL})

    def test_omitting_it_runs_both(self):
        self.assertEqual(set(self._run([])), {pre.POPULATION_BRIEFED, pre.POPULATION_ALL})

    def test_an_unknown_population_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            self._run(["--population", "everything"])


# ------------------------------------------------- feature-aligned noise
# The mechanism measured 2026-11-07: the simulator above is 1.36x optimistic on
# the slope standard error because the real noise ALIGNS with the feature under
# test. Everything below either pins the default path against the mechanism or
# exercises the mechanism itself. The pins come first on purpose: the published
# figures (91.5% / 92.2% at the gate, 64.6-66.2% in the overlap memo) are
# reproductions of the DEFAULT path, and a mechanism that moved them by one bit
# would invalidate all of them at once.

#: The same fixture ``test_a20_overlap_power`` pins its own simulator against, so
#: one shape guards both suites.
_PINNED_SIZES = [3, 4, 2, 5]
_PINNED_EFFECTS = {"atr": 0.2, "ma50": -0.1, "press": 0.05}

#: Computed from the UNMODIFIED module before the mechanism existed, which is
#: what makes these a pin rather than a comparison of the code to itself.
_PINNED_Y_SHA256 = "581c66776e7979a6eef206ce4467e47efab49fe5a38aa0f2c5434efdec9ec9fd"
_PINNED_X_SHA256 = "a024b26f954eb5530e0954532102c916e11c07b3ad918270f6e89f9aecd22b46"
_PINNED_CLUSTERS_SHA256 = "2445eda711337bd05017cd6dc218f6771bdbbb1930e03fcbd2d9f0737fc66cfb"
_PINNED_FIRST_OUTCOMES = (
    0.662493683787,
    -0.720530804785,
    -0.494688811208,
    -0.796718157559,
)

#: ``TestTheSimulatorHonoursTheFamily``'s configuration at n_sims 40, wcb_boot 99,
#: seed 101, measured on the unmodified module.
#: Panels behind :data:`_PINNED_POWER`. One flipped panel is 1/40 of the
#: frequency, which is the tolerance that test allows and the reason it has one.
_PINNED_POWER_SIMS = 40

_PINNED_POWER = {"atr": 0.15, "ma50": 0.075, "press": 0.025}


def _pinned_panel(**extra):
    """The pinned DGP call. ``extra`` is how the refutation control differs."""
    return pre.simulate_panel(
        burnt_signals=np.random.default_rng(0).normal(0.0, 1.0, (50, 3)),
        cluster_sizes=_PINNED_SIZES,
        effects=_PINNED_EFFECTS,
        sd_y=1.0,
        icc=0.4,
        rng=np.random.default_rng(42),
        **extra,
    )


class TestTheDefaultDgpIsPinnedToALiteral(unittest.TestCase):
    """The published figures must keep reproducing, bit for bit.

    ``test_the_default_is_unchanged`` above compares the code to ITSELF, so it
    cannot notice a change that moves both sides together. These literals were
    computed from the module before the alignment mechanism was written, so they
    fail if the default stream moves by one draw.
    """

    def test_the_default_panel_is_byte_for_byte_what_it_was(self):
        y, x_all, clusters = _pinned_panel()
        for label, array, digest in (
            ("y", y, _PINNED_Y_SHA256),
            ("X", x_all, _PINNED_X_SHA256),
            ("clusters", clusters, _PINNED_CLUSTERS_SHA256),
        ):
            with self.subTest(array=label):
                self.assertEqual(hashlib.sha256(array.tobytes()).hexdigest(), digest)

    def test_the_first_outcomes_are_the_stored_values(self):
        # The readable half of the same pin: a hash mismatch says "something
        # moved", these four say by how much.
        y, _x, _c = _pinned_panel()
        for i, expected in enumerate(_PINNED_FIRST_OUTCOMES):
            with self.subTest(row=i):
                self.assertAlmostEqual(float(y[i]), expected, places=12)

    def test_the_default_power_figure_at_a_fixed_seed_is_the_stored_one(self):
        """Within ONE panel, because a power figure is not portable to the digit.

        A power figure is a count of Holm rejections over ``n_sims`` panels, so
        it passes through a p-value compared against a bar. A one-ULP difference
        in the BLAS behind ``pinv``/``lstsq`` can flip a single panel across that
        bar and move the frequency by exactly ``1 / n_sims``, with nothing wrong.

        Measured 2026-11-07 on this exact case: macOS arm64 reads
        ``atr`` 0.150 and Linux CI reads 0.175, a difference of 0.025 = 1/40,
        with ``ma50`` and ``press`` identical on both. The first draft of this
        test used ``assertEqual`` and went red on CI for that reason alone.

        The EXACT, portable pin on the default path is the sha256 of the panel
        above: pure arithmetic on the draws, no threshold, so it catches any
        movement of the random stream to the byte. This test adds the weaker but
        still useful check that the frequency has not moved by more than one
        panel, which a real change to the noise model would exceed by far.
        """
        rng = np.random.default_rng(3)
        power = pre.simulate_power(
            burnt_signals=rng.normal(size=(200, 3)),
            cluster_sizes=[6] * 12,
            effects={"atr": -0.05, "ma50": -0.02, "press": 0.0},
            sd_y=0.165,
            icc=0.06,
            n_sims=_PINNED_POWER_SIMS,
            wcb_boot=99,
            seed=101,
        )
        self.assertEqual(set(power), set(_PINNED_POWER))
        one_panel = 1.0 / _PINNED_POWER_SIMS
        for name, expected in _PINNED_POWER.items():
            with self.subTest(hypothesis=name):
                self.assertLessEqual(
                    abs(power[name] - expected),
                    one_panel,
                    f"{name} moved by more than one panel: {power[name]} against {expected}",
                )

    def test_the_pin_can_refute(self):
        # Without this the three pins above would also pass against a mechanism
        # that silently does nothing when it is switched on.
        aligned, _x, _c = _pinned_panel(
            alignment=pre.NoiseAlignment(pre.MEASURED_GAM_SESSION, pre.ALIGNMENT_SESSION)
        )
        self.assertNotEqual(hashlib.sha256(aligned.tobytes()).hexdigest(), _PINNED_Y_SHA256)


class TestTheAlignmentMechanism(unittest.TestCase):
    """``exp(gam * z(feature))`` on noise that has already been drawn."""

    @staticmethod
    def _signals(seed=11, n=600):
        rng = np.random.default_rng(seed)
        atr = rng.normal(size=n)
        ma50 = 0.5 * atr + rng.normal(size=n)
        press = rng.integers(0, 5, size=n).astype(float)
        return np.column_stack([pre._z(atr), pre._z(ma50), pre._z(press)])

    def test_the_off_path_is_the_same_array_as_no_argument(self):
        explicit, _x, _c = _pinned_panel(alignment=None)
        implicit, _x2, _c2 = _pinned_panel()
        self.assertTrue(np.array_equal(explicit, implicit))

    def test_the_mechanism_draws_no_extra_randomness(self):
        # The structural reason the overlap suite's bit-identical pin survives:
        # the scale is a function of x, which is already drawn, so switching the
        # mechanism on consumes no stream. A per-cluster scale VARIATE would
        # advance the generator even at gam 0 and break every published figure.
        off = np.random.default_rng(42)
        on = np.random.default_rng(42)
        signals = self._signals()
        for rng, alignment in ((off, None), (on, pre.NoiseAlignment(0.4, pre.ALIGNMENT_ROW))):
            pre.simulate_panel(
                burnt_signals=signals,
                cluster_sizes=[6] * 8,
                effects={"atr": -0.05, "ma50": -0.02, "press": 0.0},
                sd_y=0.165,
                icc=0.06,
                rng=rng,
                alignment=alignment,
            )
        self.assertEqual(off.bit_generator.state, on.bit_generator.state)

    def test_an_aligned_panel_still_has_the_calibrated_variance(self):
        # The same check as TestTheSimulatedOutcomeKeepsThePanelsVariance, with
        # the mechanism ON: alignment WITHOUT the renormaliser is the mutation
        # this must keep catching.
        signals = self._signals()
        sd_y = 0.1653
        effects = {"atr": -0.30 * sd_y, "ma50": -0.20 * sd_y, "press": 0.0}
        for level in pre.ALIGNMENT_LEVELS:
            with self.subTest(level=level):
                rng = np.random.default_rng(11)
                spreads = []
                for _ in range(40):
                    y, _x, _c = pre.simulate_panel(
                        burnt_signals=signals,
                        cluster_sizes=[12] * 40,
                        effects=effects,
                        sd_y=sd_y,
                        icc=0.06,
                        rng=rng,
                        alignment=pre.NoiseAlignment(pre.MEASURED_GAM_SESSION, level),
                    )
                    spreads.append(float(np.std(y)))
                self.assertAlmostEqual(float(np.mean(spreads)), sd_y, delta=0.004)

    def test_a_session_scale_is_constant_within_a_cluster(self):
        # icc 1.0 puts the whole residual in the cluster shock and zero effects
        # leave y as the scaled residual itself, so the scale is readable off a
        # single panel: a SESSION scale is one number per cluster.
        shared = {
            "burnt_signals": self._signals(),
            "cluster_sizes": [8] * 6,
            "effects": {"atr": 0.0, "ma50": 0.0, "press": 0.0},
            "sd_y": 1.0,
            "icc": 1.0,
        }
        gam = 0.5
        y_s, _x, clusters = pre.simulate_panel(
            **shared,
            rng=np.random.default_rng(5),
            alignment=pre.NoiseAlignment(gam, pre.ALIGNMENT_SESSION),
        )
        for c in sorted(set(clusters)):
            with self.subTest(cluster=c, level=pre.ALIGNMENT_SESSION):
                self.assertAlmostEqual(float(np.ptp(y_s[clusters == c])), 0.0, places=12)

        y_r, x_r, clusters_r = pre.simulate_panel(
            **shared,
            rng=np.random.default_rng(5),
            alignment=pre.NoiseAlignment(gam, pre.ALIGNMENT_ROW),
        )
        for c in sorted(set(clusters_r)):
            rows = clusters_r == c
            with self.subTest(cluster=c, level=pre.ALIGNMENT_ROW):
                # Positive control first: the row scale is NOT flat, so the
                # ratio below is not constant for a trivial reason.
                self.assertGreater(float(np.ptp(y_r[rows])), 1e-9)
                ratio = y_r[rows] / np.exp(gam * x_r[rows, 1])
                self.assertAlmostEqual(float(np.ptp(ratio)), 0.0, places=12)

    def test_an_unknown_level_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            pre.NoiseAlignment(0.3, "episode")
        self.assertIn("episode", str(caught.exception))

    def test_a_negative_magnitude_is_refused(self):
        # A negative gam says the QUIET names are the noisy ones, which nothing
        # measured supports and which would make the bracket look harmless.
        for gam in (-0.1, float("nan"), float("inf")):
            with self.subTest(gam=gam), self.assertRaises(ValueError):
                pre.NoiseAlignment(gam, pre.ALIGNMENT_SESSION)

    def test_a_feature_the_effects_do_not_name_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            _pinned_panel(alignment=pre.NoiseAlignment(0.3, pre.ALIGNMENT_ROW, feature="vol"))
        self.assertIn("vol", str(caught.exception))
        self.assertIn("atr", str(caught.exception))


#: Panels per arm in the precision tests. Three arms at this count run in a few
#: seconds, which keeps the file a single pre-merge gate.
_PRECISION_PANELS = 300

#: The aligned arm must widen the ATR slope spread by at least this factor.
#:
#: NOT the memo's 1.34x, and the difference is a measured fact rather than a
#: looser threshold: how much ``exp(gam*z)`` widens a slope depends on the
#: feature's own distribution, because the inflation is driven by
#: ``E[x**2 * exp(2*gam*x)] / E[exp(2*gam*x)]``. For a NORMAL column that is
#: ``1 + 4*gam**2`` exactly, which at gam 0.205 is 1.08 on the variance and only
#: 1.04 on the spread. The real ATR is right-skewed, so the fixture below is
#: too. Measured on it at row-level gam 0.205, 300 panels, three seeds: 1.613,
#: 1.644 and 1.573 (and 0.984, 0.985, 0.979 for the unaligned arm).
_ALIGNED_WIDENING_MIN = 1.30

#: How much power the row-aligned corner must lose against the homoskedastic one
#: on the bracket fixture. Measured there: 50.0% against 30.0%, a 20-point gap,
#: so this threshold is half the measured effect.
_ALIGNED_ARM_COSTS_AT_LEAST = 0.10

#: The same noise spread on a column the test does not look at must leave the
#: ATR slope spread alone. Section 7.2: "the inflation comes from the ALIGNMENT,
#: not from the spread". The arms are paired on one seed, so the margin is the
#: Monte Carlo error of a ratio of two correlated spreads.
_UNALIGNED_DRIFT_MAX = 0.10


class TestAlignmentIsWhatMovesThePrecision(unittest.TestCase):
    """The memo's central claim: alignment widens the slope, spread alone does not.

    Without the second arm here the first test passes for the wrong reason - any
    heteroskedasticity at all would satisfy it, and a survey of this suite found
    it cannot otherwise tell aligned noise from merely spread noise.
    """

    _SIZES = [6] * 33
    _SD_Y = 0.1653

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(2026)
        n = 400
        # Right-skewed, like the real ATR percent: skew 2.3 against a normal
        # column's 0.0. On a normal column the same mechanism at the same gam
        # widens the spread by only 1.06x, which is a property of the FEATURE
        # and not of the mechanism.
        atr = rng.lognormal(0.0, 0.6, size=n)
        ma50 = 0.5 * pre._z(atr) + rng.normal(size=n)
        press = rng.integers(0, 5, size=n).astype(float)
        # A fourth column with a zero effect: the refutation arm aligns the noise
        # with THIS one, so the noise is just as spread out and not aligned with
        # the feature under test.
        unrelated = rng.normal(size=n)
        cls.signals = np.column_stack([pre._z(atr), pre._z(ma50), pre._z(press), pre._z(unrelated)])
        cls.effects = {
            "atr": -0.30 * cls._SD_Y,
            "ma50": -0.20 * cls._SD_Y,
            "press": 0.0,
            "noise": 0.0,
        }

    def _slope_spread(self, alignment):
        rng = np.random.default_rng(777)
        slopes = []
        for _ in range(_PRECISION_PANELS):
            y, x_all, _c = pre.simulate_panel(
                burnt_signals=self.signals,
                cluster_sizes=self._SIZES,
                effects=self.effects,
                sd_y=self._SD_Y,
                icc=0.05,
                rng=rng,
                alignment=alignment,
            )
            beta, *_ = np.linalg.lstsq(x_all, y, rcond=None)
            slopes.append(float(beta[1]))
        return float(np.std(slopes, ddof=1))

    def test_aligning_the_noise_with_the_tested_feature_widens_its_slope(self):
        plain = self._slope_spread(None)
        aligned = self._slope_spread(
            pre.NoiseAlignment(pre.GAM_AT_REALIZED_SE_ROW, pre.ALIGNMENT_ROW, feature="atr")
        )
        self.assertGreater(aligned / plain, _ALIGNED_WIDENING_MIN)

    def test_the_same_spread_on_an_unrelated_column_does_not(self):
        plain = self._slope_spread(None)
        unaligned = self._slope_spread(
            pre.NoiseAlignment(pre.GAM_AT_REALIZED_SE_ROW, pre.ALIGNMENT_ROW, feature="noise")
        )
        self.assertLess(abs(unaligned / plain - 1.0), _UNALIGNED_DRIFT_MAX)

    def test_the_two_arms_carry_the_same_noise_spread(self):
        # Positive control for the refutation arm: it must really be as spread
        # out, not homoskedastic in disguise. Asserted EXACTLY rather than
        # statistically: every column comes from `_z`, which divides by the
        # POPULATION sd, so log(scale) is `gam * z` plus a constant and its
        # spread is gam itself whatever the column's shape.
        names = list(self.effects)
        spreads = []
        for feature in ("atr", "noise"):
            scale = pre._alignment_scale(
                [self.signals],
                alignment=pre.NoiseAlignment(
                    pre.GAM_AT_REALIZED_SE_ROW, pre.ALIGNMENT_ROW, feature=feature
                ),
                names=names,
            )[0]
            spreads.append(float(np.std(np.log(scale))))
        self.assertAlmostEqual(spreads[0], pre.GAM_AT_REALIZED_SE_ROW, places=12)
        self.assertAlmostEqual(spreads[1], pre.GAM_AT_REALIZED_SE_ROW, places=12)
        self.assertGreater(spreads[0], 0.1, "positive control: the scale really varies")


class TestThePowerBracket(unittest.TestCase):
    """Power over the identified set, never a point."""

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(4)
        cls.signals = rng.normal(size=(120, 3))
        cls.kwargs = {
            "burnt_signals": cls.signals,
            "cluster_sizes": [5] * 8,
            "effects": {"atr": -0.1, "ma50": -0.05, "press": 0.0},
            "sd_y": 0.2,
            "icc": 0.05,
            "n_sims": 6,
            "wcb_boot": 19,
            "seed": 31,
        }
        cls.bracket = pre.power_bracket(**cls.kwargs)

    @staticmethod
    def _hand_built(powers, n_sims):
        return pre.PowerBracket(
            arms={arm: {"atr": p} for arm, p in zip(pre.BRACKET_ARMS, powers, strict=True)},
            alignments={
                "homoskedastic": None,
                # The CALIBRATED pair, matching `power_bracket`'s own default.
                # This helper stands in for a real bracket, so a helper left on
                # the measured readings would have every test that uses it
                # exercising an arm configuration the library never produces.
                pre.ALIGNMENT_SESSION: pre.NoiseAlignment(
                    pre.GAM_AT_REALIZED_SE_SESSION, pre.ALIGNMENT_SESSION
                ),
                pre.ALIGNMENT_ROW: pre.NoiseAlignment(
                    pre.GAM_AT_REALIZED_SE_ROW, pre.ALIGNMENT_ROW
                ),
            },
            target="atr",
            shrinkage=pre.GATE_SHRINKAGE,
            n_sims=n_sims,
        )

    def test_every_corner_of_the_identified_set_is_present(self):
        self.assertEqual(set(self.bracket.arms), set(pre.BRACKET_ARMS))

    def test_a_bracket_missing_an_arm_is_refused(self):
        # A two-arm bracket prints exactly like a three-arm one and its span
        # reads as the identified set while being a subset of it.
        with self.assertRaises(ValueError):
            pre.PowerBracket(
                arms={"homoskedastic": {"atr": 0.9}, pre.ALIGNMENT_SESSION: {"atr": 0.7}},
                alignments={"homoskedastic": None, pre.ALIGNMENT_SESSION: None},
                target="atr",
                shrinkage=pre.GATE_SHRINKAGE,
                n_sims=10,
            )

    def test_each_arm_records_the_magnitude_it_ran_at(self):
        self.assertIsNone(self.bracket.alignments["homoskedastic"])
        self.assertEqual(
            self.bracket.alignments[pre.ALIGNMENT_SESSION].gam,
            pre.GAM_AT_REALIZED_SE_SESSION,
        )
        self.assertEqual(self.bracket.alignments[pre.ALIGNMENT_ROW].gam, pre.GAM_AT_REALIZED_SE_ROW)

    def test_each_arm_records_the_LEVEL_it_ran_at(self):
        # The magnitude alone does not pin the mechanism. Computing the session
        # corner at the row level collapses the three-corner identified set to
        # one mechanism at two magnitudes, which is the failure the span exists
        # to prevent, and no assertion on `.gam` can see it.
        self.assertEqual(
            self.bracket.alignments[pre.ALIGNMENT_SESSION].level, pre.ALIGNMENT_SESSION
        )
        self.assertEqual(self.bracket.alignments[pre.ALIGNMENT_ROW].level, pre.ALIGNMENT_ROW)

    def test_the_aligned_arms_run_at_the_magnitudes_that_REACH_the_realized_se(self):
        # A power bracket is a BOUND. Its worst corner has to be at least as bad
        # as the one out-of-sample precision this programme actually measured,
        # or the bracket would have failed to warn about #1227 itself. The
        # magnitudes that reproduce the realized CR2 SE are the calibrated pair;
        # the burnt-panel READINGS belong to the estimator's report, where the
        # row reading of 0.132 carries a t of 1.55 and is not significant.
        session = self.bracket.alignments[pre.ALIGNMENT_SESSION].gam
        row = self.bracket.alignments[pre.ALIGNMENT_ROW].gam
        self.assertNotEqual(session, pre.MEASURED_GAM_SESSION)
        self.assertNotEqual(row, pre.MEASURED_GAM_ROW)
        self.assertEqual(
            (session, row), (pre.GAM_AT_REALIZED_SE_SESSION, pre.GAM_AT_REALIZED_SE_ROW)
        )

    def test_a_mapping_with_an_unknown_level_is_refused(self):
        # Without this, a typo silently falls back to the calibrated default for
        # BOTH arms and the caller gets a bracket they did not ask for. Caught by
        # mutation: removing the guard left the whole suite green.
        with self.assertRaises(ValueError) as caught:
            pre.power_bracket(**self.kwargs, gam={"sesion": 0.9})
        self.assertIn("sesion", str(caught.exception))

    def test_a_partial_mapping_fills_the_other_arm_from_the_calibrated_default(self):
        # The fallback is a decision worth pinning: nothing else stops it
        # becoming zero, or the measured reading, by accident.
        bracket = pre.power_bracket(**self.kwargs, gam={pre.ALIGNMENT_SESSION: 0.9})
        self.assertEqual(bracket.alignments[pre.ALIGNMENT_SESSION].gam, 0.9)
        self.assertEqual(bracket.alignments[pre.ALIGNMENT_ROW].gam, pre.GAM_AT_REALIZED_SE_ROW)

    def test_an_empty_mapping_is_the_calibrated_pair(self):
        bracket = pre.power_bracket(**self.kwargs, gam={})
        self.assertEqual(
            (
                bracket.alignments[pre.ALIGNMENT_SESSION].gam,
                bracket.alignments[pre.ALIGNMENT_ROW].gam,
            ),
            (pre.GAM_AT_REALIZED_SE_SESSION, pre.GAM_AT_REALIZED_SE_ROW),
        )

    def test_a_non_numeric_mapping_value_names_the_level(self):
        with self.assertRaises(ValueError) as caught:
            pre.power_bracket(**self.kwargs, gam={pre.ALIGNMENT_ROW: "big"})
        self.assertIn(pre.ALIGNMENT_ROW, str(caught.exception))

    def test_a_default_magnitude_is_refused_for_another_feature(self):
        # The calibrated pair is a fact about the ATR slope. Applying it to
        # another feature would print an ATR calibration sentence beside that
        # feature's span, which is the one way this module can still publish a
        # number with a false claim next to it.
        with self.assertRaises(ValueError) as caught:
            pre.power_bracket(**self.kwargs, feature="ma50", target="ma50")
        message = str(caught.exception)
        self.assertIn("ma50", message)
        self.assertIn("measure_noise_alignment", message)

    def test_another_feature_works_once_its_own_magnitude_is_given(self):
        bracket = pre.power_bracket(**self.kwargs, feature="ma50", target="ma50", gam=0.25)
        self.assertEqual(bracket.alignments[pre.ALIGNMENT_SESSION].feature, "ma50")
        self.assertEqual(bracket.alignments[pre.ALIGNMENT_SESSION].gam, 0.25)

    def test_a_per_level_override_can_reach_the_measured_pair(self):
        # One scalar `gam` cannot express two levels on two scales, so the
        # measured pair was unreachable by any invocation.
        bracket = pre.power_bracket(
            **self.kwargs,
            gam={
                pre.ALIGNMENT_SESSION: pre.MEASURED_GAM_SESSION,
                pre.ALIGNMENT_ROW: pre.MEASURED_GAM_ROW,
            },
        )
        self.assertEqual(bracket.alignments[pre.ALIGNMENT_SESSION].gam, pre.MEASURED_GAM_SESSION)
        self.assertEqual(bracket.alignments[pre.ALIGNMENT_ROW].gam, pre.MEASURED_GAM_ROW)

    def test_a_straddling_corner_is_refused_even_when_every_point_clears(self):
        # The Monte Carlo straddle guard, one level up from TestTheMonteCarloGuard.
        # Every corner is above the bar by point estimate, but the worst one's
        # Wilson interval crosses it, so the span is not an answer.
        bracket = self._hand_built((0.95, 0.90, 0.81), 100)
        low, high = pre.wilson_interval(0.81, 100)
        self.assertLess(low, pre.GATE_POWER)
        self.assertGreater(high, pre.GATE_POWER)
        self.assertFalse(bracket.is_resolved())
        with self.assertRaises(pre.AlignmentLevelNotIdentifiedError):
            bracket.power_at_gate()

    def test_a_zero_magnitude_is_not_a_second_arm(self):
        # At gam 0 both aligned corners are bit-identical to the homoskedastic
        # one, so the span is zero-wide and a BRACKET VERDICT would hand back
        # the corner this whole module measured to be 1.36x optimistic.
        with self.assertRaises(ValueError):
            pre.NoiseAlignment(0.0, pre.ALIGNMENT_SESSION)

    def test_one_magnitude_overrides_both_aligned_arms(self):
        bracket = pre.power_bracket(**self.kwargs, gam=0.3)
        self.assertEqual(bracket.alignments[pre.ALIGNMENT_SESSION].gam, 0.3)
        self.assertEqual(bracket.alignments[pre.ALIGNMENT_ROW].gam, 0.3)

    def test_the_only_scalar_accessor_returns_the_worst_corner(self):
        # Reaching for a number can only ever hand back the most pessimistic
        # corner, never the homoskedastic one an ordered container would give.
        bracket = self._hand_built((0.95, 0.90, 0.88), 4000)
        self.assertTrue(bracket.is_resolved())
        self.assertAlmostEqual(bracket.power_at_gate(), 0.88, places=12)

    def test_the_span_is_the_widest_pair_of_corners(self):
        bracket = self._hand_built((0.95, 0.90, 0.88), 4000)
        self.assertEqual(bracket.span(), (0.88, 0.95))

    def test_the_arms_are_paired_at_one_seed(self):
        # Same seed on every arm, so the span is a mechanism difference and not
        # three independent Monte Carlo draws.
        plain = pre.simulate_power(**self.kwargs)
        self.assertEqual(self.bracket.arms["homoskedastic"], plain)

    def test_an_aligned_arm_really_costs_power(self):
        # The refutation control for the whole bracket, and the hole every other
        # case here leaves open: if `alignment` were silently dropped on its way
        # into simulate_power, the three corners would be three copies of the
        # optimistic one and every assertion above would still pass.
        #
        # Measured on this fixture (40 panels, 49 resamples, seed 17, row-level
        # gam 0.205 on a right-skewed ATR column): homoskedastic 50.0%, session
        # 40.0%, row 30.0%. The ordering held at every one of eight
        # configurations probed, which is the memo's direction - the
        # homoskedastic corner is the optimistic one.
        rng = np.random.default_rng(9)
        n = 300
        atr = rng.lognormal(0.0, 0.6, size=n)
        ma50 = 0.5 * pre._z(atr) + rng.normal(size=n)
        press = rng.integers(0, 5, size=n).astype(float)
        sd_y = 0.1653
        bracket = pre.power_bracket(
            burnt_signals=np.column_stack([pre._z(atr), pre._z(ma50), pre._z(press)]),
            cluster_sizes=[6] * 20,
            effects={"atr": -0.25 * sd_y, "ma50": -0.1 * sd_y, "press": 0.0},
            sd_y=sd_y,
            icc=0.05,
            n_sims=40,
            wcb_boot=49,
            seed=17,
            family_size=1,
            gam=pre.GAM_AT_REALIZED_SE_ROW,
        )
        self.assertGreater(
            bracket.arm_power("homoskedastic") - bracket.arm_power(pre.ALIGNMENT_ROW),
            _ALIGNED_ARM_COSTS_AT_LEAST,
        )
        low, high = bracket.span()
        self.assertEqual(high, bracket.arm_power("homoskedastic"))
        self.assertLess(low, high)


class TestTheBracketRefusesToCollapse(unittest.TestCase):
    """One refusal, raised only where a single figure would be returned."""

    def _straddling(self):
        return TestThePowerBracket._hand_built((0.95, 0.80, 0.60), 400)

    def test_a_span_crossing_the_bar_raises_the_named_class(self):
        # The class, not a bare ValueError: this module already raises six of
        # those and none of them is this defect.
        with self.assertRaises(pre.AlignmentLevelNotIdentifiedError):
            self._straddling().power_at_gate()

    def test_the_message_names_both_magnitudes_and_the_bar(self):
        with self.assertRaises(pre.AlignmentLevelNotIdentifiedError) as caught:
            self._straddling().power_at_gate()
        message = str(caught.exception)
        for fragment in ("0.400", "0.205", "80%", "section 7.5", pre.ALIGNMENT_MEMO):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, message)

    def test_no_single_power_figure_is_printed_when_the_span_crosses(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            pre.print_power_bracket(self._straddling())
        printed = buffer.getvalue()
        self.assertIn("NO BRACKET VERDICT", printed)
        self.assertNotIn("BRACKET VERDICT: every corner", printed)

    def test_the_calibration_clause_describes_the_arms_that_actually_ran(self):
        # An earlier version printed the calibrated sentence unconditionally, so
        # arms run at any other magnitude sat beside a sentence that read as a
        # description of them. That is the same inconsistency the DEFAULT was
        # already fixed for once, reappearing on the documented --alignment-gam
        # path. Caught by mutation: forcing the clause on left the suite green.
        at_default = TestThePowerBracket._hand_built((0.95, 0.90, 0.88), 4000)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            pre.print_power_bracket(at_default)
        printed = buffer.getvalue()
        self.assertIn("the arms run at the CALIBRATED pair", printed)
        self.assertNotIn("do NOT run at the calibrated pair", printed)

        elsewhere = dataclasses.replace(
            at_default,
            alignments={
                "homoskedastic": None,
                pre.ALIGNMENT_SESSION: pre.NoiseAlignment(0.1, pre.ALIGNMENT_SESSION),
                pre.ALIGNMENT_ROW: pre.NoiseAlignment(0.1, pre.ALIGNMENT_ROW),
            },
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            pre.print_power_bracket(elsewhere)
        printed = buffer.getvalue()
        self.assertIn("do NOT run at the calibrated pair", printed)
        self.assertNotIn("the arms run at the CALIBRATED pair", printed)

    def test_the_printed_span_names_what_it_covers_and_what_it_does_not(self):
        # Two LEVELS at one magnitude each, not the range of magnitudes the data
        # admits, and the projections are not bracketed at all.
        bracket = TestThePowerBracket._hand_built((0.95, 0.90, 0.88), 4000)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            pre.print_power_bracket(bracket)
        printed = buffer.getvalue()
        self.assertIn("two LEVELS at one magnitude each", printed)
        self.assertIn("gate DATE above are NOT bracketed", printed)
        self.assertIn(f"{pre.REALIZED_SLOPE_SE_CR2_RELATIVE_SD:.1%}", printed)

    def test_a_resolved_span_does_return_a_number(self):
        # The positive control: otherwise the refusal could be unconditional.
        resolved = TestThePowerBracket._hand_built((0.95, 0.93, 0.92), 4000)
        self.assertAlmostEqual(resolved.power_at_gate(), 0.92, places=12)

    def test_a_resolved_span_below_the_bar_is_not_called_a_pass(self):
        # is_resolved() is true when every corner sits BELOW the bar too, so the
        # printed verdict has to say which side it resolved on.
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            pre.print_power_bracket(TestThePowerBracket._hand_built((0.30, 0.20, 0.10), 4000))
        printed = buffer.getvalue()
        self.assertIn("no corner clears", printed)
        self.assertNotIn("every corner clears", printed)


#: Twenty-one arrival sessions carrying 3 to 26 episodes each, 198 in all: the
#: shape section 7.3 and section 7.5 measured `gam` on (21 sessions above the
#: 3-episode floor, 198 row-level points). The individual sizes are a spread
#: across that range; the real panel's own list is not published.
_BURNT_LIKE_SESSION_SIZES = (3, 3, 4, 4, 5, 5, 5, 6, 6, 7, 7, 8, 8, 9, 10, 11, 14, 17, 18, 22, 26)

#: Noise scale of the synthetic burnt panel before alignment.
_FIXTURE_NOISE_SD = 0.12


def _aligned_burnt_panel(
    *,
    gam: float,
    level: str | None,
    seed: int,
    session_sizes: tuple[int, ...] = _BURNT_LIKE_SESSION_SIZES,
) -> pd.DataFrame:
    """A burnt-shaped panel whose noise alignment is KNOWN by construction.

    Purpose-made rather than a widened ``_Store``: that store is homoskedastic by
    construction with 14 sessions of 4 rows, which cannot check a reading of
    0.36, and ``test_a20_overlap_power`` imports it, so widening it would change
    a second suite.

    ``level`` is ``None`` for the homoskedastic control. Within-session ATR is
    drawn i.i.d., matching the burnt panel's measured within-session ATR ICC of
    0.0080 - and that near-zero ICC is exactly why the session arm cannot
    identify the level.
    """
    rng = np.random.default_rng(seed)
    sessions = [d.isoformat() for d in _sessions(len(session_sizes), dt.date(2026, 3, 2))]
    arrival: list[str] = []
    atr_parts, ma50_parts, press_parts = [], [], []
    for day, size in zip(sessions, session_sizes, strict=True):
        arrival.extend([day] * size)
        atr_parts.append(rng.normal(size=size))
        ma50_parts.append(0.5 * atr_parts[-1] + rng.normal(size=size))
        press_parts.append(rng.integers(0, 5, size=size).astype(float))

    atr = np.concatenate(atr_parts)
    ma50 = np.concatenate(ma50_parts)
    press = np.concatenate(press_parts)
    arrival_all = np.array(arrival)
    z_atr = pre._z(atr)

    if level is None:
        scale = np.ones(len(z_atr))
    elif level == pre.ALIGNMENT_ROW:
        scale = np.exp(gam * z_atr)
    elif level == pre.ALIGNMENT_SESSION:
        means = np.array([float(z_atr[arrival_all == s].mean()) for s in sessions])
        by_session = dict(zip(sessions, np.exp(gam * pre._z(means)), strict=True))
        scale = np.array([by_session[s] for s in arrival_all])
    else:
        raise AssertionError(f"fixture cannot build level {level!r}")
    scale = scale / math.sqrt(float(np.mean(scale**2)))

    noise = rng.normal(scale=_FIXTURE_NOISE_SD, size=len(z_atr)) * scale
    return pd.DataFrame(
        {
            "arrival": arrival_all,
            pre.OUTCOME: -0.06 * atr - 0.03 * ma50 + noise,
            pre.SIGNALS["atr"]: atr,
            pre.SIGNALS["ma50"]: ma50,
            pre.SIGNALS["press"]: press,
        }
    )


class TestTheAlignmentEstimator(unittest.TestCase):
    """``gam`` measured from inside the module, on burnt data, with its own se."""

    def test_a_known_session_truth_is_recovered_in_sign_and_rough_size(self):
        estimate = pre.measure_noise_alignment(
            _aligned_burnt_panel(gam=pre.MEASURED_GAM_SESSION, level=pre.ALIGNMENT_SESSION, seed=1)
        )
        self.assertGreater(estimate.session_gam, 0.15)
        self.assertLess(estimate.session_gam, 0.70)

    def test_a_homoskedastic_panel_reads_about_zero(self):
        # The refutation control: a broken estimator returning a constant would
        # pass the case above.
        estimate = pre.measure_noise_alignment(_aligned_burnt_panel(gam=0.0, level=None, seed=1))
        self.assertLess(abs(estimate.session_gam), 0.15)

    def test_the_slope_and_its_standard_error_match_a_closed_form(self):
        # The estimator's SE is the number requirement (4) exists for: a
        # registration quotes its feature's alignment WITH a standard error. It
        # had no numeric assertion, so it could have been wrong by a factor of
        # two, or exactly zero, which makes the reported t silently NaN.
        # Closed form on four points with an exact answer:
        #   x = 0,1,2,3 ; y = 1,3,5,8  ->  slope 2.3, intercept 0.8,
        #   residuals 0.2,-0.1,-0.4,0.3 ; SSR 0.30 ; dof 2 ; Sxx 5
        #   se = sqrt((0.30/2)/5) = sqrt(0.03)
        # The first draft of this test used intercept 0.6 and SSR 0.62, which is
        # wrong arithmetic; the estimator was right and the test was not.
        x = np.array([0.0, 1.0, 2.0, 3.0])
        y = np.array([1.0, 3.0, 5.0, 8.0])
        slope, se, t = pre._ols_slope(y, x)
        self.assertAlmostEqual(slope, 2.3, places=12)
        self.assertAlmostEqual(se, math.sqrt(0.03), places=12)
        self.assertAlmostEqual(t, 2.3 / math.sqrt(0.03), places=12)

    def test_the_reported_t_is_the_slope_over_its_own_standard_error(self):
        # Pins the three returned fields against each other, so an SE that is
        # scaled or zeroed cannot leave a plausible-looking t behind.
        estimate = pre.measure_noise_alignment(
            _aligned_burnt_panel(gam=pre.MEASURED_GAM_SESSION, level=pre.ALIGNMENT_SESSION, seed=1)
        )
        self.assertAlmostEqual(
            estimate.session_t, estimate.session_gam / estimate.session_se, places=12
        )
        self.assertAlmostEqual(estimate.row_t, estimate.row_gam / estimate.row_se, places=12)
        self.assertGreater(estimate.session_se, 0.0)
        self.assertGreater(estimate.row_se, 0.0)

    def test_both_levels_come_back_in_one_object(self):
        # Neither reading can be obtained without the other, because reading one
        # alone is what makes the level look identified.
        estimate = pre.measure_noise_alignment(
            _aligned_burnt_panel(gam=pre.MEASURED_GAM_SESSION, level=pre.ALIGNMENT_SESSION, seed=2)
        )
        for field in (
            "session_gam",
            "session_se",
            "session_t",
            "row_gam",
            "row_se",
            "row_t",
        ):
            with self.subTest(field=field):
                self.assertIsNotNone(getattr(estimate, field))
        self.assertEqual(estimate.feature, pre.ALIGNMENT_FEATURE)

    def test_too_few_sessions_is_reported_not_dropped(self):
        panel = _aligned_burnt_panel(gam=0.0, level=None, seed=3, session_sizes=(6, 6))
        estimate = pre.measure_noise_alignment(panel)
        self.assertIsNone(estimate.session_gam)
        self.assertIsNone(estimate.session_se)
        self.assertEqual(estimate.session_points, 2)

    def test_a_session_below_the_episode_floor_is_excluded(self):
        panel = _aligned_burnt_panel(gam=0.0, level=None, seed=4, session_sizes=(8, 8, 8, 2))
        estimate = pre.measure_noise_alignment(panel)
        self.assertEqual(estimate.session_points, 3)

    def test_the_row_arm_uses_every_episode(self):
        panel = _aligned_burnt_panel(gam=0.0, level=None, seed=5)
        self.assertEqual(pre.measure_noise_alignment(panel).row_points, len(panel))
        self.assertEqual(len(panel), sum(_BURNT_LIKE_SESSION_SIZES))


#: Panels per arm when the non-identification itself is being measured. The memo
#: used 1500; this is the count a pre-merge suite can afford, and the assertions
#: are about SIGN and direction rather than the second decimal.
#:
#: Measured on this fixture over 200 panels, so the thresholds below carry their
#: own Monte Carlo error: a per-panel session reading has sd about 0.10, so the
#: mean of 40 has a standard error of about 0.016.
_IDENTIFICATION_PANELS = 40

#: A ROW-level truth must still read clearly positive on the SESSION arm. The
#: fixture reads +0.128 (per-panel sd 0.103) against a homoskedastic -0.003, so
#: this threshold sits about 4 standard errors below the one and 4 above the
#: other. The memo's figure on the real burnt design is +0.240.
_ROW_TRUTH_READS_AT_LEAST = 0.06

#: And a homoskedastic truth must not. Measured -0.003 over 200 panels.
_HOMOSKEDASTIC_READS_WITHIN = 0.05


class TestTheEstimatorDoesNotIdentifyTheLevel(unittest.TestCase):
    """Pins the documented non-identification, which is the whole reason the fix
    is a bracket and not a calibration.

    Section 7.5, 1500 panels per row on the real burnt design: a ROW-level truth
    of 0.36 reads back as +0.240 on the SESSION arm and is significant in 72.4%
    of panels. So a positive, significant session reading is not evidence for the
    session level, and a fix that wrote one level into the DGP would miscalibrate
    every later power figure by about a factor of two in the parameter.
    """

    def _session_readings(self, *, level, gam):
        readings = []
        for seed in range(_IDENTIFICATION_PANELS):
            estimate = pre.measure_noise_alignment(
                _aligned_burnt_panel(gam=gam, level=level, seed=100 + seed)
            )
            readings.append(estimate.session_gam)
        return float(np.mean(readings))

    def test_a_row_level_truth_reads_positive_on_the_session_arm(self):
        mean = self._session_readings(level=pre.ALIGNMENT_ROW, gam=pre.MEASURED_GAM_SESSION)
        self.assertGreater(mean, _ROW_TRUTH_READS_AT_LEAST)

    def test_a_homoskedastic_truth_does_not(self):
        # The control that makes the case above a measurement of alignment and
        # not of an estimator biased upward on everything.
        mean = self._session_readings(level=None, gam=0.0)
        self.assertLess(abs(mean), _HOMOSKEDASTIC_READS_WITHIN)

    def test_a_session_level_truth_is_recovered_near_its_own_size(self):
        # NOT the memo's over-read to +0.419, and the difference is reported
        # rather than asserted: measured on THIS fixture over 200 panels a
        # session truth of 0.360 reads back as +0.347 (sd 0.094), so the
        # over-read is a property of the real burnt design - 26 real sessions,
        # the real ATR column and the >=3-episode survival filter - and not of
        # the mechanism. The disclosure in the estimator's own docstring quotes
        # the memo's measurement, which is the one a registration would cite.
        mean = self._session_readings(level=pre.ALIGNMENT_SESSION, gam=pre.MEASURED_GAM_SESSION)
        self.assertGreater(mean, pre.MEASURED_GAM_SESSION * 0.85)
        self.assertLess(mean, pre.ESTIMATOR_READING_OF_A_SESSION_TRUTH * 1.15)

    def test_the_docstring_discloses_the_non_identification(self):
        # Cheap, and the only thing that stops the disclosure being deleted by a
        # later tidy-up.
        doc = pre.measure_noise_alignment.__doc__ or ""
        for fragment in ("does not identify", "over-read", "0.419"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, doc.lower() if fragment.islower() else doc)


def _held_out_reads(source: str) -> list[tuple[str, str | None]]:
    """Every held-out-side read in a module, with the function that encloses it.

    A read of the held-out side is a comparison against ``DISCOVERY_CUTOFF`` or a
    directory ``glob``. One predicate over the whole module, rather than a
    watcher per function, because the hole this closes is a NEW function reading
    the held-out store - and a new function has no watcher.
    """
    found: list[tuple[str, str | None]] = []

    def visit(node: ast.AST, enclosing: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else enclosing
            )
            if isinstance(child, ast.Compare) and any(
                isinstance(part, ast.Name) and part.id == "DISCOVERY_CUTOFF"
                for part in [child.left, *child.comparators]
            ):
                found.append(("cutoff", name))
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "glob"
            ):
                found.append(("glob", name))
            visit(child, name)

    visit(ast.parse(source), None)
    return found


class TestTheNewReaderStaysBlind(unittest.TestCase):
    """The new estimator reads the BURNT panel and cannot reach the held-out side."""

    _ALLOWED_READERS = {"load_held_out", "burnt_panel"}

    def test_the_estimator_reads_only_burnt_columns(self):
        panel = _aligned_burnt_panel(gam=0.0, level=None, seed=7)
        seen: list[str] = []

        class _Watched(pd.DataFrame):
            @property
            def _constructor(self):
                return _Watched

            def __getitem__(self, key):
                seen.extend([key] if isinstance(key, str) else list(key))
                return super().__getitem__(key)

        pre.measure_noise_alignment(_Watched(panel))
        self.assertTrue(seen, "positive control: the watcher recorded nothing at all")
        allowed = {pre.OUTCOME, "arrival", *pre.SIGNALS.values()}
        self.assertTrue(
            set(seen) <= allowed,
            f"the estimator read outside the burnt panel: {sorted(set(seen) - allowed)}",
        )

    def test_only_load_held_out_reaches_the_held_out_side(self):
        from pathlib import Path

        source = Path(pre.__file__).read_text()
        sites = _held_out_reads(source)
        self.assertTrue(sites, "positive control: the module does read the held-out side")
        offenders = [site for site in sites if site[1] not in self._ALLOWED_READERS]
        self.assertEqual(offenders, [])

    def test_the_source_gate_can_refute(self):
        offending = (
            "def measure_something(labels_dir):\n"
            "    return [p for p in labels_dir.glob('*.parquet') "
            "if p.stem > DISCOVERY_CUTOFF]\n"
        )
        sites = _held_out_reads(offending)
        self.assertEqual(
            sorted(sites), [("cutoff", "measure_something"), ("glob", "measure_something")]
        )
        self.assertTrue([s for s in sites if s[1] not in self._ALLOWED_READERS])


class TestTheAlignmentFlags(unittest.TestCase):
    """The optimistic path must be hard to take by accident."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = _Store(self.root)

    def _run(self, extra: list[str]) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pre.main(
                [
                    "--labels-dir",
                    str(self.store.labels),
                    "--briefs-dir",
                    str(self.store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "9",
                    "--max-clusters",
                    "8",
                    "--population",
                    pre.POPULATION_BRIEFED,
                    *extra,
                ]
            )
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_a_zero_magnitude_is_refused_at_parse_time(self):
        # The dataclass half of this guard was pinned; the CLI half was not.
        # Caught by mutation: relaxing the CLI check from <= 0 to < 0 left the
        # whole suite green, and the failure then surfaced as a raw ValueError
        # traceback instead of a clean parser error.
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), self.assertRaises(SystemExit):
            pre.main(
                [
                    "--labels-dir",
                    str(self.root / "nowhere"),
                    "--briefs-dir",
                    str(self.root / "nowhere"),
                    "--alignment-bracket",
                    "--alignment-gam",
                    "0",
                ]
            )
        message = buffer.getvalue()
        self.assertIn("strictly positive", message)
        # The way out, which no earlier message named.
        self.assertIn("Omit --alignment-gam", message)

    def test_a_non_finite_magnitude_is_refused_without_the_zero_explanation(self):
        # The zero explanation is wrong for nan, and one combined branch printed
        # it for nan and for negatives too.
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), self.assertRaises(SystemExit):
            pre.main(
                [
                    "--labels-dir",
                    str(self.root / "nowhere"),
                    "--briefs-dir",
                    str(self.root / "nowhere"),
                    "--alignment-bracket",
                    "--alignment-gam",
                    "nan",
                ]
            )
        message = buffer.getvalue()
        self.assertIn("must be finite", message)
        self.assertNotIn("homoskedastic corner", message)

    def test_a_magnitude_without_a_bracket_is_refused_before_the_read(self):
        # The likeliest accident is someone wanting "the aligned number". It is
        # caught at parse time, with the reason - and the labels dir below does
        # not exist, which proves nothing was read before the refusal.
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), self.assertRaises(SystemExit):
            pre.main(
                [
                    "--labels-dir",
                    str(self.root / "nowhere"),
                    "--briefs-dir",
                    str(self.root / "nowhere"),
                    "--alignment-gam",
                    "0.36",
                ]
            )
        self.assertIn("--alignment-gam", buffer.getvalue())
        self.assertIn("--alignment-bracket", buffer.getvalue())

    def test_a_negative_magnitude_is_refused_at_parse_time(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), self.assertRaises(SystemExit):
            pre.main(
                [
                    "--labels-dir",
                    str(self.root / "nowhere"),
                    "--briefs-dir",
                    str(self.root / "nowhere"),
                    "--alignment-bracket",
                    "--alignment-gam",
                    "-0.2",
                ]
            )
        # The phrase, not just the value: before the flag existed argparse said
        # "unrecognized arguments: -0.2" and that is not this refusal.
        self.assertIn("-0.2", buffer.getvalue())
        self.assertIn("--alignment-gam must be", buffer.getvalue())

    def test_a_feature_with_no_consumer_is_refused(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), self.assertRaises(SystemExit):
            pre.main(
                [
                    "--labels-dir",
                    str(self.root / "nowhere"),
                    "--briefs-dir",
                    str(self.root / "nowhere"),
                    "--alignment-feature",
                    "ma50",
                ]
            )
        self.assertIn("--alignment-feature", buffer.getvalue())
        self.assertIn("has no consumer", buffer.getvalue())

    def test_the_default_run_discloses_the_measured_optimism(self):
        # Printed on EVERY default run, including the published reproductions,
        # and it moves no number.
        printed = self._run([])
        self.assertIn("HOMOSKEDASTIC corner", printed)
        self.assertIn("1.36", printed)
        self.assertIn(pre.ALIGNMENT_MEMO, printed)

    def test_the_bracket_block_names_all_three_corners(self):
        printed = self._run(["--alignment-bracket"])
        for arm in ("homoskedastic", "session", "row"):
            with self.subTest(arm=arm):
                self.assertIn(arm, printed)
        self.assertIn("span", printed)

    def test_the_estimate_block_states_the_non_identification(self):
        printed = self._run(["--measure-alignment"])
        self.assertIn("NOT IDENTIFIED", printed)
        self.assertIn("OVER-READ", printed)

    def test_the_report_carries_the_new_blocks_only_when_asked(self):
        import json

        plain = self.root / "plain.json"
        with contextlib.redirect_stdout(io.StringIO()):
            pre.main(
                [
                    "--labels-dir",
                    str(self.store.labels),
                    "--briefs-dir",
                    str(self.store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "9",
                    "--max-clusters",
                    "8",
                    "--population",
                    pre.POPULATION_BRIEFED,
                    "--out-json",
                    str(plain),
                ]
            )
        written = json.loads(plain.read_text())[pre.POPULATION_BRIEFED]
        self.assertIsNone(written["alignment_bracket"])
        self.assertIsNone(written["alignment_estimate"])

    def test_a_recorded_bracket_keeps_the_magnitude_of_every_corner(self):
        # Structured data, not a stringified repr: a span in a file that lost
        # the gam that produced each corner is a number without a construction.
        import json

        out = self.root / "bracket.json"
        with contextlib.redirect_stdout(io.StringIO()):
            pre.main(
                [
                    "--labels-dir",
                    str(self.store.labels),
                    "--briefs-dir",
                    str(self.store.briefs),
                    "--n-sims",
                    "2",
                    "--wcb-boot",
                    "9",
                    "--max-clusters",
                    "8",
                    "--population",
                    pre.POPULATION_BRIEFED,
                    "--alignment-bracket",
                    "--measure-alignment",
                    "--out-json",
                    str(out),
                ]
            )
        written = json.loads(out.read_text())[pre.POPULATION_BRIEFED]
        bracket = written["alignment_bracket"]
        self.assertEqual(set(bracket["arms"]), set(pre.BRACKET_ARMS))
        self.assertEqual(
            bracket["alignments"][pre.ALIGNMENT_SESSION]["gam"],
            pre.GAM_AT_REALIZED_SE_SESSION,
        )
        self.assertEqual(
            bracket["alignments"][pre.ALIGNMENT_SESSION]["level"], pre.ALIGNMENT_SESSION
        )
        self.assertEqual(
            bracket["alignments"][pre.ALIGNMENT_ROW]["gam"], pre.GAM_AT_REALIZED_SE_ROW
        )
        self.assertEqual(bracket["alignments"][pre.ALIGNMENT_ROW]["level"], pre.ALIGNMENT_ROW)
        self.assertIsNone(bracket["alignments"]["homoskedastic"])
        self.assertEqual(written["alignment_estimate"]["feature"], pre.ALIGNMENT_FEATURE)

    def test_the_machine_readable_artifact_carries_the_disclosure(self):
        # The JSON is what a memo or a ledger row quotes. A power figure recorded
        # without its condition is the accident this module exists to prevent,
        # and the human output printing a warning does not help a reader who
        # only ever sees the artifact.
        import json

        out = self.root / "power.json"
        self._run(["--out-json", str(out)])
        written = json.loads(out.read_text())
        self.assertTrue(written)
        for population, record in written.items():
            self.assertIn(pre.PRECISION_DISCLOSURE_KEY, record, population)
            disclosure = record[pre.PRECISION_DISCLOSURE_KEY]
            self.assertEqual(disclosure["memo"], pre.ALIGNMENT_MEMO)
            self.assertEqual(disclosure["optimism_ratio"], pre.SIMULATOR_OPTIMISM_RATIO)
            self.assertEqual(disclosure["realized_slope_se_cr2"], pre.REALIZED_SLOPE_SE_CR2)
            # The calibration target's OWN noise. Without these a reader of the
            # artifact would take 0.0731 for a constant, and the magnitudes that
            # reach it inherit its spread.
            self.assertEqual(
                disclosure["realized_slope_se_cr2_relative_sd"],
                pre.REALIZED_SLOPE_SE_CR2_RELATIVE_SD,
            )
            self.assertEqual(
                disclosure["realized_slope_se_cr2_percentile"],
                pre.REALIZED_SLOPE_SE_CR2_PERCENTILE,
            )
            self.assertIn("one panel", disclosure["calibration_target_is_one_panel"])
            self.assertIn(pre.ALIGNMENT_FEATURE, disclosure["calibrated_pair_is_feature_specific"])
            # The things a reader of the artifact alone would otherwise miss.
            self.assertIn("not identified", disclosure["level_not_identified"])
            self.assertIn("gate", disclosure["projections_are_not_bracketed"])

    def test_the_disclosure_does_not_masquerade_as_a_population(self):
        # It lives inside each population's record, never beside them: the top
        # level is keyed by population and consumers iterate it.
        import json

        out = self.root / "power.json"
        self._run(["--out-json", str(out)])
        written = json.loads(out.read_text())
        self.assertNotIn(pre.PRECISION_DISCLOSURE_KEY, written)
        self.assertTrue(set(written) <= {pre.POPULATION_BRIEFED, pre.POPULATION_ALL})

    def test_the_level_has_no_default_so_it_cannot_be_chosen_silently(self):
        # A default on `level` would re-introduce the unidentified choice: every
        # call site that forgot the argument would silently mean one level.
        with self.assertRaises(TypeError):
            pre.NoiseAlignment(0.36)

    def test_a_session_at_the_episode_floor_is_kept_not_dropped(self):
        # The printed construction says ">= 3 episodes". An off-by-one here
        # silently changes the denominator of a published estimate, and that
        # estimate is what a registration quotes.
        self.assertEqual(pre.MIN_EPISODES_PER_SESSION_FOR_GAM, 3)
        estimate = pre.measure_noise_alignment(
            _aligned_burnt_panel(
                gam=pre.MEASURED_GAM_SESSION,
                level=pre.ALIGNMENT_SESSION,
                seed=5,
                session_sizes=(3,) * 6,
            )
        )
        self.assertEqual(estimate.session_points, 6)


class TestTheDisclosureDefaultIsUnchanged(unittest.TestCase):
    """``precision_disclosure`` gained an argument; its default must not move.

    ``a20_overlap_power`` reuses this disclosure rather than copying it, but one
    key cannot be reused verbatim: ``projections_are_not_bracketed`` names the
    cluster-count search, the gate DATE and ``--alignment-bracket``, none of
    which exist in that module. So the sentence became an argument. This pins
    that the argument's default is still the exact string this module's own
    merged artifact carries.
    """

    _MERGED_NOTE = (
        "the cluster-count search and the gate DATE are computed on the "
        "homoskedastic corner only; --alignment-bracket does not move them"
    )

    def test_calling_it_with_no_argument_returns_the_merged_sentence(self):
        self.assertEqual(
            pre.precision_disclosure()["projections_are_not_bracketed"], self._MERGED_NOTE
        )

    def test_a_caller_with_other_surfaces_can_name_them_instead(self):
        note = "only the per-arm sizes and powers are bracketed here"
        self.assertEqual(
            pre.precision_disclosure(projections_note=note)["projections_are_not_bracketed"], note
        )

    def test_overriding_the_note_moves_nothing_else(self):
        default = pre.precision_disclosure()
        overridden = pre.precision_disclosure(projections_note="something else entirely")
        del default["projections_are_not_bracketed"]
        del overridden["projections_are_not_bracketed"]
        self.assertEqual(default, overridden)


if __name__ == "__main__":
    unittest.main()

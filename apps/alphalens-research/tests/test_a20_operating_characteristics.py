"""Unit tests for the #1227 operating-characteristics probe.

Hermetic: every case feeds synthetic frames to the pure functions. The two
numbers the probe finally prints are single recorded runs, not regression
fixtures, and they are published in
``docs/research/atr_confirmation_prereg_2026_09.md`` §11 with their seeds.

The load-bearing cases are :class:`TestTheArmsMatchTheRegistration` and
:class:`TestBlindness`. The first exists because this probe reports the
frequencies of the registration's own three arms, so its classifier has to BE
``decide()``'s mapping rather than resemble it — a probe that grades on a
near-copy of the rule publishes a table about a rule nobody registered. The
second exists because the probe's whole claim to not being a look is that it
reads cluster structure and simulates every outcome it grades.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


def _load(stem: str, name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / "ml" / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # In sys.modules BEFORE it executes: @dataclass resolves its own class's
    # module to read annotations, and a module missing from the table makes that
    # lookup return None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


opchar = _load("2026_11_a20_operating_characteristics", "_a20_opchar")
atr = _load("2026_09_a20_atr_confirmation", "_a20_atr_confirmation_for_opchar")


class TestTheArmsMatchTheRegistration(unittest.TestCase):
    """``classify_tilt`` must agree with ``decide()`` on every sign case.

    ``decide()`` reads the p-value as well, so the comparison is pinned at a
    p-value that satisfies the significance leg; the probe's documented
    assumption is that the FLOOR binds, and this test is what keeps the
    assumption honest about the mapping itself.
    """

    CLEARS_P = 0.001

    def _registered(self, beta: float) -> str:
        return atr.decide(beta=beta, p_value=self.CLEARS_P, ci=(beta - 0.09, beta + 0.09)).tilt

    def test_a_slope_at_or_past_the_floor_is_confirmed(self):
        for beta in (-0.10, -0.1001, -0.25, -1.0):
            with self.subTest(beta=beta):
                self.assertEqual("confirmed", opchar.classify_tilt(beta, atr.DELTA))
                self.assertEqual(self._registered(beta), opchar.classify_tilt(beta, atr.DELTA))

    def test_a_negative_slope_short_of_the_floor_is_unchanged(self):
        for beta in (-0.0999, -0.05, -1e-12):
            with self.subTest(beta=beta):
                self.assertEqual("unchanged", opchar.classify_tilt(beta, atr.DELTA))
                self.assertEqual(self._registered(beta), opchar.classify_tilt(beta, atr.DELTA))

    def test_a_zero_or_positive_slope_is_retired(self):
        for beta in (0.0, 1e-12, 0.05, 0.4):
            with self.subTest(beta=beta):
                self.assertEqual("retired", opchar.classify_tilt(beta, atr.DELTA))
                self.assertEqual(self._registered(beta), opchar.classify_tilt(beta, atr.DELTA))

    def test_exactly_zero_lands_on_retired_and_not_on_unchanged(self):
        """The boundary the registration puts on the kill trigger: ``beta < 0``,
        so a slope of exactly zero fires it. Flipping this to ``<=`` would move
        a measure-zero case, but it would move it in the direction that reports
        "the July trigger did not fire" when the rule says it did."""
        self.assertEqual("retired", opchar.classify_tilt(0.0, atr.DELTA))


class TestTheEvidenceAgainstArm(unittest.TestCase):
    """Arm (iii): the whole interval inside (-DELTA, +DELTA), containment STRICT."""

    def test_an_interval_well_inside_the_floor_is_evidence_against(self):
        self.assertTrue(opchar.is_evidence_against(-0.02, 0.03, atr.DELTA))

    def test_an_endpoint_exactly_on_the_floor_has_not_excluded_it(self):
        self.assertFalse(opchar.is_evidence_against(-0.10, 0.03, atr.DELTA))
        self.assertFalse(opchar.is_evidence_against(-0.02, 0.10, atr.DELTA))

    def test_an_interval_reaching_past_the_floor_is_not_evidence_against(self):
        self.assertFalse(opchar.is_evidence_against(-0.12, 0.03, atr.DELTA))

    def test_it_agrees_with_the_registration_on_the_same_interval(self):
        for lo, hi in ((-0.02, 0.03), (-0.10, 0.03), (-0.12, 0.03), (-0.02, 0.10)):
            with self.subTest(ci=(lo, hi)):
                registered = atr.decide(beta=-0.01, p_value=0.5, ci=(lo, hi))
                self.assertEqual(
                    registered.conclusion == "evidence-against",
                    opchar.is_evidence_against(lo, hi, atr.DELTA),
                )


class TestTheInjectedEffects(unittest.TestCase):
    """The grid walks a TRUE ATR effect; the nuisance signals stay where the
    power gate puts them."""

    BURNT = {"atr": -0.358, "ma50": -0.315, "press": 0.006}
    SD_Y = 0.1681

    def test_the_atr_effect_carries_the_registered_negative_direction(self):
        for rho in (0.0, 0.05, 0.25):
            with self.subTest(rho=rho):
                injected = opchar.inject_effects(self.BURNT, rho=rho, sd_y=self.SD_Y)
                self.assertAlmostEqual(-abs(rho) * self.SD_Y, injected["atr"])

    def test_a_positive_rho_argument_cannot_inject_a_positive_atr_effect(self):
        """The grid is indexed by |rho|. Passing a signed value must not flip
        the direction under test, because a positive ATR effect is the kill
        trigger and not a point on this grid."""
        self.assertLess(opchar.inject_effects(self.BURNT, rho=0.15, sd_y=self.SD_Y)["atr"], 0.0)
        self.assertLess(opchar.inject_effects(self.BURNT, rho=-0.15, sd_y=self.SD_Y)["atr"], 0.0)

    def test_the_nuisance_signals_are_halved_and_rescaled(self):
        injected = opchar.inject_effects(self.BURNT, rho=0.10, sd_y=self.SD_Y)
        for name in ("ma50", "press"):
            with self.subTest(name=name):
                self.assertAlmostEqual(self.BURNT[name] * 0.50 * self.SD_Y, injected[name])

    def test_the_atr_burnt_effect_is_not_used_for_the_atr_arm(self):
        """A grid point of zero means zero, not "the burnt effect, halved"."""
        injected = opchar.inject_effects(self.BURNT, rho=0.0, sd_y=self.SD_Y)
        self.assertEqual(0.0, injected["atr"])

    def test_the_key_order_matches_the_signal_order_it_was_given(self):
        """KEY ORDER IS THE EFFECT-TO-COLUMN MAPPING, not a cosmetic detail.

        ``a20_power.simulate_panel`` does ``names = list(effects)`` and then
        ``effects[name] * x[:, i]`` by position, so the dict's insertion order
        aligns effects against the simulated design's columns. A mapping built in
        any other order silently puts one signal's effect on another signal's
        column. Measured 2026-11-06: building it as (ma50, press, atr) put the
        MA50 effect on the ATR column and reported 84.8% promotion under a TRUE
        ZERO, where the correct figure is about 3%.
        """
        for order in (("atr", "ma50", "press"), ("ma50", "atr", "press")):
            with self.subTest(order=order):
                injected = opchar.inject_effects(self.BURNT, rho=0.10, sd_y=self.SD_Y, order=order)
                self.assertEqual(list(order), list(injected))

    def test_it_refuses_an_order_that_does_not_cover_the_burnt_effects(self):
        """A short order would drop a signal from the simulation and shift every
        later column by one, which is the same defect in a quieter form."""
        with self.assertRaises(ValueError):
            opchar.inject_effects(self.BURNT, rho=0.10, sd_y=self.SD_Y, order=("atr", "ma50"))

    def test_it_refuses_an_order_naming_a_signal_with_no_burnt_effect(self):
        with self.assertRaises(ValueError):
            opchar.inject_effects(
                self.BURNT, rho=0.10, sd_y=self.SD_Y, order=("atr", "ma50", "press", "jev")
            )

    def test_the_default_order_is_the_power_scripts_signal_order(self):
        """The runtime path passes ``tuple(power.SIGNALS)``; the module literal
        is the fallback for pure-helper calls. If the power script ever reorders
        its signals, the literal becomes a trap, so it is pinned here."""
        power = _load("a20_power", "_a20_power_for_order_test")
        self.assertEqual(tuple(power.SIGNALS), opchar.ATR_FIRST_ORDER)
        self.assertEqual("atr", opchar.ATR_KEY)
        self.assertIn(opchar.ATR_KEY, power.SIGNALS)

    def test_the_shrinkage_agrees_with_the_power_gate_own_function(self):
        """The gate shrinks with ``a20_power.shrink``. This probe multiplies
        inline so the helper stays pure and testable, which is only safe while
        the two agree — including on the sign, which ``shrink`` exists to keep."""
        power = _load("a20_power", "_a20_power_for_opchar_test")
        injected = opchar.inject_effects(self.BURNT, rho=0.10, sd_y=self.SD_Y)
        for name in ("ma50", "press"):
            with self.subTest(name=name):
                self.assertAlmostEqual(
                    power.shrink(self.BURNT[name], opchar.NUISANCE_SHRINKAGE) * self.SD_Y,
                    injected[name],
                )


class TestBlindness(unittest.TestCase):
    """The probe grades simulated outcomes on real cluster structure. A frame
    carrying a stored label value means the structure read leaked an outcome."""

    def _structure(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "brief_date": ["2026-08-01", "2026-08-01"],
                "ticker": ["AAA", "BBB"],
                "anchor_session": ["2026-08-03", "2026-08-03"],
                "briefed_any_theme": [True, True],
                "sel_label_status_20": ["ok", "ok"],
            }
        )

    def test_a_structure_only_frame_passes(self):
        opchar.assert_no_stored_outcome(self._structure())

    def test_every_value_key_the_stamper_writes_is_refused(self):
        from alphalens_pipeline.feedback.selection_label import VALUE_KEYS

        self.assertTrue(VALUE_KEYS, "the stamper writes no value columns; the screen is vacuous")
        for key in VALUE_KEYS:
            with self.subTest(key=key):
                frame = self._structure().assign(**{key: [0.1, 0.2]})
                with self.assertRaises(opchar.BlindnessError):
                    opchar.assert_no_stored_outcome(frame)

    def test_a_legacy_event_window_label_is_refused(self):
        for legacy in ("car_10", "car_20"):
            with self.subTest(legacy=legacy):
                frame = self._structure().assign(**{legacy: [0.1, 0.2]})
                with self.assertRaises(opchar.BlindnessError):
                    opchar.assert_no_stored_outcome(frame)

    def test_the_refusal_names_the_offending_column(self):
        frame = self._structure().assign(sel_zar_20=[0.1, 0.2])
        with self.assertRaises(opchar.BlindnessError) as caught:
            opchar.assert_no_stored_outcome(frame)
        self.assertIn("sel_zar_20", str(caught.exception))

    def test_the_simulated_panel_the_probe_grades_carries_no_stored_label(self):
        """The frame handed to ``atr_slope`` holds a SIMULATED outcome under the
        registered name. That is the one column the screen must tolerate,
        because refusing it would refuse the probe's own work."""
        frame = opchar.simulated_frame(
            outcome=np.array([0.01, -0.02]),
            design=np.array([[1.0, 0.5, -0.3, 2.0], [1.0, -0.5, 0.3, 1.0]]),
            clusters=np.array(["2026-08-03", "2026-08-03"]),
            outcome_name=atr.OUTCOME,
            signal_columns=[atr.ATR, *atr.COVARIATES],
        )
        self.assertEqual(2, len(frame))
        self.assertIn(atr.OUTCOME, frame.columns)
        self.assertIn("arrival", frame.columns)
        for column in (atr.ATR, *atr.COVARIATES):
            self.assertIn(column, frame.columns)

    def test_the_simulated_frame_drops_the_intercept_column(self):
        """``simulate_panel`` hands back a design whose first column is ones.
        Carrying it through as a signal would hand ``atr_slope`` a duplicated
        intercept and a rank-deficient design."""
        frame = opchar.simulated_frame(
            outcome=np.array([0.01, -0.02]),
            design=np.array([[1.0, 0.5, -0.3, 2.0], [1.0, -0.5, 0.3, 1.0]]),
            clusters=np.array(["2026-08-03", "2026-08-03"]),
            outcome_name=atr.OUTCOME,
            signal_columns=[atr.ATR, *atr.COVARIATES],
        )
        np.testing.assert_allclose([0.5, -0.5], frame[atr.ATR].to_numpy())


class TestTheStructureSource(unittest.TestCase):
    """Which panel the probe simulates on.

    This matters more than it looks. The figure this probe replaces was wrong
    because it had been computed on a panel counted in the wrong unit. The power
    instrument counts the held-out side from the LABEL store alone; the gate
    joins it to the brief store for the signal columns and lands on fewer
    episodes. Simulating on the first and publishing a characteristic of the
    second repeats the original defect in miniature.
    """

    def test_the_gate_structure_is_the_default(self):
        self.assertEqual("gate", opchar.DEFAULT_STRUCTURE)

    def test_both_sources_are_named(self):
        self.assertEqual(("gate", "power"), opchar.STRUCTURE_SOURCES)

    def test_an_unknown_source_is_refused_rather_than_defaulted(self):
        with self.assertRaises(ValueError) as caught:
            opchar.cluster_sizes_from(
                source="labels-only",
                power=None,
                conf=None,
                labels_dir=Path("/x"),
                briefs_dir=Path("/y"),
            )
        self.assertIn("labels-only", str(caught.exception))

    def test_the_gate_source_reads_the_confirmation_blind_path(self):
        """``_held_out_episodes(with_signals=False)`` is the preflight path and
        reads no outcome. Calling ``held_out_panel`` instead would BE the look,
        so the source is pinned by which function it calls."""
        seen = {}

        class FakeConf:
            @staticmethod
            def _held_out_episodes(labels_dir, briefs_dir, *, with_signals):
                seen["with_signals"] = with_signals
                return pd.DataFrame({"arrival": ["d1", "d1", "d2"]})

        sizes = opchar.cluster_sizes_from(
            source="gate",
            power=None,
            conf=FakeConf,
            labels_dir=Path("/x"),
            briefs_dir=Path("/y"),
        )
        self.assertIs(False, seen["with_signals"])
        self.assertEqual([2, 1], sorted(sizes, reverse=True))

    def test_the_gate_source_refuses_a_frame_carrying_an_outcome(self):
        class LeakyConf:
            @staticmethod
            def _held_out_episodes(labels_dir, briefs_dir, *, with_signals):
                return pd.DataFrame({"arrival": ["d1"], "sel_ar_20": [0.02]})

        with self.assertRaises(opchar.BlindnessError):
            opchar.cluster_sizes_from(
                source="gate",
                power=None,
                conf=LeakyConf,
                labels_dir=Path("/x"),
                briefs_dir=Path("/y"),
            )


class TestMonteCarloError(unittest.TestCase):
    """A simulated frequency without its error does not settle anything."""

    def test_the_error_shrinks_with_the_square_root_of_the_count(self):
        wide = opchar.monte_carlo_error(0.2, 100)
        tight = opchar.monte_carlo_error(0.2, 400)
        self.assertAlmostEqual(wide / 2.0, tight, places=12)

    def test_a_degenerate_frequency_has_no_error(self):
        self.assertEqual(0.0, opchar.monte_carlo_error(0.0, 400))
        self.assertEqual(0.0, opchar.monte_carlo_error(1.0, 400))

    def test_a_zero_count_does_not_divide_by_zero(self):
        self.assertTrue(np.isnan(opchar.monte_carlo_error(0.2, 0)))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the #1227 operating-characteristics probe.

Hermetic: every case feeds synthetic frames to the pure functions. The two
numbers the probe finally prints are single recorded runs, not regression
fixtures, and they are published in
``docs/research/atr_confirmation_prereg_2026_09.md`` section 10 with their seeds.

The load-bearing cases are :class:`TestTheArmsMatchTheRegistration` and
:class:`TestBlindness`. The first exists because this probe reports the
frequencies of the registration's own three arms, so its classifier has to BE
``decide()``'s mapping rather than resemble it — a probe that grades on a
near-copy of the rule publishes a table about a rule nobody registered. The
second exists because the probe's whole claim to not being a look is that it
reads cluster structure and simulates every outcome it grades.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import sys
import unittest
import warnings
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

    def test_the_simulated_panel_the_probe_grades_has_the_columns_the_fit_needs(self):
        """The frame handed to ``atr_slope`` carries the simulated outcome under
        the registered name, the arrival clusters, and the three signals.

        It deliberately does NOT assert anything about the blindness screen. The
        screen is pinned against `VALUE_KEYS`, and the registered outcome name is
        a member, so running it on this frame WOULD raise — see the test below,
        which states that rather than leaving it implied."""
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

    def test_the_screen_is_for_structure_reads_and_would_refuse_a_graded_panel(self):
        """The screen guards what comes OUT of the stores, not what the probe
        builds. Stating it as a test because the distinction is the whole reason
        the screen can be pinned against `VALUE_KEYS`: the probe's own frame
        carries `sel_ar_20`, so a screen applied there would refuse every panel
        the probe grades."""
        frame = opchar.simulated_frame(
            outcome=np.array([0.01, -0.02]),
            design=np.array([[1.0, 0.5, -0.3, 2.0], [1.0, -0.5, 0.3, 1.0]]),
            clusters=np.array(["2026-08-03", "2026-08-03"]),
            outcome_name=atr.OUTCOME,
            signal_columns=[atr.ATR, *atr.COVARIATES],
        )
        with self.assertRaises(opchar.BlindnessError):
            opchar.assert_no_stored_outcome(frame)

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

    def test_the_gate_source_needs_the_confirmation_module(self):
        """``conf`` carries a default of None while ``structure`` defaults to the
        gate, so the two defaults together used to reach an AttributeError on
        NoneType deep inside the reader. A named refusal says which argument is
        missing."""
        with self.assertRaises(ValueError) as caught:
            opchar.cluster_sizes_from(
                source="gate",
                power=None,
                conf=None,
                labels_dir=Path("/x"),
                briefs_dir=Path("/y"),
            )
        self.assertIn("conf", str(caught.exception))

    def test_the_power_source_needs_the_power_module(self):
        with self.assertRaises(ValueError) as caught:
            opchar.cluster_sizes_from(
                source="power",
                power=None,
                conf=None,
                labels_dir=Path("/x"),
                briefs_dir=Path("/y"),
            )
        self.assertIn("power", str(caught.exception))

    def test_the_power_source_counts_episodes_per_arrival_from_labels_alone(self):
        """Reachable from ``--structure power`` and otherwise untested. It must
        read through the allowlist and must NOT join the brief store."""
        seen = {}

        class FakePower:
            POPULATION_BRIEFED = "briefed"

            @staticmethod
            def load_held_out(labels_dir):
                seen["labels_dir"] = labels_dir
                return pd.DataFrame({"brief_date": ["2026-08-01"], "ticker": ["AAA"]})

            @staticmethod
            def held_out_episodes_by_arrival(frame, *, population):
                seen["population"] = population
                return {"d1": 3, "d2": 1}

        sizes = opchar.cluster_sizes_from(
            source="power",
            power=FakePower,
            conf=None,
            labels_dir=Path("/x"),
            briefs_dir=Path("/y"),
        )
        self.assertEqual([3, 1], sizes)
        self.assertEqual("briefed", seen["population"])
        self.assertEqual(Path("/x"), seen["labels_dir"])

    def test_the_power_source_refuses_a_frame_carrying_an_outcome(self):
        class LeakyPower:
            POPULATION_BRIEFED = "briefed"

            @staticmethod
            def load_held_out(labels_dir):
                return pd.DataFrame({"ticker": ["AAA"], "sel_car_mean_20": [0.01]})

            @staticmethod
            def held_out_episodes_by_arrival(frame, *, population):
                raise AssertionError("must refuse before counting")

        with self.assertRaises(opchar.BlindnessError):
            opchar.cluster_sizes_from(
                source="power",
                power=LeakyPower,
                conf=None,
                labels_dir=Path("/x"),
                briefs_dir=Path("/y"),
            )

    def test_the_two_modes_cannot_draw_the_same_panels(self):
        """The memo cross-checks the two modes against each other, which only
        means something if they draw different panels. At ARM_SEED = SLOPE_SEED
        + 1 they did not: the arms-mode rho=0 seed equalled the slopes-mode
        rho=0 grid seed, so 400 of the 3000 panels were the same draws and the
        'agreement' was a sample against its own prefix."""
        slopes = {
            opchar.SLOPE_SEED,
            *(opchar.SLOPE_SEED + 1 + i for i in range(len(opchar.SLOPE_GRID))),
        }
        arms = {opchar.ARM_SEED + round(rho * 1000) for rho in opchar.ARM_RHOS}
        self.assertEqual(set(), slopes & arms, "the two modes share a panel seed")

    def test_the_interval_bootstrap_seed_space_is_disjoint_from_the_panel_seeds(self):
        """A panel draw and the bootstrap resampling OF that panel must not start
        from one number."""
        panel_seeds = {opchar.ARM_SEED + round(rho * 1000) for rho in opchar.ARM_RHOS}
        ci_seeds = {opchar.ARM_SEED + opchar.CI_SEED_OFFSET + i for i in range(opchar.ARM_SIMS)}
        self.assertEqual(set(), panel_seeds & ci_seeds)

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


def _sessions(n: int, start: dt.date = dt.date(2026, 6, 1)) -> list[str]:
    from alphalens_pipeline.market.calendar import advance_trading_sessions

    return [advance_trading_sessions(start, i, "XNYS").isoformat() for i in range(n)]


class _Store:
    """Two directories of per-date parquets shaped like the real stores.

    Burnt dates (<= DISCOVERY_CUTOFF) carry the outcome the probe is allowed to
    read for scale and ICC; held-out dates supply cluster structure.
    """

    def __init__(self, root: Path):
        self.labels = root / "selection_labels"
        self.briefs = root / "thematic_briefs"
        self.labels.mkdir(parents=True)
        self.briefs.mkdir(parents=True)

    def write(self, brief_date: str, rows: list[tuple[str, float, float]], *, briefed: bool = True):
        pd.DataFrame(
            {
                "brief_date": brief_date,
                "ticker": [t for t, _, _ in rows],
                "anchor_session": brief_date,
                "briefed_any_theme": briefed,
                "sel_label_status_20": "ok",
                "sel_ar_20": [y for _, y, _ in rows],
            }
        ).to_parquet(self.labels / f"{brief_date}.parquet", index=False)
        pd.DataFrame(
            {
                "ticker": [t for t, _, _ in rows],
                "technical_atr_pct": [x for _, _, x in rows],
                "technical_ma50_distance_pct": np.linspace(-3.0, 3.0, len(rows)),
                "n_gates_passed": [i % 3 for i in range(len(rows))],
                "market_state": "low",
            }
        ).to_parquet(self.briefs / f"{brief_date}.parquet", index=False)


#: Resamples per panel for the end-to-end runs. These tests exist to cover the
#: runtime paths — the readers, the two reporters, the argument wiring — not to
#: estimate anything, and the registered CI_BOOT of 2999 per simulated panel put
#: this one module at 13 of the research suite's 23 minutes on CI (measured
#: 2026-11-07, runs 42318730964 and 42372930894).
_SMOKE_BOOT = 9


class TestTheProbeEndToEnd(unittest.TestCase):
    """Both modes against seeded stores. These cover the paths that only exist
    at runtime — the readers, the two reporters and the argument wiring — which
    no amount of pure-helper testing reaches."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = _Store(Path(self._tmp.name))
        rng = np.random.default_rng(5)
        # Burnt side: enough episodes to clear a20_power's _MIN_BURNT_EPISODES.
        for i, session in enumerate(_sessions(20, dt.date(2026, 5, 1))):
            self.store.write(
                session,
                [
                    (f"B{i:03d}X{j:03d}", float(rng.normal(scale=0.17)), float(rng.normal() + 5.0))
                    for j in range(10)
                ],
            )
        # Held-out side, sized so the one-sided boundary still sits closer to
        # zero than DELTA. A SMALL panel makes the probe refuse the grid, which
        # is correct behaviour and is pinned by its own test below; the happy
        # path needs a panel of roughly the real one's shape to exercise.
        for i, session in enumerate(_sessions(60, dt.date(2026, 7, 6))):
            self.store.write(
                session,
                [
                    (f"H{i:03d}X{j:03d}", float(rng.normal(scale=0.17)), float(rng.normal() + 5.0))
                    for j in range(7)
                ],
            )

    def _run(self, *argv: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = opchar.main(
                [
                    *argv,
                    "--labels-dir",
                    str(self.store.labels),
                    "--briefs-dir",
                    str(self.store.briefs),
                ]
            )
        self.assertEqual(0, code)
        return buffer.getvalue()

    def test_the_slopes_mode_reports_a_standard_error_and_the_three_arms(self):
        printed = self._run("--mode", "slopes", "--sims", "8", "--seed", "11")
        self.assertIn("standard error of the registered slope", printed)
        self.assertIn("source: gate", printed)
        self.assertIn("confirmed", printed)
        self.assertIn("RETIRED", printed)
        for rho in opchar.SLOPE_GRID:
            self.assertIn(f"{rho:10.3f}", printed)

    def test_the_arms_mode_reports_the_evidence_against_arm(self):
        printed = self._run(
            "--mode", "arms", "--sims", "4", "--seed", "13", "--ci-boot", str(_SMOKE_BOOT)
        )
        self.assertIn("evidence-against arm fires", printed)
        self.assertIn("interval half-width", printed)
        self.assertIn("MEASURED:", printed)

    def test_the_arms_mode_prints_progress_on_a_run_long_enough_to_need_it(self):
        """The progress branch is skipped entirely below ARM_PROGRESS_EVERY
        panels, so a short run cannot exercise it — which is exactly the trap a
        12-panel determinism check fell into.

        The STRIDE is lowered rather than the panel count raised. Running the
        real stride of 50 panels cost one cluster bootstrap PER panel and PER
        entry of ARM_RHOS, which has two - 100 bootstraps at the registered 2999
        resamples, measured at 153 s of this module's 165 s. What the test
        exists to prove is that the branch executes and prints, which a stride
        of 2 proves identically.
        """
        self.addCleanup(setattr, opchar, "ARM_PROGRESS_EVERY", opchar.ARM_PROGRESS_EVERY)
        opchar.ARM_PROGRESS_EVERY = 2
        printed = self._run(
            "--mode", "arms", "--sims", "2", "--seed", "17", "--ci-boot", str(_SMOKE_BOOT)
        )
        self.assertIn("2/2 panels", printed)

    def test_a_reduced_bootstrap_says_so_in_the_report(self):
        """A figure computed at a shrunken bootstrap is not the registered
        interval. The report has to carry that in its own output, because the
        flag that shrank it lives in a shell line nobody keeps."""
        printed = self._run(
            "--mode", "arms", "--sims", "2", "--seed", "23", "--ci-boot", str(_SMOKE_BOOT)
        )
        self.assertIn(f"CI_BOOT {_SMOKE_BOOT}", printed)
        self.assertIn("SMOKE TEST, not a measurement", printed)
        self.assertIn("may not be published", printed)

    def _record_bootstrap_sizes(self) -> list[int]:
        """Record the `n_boot` every `slope_ci` call receives, and skip the work.

        `_load_sibling` re-executes the confirmation module on every call, so
        patching `slope_ci` after the fact does not survive into the run: the
        LOADER is the only seam. Returning a fixed interval rather than calling
        through is deliberate - it makes the two wiring tests cost nothing, and
        the real bootstrap still runs end-to-end in
        `test_the_arms_mode_reports_the_evidence_against_arm`.
        """
        seen: list[int] = []
        real_loader = opchar._load_sibling
        self.addCleanup(setattr, opchar, "_load_sibling", real_loader)

        def loader(stem, name):
            module = real_loader(stem, name)
            if stem == "2026_09_a20_atr_confirmation":
                default = module.CI_BOOT

                def spy(panel, *, n_boot=default, seed=module.SEED, level=module.CI_LEVEL):
                    seen.append(n_boot)
                    return -0.05, 0.05

                module.slope_ci = spy
            return module

        opchar._load_sibling = loader
        return seen

    def test_the_ci_boot_flag_reaches_the_bootstrap(self):
        """The one line this whole change rests on. Asserting on the PRINTED
        header cannot see it: the header echoes the parsed flag, so deleting
        `n_boot=ci_boot` from the `slope_ci` call leaves every string assertion
        green while the suite silently goes back to costing 29 s for this class.
        Measured by removing that keyword: 4.5 s -> 28.7 s, 0 failures.
        """
        seen = self._record_bootstrap_sizes()
        self._run("--mode", "arms", "--sims", "2", "--seed", "31", "--ci-boot", str(_SMOKE_BOOT))
        self.assertTrue(seen, "slope_ci was never called, so nothing was observed")
        self.assertEqual({_SMOKE_BOOT}, set(seen))

    def test_the_default_bootstrap_is_the_registered_one_and_is_not_labelled(self):
        """Absent the flag, every call must use the FROZEN constant, and the
        header must say positively that this is the registered interval."""
        conf = self.conf()
        seen = self._record_bootstrap_sizes()
        printed = self._run("--mode", "arms", "--sims", "2", "--seed", "29")
        self.assertEqual({conf.CI_BOOT}, set(seen))
        self.assertIn(f"CI_BOOT {conf.CI_BOOT} (the registered value)", printed)
        self.assertNotIn("SMOKE TEST", printed)
        self.assertNotIn("[SMOKE BOOT", printed)

    def test_a_bootstrap_above_the_registered_one_is_not_called_a_smoke_test(self):
        """A larger resample count is the one direction that buys precision.
        Equality alone would label it unpublishable, which is backwards."""
        conf = self.conf()
        seen = self._record_bootstrap_sizes()
        printed = self._run(
            "--mode",
            "arms",
            "--sims",
            "2",
            "--seed",
            "37",
            "--ci-boot",
            str(conf.CI_BOOT + 1),
        )
        self.assertEqual({conf.CI_BOOT + 1}, set(seen))
        self.assertIn("ABOVE the registered", printed)
        self.assertNotIn("SMOKE TEST", printed)
        self.assertIn(f"[CI_BOOT {conf.CI_BOOT + 1}]", printed)

    def test_one_panel_reports_no_spread_instead_of_nan(self):
        """`np.std(..., ddof=1)` on one observation is zero degrees of freedom:
        numpy warns and returns nan, which would print as a figure."""
        seen = self._record_bootstrap_sizes()
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            printed = self._run("--mode", "arms", "--sims", "1", "--seed", "41")
        self.assertEqual(1, len(set(seen)))
        self.assertIn("sd of the slope: not defined on one panel", printed)
        self.assertNotIn("nan", printed)

    def test_a_bootstrap_below_one_is_refused_before_the_structure_read(self):
        """`slope_ci(n_boot=0)` dies on `np.percentile([], ...)` inside the
        FROZEN confirmation script, an IndexError naming neither the flag nor
        the file, and only after the structure read has cost minutes."""
        for value in ("0", "-5"):
            with self.subTest(ci_boot=value):
                with self.assertRaises(SystemExit):
                    with contextlib.redirect_stderr(io.StringIO()):
                        opchar.main(["--mode", "arms", "--ci-boot", value])

    def test_the_flag_is_refused_in_the_mode_that_ignores_it(self):
        """`--mode slopes` computes no interval, so the flag would be accepted
        and silently do nothing after a 15-minute wait."""
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                opchar.main(["--mode", "slopes", "--ci-boot", "9"])

    def test_the_power_structure_is_reachable_from_the_command_line(self):
        printed = self._run(
            "--mode", "slopes", "--sims", "4", "--seed", "19", "--structure", "power"
        )
        self.assertIn("source: power", printed)

    def test_the_two_structures_disagree_when_a_label_row_has_no_brief_row(self):
        """The 19-row gap on the real store, in miniature: a label row whose
        ticker the brief parquet does not carry counts for `power` and not for
        `gate`."""
        frame = pd.read_parquet(self.store.labels / "2026-07-06.parquet")
        frame.loc[0, "ticker"] = "GHOST"
        frame.to_parquet(self.store.labels / "2026-07-06.parquet", index=False)
        gate = opchar.cluster_sizes_from(
            source="gate",
            power=self.power(),
            conf=self.conf(),
            labels_dir=self.store.labels,
            briefs_dir=self.store.briefs,
        )
        power = opchar.cluster_sizes_from(
            source="power",
            power=self.power(),
            conf=self.conf(),
            labels_dir=self.store.labels,
            briefs_dir=self.store.briefs,
        )
        self.assertEqual(sum(power) - 1, sum(gate))

    def conf(self):
        return _load("2026_09_a20_atr_confirmation", "_a20_conf_e2e")

    def power(self):
        return _load("a20_power", "_a20_power_e2e")

    def test_an_explicit_seed_of_zero_is_honoured_rather_than_replaced(self):
        """`args.seed or SLOPE_SEED` treated 0 as absent, which would file a run
        under a seed it did not use."""
        printed = self._run("--mode", "slopes", "--sims", "4", "--seed", "0")
        self.assertIn("seed 0)", printed)
        self.assertNotIn(f"seed {opchar.SLOPE_SEED})", printed)

    def test_the_grid_is_refused_when_the_floor_stops_binding(self):
        """A larger standard error pushes the one-sided boundary past DELTA, and
        then the slope-only promote arm is no longer the registered rule. The
        probe must refuse the table, not print it beside a 'NO'."""
        real = opchar.ONE_SIDED_95_Z
        opchar.ONE_SIDED_95_Z = 1000.0  # forces the boundary far past -DELTA
        self.addCleanup(setattr, opchar, "ONE_SIDED_95_Z", real)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(opchar.FloorDoesNotBindError):
                opchar.main(
                    [
                        "--mode",
                        "slopes",
                        "--sims",
                        "4",
                        "--labels-dir",
                        str(self.store.labels),
                        "--briefs-dir",
                        str(self.store.briefs),
                    ]
                )
        printed = buffer.getvalue()
        self.assertIn("the FLOOR binds", printed)
        self.assertNotIn("confirmed", printed)


if __name__ == "__main__":
    unittest.main()

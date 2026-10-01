"""Contracts of the burnt-panel discovery pass for the System One features.

The script reads BURNT outcome values, so it is a look even though it charges 0, and
what has to be pinned is everything that could make the look mean something other
than what its ledger row says:

* the candidate list, frozen before the run, so a later addition is a visible diff
  and not a quiet extra chance;
* the instrument it reads, so it cannot silently pool two feature versions;
* the anchor, which is READ and never recomputed;
* `catalyst_mass`, which must track the live noise vocabulary and must not turn an
  unreadable distribution into a confident zero.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "2026_10_jev_discovery_burnt.py"


def _load():
    spec = importlib.util.spec_from_file_location("_jev_discovery", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


disc = _load()


class TestTheFrozenSpecification(unittest.TestCase):
    """Freezing the list before the run is the whole difference between a discovery
    pass and a fishing trip. These tests make a change to it visible in review."""

    EXPECTED = (
        "jev_materiality",
        "jev_catalyst_mass",
        "jev_concrete_fact",
        "jev_company_gain",
        "jev_type_confidence",
        "jev_touches_industry",
    )

    def test_the_six_candidates_are_the_ones_the_ledger_row_names(self):
        self.assertEqual(tuple(n for _, n in disc.CANDIDATES), self.EXPECTED)

    def test_the_reference_line_matches_the_candidate_count(self):
        # Six candidates reported against a plain 0.05 would be six chances at it.
        self.assertAlmostEqual(disc.REFERENCE_BAR, 0.05 / len(disc.CANDIDATES))

    def test_the_controls_are_the_two_already_known_to_be_real(self):
        # Keeping them as CONTROLS is what makes a candidate's coefficient an
        # increment over known structure rather than a restatement of it.
        self.assertEqual(tuple(n for _, n in disc.CONTROLS), ("atr", "ma50_dist"))

    def test_the_cutoff_is_the_burnt_window(self):
        self.assertEqual(disc.BURNT_CUTOFF, "2026-07-05")

    def test_the_instrument_is_pinned_to_one_feature_version(self):
        # Reading two versions would average two different instruments.
        self.assertRegex(disc.FEATURE_VERSION, r"^jev-v\d+-[0-9a-f]{12}$")

    def test_the_actionable_floor_is_the_one_1227_froze(self):
        self.assertAlmostEqual(disc.ACTIONABLE_DELTA, 0.10)


class TestTheAnchorIsReadAndNeverRecomputed(unittest.TestCase):
    """Owner decision D4 fixes the anchor at the first session AFTER the brief date.
    `session_on_or_after` returns the brief date itself when that date is a session,
    and the two disagreed on 296 of 466 burnt rows. A test that only checked for an
    `arrival` column would pass either way, so this reads the source."""

    @staticmethod
    def _called_names(source: str) -> set[str]:
        called = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        return called

    def test_the_module_never_calls_session_on_or_after(self):
        self.assertNotIn("session_on_or_after", self._called_names(_SCRIPT.read_text()))

    def test_the_gate_reads_calls_and_not_prose(self):
        # Positive control both ways: a substring check would fire on the comment
        # that explains the rule, and would miss a call written as an attribute.
        self.assertIn("session_on_or_after", _SCRIPT.read_text())
        self.assertIn("f", self._called_names("f(1)"))
        self.assertIn("g", self._called_names("mod.g(1)"))
        self.assertNotIn("h", self._called_names('"""h is forbidden"""'))

    def test_the_module_does_read_the_stored_anchor(self):
        # A file that dropped the anchor entirely would satisfy the gate above.
        self.assertIn("anchor_session", _SCRIPT.read_text())


class TestCatalystMass(unittest.TestCase):
    def test_all_noise_is_zero_and_all_catalyst_is_one(self):
        self.assertAlmostEqual(
            disc.catalyst_mass(json.dumps({"opinion": 0.7, "listicle": 0.3})), 0.0
        )
        self.assertAlmostEqual(
            disc.catalyst_mass(json.dumps({"m_and_a": 0.9, "earnings": 0.1})), 1.0
        )

    def test_a_split_distribution_is_the_non_noise_share(self):
        self.assertAlmostEqual(
            disc.catalyst_mass(json.dumps({"opinion": 0.4, "earnings": 0.6})), 0.6
        )

    def test_an_unreadable_distribution_is_None_and_not_a_confident_zero(self):
        # A row we could not read is not a row whose catalyst mass is zero. Imputing
        # one would put a confident value where there is no measurement, and the fit
        # would treat it as evidence.
        for bad in (None, "", "not json", "[1,2]", json.dumps("a string"), 7):
            self.assertIsNone(disc.catalyst_mass(bad), repr(bad))

    def test_it_tracks_the_LIVE_noise_vocabulary(self):
        # A hand-copied noise list would drift from the pipeline's, and a drifted copy
        # does not fail: the mass is just computed over the wrong set.
        from alphalens_pipeline.thematic.screening.catalyst_signals import NOISE_EVENT_TYPES

        for noise_type in NOISE_EVENT_TYPES:
            self.assertAlmostEqual(
                disc.catalyst_mass(json.dumps({noise_type: 1.0})), 0.0, msg=noise_type
            )

    def test_a_non_noise_type_the_pipeline_adds_later_counts_as_catalyst(self):
        self.assertAlmostEqual(disc.catalyst_mass(json.dumps({"some_future_type": 1.0})), 1.0)

    def test_the_result_is_clamped_to_the_unit_interval(self):
        # Probabilities that do not sum to one must not produce a negative mass.
        self.assertAlmostEqual(disc.catalyst_mass(json.dumps({"opinion": 1.4})), 0.0)


class TestTheLoadersRefuseAnEmptyStore(unittest.TestCase):
    """Running this before the builder must say so, not return an empty panel that
    reads as 'the features carry nothing'."""

    def test_the_article_loader_names_the_builder_to_run(self):
        with mock.patch.object(disc.glob, "glob", return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                disc.load_jev_article()
        self.assertIn("2026_10_jev_feature_layer.py", str(caught.exception))

    def test_the_candidate_loader_names_the_builder_to_run(self):
        with mock.patch.object(disc.glob, "glob", return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                disc.load_jev_candidate()
        self.assertIn("2026_10_jev_feature_layer.py", str(caught.exception))


class TestStandardise(unittest.TestCase):
    def test_a_standardised_column_has_zero_mean_and_unit_sd(self):
        frame = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0]})
        out = disc.standardise(frame, ["x"])
        self.assertAlmostEqual(out["x"].mean(), 0.0)
        self.assertAlmostEqual(out["x"].std(ddof=0), 1.0)

    def test_a_constant_column_becomes_nan_rather_than_dividing_by_zero(self):
        # A feature with no variance cannot have a coefficient; NaN drops the rows
        # from the fit instead of producing an infinity.
        out = disc.standardise(pd.DataFrame({"x": [2.0, 2.0, 2.0]}), ["x"])
        self.assertTrue(out["x"].isna().all())


class TestTheMinimumDetectableEffect(unittest.TestCase):
    """The deliverable. A coefficient without it cannot distinguish 'no effect' from
    'no power', and on 2026-10-01 a null of mine was the latter."""

    def test_the_z_multiplier_is_two_sided_five_percent_at_eighty_percent_power(self):
        self.assertAlmostEqual(disc._MDE_Z, 1.959964 + 0.841621, places=5)

    def test_the_multiplier_is_the_one_that_reproduces_the_published_bound(self):
        # Reproduces the 2026-10-01 self-grade row: se 0.0102, sd(label) 0.1713 gave
        # an MDE of 0.167 standardised. If the multiplier ever changes, that published
        # number stops being reproducible from this code.
        self.assertAlmostEqual(disc._MDE_Z * 0.0102 / 0.1713, 0.167, places=3)


if __name__ == "__main__":
    unittest.main()

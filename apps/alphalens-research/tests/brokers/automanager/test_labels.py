"""Hermetic tests for the execution-side human-label helpers.

The operator must see E1/E2/E3 for entry tiers and TP1/TP2/TP3 for take-profit
tranches — never a raw crid ("...-entry-t0"), a 0-based "tier 0", or a lowercase
"tp1". Every helper renders ONLY message text; it must never crash on a
malformed / foreign input, always returning some sensible string (FALLBACK).
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager import labels


class TestHumanEntryLabel(unittest.TestCase):
    def test_zero_based_index_renders_one_based_e_label(self) -> None:
        self.assertEqual(labels.human_entry_label(0), "E1")
        self.assertEqual(labels.human_entry_label(1), "E2")
        self.assertEqual(labels.human_entry_label(2), "E3")


class TestHumanTpLabel(unittest.TestCase):
    def test_zero_based_index_renders_one_based_tp_label(self) -> None:
        self.assertEqual(labels.human_tp_label(0), "TP1")
        self.assertEqual(labels.human_tp_label(1), "TP2")
        self.assertEqual(labels.human_tp_label(2), "TP3")


class TestEntryTierIndexFromCrid(unittest.TestCase):
    def test_parses_trailing_tier_index(self) -> None:
        self.assertEqual(labels.entry_tier_index_from_crid("OLN-2026-08-18-entry-t0"), 0)
        self.assertEqual(labels.entry_tier_index_from_crid("OLN-2026-08-18-entry-t2"), 2)

    def test_tolerates_fire_suffix(self) -> None:
        self.assertEqual(labels.entry_tier_index_from_crid("OLN-2026-08-18-entry-t1-fire"), 1)

    def test_returns_none_for_unparseable(self) -> None:
        self.assertIsNone(labels.entry_tier_index_from_crid("not-a-crid"))


class TestEntryLabelFromCrid(unittest.TestCase):
    def test_happy_path(self) -> None:
        self.assertEqual(labels.entry_label_from_crid("OLN-2026-08-18-entry-t0"), "E1")
        self.assertEqual(labels.entry_label_from_crid("MP-2026-07-14-entry-t2"), "E3")

    def test_fire_suffix_tolerated(self) -> None:
        self.assertEqual(labels.entry_label_from_crid("OLN-2026-08-18-entry-t0-fire"), "E1")

    def test_fallback_to_raw_crid_when_unparseable(self) -> None:
        # A malformed / foreign crid still prints something, never crashes.
        self.assertEqual(labels.entry_label_from_crid("garbage-ref"), "garbage-ref")
        self.assertEqual(labels.entry_label_from_crid(""), "")


class TestTpLabelFromTag(unittest.TestCase):
    def test_happy_path_is_one_based_already(self) -> None:
        self.assertEqual(labels.tp_label_from_tag("tp1"), "TP1")
        self.assertEqual(labels.tp_label_from_tag("tp3"), "TP3")

    def test_fallback_uppercases_non_tp_tag(self) -> None:
        self.assertEqual(labels.tp_label_from_tag("sl"), "SL")
        self.assertEqual(labels.tp_label_from_tag("runner"), "RUNNER")


class TestHumanLabelFromExternalReference(unittest.TestCase):
    def test_entry_ref_includes_ticker_prefix(self) -> None:
        self.assertEqual(
            labels.human_label_from_external_reference("OLN-2026-08-18-entry-t0"),
            "OLN E1",
        )

    def test_entry_fire_ref_flags_fire(self) -> None:
        self.assertEqual(
            labels.human_label_from_external_reference("OLN-2026-08-18-entry-t0-fire"),
            "OLN E1 (fire)",
        )

    def test_tp_ref_includes_ticker_prefix(self) -> None:
        self.assertEqual(
            labels.human_label_from_external_reference("OLN-2026-08-18-tp2-sell"),
            "OLN TP2",
        )

    def test_fallback_to_raw_ref_when_foreign(self) -> None:
        self.assertEqual(
            labels.human_label_from_external_reference("some-foreign-ref"),
            "some-foreign-ref",
        )
        self.assertEqual(labels.human_label_from_external_reference(""), "")


if __name__ == "__main__":
    unittest.main()

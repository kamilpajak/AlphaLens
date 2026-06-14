"""The O'Neil Expert Protocol adapter + registry + display-only invariants.

O'Neil is numeric-only: ``assess_qualitative`` is ``None`` and it is NOT a
``QualEnrichExpert``. It is registered, and none of its columns may enter the brief
sort (display-only until O'Neil×EDGE is validated).
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.experts.base import Expert, QualEnrichExpert
from alphalens_pipeline.experts.oneil.comparison import ONeilPanel
from alphalens_pipeline.experts.oneil.expert import ONeilExpert
from alphalens_pipeline.experts.oneil.quant_enrichment import ONEIL_COLUMNS
from alphalens_pipeline.experts.registry import all_experts, expert_ids, get_expert

ASOF = dt.date(2026, 5, 1)


class TestProtocolShape(unittest.TestCase):
    def test_satisfies_expert_protocol(self):
        expert = ONeilExpert()
        self.assertIsInstance(expert, Expert)
        self.assertEqual(expert.id, "oneil")
        self.assertTrue(expert.name)
        self.assertEqual(expert.column_names, ONEIL_COLUMNS)

    def test_numeric_only_not_qual_enrich(self):
        expert = ONeilExpert()
        self.assertIsNone(expert.assess_qualitative(None, ASOF, "AAA"))
        self.assertNotIsInstance(expert, QualEnrichExpert)
        self.assertFalse(hasattr(expert, "enrich_brief_frame"))
        self.assertFalse(hasattr(expert, "migrate_qual_cache"))

    def test_compute_score_on_full_panel(self):
        # compute_score scores whatever panel dict it is given (the frame path
        # supplies the full panel). A full, N-present panel scores a real number.
        from dataclasses import asdict

        panel = ONeilPanel(
            ticker="AAA",
            theme="t",
            pct_off_52w_high=0.0,
            ma200_slope_pct_per_day=0.10,
            ma200_distance_pct=5.0,
            earnings_growth_yoy_pct=50.0,
            earnings_growth_near_zero_base=False,
            new_high_split_suspected=False,
            data_coverage=1.0,
        )
        self.assertAlmostEqual(ONeilExpert().compute_score(asdict(panel)), 100.0)

    def test_compute_score_none_panel(self):
        self.assertIsNone(ONeilExpert().compute_score(None))


class TestRegistry(unittest.TestCase):
    def test_oneil_registered(self):
        self.assertIn("oneil", expert_ids())
        self.assertIsInstance(get_expert("oneil"), ONeilExpert)

    def test_both_experts_present(self):
        ids = {e.id for e in all_experts()}
        self.assertEqual({"buffett", "oneil"}, ids)


class TestRsColumnPresent(unittest.TestCase):
    def test_rs_column_present(self):
        # R (relative strength) re-activated: oneil_rs_approx_pct is the 9th column.
        self.assertIn("oneil_rs_approx_pct", ONEIL_COLUMNS)
        self.assertEqual(len(ONEIL_COLUMNS), 9)


class TestDisplayOnly(unittest.TestCase):
    def test_no_oneil_column_in_brief_sort_chain(self):
        # Display-only: no oneil_* column may enter the production brief sort chain
        # (the PR-6 allowlist test guards the mechanism; here we pin the real
        # invariant against the actual _BRIEF_SORT_KEYS constant).
        from alphalens_pipeline.thematic.argumentation import orchestrator

        sort_keys = {col for col, _asc, _default in orchestrator._BRIEF_SORT_KEYS}
        offenders = [c for c in ONEIL_COLUMNS if c in sort_keys]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

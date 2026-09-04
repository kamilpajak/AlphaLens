"""Anti-rot guard: short-interest telemetry must NEVER feed selection.

The si_* columns (#1269) are stamped-forward telemetry only — no selection,
no ordering, no display verdict until a pre-registered first look at N>=30
(~2026-11/12). Mirrors test_no_market_state_in_selection: a negative scan of
the selection-computing modules for the tokens, a positive control so the
scan cannot silently rot to always-pass, a live check that the composite
rejects a short-interest input, and the brief sort-key exclusion.
"""

import re
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.screening import scorer as scorer_mod
from alphalens_pipeline.thematic.screening import selection_score as selection_mod

# \b before si_ so words like "quasi_" cannot false-positive the scan.
_SHORT_INTEREST = re.compile(r"short_interest|\bsi_")


def _source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


class TestShortInterestNotInSelection(unittest.TestCase):
    def test_scorer_module_never_references_short_interest(self):
        # scorer.py computes layer4_weighted_score + selection_score.
        self.assertNotRegex(_source(scorer_mod), _SHORT_INTEREST)

    def test_selection_score_module_never_references_short_interest(self):
        self.assertNotRegex(_source(selection_mod), _SHORT_INTEREST)

    def test_positive_control_regex_would_catch_a_leak(self):
        # If the scan rotted to always-pass, these planted samples would slip by.
        planted_col = 'weighted = compose_weighted_score(x=row["si_days_to_cover"])'
        planted_name = "signal = short_interest_pct"
        self.assertRegex(planted_col, _SHORT_INTEREST)
        self.assertRegex(planted_name, _SHORT_INTEREST)

    def test_compose_weighted_score_rejects_a_short_interest_input(self):
        # The composite takes a fixed kwarg set; an si input must not be
        # silently accepted (mirrors the market_state positive control).
        with self.assertRaises(TypeError):
            scorer_mod.compose_weighted_score(
                fcff_positive=False,
                magic_formula_top_quartile=False,
                deep_drawdown_reversal=False,
                technicals_positive=False,
                catalyst_strength=0.0,
                si_days_to_cover=2.0,
            )

    def test_short_interest_is_not_a_brief_sort_key(self):
        from alphalens_pipeline.thematic.argumentation.orchestrator import (
            _BRIEF_SORT_KEYS,
        )

        offenders = [key for key, *_ in _BRIEF_SORT_KEYS if _SHORT_INTEREST.search(key)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

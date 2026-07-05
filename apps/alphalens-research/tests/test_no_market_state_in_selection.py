"""Anti-rot guard: the index-level market_state signal must NEVER feed selection.

market_state (PR-1) is a display-only context label (memo §3.2). It must not
become an input to ``layer4_weighted_score`` / ``selection_score``, nor a brief
sort key. This mirrors the ``test_no_raw_<vendor>_http`` anti-rot pattern: a
negative scan of the selection-computing modules for the token, plus a positive
control so the scan cannot silently rot to always-pass, plus a live check that
the composite rejects a market_state input and the sort chain excludes it.
"""

import re
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.screening import scorer as scorer_mod
from alphalens_pipeline.thematic.screening import selection_score as selection_mod

_MARKET_STATE = re.compile(r"market_state")


def _source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


class TestMarketStateNotInSelection(unittest.TestCase):
    def test_scorer_module_never_references_market_state(self):
        # scorer.py computes layer4_weighted_score + selection_score.
        self.assertNotRegex(_source(scorer_mod), _MARKET_STATE)

    def test_selection_score_module_never_references_market_state(self):
        self.assertNotRegex(_source(selection_mod), _MARKET_STATE)

    def test_positive_control_regex_would_catch_a_leak(self):
        # If the scan rotted to always-pass, this planted sample would slip by.
        planted = 'weighted = compose_weighted_score(x=row["market_state_dist200"])'
        self.assertRegex(planted, _MARKET_STATE)

    def test_compose_weighted_score_rejects_a_market_state_input(self):
        # The composite takes a fixed kwarg set; a market_state input must not be
        # silently accepted (mirrors the insider-not-a-component positive control).
        with self.assertRaises(TypeError):
            scorer_mod.compose_weighted_score(
                fcff_positive=False,
                magic_formula_top_quartile=False,
                deep_drawdown_reversal=False,
                technicals_positive=False,
                catalyst_strength=0.0,
                market_state="bull_quiet",
            )

    def test_market_state_is_not_a_brief_sort_key(self):
        from alphalens_pipeline.thematic.argumentation.orchestrator import (
            _BRIEF_SORT_KEYS,
        )

        offenders = [key for key, *_ in _BRIEF_SORT_KEYS if "market_state" in key]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

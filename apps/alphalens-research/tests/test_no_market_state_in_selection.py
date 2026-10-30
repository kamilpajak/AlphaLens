"""Anti-rot guard: the index-level market context must NEVER feed selection.

Covers BOTH index-level families, because both are display-only and neither is
named by the other's prefix:

* ``market_state*`` (PR-1) — the 4-state regime label + its drivers (memo §3.2);
* ``market_ret5_*`` — the three raw broad-index 5-session returns.

Neither may become an input to ``layer4_weighted_score`` / ``selection_score``,
nor a brief sort key. This mirrors the ``test_no_raw_<vendor>_http`` anti-rot
pattern: a negative scan of the selection-computing modules for the tokens, plus
one positive control PER FORM so the scan cannot silently rot to always-pass (a
regex matching only ``market_state`` passes happily over a ``market_ret5_qqq``
leak), plus a live check that the composite rejects such an input and the sort
chain excludes it.
"""

import re
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.screening import scorer as scorer_mod
from alphalens_pipeline.thematic.screening import selection_score as selection_mod

# Both prefixes in one pattern. Written as an alternation rather than a bare
# ``market_`` so an unrelated future ``market_`` name (e.g. a genuine selection
# input) is not silently claimed by this guard.
_MARKET_CONTEXT = re.compile(r"market_state|market_ret5_")


def _source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


class TestMarketStateNotInSelection(unittest.TestCase):
    def test_scorer_module_never_references_market_context(self):
        # scorer.py computes layer4_weighted_score + selection_score.
        self.assertNotRegex(_source(scorer_mod), _MARKET_CONTEXT)

    def test_selection_score_module_never_references_market_context(self):
        self.assertNotRegex(_source(selection_mod), _MARKET_CONTEXT)

    def test_positive_control_regex_would_catch_a_market_state_leak(self):
        # If the scan rotted to always-pass, this planted sample would slip by.
        planted = 'weighted = compose_weighted_score(x=row["market_state_dist200"])'
        self.assertRegex(planted, _MARKET_CONTEXT)

    def test_positive_control_regex_would_catch_an_index_return_leak(self):
        # The second form needs its OWN control: the pre-#1826 pattern matched
        # only ``market_state`` and would have read this line as clean.
        planted = 'weighted = compose_weighted_score(x=row["market_ret5_qqq"])'
        self.assertRegex(planted, _MARKET_CONTEXT)

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

    def test_no_market_context_column_is_a_brief_sort_key(self):
        from alphalens_pipeline.market.market_state import (
            MARKET_SERIES_COLUMNS,
            MARKET_STATE_COLUMNS,
        )
        from alphalens_pipeline.thematic.argumentation.orchestrator import (
            _BRIEF_SORT_KEYS,
        )

        display_only = set(MARKET_STATE_COLUMNS) | set(MARKET_SERIES_COLUMNS)
        offenders = [key for key, *_ in _BRIEF_SORT_KEYS if key in display_only]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

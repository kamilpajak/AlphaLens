"""Arm selection + synthetic-brief construction for the bracket-cost read.

Every rule here is a clause of
``docs/research/mcap_bracket_cost_contract_2026_08_22.md``. The contract is
committed; these tests are what stops the script from quietly disagreeing with
it, which is the failure that cost the most on #1002 (the script did not
implement two clauses of its own contract).
"""

from __future__ import annotations

import json
import unittest

import pandas as pd
from scripts.replay_bracket_arms import (
    ARM_DISCARDED,
    ARM_KEPT,
    Attrition,
    select_arms,
    synthetic_brief_frame,
)


def _funnel(**overrides) -> pd.DataFrame:
    base = {
        "ticker": ["AAA", "BBB", "CCC", "DDD"],
        "theme": ["t1", "t1", "t2", "t2"],
        "bracket_verdict": ["too_big", "in_bracket", "too_small", "no_mcap"],
        "market_cap": [50e9, 3e9, 1e8, None],
    }
    base.update(overrides)
    return pd.DataFrame(base)


class TestSelectArms(unittest.TestCase):
    def test_keeps_only_the_two_contract_arms(self):
        out = select_arms(_funnel())

        self.assertEqual(sorted(out["ticker"]), ["AAA", "BBB"])
        self.assertEqual(
            out.set_index("ticker")["arm"].to_dict(),
            {"AAA": ARM_DISCARDED, "BBB": ARM_KEPT},
        )

    def test_unknown_verdict_never_enters_an_arm(self):
        """A whitelist, not a negation. #1002's latent bug was the negation."""
        f = _funnel(bracket_verdict=["too_big", "in_bracket", "wat", None])

        out = select_arms(f)

        self.assertEqual(sorted(out["ticker"]), ["AAA", "BBB"])

    def test_null_ticker_is_dropped(self):
        f = _funnel(ticker=["AAA", None, "CCC", "DDD"])

        out = select_arms(f)

        self.assertEqual(list(out["ticker"]), ["AAA"])

    def test_duplicate_asof_ticker_collapses_to_one_row(self):
        """The unit is (asof, ticker); six daily slots must not multiply it."""
        f = pd.DataFrame(
            {
                "ticker": ["AAA", "AAA"],
                "theme": ["t1", "t1"],
                "bracket_verdict": ["too_big", "too_big"],
                "market_cap": [50e9, 50e9],
            }
        )

        out = select_arms(f)

        self.assertEqual(len(out), 1)


class TestAttrition(unittest.TestCase):
    def test_counts_sum_to_the_input(self):
        a = Attrition(in_scope=515, no_structure=12, no_bars=3, terminal=100, ongoing=400)

        self.assertTrue(a.balanced())

    def test_unbalanced_is_reported_not_swallowed(self):
        a = Attrition(in_scope=515, no_structure=12, no_bars=3, terminal=100, ongoing=399)

        self.assertFalse(a.balanced())


class TestSyntheticBrief(unittest.TestCase):
    def test_marks_both_arms_verified(self):
        rows = select_arms(_funnel())
        setups = {"AAA": {"entries": [1]}, "BBB": {"entries": [2]}}

        out = synthetic_brief_frame(rows, setups)

        self.assertEqual(sorted(out["ticker"]), ["AAA", "BBB"])
        self.assertTrue(out["verified"].all())

    def test_trade_setup_is_written_as_json_text(self):
        rows = select_arms(_funnel())
        setups = {"AAA": {"entries": [1]}, "BBB": {"entries": [2]}}

        out = synthetic_brief_frame(rows, setups)

        raw = out.set_index("ticker").loc["AAA", "brief_trade_setup"]
        self.assertIsInstance(raw, str)
        self.assertEqual(json.loads(raw), {"entries": [1]})

    def test_row_without_a_setup_is_excluded_not_null_filled(self):
        rows = select_arms(_funnel())
        setups = {"AAA": {"entries": [1]}}

        out = synthetic_brief_frame(rows, setups)

        self.assertEqual(list(out["ticker"]), ["AAA"])

    def test_arm_travels_with_the_row(self):
        """The store is keyed on ticker; without this column the arms are lost."""
        rows = select_arms(_funnel())
        setups = {"AAA": {"entries": [1]}, "BBB": {"entries": [2]}}

        out = synthetic_brief_frame(rows, setups)

        self.assertEqual(
            out.set_index("ticker")["arm"].to_dict(),
            {"AAA": ARM_DISCARDED, "BBB": ARM_KEPT},
        )


if __name__ == "__main__":
    unittest.main()

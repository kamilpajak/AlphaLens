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

import numpy as np
import pandas as pd
from alphalens_pipeline.thematic.trade_setup.builder import build_trade_setup_from_frame
from alphalens_pipeline.thematic.trade_setup.model import TradeSetup
from scripts.replay_bracket_arms import (
    ARM_DISCARDED,
    ARM_KEPT,
    Attrition,
    is_plannable_setup,
    select_arms,
    synthetic_brief_frame,
)


def _real_ohlcv(bars: int = 300) -> pd.DataFrame:
    """A frame the real builder accepts — deterministic, not random per run."""
    rng = np.random.default_rng(20260822)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.02, bars))
    span = np.abs(rng.normal(0.0, 0.015, bars)) * close
    return pd.DataFrame(
        {
            "open": close - span / 3,
            "high": close + span,
            "low": close - span,
            "close": close,
            "volume": rng.integers(1_000_000, 5_000_000, bars).astype(float),
        },
        index=pd.date_range("2025-06-01", periods=bars, freq="B"),
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


class TestIsPlannableSetup(unittest.TestCase):
    """Guarded against the REAL payload shape, never against a guessed one.

    The first version of this script tested its plannability guard with a
    hand-written ``{"entries": [...]}`` dict. The real class emits ``status`` and
    ``entry_tiers``, so the guard rejected 413 of 413 rows and the run reported a
    clean, entirely fictional zero.
    """

    def test_accepts_a_setup_the_real_builder_produced(self):
        setup = build_trade_setup_from_frame(_real_ohlcv())

        self.assertTrue(is_plannable_setup(setup.to_dict()))

    def test_rejects_the_real_no_structure_payload(self):
        setup = TradeSetup.no_structure(asof_close=10.0, atr=0.0, order_ttl_days=7)

        self.assertFalse(is_plannable_setup(setup.to_dict()))

    def test_rejects_a_payload_with_no_entry_tiers(self):
        payload = build_trade_setup_from_frame(_real_ohlcv()).to_dict()
        payload["entry_tiers"] = []

        self.assertFalse(is_plannable_setup(payload))


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

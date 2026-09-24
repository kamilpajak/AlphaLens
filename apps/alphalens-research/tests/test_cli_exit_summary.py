"""The plain-text exit summary shared by `thematic intent` and `broker arm` (#1530).

A brief pick's exit used to be invisible before arming: the producer wrote it
into the JSON and the door's human output left it out. These tests pin what the
operator now reads, with the arm prices worked out by hand so the renderer is
not checked against itself.
"""

from __future__ import annotations

import unittest

from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    ModelPush,
    ReanchorOnFill,
    TrailingStop,
)

STOP = 54.0
LADDER = (
    EntryTierSpec(limit_price=60.0, alloc_pct=60.0),
    EntryTierSpec(limit_price=58.0, alloc_pct=40.0),
)
TRAIL = ExitGeometrySpec(reaction_plan=(TrailingStop(arm_trigger_r=0.5, trail_frac=0.6),))


def _describe(exit_spec, tiers=LADDER, stop=STOP) -> str:
    from alphalens_cli.exit_summary import describe_exit

    return "\n".join(describe_exit(exit_spec, entry_tiers=tiers, disaster_stop=stop))


class NoDeclaredManagement(unittest.TestCase):
    def test_no_exit_says_the_stop_never_moves(self) -> None:
        self.assertIn("exit: none — the stop stays at 54 unless you move it", _describe(None))

    def test_an_empty_plan_reads_the_same(self) -> None:
        self.assertEqual(_describe(ExitGeometrySpec()), _describe(None))


class ATrailingStop(unittest.TestCase):
    def test_it_names_the_trigger_and_the_kept_share(self) -> None:
        text = _describe(TRAIL)

        self.assertIn("trailing stop", text)
        self.assertIn("+0.5R", text)
        self.assertIn("keeps 60% of the gain", text)

    def test_arm_prices_for_the_first_tier_and_the_full_ladder(self) -> None:
        # E1 only: avg 60, R = 6, arm = 60 + 0.5 * 6 = 63.00.
        # Full ladder, share-weighted: avg = 100 / (60/60 + 40/58) = 59.1837,
        # R = 5.1837, arm = 59.1837 + 2.5918 = 61.78.
        text = _describe(TRAIL)

        self.assertIn("63.00 if only E1 fills", text)
        self.assertIn("61.78 if the full ladder fills", text)

    def test_a_single_tier_shows_one_price(self) -> None:
        text = _describe(TRAIL, tiers=(EntryTierSpec(limit_price=60.0, alloc_pct=100.0),))

        self.assertIn("63.00", text)
        self.assertNotIn("full ladder", text)

    def test_an_immediate_tier_gives_an_upper_bound(self) -> None:
        # The limit of an immediate tier is the cap, the worst acceptable fill.
        # A cheaper fill means a lower average and a lower arm price.
        tiers = (
            EntryTierSpec(limit_price=60.0, alloc_pct=50.0, entry_mode="immediate"),
            EntryTierSpec(limit_price=58.0, alloc_pct=50.0),
        )
        text = _describe(TRAIL, tiers=tiers)

        self.assertIn("at most 63.00 if only E1 fills", text)
        self.assertIn("at most", text.split("if only E1 fills")[1])

    def test_a_ladder_of_pullbacks_is_not_labelled_a_bound(self) -> None:
        self.assertNotIn("at most", _describe(TRAIL))


class OtherDeclarations(unittest.TestCase):
    def test_reanchor_on_fill(self) -> None:
        spec = ExitGeometrySpec(reaction_plan=(ReanchorOnFill(k_atr=1.5, atr=2.0),))

        self.assertIn("re-anchor on fill", _describe(spec))
        self.assertIn("1.5×ATR", _describe(spec))

    def test_the_reserved_model_tag(self) -> None:
        spec = ExitGeometrySpec(reaction_plan=(ModelPush(),))

        self.assertIn("the stop is not moved today", _describe(spec))

    def test_initial_levels_replace_the_ladder(self) -> None:
        spec = ExitGeometrySpec(initial_levels=InitialLevels(stop=55.0, tp=70.0))

        text = _describe(spec)
        self.assertIn("places stop 55 and TP 70 instead of the ladder", text)
        self.assertIn("exit: none", text)


if __name__ == "__main__":
    unittest.main()

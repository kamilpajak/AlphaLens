"""The fill model of the #1113 instrument: when a tier fills, and at what price.

Two things the issue names explicitly and this file pins:

* the fill PRICE is the measured trailing overshoot above the tier limit, never
  the bare limit -- and the constant is checked against the live prices recorded
  in ``tests/incident_1112_fixture.py``, never typed from the issue prose;
* a bar low does not imply a fill -- the slippage-adverse THROUGH model needs the
  bar to trade one tick past the limit.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.entry_trail_geometry import entry_fill_estimate
from alphalens_pipeline.feedback.ladder_replay import replay_ladder
from alphalens_research.diagnostics import fill_partition as fp

from tests.incident_1112_fixture import (
    SMG_ACTUAL_FILL,
    SMG_D_BPS,
    SMG_E1_LIMIT,
    SMG_TIERS,
    SMG_TOUCH_BID,
)

BPS = 1e4

# The issue prose's number. Its own quoted prices give 23.43, so this is here
# only so a test can assert the constant is NOT it.
ISSUE_PROSE_OVERSHOOT_BPS = 40.0


def _tiers(*specs: tuple[str, float, float]) -> tuple[fp.EntryTier, ...]:
    return tuple(fp.EntryTier(tier_id=t, limit=lim, alloc_pct=a) for t, lim, a in specs)


def _bar(ts_ms: int, low: float) -> dict:
    return {"t": ts_ms, "l": low, "h": low + 1.0, "c": low + 0.5, "o": low + 0.5}


class TestTrailingEntryOvershoot(unittest.TestCase):
    def test_the_constant_is_the_measured_smg_overshoot(self) -> None:
        measured = (SMG_ACTUAL_FILL - SMG_E1_LIMIT) / SMG_E1_LIMIT * BPS
        self.assertAlmostEqual(fp.ENTRY_TRAIL_OVERSHOOT_BPS, measured, places=9)
        self.assertAlmostEqual(fp.ENTRY_TRAIL_OVERSHOOT_BPS, 23.4307, places=4)

    def test_the_constant_is_not_the_issue_prose_number(self) -> None:
        # #1113 says "+40 bps above the limit" and then quotes the two prices that
        # give 23.43. The prices win.
        self.assertNotAlmostEqual(fp.ENTRY_TRAIL_OVERSHOOT_BPS, ISSUE_PROSE_OVERSHOOT_BPS, places=1)

    def test_the_fill_price_reproduces_the_live_fill(self) -> None:
        self.assertAlmostEqual(
            fp.fill_price_from_limit(SMG_E1_LIMIT, fp.ENTRY_TRAIL_OVERSHOOT_BPS),
            SMG_ACTUAL_FILL,
            places=6,
        )

    def test_the_fill_estimate_is_never_the_bare_limit(self) -> None:
        for limit in (1.0, SMG_E1_LIMIT, 500.0):
            with self.subTest(limit=limit):
                self.assertGreater(
                    fp.fill_price_from_limit(limit, fp.ENTRY_TRAIL_OVERSHOOT_BPS), limit
                )

    def test_the_zero_bps_arm_reproduces_the_replay_engines_limit_fill(self) -> None:
        self.assertEqual(fp.OVERSHOOT_ARMS_BPS[fp.OVERSHOOT_ARM_LIMIT], 0.0)
        self.assertEqual(fp.fill_price_from_limit(SMG_E1_LIMIT, 0.0), SMG_E1_LIMIT)

    def test_the_ceiling_arm_is_a_strictly_more_conservative_upper_bound(self) -> None:
        measured = fp.OVERSHOOT_ARMS_BPS[fp.OVERSHOOT_ARM_MEASURED]
        ceiling = fp.OVERSHOOT_ARMS_BPS[fp.OVERSHOOT_ARM_CEILING]
        self.assertGreater(ceiling, measured)
        # The ceiling arm is #1112's broker-enforced StopLimit ceiling for the same
        # incident -- an upper bound on the fill, not an expected fill.
        bound = entry_fill_estimate(reference=SMG_TOUCH_BID, trough=SMG_TOUCH_BID, d_bps=SMG_D_BPS)
        assert bound is not None
        self.assertAlmostEqual(ceiling, (bound - SMG_E1_LIMIT) / SMG_E1_LIMIT * BPS, places=9)

    def test_every_arm_is_named_in_the_public_tuple(self) -> None:
        self.assertEqual(set(fp.OVERSHOOT_ARMS_BPS), set(fp.OVERSHOOT_ARMS))

    def test_a_negative_overshoot_is_refused(self) -> None:
        # A buy that fills BELOW its own limit is price improvement, not overshoot;
        # accepting it silently would flatter every partition.
        with self.assertRaises(ValueError):
            fp.fill_price_from_limit(100.0, -1.0)


class TestTouchVersusThroughFillModel(unittest.TestCase):
    def test_a_limit_only_touched_does_not_fill_under_the_through_model(self) -> None:
        self.assertTrue(fp.tier_fills(low=100.0, limit=100.0, fill_model=fp.FILL_MODEL_TOUCH))
        self.assertFalse(fp.tier_fills(low=100.0, limit=100.0, fill_model=fp.FILL_MODEL_THROUGH))

    def test_the_through_model_fills_one_tick_past_the_limit(self) -> None:
        low = 100.0 - fp.TICK_USD
        self.assertTrue(fp.tier_fills(low=low, limit=100.0, fill_model=fp.FILL_MODEL_THROUGH))

    def test_the_touch_model_agrees_with_the_replay_engine_on_the_same_bar(self) -> None:
        setup = {
            "status": "OK",
            "disaster_stop": 90.0,
            "entry_tiers": [
                {"limit": 100.0, "alloc_pct": 20.0},
                {"limit": 97.0, "alloc_pct": 30.0},
            ],
            "tp_tranches": [{"target": 130.0, "tranche_pct": 100.0}],
        }
        tiers = _tiers(("E1", 100.0, 20.0), ("E2", 97.0, 30.0))
        for low in (101.0, 100.0, 98.0, 97.0, 96.0):
            with self.subTest(low=low):
                engine = replay_ladder(setup, [_bar(0, low)]).entries_filled
                walked = fp.walk_entry_fills(
                    tiers, [_bar(0, low)], fill_model=fp.FILL_MODEL_TOUCH, overshoot_bps=0.0
                )
                self.assertEqual(tuple(f.tier_id for f in walked), engine)

    def test_an_unknown_fill_model_raises(self) -> None:
        with self.assertRaises(ValueError):
            fp.tier_fills(low=1.0, limit=1.0, fill_model="nearly")


class TestEntryFillWalk(unittest.TestCase):
    TIERS = _tiers(("E1", 100.0, 20.0), ("E2", 97.0, 30.0), ("E3", 95.0, 50.0))

    def test_a_gap_down_fills_several_tiers_inside_one_bar(self) -> None:
        fills = fp.walk_entry_fills(
            self.TIERS,
            [_bar(1_000, 94.0)],
            fill_model=fp.FILL_MODEL_TOUCH,
            overshoot_bps=fp.ENTRY_TRAIL_OVERSHOOT_BPS,
        )
        self.assertEqual(tuple(f.tier_id for f in fills), ("E1", "E2", "E3"))
        self.assertEqual({f.bar_ts_ms for f in fills}, {1_000})

    def test_a_deeper_tier_filling_on_a_later_bar_carries_that_later_timestamp(self) -> None:
        fills = fp.walk_entry_fills(
            self.TIERS,
            [_bar(1_000, 99.0), _bar(2_000, 96.0)],
            fill_model=fp.FILL_MODEL_TOUCH,
            overshoot_bps=0.0,
        )
        self.assertEqual(tuple(f.tier_id for f in fills), ("E1", "E2"))
        self.assertEqual([f.bar_ts_ms for f in fills], [1_000, 2_000])

    def test_a_limit_touched_at_or_after_the_entry_expiry_does_not_fill(self) -> None:
        fills = fp.walk_entry_fills(
            self.TIERS,
            [_bar(1_000, 99.0), _bar(2_000, 96.0)],
            fill_model=fp.FILL_MODEL_TOUCH,
            overshoot_bps=0.0,
            entry_expiry_ms=2_000,
        )
        self.assertEqual(tuple(f.tier_id for f in fills), ("E1",))

    def test_the_walk_prices_each_fill_above_its_own_limit(self) -> None:
        fills = fp.walk_entry_fills(
            self.TIERS,
            [_bar(1_000, 94.0)],
            fill_model=fp.FILL_MODEL_TOUCH,
            overshoot_bps=fp.ENTRY_TRAIL_OVERSHOOT_BPS,
        )
        for f in fills:
            with self.subTest(tier=f.tier_id):
                self.assertGreater(f.fill_price, f.limit)

    def test_bars_are_walked_in_timestamp_order_even_when_the_input_is_not(self) -> None:
        fills = fp.walk_entry_fills(
            self.TIERS,
            [_bar(2_000, 96.0), _bar(1_000, 99.0)],
            fill_model=fp.FILL_MODEL_TOUCH,
            overshoot_bps=0.0,
        )
        self.assertEqual([f.bar_ts_ms for f in fills], [1_000, 2_000])


class TestPartialFillWeighting(unittest.TestCase):
    SMG = _tiers(*(("E%d" % (i + 1), lim, alloc) for i, (lim, alloc) in enumerate(SMG_TIERS)))

    def test_partition_weight_is_the_alloc_fraction_not_the_tier_count(self) -> None:
        # SMG allocs are 21.07 / 32.14 / 46.79. Only the top tier filling deploys
        # 21.07% of the intended position, NOT one third of it.
        self.assertAlmostEqual(fp.filled_fraction(self.SMG, ("E1",)), 0.2107, places=6)
        self.assertNotAlmostEqual(fp.filled_fraction(self.SMG, ("E1",)), 1 / 3, places=3)

    def test_a_full_fill_is_exactly_one(self) -> None:
        self.assertAlmostEqual(fp.filled_fraction(self.SMG, ("E1", "E2", "E3")), 1.0, places=12)

    def test_nothing_filled_is_zero_not_none(self) -> None:
        self.assertEqual(fp.filled_fraction(self.SMG, ()), 0.0)

    def test_a_ladder_without_alloc_weights_falls_back_to_the_tier_count(self) -> None:
        flat = _tiers(("E1", 100.0, 0.0), ("E2", 97.0, 0.0), ("E3", 95.0, 0.0))
        self.assertAlmostEqual(fp.filled_fraction(flat, ("E1",)), 1 / 3, places=12)

    def test_blended_fill_price_is_alloc_weighted_over_the_overshoot_prices(self) -> None:
        fills = fp.walk_entry_fills(
            self.SMG,
            [_bar(1_000, SMG_TIERS[1][0])],
            fill_model=fp.FILL_MODEL_TOUCH,
            overshoot_bps=fp.ENTRY_TRAIL_OVERSHOOT_BPS,
        )
        self.assertEqual(tuple(f.tier_id for f in fills), ("E1", "E2"))
        w1, w2 = SMG_TIERS[0][1], SMG_TIERS[1][1]
        expected = (fills[0].fill_price * w1 + fills[1].fill_price * w2) / (w1 + w2)
        self.assertAlmostEqual(fp.blended_fill_price(fills), expected, places=12)

    def test_blended_fill_price_of_nothing_is_none(self) -> None:
        self.assertIsNone(fp.blended_fill_price(()))

    def test_blended_fill_price_falls_back_to_equal_weight_without_allocs(self) -> None:
        flat = _tiers(("E1", 100.0, 0.0), ("E2", 90.0, 0.0))
        fills = fp.walk_entry_fills(
            flat, [_bar(1_000, 89.0)], fill_model=fp.FILL_MODEL_TOUCH, overshoot_bps=0.0
        )
        self.assertAlmostEqual(fp.blended_fill_price(fills), 95.0, places=12)


class TestEntryTiersFromSetup(unittest.TestCase):
    def test_the_tiers_are_read_in_brief_order_and_labelled_e1_first(self) -> None:
        setup = {
            "status": "OK",
            "disaster_stop": 40.0,
            "entry_tiers": [{"limit": lim, "alloc_pct": alloc} for lim, alloc in SMG_TIERS],
        }
        tiers = fp.entry_tiers_from_setup(setup)
        self.assertEqual([t.tier_id for t in tiers], ["E1", "E2", "E3"])
        self.assertEqual([t.limit for t in tiers], [lim for lim, _ in SMG_TIERS])
        self.assertEqual([t.alloc_pct for t in tiers], [a for _, a in SMG_TIERS])

    def test_a_missing_or_empty_setup_yields_no_tiers(self) -> None:
        for setup in (None, {}, {"entry_tiers": []}, {"entry_tiers": None}):
            with self.subTest(setup=setup):
                self.assertEqual(fp.entry_tiers_from_setup(setup), ())

    def test_a_tier_without_an_alloc_defaults_to_zero_rather_than_raising(self) -> None:
        tiers = fp.entry_tiers_from_setup({"entry_tiers": [{"limit": 10.0}]})
        self.assertEqual(tiers, (fp.EntryTier(tier_id="E1", limit=10.0, alloc_pct=0.0),))


if __name__ == "__main__":
    unittest.main()

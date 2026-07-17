"""Decomposer tests: ``SetupPlan`` -> per-tier ``BracketOrderRequest`` list.

Pins the P2 decomposition decision (design memo §P2): ONE order-attached
3-way bracket per NON-ZERO entry tier — quantity = tier.qty, entry limit =
tier.limit_price, stop = the shared disaster stop at tier-sized Amount,
take-profit index-paired to the tp tranches and clamped to the last tranche,
zero-qty tiers skipped with a structured log entry (never POSTed), TTL
passthrough with the 0-sentinel falling back to the planner default.
"""

from __future__ import annotations

import unittest
import uuid

from alphalens_pipeline.brokers.contract import BracketOrderRequest, InstrumentRef
from alphalens_pipeline.brokers.execution import (
    _TTL_ZERO_SENTINEL_DAYS,
    decompose_setup_plan,
)
from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS
from alphalens_pipeline.paper.sizing import SetupPlan, TierPlan, TpTranchePlan


def _instrument(ticker: str = "KO", mic: str = "XNYS") -> InstrumentRef:
    return InstrumentRef(
        ticker=ticker,
        exchange_mic=mic,
        asset_type="Stock",
        broker_instrument_id="307",
        broker_symbol=f"{ticker.lower()}:{mic.lower()}",
    )


def _tier(index: int, limit: float, qty: int) -> TierPlan:
    return TierPlan(tier_index=index, limit_price=limit, qty=qty, alloc_pct=33.0, tag=f"t{index}")


def _tranche(index: int, target: float) -> TpTranchePlan:
    return TpTranchePlan(
        tranche_index=index, target_price=target, tranche_pct=50.0, r_multiple=1.5, tag=f"tp{index}"
    )


def _plan(
    *,
    tiers: tuple[TierPlan, ...],
    tranches: tuple[TpTranchePlan, ...] = (),
    disaster_stop: float = 40.0,
    order_ttl_days: int = 5,
) -> SetupPlan:
    return SetupPlan(
        suggested_size_pct=3.0,
        scale_factor=1.0,
        final_size_pct=3.0,
        total_notional=3_000.0,
        paper_equity=100_000.0,
        disaster_stop=disaster_stop,
        order_ttl_days=order_ttl_days,
        entry_tiers=tiers,
        tp_tranches=tranches,
    )


class TestLadderDecomposition(unittest.TestCase):
    def test_one_bracket_per_nonzero_entry_tier(self):
        plan = _plan(
            tiers=(_tier(0, 50.0, 10), _tier(1, 48.0, 12), _tier(2, 46.0, 14)),
            tranches=(_tranche(0, 55.0), _tranche(1, 60.0), _tranche(2, 66.0)),
        )

        brackets = decompose_setup_plan(plan, _instrument())

        self.assertEqual(len(brackets), 3)
        for bracket, tier in zip(brackets, plan.entry_tiers, strict=True):
            self.assertIsInstance(bracket, BracketOrderRequest)
            self.assertEqual(bracket.quantity, tier.qty)
            self.assertEqual(bracket.entry_limit, tier.limit_price)
            self.assertEqual(bracket.instrument, _instrument())
            self.assertEqual(bracket.side, "BUY")

    def test_disaster_stop_price_shared_qty_per_tier(self):
        plan = _plan(
            tiers=(_tier(0, 50.0, 10), _tier(1, 48.0, 12)),
            tranches=(_tranche(0, 55.0),),
            disaster_stop=41.25,
        )

        brackets = decompose_setup_plan(plan, _instrument())

        # Same stop PRICE on every bracket; the Amount (qty) stays tier-sized
        # so aggregate stop coverage always equals filled quantity exactly.
        self.assertEqual([b.stop_loss for b in brackets], [41.25, 41.25])
        self.assertEqual([b.quantity for b in brackets], [10, 12])

    def test_tp_index_pairing_clamped_to_last_tranche(self):
        plan = _plan(
            tiers=(_tier(0, 50.0, 10), _tier(1, 48.0, 12), _tier(2, 46.0, 14)),
            tranches=(_tranche(0, 55.0), _tranche(1, 60.0)),
        )

        brackets = decompose_setup_plan(plan, _instrument())

        # tiers > tranches: the deep tier reuses the LAST target (clamp).
        self.assertEqual([b.take_profit for b in brackets], [55.0, 60.0, 60.0])

    def test_excess_tranches_beyond_tiers_are_unused(self):
        plan = _plan(
            tiers=(_tier(0, 50.0, 10),),
            tranches=(_tranche(0, 55.0), _tranche(1, 60.0), _tranche(2, 66.0)),
        )

        brackets = decompose_setup_plan(plan, _instrument())

        self.assertEqual([b.take_profit for b in brackets], [55.0])

    def test_tp_none_when_no_tranches(self):
        plan = _plan(tiers=(_tier(0, 50.0, 10),), tranches=())

        brackets = decompose_setup_plan(plan, _instrument())

        self.assertEqual(len(brackets), 1)
        self.assertIsNone(brackets[0].take_profit)
        self.assertIsNotNone(brackets[0].stop_loss, "stop-only bracket keeps the disaster stop")

    def test_zero_qty_tiers_skipped_never_posted(self):
        plan = _plan(
            tiers=(_tier(0, 500.0, 0), _tier(1, 480.0, 6)),
            tranches=(_tranche(0, 550.0), _tranche(1, 600.0)),
        )

        with self.assertLogs("alphalens_pipeline.brokers.execution", level="INFO") as captured:
            brackets = decompose_setup_plan(plan, _instrument())

        self.assertEqual(len(brackets), 1)
        self.assertEqual(brackets[0].quantity, 6)
        joined = "\n".join(captured.output)
        self.assertIn("zero-qty", joined)
        self.assertIn("tier_index=0", joined)
        self.assertIn("KO", joined)

    def test_tp_pairing_keys_on_tier_index_across_zero_qty_skips(self):
        # The skipped tier does NOT shift the survivors' target pairing:
        # tier 1 keeps tranche 1 even though it becomes the first bracket.
        plan = _plan(
            tiers=(_tier(0, 50.0, 0), _tier(1, 48.0, 12)),
            tranches=(_tranche(0, 55.0), _tranche(1, 60.0)),
        )

        brackets = decompose_setup_plan(plan, _instrument())

        self.assertEqual([b.take_profit for b in brackets], [60.0])

    def test_entry_ttl_passthrough_and_zero_sentinel_default(self):
        explicit = decompose_setup_plan(
            _plan(tiers=(_tier(0, 50.0, 10),), order_ttl_days=9), _instrument()
        )
        sentinel = decompose_setup_plan(
            _plan(tiers=(_tier(0, 50.0, 10),), order_ttl_days=0), _instrument()
        )

        self.assertEqual(explicit[0].entry_ttl_days, 9)
        self.assertEqual(sentinel[0].entry_ttl_days, _TTL_ZERO_SENTINEL_DAYS)
        self.assertEqual(_TTL_ZERO_SENTINEL_DAYS, DEFAULT_ORDER_TTL_DAYS)

    def test_client_request_id_unique_uuid4_per_bracket(self):
        plan = _plan(
            tiers=(_tier(0, 50.0, 10), _tier(1, 48.0, 12), _tier(2, 46.0, 14)),
            tranches=(_tranche(0, 55.0),),
        )

        brackets = decompose_setup_plan(plan, _instrument())

        ids = [b.client_request_id for b in brackets]
        self.assertEqual(len(set(ids)), 3, "each bracket gets a FRESH uuid4")
        for request_id in ids:
            parsed = uuid.UUID(request_id)
            self.assertEqual(parsed.version, 4)

    def test_sell_side_mirrors(self):
        plan = _plan(tiers=(_tier(0, 50.0, 10),), tranches=(_tranche(0, 55.0),))

        brackets = decompose_setup_plan(plan, _instrument(), side="SELL")

        self.assertEqual(brackets[0].side, "SELL")
        # Geometry passes through untouched — the broker mirrors the exit
        # BuySell direction, not the decomposer.
        self.assertEqual(brackets[0].entry_limit, 50.0)
        self.assertEqual(brackets[0].stop_loss, 40.0)
        self.assertEqual(brackets[0].take_profit, 55.0)


if __name__ == "__main__":
    unittest.main()

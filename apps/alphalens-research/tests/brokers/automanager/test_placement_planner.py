"""Hermetic tests for placement_planner.classify (Option-C in-band-subset).

Rules: (1) a tier's TP is a bracket CHILD only when it clears the 0.15
child-distance guard (inclusive <=); a farther TP is reported operator-managed,
never dropped, never POSTed; (2) the disaster stop is NEVER a child —
represented exactly once at plan level, placed later as a standalone
StopIfTraded after fill. Fixtures: LAZ 2026-07-14, S 2026-07-13, +15.00 vs
+15.01 knife-edge.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers import execution as execution_policy
from alphalens_pipeline.brokers.automanager.placement_planner import (
    PlacementPlan,
    TierPlacement,
    classify,
)
from alphalens_pipeline.brokers.contract import BracketOrderRequest, InstrumentRef
from alphalens_pipeline.paper.sizing import SetupPlan, TierPlan, TpTranchePlan


def _instrument(ticker: str = "LAZ") -> InstrumentRef:
    return InstrumentRef(
        ticker=ticker,
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id="999",
        broker_symbol=f"{ticker.lower()}:xnys",
    )


def _setup_plan(
    *, disaster_stop: float, entries: list[float], tps: list[float], order_ttl_days: int = 7
) -> SetupPlan:
    alloc = 100.0 / len(entries)
    tiers = tuple(
        TierPlan(tier_index=i, limit_price=lim, qty=10, alloc_pct=alloc, tag=f"t{i}")
        for i, lim in enumerate(entries)
    )
    tranches = tuple(
        TpTranchePlan(
            tranche_index=i,
            target_price=t,
            tranche_pct=100.0 / len(tps),
            r_multiple=1.5,
            tag=f"tp{i}",
        )
        for i, t in enumerate(tps)
    )
    return SetupPlan(
        suggested_size_pct=10.0,
        scale_factor=1.0,
        final_size_pct=10.0,
        total_notional=10_000.0,
        paper_equity=100_000.0,
        disaster_stop=disaster_stop,
        order_ttl_days=order_ttl_days,
        entry_tiers=tiers,
        tp_tranches=tranches,
        fx=None,
    )


def _laz() -> tuple[SetupPlan, InstrumentRef]:
    return (
        _setup_plan(disaster_stop=35.76, entries=[41.10, 38.85], tps=[46.54, 50.95]),
        _instrument("LAZ"),
    )


def _s() -> tuple[SetupPlan, InstrumentRef]:
    return (
        _setup_plan(disaster_stop=12.57, entries=[18.08, 16.68, 15.81], tps=[18.81, 19.30, 21.40]),
        _instrument("S"),
    )


def _knife() -> tuple[SetupPlan, InstrumentRef]:
    return (
        _setup_plan(disaster_stop=90.0, entries=[100.0, 100.0], tps=[115.00, 115.01]),
        _instrument("KNF"),
    )


class TestClassifyLaz(unittest.TestCase):
    def test_tier0_places_tp_child_tier1_operator_managed(self):
        setup, instrument = _laz()
        plan = classify(setup, instrument)
        self.assertIsInstance(plan, PlacementPlan)
        self.assertEqual(len(plan.tiers), 2)
        t0, t1 = plan.tiers
        self.assertIsInstance(t0, TierPlacement)
        self.assertTrue(t0.tp_placed_as_child)
        self.assertEqual(t0.bracket.take_profit, 46.54)
        self.assertIsNone(t0.tp_operator_managed)
        self.assertIsNone(t0.bracket.stop_loss, "disaster stop is never a child")
        self.assertEqual(t0.bracket.entry_limit, 41.10)
        self.assertFalse(t1.tp_placed_as_child)
        self.assertIsNone(t1.bracket.take_profit, "no Limit child for a far TP")
        self.assertEqual(t1.tp_operator_managed, 50.95)
        self.assertIsNone(t1.bracket.stop_loss)
        self.assertEqual(t1.bracket.entry_limit, 38.85, "the sized entry is preserved")

    def test_report_enumerates_every_tier_and_tp_no_silent_drop(self):
        setup, instrument = _laz()
        report = classify(setup, instrument).operator_report
        for token in ("tier 0", "tier 1", "46.54", "50.95", "operator-managed", "13.2", "31.1"):
            self.assertIn(token, report)


class TestClassifyKnifeEdge(unittest.TestCase):
    def test_15_00_places_15_01_operator_managed(self):
        setup, instrument = _knife()
        plan = classify(setup, instrument)
        self.assertTrue(plan.tiers[0].tp_placed_as_child, "+15.00% clears the inclusive (<=) guard")
        self.assertFalse(plan.tiers[1].tp_placed_as_child, "+15.01% is beyond the guard")
        self.assertEqual(plan.tiers[1].tp_operator_managed, 115.01)

    def test_boundary_uses_the_shared_execution_constant(self):
        self.assertEqual(execution_policy._MAX_CHILD_DISTANCE_FRAC, 0.15)


class TestFarTpTierShape(unittest.TestCase):
    def test_far_tp_tier_emits_entry_only_bracket_not_a_reject(self):
        setup, instrument = _laz()
        tier1 = classify(setup, instrument).tiers[1]
        self.assertFalse(tier1.tp_placed_as_child)
        self.assertIsNone(tier1.bracket.take_profit)
        self.assertIsNone(tier1.bracket.stop_loss)
        self.assertIsInstance(tier1.bracket, BracketOrderRequest)


class TestDisasterStopExactlyOnce(unittest.TestCase):
    def test_disaster_stop_represented_exactly_once_across_fixtures(self):
        for name, factory in (("LAZ", _laz), ("S", _s), ("knife", _knife)):
            with self.subTest(fixture=name):
                setup, instrument = factory()
                plan = classify(setup, instrument)
                self.assertEqual(plan.disaster_stop_price, setup.disaster_stop)
                self.assertGreater(plan.disaster_stop_price, 0.0)
                for tier in plan.tiers:
                    self.assertIsNone(tier.bracket.stop_loss)
                self.assertEqual(plan.operator_report.lower().count("disaster stop"), 1)

    def test_s_incident_all_stops_far_still_one_standalone(self):
        setup, instrument = _s()
        plan = classify(setup, instrument)
        self.assertEqual(len(plan.tiers), 3)
        for tier in plan.tiers:
            self.assertIsNone(tier.bracket.stop_loss)
        self.assertEqual(plan.disaster_stop_price, 12.57)


if __name__ == "__main__":
    unittest.main()

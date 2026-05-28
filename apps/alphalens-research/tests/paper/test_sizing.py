"""Pure-math tests for the paper-trade sizing formula.

The locked sizing math is documented in
``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §3 — these tests
pin the numerical contract so a refactor of ``sizing.py`` cannot silently
drift.

The key invariants:
- ``effective_size_pct = min(suggested_size_pct, 100/N_FIXED)``
- ``total_notional = effective_size_pct/100 × equity``
- ``per_tier_qty = floor(total_notional × alloc_pct/100 / limit_price)``
- malformed setups raise :class:`TradeSetupNotPlannableError`, not silent zero-qty
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.paper.constants import N_FIXED
from alphalens_pipeline.paper.sizing import (
    SetupPlan,
    TradeSetupNotPlannableError,
    compute_setup_plan,
    setup_plan_gross_notional,
)


def _make_setup(
    *,
    suggested_size_pct=5.0,
    disaster_stop=90.0,
    entry_tiers=None,
    tp_tranches=None,
    status="OK",
    schema_version="1.0.0",
    order_ttl_days=10,
) -> dict:
    """Build a sample trade_setup dict so each test focuses on one parameter."""
    if entry_tiers is None:
        entry_tiers = [
            {"limit": 100.0, "alloc_pct": 50.0, "atr_distance": 0.0, "tag": "tier-0"},
            {"limit": 95.0, "alloc_pct": 30.0, "atr_distance": 1.0, "tag": "tier-1"},
            {"limit": 90.0, "alloc_pct": 20.0, "atr_distance": 2.0, "tag": "tier-2"},
        ]
    if tp_tranches is None:
        tp_tranches = [
            {"target": 110.0, "tranche_pct": 50.0, "r_multiple": 1.0, "tag": "tp-1"},
            {"target": 120.0, "tranche_pct": 50.0, "r_multiple": 2.0, "tag": "tp-2"},
        ]
    return {
        "schema_version": schema_version,
        "status": status,
        "asof_close": 100.0,
        "atr": 1.5,
        "disaster_stop": disaster_stop,
        "suggested_size_pct": suggested_size_pct,
        "order_ttl_days": order_ttl_days,
        "entry_tiers": entry_tiers,
        "tp_tranches": tp_tranches,
    }


class TestEffectiveSizeCap(unittest.TestCase):
    """The cap at ``100 / N_FIXED`` is the structural limit binding ~95% of
    real candidates (per memo §3 + the empirical suggested-size distribution).
    """

    def test_suggested_above_cap_uses_cap(self):
        setup = _make_setup(suggested_size_pct=6.0)
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        self.assertAlmostEqual(plan.effective_size_pct, 100.0 / N_FIXED, places=10)
        self.assertEqual(plan.suggested_size_pct, 6.0)  # raw preserved for analysis

    def test_suggested_below_cap_passes_through(self):
        cap = 100.0 / N_FIXED  # ≈ 0.278%
        below_cap = cap / 2.0
        setup = _make_setup(suggested_size_pct=below_cap)
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        self.assertAlmostEqual(plan.effective_size_pct, below_cap, places=10)

    def test_total_notional_matches_effective_times_equity(self):
        setup = _make_setup(suggested_size_pct=6.0)
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        expected = (100.0 / N_FIXED) / 100.0 * 1_000_000.0
        self.assertAlmostEqual(plan.total_notional, expected, places=4)


class TestPerTierQuantity(unittest.TestCase):
    def test_qty_is_floor_of_alloc_notional_over_limit(self):
        # Equity $1M, cap at 100/360 ≈ 0.2778% → total_notional ≈ $2778
        # Tier 0: alloc 50% → tier_notional ≈ $1389 / limit $100 = 13.89 → floor 13
        # Tier 1: alloc 30% → tier_notional ≈ $833 / limit $95 = 8.77 → floor 8
        # Tier 2: alloc 20% → tier_notional ≈ $556 / limit $90 = 6.17 → floor 6
        setup = _make_setup(suggested_size_pct=5.0)  # above cap → uses cap
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)

        # Re-derive expected from invariants so the test self-explains.
        eff = 100.0 / N_FIXED
        total = eff / 100.0 * 1_000_000.0
        expected_qtys = [
            math.floor(total * a / 100.0 / p) for p, a in [(100, 50), (95, 30), (90, 20)]
        ]
        self.assertEqual([t.qty for t in plan.entry_tiers], expected_qtys)

    def test_zero_alloc_tier_keeps_qty_zero_for_audit_visibility(self):
        """A zero-alloc tier survives planning with qty=0 instead of silent drop —
        the planner needs the count for analysis even if no shares were sized."""
        setup = _make_setup(
            entry_tiers=[
                {"limit": 100.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "t0"},
                {"limit": 95.0, "alloc_pct": 0.0, "atr_distance": 1.0, "tag": "t1"},
            ],
        )
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        self.assertEqual(plan.entry_tiers[1].qty, 0)

    def test_small_alloc_below_one_share_floors_to_zero(self):
        """If alloc * notional < limit, qty rounds to 0 — kept in plan for audit."""
        setup = _make_setup(
            suggested_size_pct=0.001,  # tiny → effective_pct also tiny
            entry_tiers=[
                {"limit": 500.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "expensive"},
            ],
        )
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        self.assertEqual(plan.entry_tiers[0].qty, 0)


class TestUnplannable(unittest.TestCase):
    def test_status_no_structure_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(
                brief_trade_setup=_make_setup(status="NO_STRUCTURE"),
                paper_equity=1_000_000.0,
            )

    def test_unknown_schema_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(
                brief_trade_setup=_make_setup(schema_version="2.0.0"),
                paper_equity=1_000_000.0,
            )

    def test_missing_suggested_size_rejected(self):
        setup = _make_setup()
        setup["suggested_size_pct"] = None
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)

    def test_non_positive_suggested_size_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(
                brief_trade_setup=_make_setup(suggested_size_pct=0.0),
                paper_equity=1_000_000.0,
            )

    def test_missing_disaster_stop_rejected(self):
        setup = _make_setup()
        setup["disaster_stop"] = None
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)

    def test_empty_entry_tiers_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(
                brief_trade_setup=_make_setup(entry_tiers=[]),
                paper_equity=1_000_000.0,
            )

    def test_non_dict_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(brief_trade_setup="not a dict", paper_equity=1_000_000.0)

    def test_tiers_with_non_positive_limits_dropped_silently(self):
        """A malformed tier is dropped from the plan (defense-in-depth), but
        the rest of the setup still plans cleanly so we don't lose the
        candidate over one bad tier."""
        setup = _make_setup(
            entry_tiers=[
                {"limit": 100.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "ok"},
                {"limit": 0.0, "alloc_pct": 0.0, "atr_distance": 0.0, "tag": "bad"},
            ],
        )
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        self.assertEqual(len(plan.entry_tiers), 1)

    def test_all_tiers_dropped_raises(self):
        setup = _make_setup(
            entry_tiers=[
                {"limit": 0.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "bad"},
            ]
        )
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)


class TestTpTranches(unittest.TestCase):
    def test_tp_tranches_preserved_in_order(self):
        setup = _make_setup()
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        self.assertEqual(len(plan.tp_tranches), 2)
        self.assertEqual(plan.tp_tranches[0].tranche_index, 0)
        self.assertEqual(plan.tp_tranches[0].target_price, 110.0)
        self.assertEqual(plan.tp_tranches[1].target_price, 120.0)

    def test_tp_tranches_empty_is_allowed(self):
        """A candidate without TP tranches is plannable for entry-only paper
        observation (atypical but should not crash the planner)."""
        setup = _make_setup(tp_tranches=[])
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        self.assertEqual(len(plan.tp_tranches), 0)


class TestGrossNotional(unittest.TestCase):
    def test_gross_notional_sums_qty_times_limit(self):
        setup = _make_setup(suggested_size_pct=5.0)
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        gross = setup_plan_gross_notional(plan)
        expected = sum(t.qty * t.limit_price for t in plan.entry_tiers)
        self.assertAlmostEqual(gross, expected)


class TestPlanReturnsAFrozenDataclass(unittest.TestCase):
    """The return value is a frozen dataclass so consumers can't accidentally
    mutate it between planning + persistence (which would desync the SQLite
    record from the in-memory plan)."""

    def test_plan_is_frozen(self):
        setup = _make_setup()
        plan = compute_setup_plan(brief_trade_setup=setup, paper_equity=1_000_000.0)
        self.assertIsInstance(plan, SetupPlan)
        with self.assertRaises(Exception):
            plan.disaster_stop = 999.0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

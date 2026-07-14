"""Pure-math tests for the paper-trade sizing formula (v2 global scaling).

The locked sizing math is documented in
``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §2.3 — these
tests pin the numerical contract so a refactor of ``sizing.py`` cannot
silently drift.

The key invariants:
- ``scale_factor = min(1.0, daily_target / aggregate_uncapped)``
  where ``daily_target = STEADY_STATE_GROSS_FRAC × equity / EXPECTED_AVG_HOLD_DAYS``
  and ``aggregate_uncapped = Σ_i suggested_size_pct_i / 100 × equity``
- ``final_size_pct = suggested_size_pct × scale_factor`` per candidate
- ``total_notional = final_size_pct / 100 × equity``
- ``per_tier_qty = floor(total_notional × alloc_pct / 100 / limit_price)``
- malformed setups raise :class:`TradeSetupNotPlannableError`, not silent zero-qty
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.paper.constants import (
    EXPECTED_AVG_HOLD_DAYS,
    STEADY_STATE_GROSS_FRAC,
)
from alphalens_pipeline.paper.sizing import (
    SetupPlan,
    TradeSetupNotPlannableError,
    compute_daily_scale_factor,
    compute_setup_plan,
    setup_plan_gross_notional,
    validate_trade_setup,
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


# ---------------------------------------------------------------------------
# compute_daily_scale_factor — the v2 entry point
# ---------------------------------------------------------------------------


class TestDailyScaleFactor(unittest.TestCase):
    """v2 §2.3: daily_target / aggregate_uncapped, clipped to 1.0."""

    def test_no_candidates_returns_one(self):
        self.assertEqual(compute_daily_scale_factor([], 1_000_000.0), 1.0)

    def test_aggregate_below_target_clips_to_one(self):
        """Quiet day: aggregate uncapped is below daily target → no scale-down."""
        # daily_target @ $1M = 0.667 × 1M / 30 = $22,233. One candidate at 1% =
        # $10k notional. Below target → scale = 1.0.
        self.assertEqual(
            compute_daily_scale_factor([1.0], 1_000_000.0),
            1.0,
        )

    def test_aggregate_above_target_scales_down_proportionally(self):
        """Busy day: aggregate exceeds target → scale = target / aggregate."""
        # 8 candidates @ suggested=6% on $1M equity:
        # aggregate = 8 × 0.06 × 1M = $480_000
        # daily_target = 0.667 × 1M / 30 = $22,233
        # scale = 22_233 / 480_000 = 0.0463
        scale = compute_daily_scale_factor([6.0] * 8, 1_000_000.0)
        expected = (STEADY_STATE_GROSS_FRAC * 1_000_000.0 / EXPECTED_AVG_HOLD_DAYS) / (
            8 * 0.06 * 1_000_000.0
        )
        self.assertAlmostEqual(scale, expected, places=8)
        # Sanity: applying scale to one candidate's 6% yields ~0.278% (matches
        # v1's per-candidate cap by construction; just preserves variance).
        self.assertAlmostEqual(6.0 * scale, 100.0 / 360, places=2)

    def test_ratios_preserved_across_candidates(self):
        """v2 § core invariant: a candidate with 8% suggested gets a position
        33% larger than one with 6% after scaling. v1's cap would have
        flattened both to 0.278%."""
        suggested = [6.0, 8.0]
        scale = compute_daily_scale_factor(suggested, 1_000_000.0)
        # final/final = 8/6 regardless of scale
        finals = [s * scale for s in suggested]
        self.assertAlmostEqual(finals[1] / finals[0], 8.0 / 6.0, places=8)

    def test_empty_iterable_returns_one(self):
        self.assertEqual(compute_daily_scale_factor(iter([]), 1_000_000.0), 1.0)

    def test_non_positive_equity_returns_one(self):
        """Defense against bad operator input — scaling against $0 is undefined."""
        self.assertEqual(compute_daily_scale_factor([6.0], 0.0), 1.0)
        self.assertEqual(compute_daily_scale_factor([6.0], -100.0), 1.0)


# ---------------------------------------------------------------------------
# compute_setup_plan — applies the precomputed scale factor
# ---------------------------------------------------------------------------


class TestSetupPlanWithScale(unittest.TestCase):
    def test_final_size_pct_equals_suggested_times_scale(self):
        setup = _make_setup(suggested_size_pct=6.0)
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=0.05
        )
        self.assertAlmostEqual(plan.suggested_size_pct, 6.0)
        self.assertAlmostEqual(plan.scale_factor, 0.05)
        self.assertAlmostEqual(plan.final_size_pct, 6.0 * 0.05)

    def test_total_notional_equals_final_pct_over_100_times_equity(self):
        setup = _make_setup(suggested_size_pct=6.0)
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=0.04
        )
        expected = (6.0 * 0.04) / 100.0 * 1_000_000.0
        self.assertAlmostEqual(plan.total_notional, expected, places=4)

    def test_scale_one_preserves_full_suggested_size(self):
        """When scale=1.0 (quiet day), each candidate gets its full
        suggested_size_pct as the final size."""
        setup = _make_setup(suggested_size_pct=2.5)
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=1.0
        )
        self.assertAlmostEqual(plan.final_size_pct, 2.5)


# ---------------------------------------------------------------------------
# Per-tier quantity (math unchanged from v1 — just sources total_notional
# from the v2 final_size_pct chain)
# ---------------------------------------------------------------------------


class TestPerTierQuantity(unittest.TestCase):
    def test_qty_is_floor_of_alloc_notional_over_limit(self):
        # Suggested 5%, scale 0.05 → final 0.25% → total_notional $2500
        # Tier 0: alloc 50% → tier_notional $1250 / limit $100 → floor 12
        # Tier 1: alloc 30% → tier_notional $750 / limit $95 → floor 7
        # Tier 2: alloc 20% → tier_notional $500 / limit $90 → floor 5
        setup = _make_setup(suggested_size_pct=5.0)
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=0.05
        )

        total = 5.0 * 0.05 / 100.0 * 1_000_000.0
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
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=0.05
        )
        self.assertEqual(plan.entry_tiers[1].qty, 0)

    def test_small_alloc_below_one_share_floors_to_zero(self):
        """If alloc × notional × scale < limit, qty rounds to 0 — kept in plan."""
        setup = _make_setup(
            suggested_size_pct=0.001,  # tiny → final_pct also tiny
            entry_tiers=[
                {"limit": 500.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "expensive"},
            ],
        )
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=1.0
        )
        self.assertEqual(plan.entry_tiers[0].qty, 0)


# ---------------------------------------------------------------------------
# Unplannable cases (validation shared with validate_trade_setup)
# ---------------------------------------------------------------------------


class TestUnplannable(unittest.TestCase):
    def _compute(self, setup):
        return compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=1.0
        )

    def test_status_no_structure_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            self._compute(_make_setup(status="NO_STRUCTURE"))

    def test_unknown_schema_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            self._compute(_make_setup(schema_version="2.0.0"))

    def test_schema_1_1_0_accepted(self):
        # 1.1.0 adds only the builder_config_version key (ADR 0013) — every field
        # the planner reads is unchanged, so it must stay plannable.
        plan = self._compute(_make_setup(schema_version="1.1.0"))
        self.assertIsNotNone(plan)

    def test_schema_1_2_0_rejected_until_reviewed(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            self._compute(_make_setup(schema_version="1.2.0"))

    def test_missing_suggested_size_rejected(self):
        setup = _make_setup()
        setup["suggested_size_pct"] = None
        with self.assertRaises(TradeSetupNotPlannableError):
            self._compute(setup)

    def test_non_positive_suggested_size_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            self._compute(_make_setup(suggested_size_pct=0.0))

    def test_missing_disaster_stop_rejected(self):
        setup = _make_setup()
        setup["disaster_stop"] = None
        with self.assertRaises(TradeSetupNotPlannableError):
            self._compute(setup)

    def test_empty_entry_tiers_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            self._compute(_make_setup(entry_tiers=[]))

    def test_non_dict_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            compute_setup_plan(
                brief_trade_setup="not a dict", paper_equity=1_000_000.0, scale_factor=1.0
            )

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
        plan = self._compute(setup)
        self.assertEqual(len(plan.entry_tiers), 1)

    def test_all_tiers_dropped_raises(self):
        setup = _make_setup(
            entry_tiers=[
                {"limit": 0.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "bad"},
            ]
        )
        with self.assertRaises(TradeSetupNotPlannableError):
            self._compute(setup)


# ---------------------------------------------------------------------------
# validate_trade_setup (the shared validation entry point)
# ---------------------------------------------------------------------------


class TestValidateOnly(unittest.TestCase):
    """The cheap validator the planner uses in pass 1 to collect suggested
    pcts before computing the global scale factor."""

    def test_returns_suggested_size_pct_on_success(self):
        setup = _make_setup(suggested_size_pct=4.2)
        self.assertEqual(validate_trade_setup(setup), 4.2)

    def test_raises_on_unplannable(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            validate_trade_setup(_make_setup(status="NO_STRUCTURE"))

    def test_raises_when_all_tiers_have_non_positive_limit(self):
        """Pass-1/pass-2 drift hazard: a candidate with every tier carrying
        ``limit <= 0`` would pass ``validate_trade_setup`` (which only checked
        tier-list emptiness) but fail ``compute_setup_plan`` (which drops
        such tiers and then rejects the empty result). The downstream
        consequence was a downward bias on the day's scale_factor — the
        aggregate counted this candidate, but no plan was actually written.
        After the zen second-round fix, validate now applies the same
        sanitisation so the two passes stay in lockstep.
        """
        setup = _make_setup(
            entry_tiers=[
                {"limit": 0.0, "alloc_pct": 50.0, "atr_distance": 0.0, "tag": "bad-0"},
                {"limit": -1.0, "alloc_pct": 50.0, "atr_distance": 0.0, "tag": "bad-1"},
            ]
        )
        with self.assertRaises(TradeSetupNotPlannableError):
            validate_trade_setup(setup)

    def test_accepts_when_at_least_one_tier_has_positive_limit(self):
        """Mirror: a partially-bad tier list (one good, one zero) still
        validates — defense-in-depth drops the bad tier in pass 2 without
        rejecting the whole candidate."""
        setup = _make_setup(
            entry_tiers=[
                {"limit": 100.0, "alloc_pct": 100.0, "atr_distance": 0.0, "tag": "ok"},
                {"limit": 0.0, "alloc_pct": 0.0, "atr_distance": 0.0, "tag": "bad"},
            ]
        )
        self.assertEqual(validate_trade_setup(setup), 5.0)


# ---------------------------------------------------------------------------
# TP tranches + gross-notional + frozen dataclass
# ---------------------------------------------------------------------------


class TestTpTranches(unittest.TestCase):
    def test_tp_tranches_preserved_in_order(self):
        setup = _make_setup()
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=1.0
        )
        self.assertEqual(len(plan.tp_tranches), 2)
        self.assertEqual(plan.tp_tranches[0].tranche_index, 0)
        self.assertEqual(plan.tp_tranches[0].target_price, 110.0)
        self.assertEqual(plan.tp_tranches[1].target_price, 120.0)

    def test_tp_tranches_empty_is_allowed(self):
        setup = _make_setup(tp_tranches=[])
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=1.0
        )
        self.assertEqual(len(plan.tp_tranches), 0)


class TestGrossNotional(unittest.TestCase):
    def test_gross_notional_sums_qty_times_limit(self):
        setup = _make_setup(suggested_size_pct=5.0)
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=0.05
        )
        gross = setup_plan_gross_notional(plan)
        expected = sum(t.qty * t.limit_price for t in plan.entry_tiers)
        self.assertAlmostEqual(gross, expected)


class TestPlanReturnsAFrozenDataclass(unittest.TestCase):
    def test_plan_is_frozen(self):
        setup = _make_setup()
        plan = compute_setup_plan(
            brief_trade_setup=setup, paper_equity=1_000_000.0, scale_factor=1.0
        )
        self.assertIsInstance(plan, SetupPlan)
        with self.assertRaises(Exception):
            plan.disaster_stop = 999.0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

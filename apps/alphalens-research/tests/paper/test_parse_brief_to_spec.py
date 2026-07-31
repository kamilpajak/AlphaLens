"""Tests for ``paper/sizing.py::parse_brief_to_spec`` — the brief-parse half of
the PR-5 ``compute_setup_plan`` split (memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
section 2.3).

Pins the field mapping from a raw ``brief_trade_setup`` dict onto the
unsized :class:`~alphalens_pipeline.trade_intent.schema.TradeSpec`: every
raw entry tier / TP tranche is carried through IN ORDER (including
non-positive ``limit``/``target`` rows — sanitisation is the money half's
job, not the parse half's), the ``order_ttl_days`` 0-sentinel survives, and
unplannable briefs still raise :class:`TradeSetupNotPlannableError`.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.paper.sizing import TradeSetupNotPlannableError, parse_brief_to_spec
from alphalens_pipeline.trade_intent.schema import EntryTierSpec, TpTrancheSpec, TradeSpec


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
    if entry_tiers is None:
        entry_tiers = [
            {"limit": 100.0, "alloc_pct": 50.0, "atr_distance": 0.0, "tag": "tier-0"},
            {"limit": 95.0, "alloc_pct": 30.0, "atr_distance": 1.0, "tag": "tier-1"},
            {"limit": 0.0, "alloc_pct": 20.0, "atr_distance": 2.0, "tag": "tier-2-bad"},
        ]
    if tp_tranches is None:
        tp_tranches = [
            {"target": 110.0, "tranche_pct": 50.0, "r_multiple": 1.0, "tag": "tp-1"},
            {"target": 0.0, "tranche_pct": 50.0, "r_multiple": 2.0, "tag": "tp-2-bad"},
        ]
    setup: dict = {
        "schema_version": schema_version,
        "status": status,
        "asof_close": 100.0,
        "atr": 1.5,
        "disaster_stop": disaster_stop,
        "suggested_size_pct": suggested_size_pct,
        "entry_tiers": entry_tiers,
        "tp_tranches": tp_tranches,
    }
    if order_ttl_days is not None:
        setup["order_ttl_days"] = order_ttl_days
    return setup


class TestFieldMapping(unittest.TestCase):
    def test_returns_a_tradespec(self):
        spec = parse_brief_to_spec(_make_setup())
        self.assertIsInstance(spec, TradeSpec)

    def test_side_is_long(self):
        spec = parse_brief_to_spec(_make_setup())
        self.assertEqual(spec.side, "long")

    def test_disaster_stop_and_suggested_size_pct_carried(self):
        spec = parse_brief_to_spec(_make_setup(disaster_stop=88.5, suggested_size_pct=4.2))
        self.assertEqual(spec.disaster_stop, 88.5)
        self.assertEqual(spec.suggested_size_pct, 4.2)

    def test_all_entry_tiers_carried_in_order_including_non_positive_limit(self):
        """Sanitisation (dropping limit<=0) is the money half's job — parse
        keeps every tier, incl. the bad tier-2 (limit=0.0)."""
        spec = parse_brief_to_spec(_make_setup())
        self.assertEqual(len(spec.entry_tiers), 3)
        self.assertEqual(
            spec.entry_tiers,
            (
                EntryTierSpec(limit_price=100.0, alloc_pct=50.0, tag="tier-0"),
                EntryTierSpec(limit_price=95.0, alloc_pct=30.0, tag="tier-1"),
                EntryTierSpec(limit_price=0.0, alloc_pct=20.0, tag="tier-2-bad"),
            ),
        )

    def test_all_tp_tranches_carried_in_order_including_non_positive_target(self):
        spec = parse_brief_to_spec(_make_setup())
        self.assertEqual(len(spec.tp_tranches), 2)
        self.assertEqual(
            spec.tp_tranches,
            (
                TpTrancheSpec(price=110.0, tranche_pct=50.0, r_multiple=1.0, tag="tp-1"),
                TpTrancheSpec(price=0.0, tranche_pct=50.0, r_multiple=2.0, tag="tp-2-bad"),
            ),
        )

    def test_tp_tranches_empty_when_brief_omits_them(self):
        setup = _make_setup()
        setup["tp_tranches"] = []
        spec = parse_brief_to_spec(setup)
        self.assertEqual(spec.tp_tranches, ())

    def test_order_ttl_days_present_is_preserved(self):
        spec = parse_brief_to_spec(_make_setup(order_ttl_days=7))
        self.assertEqual(spec.order_ttl_days, 7)

    def test_order_ttl_days_zero_sentinel_when_brief_omits_it(self):
        """CRITICAL: a brief with no order_ttl_days must parse to the 0
        sentinel, NOT fall through to TradeSpec's own default (7) — the
        planner distinguishes "explicit 7-day TTL" from "no TTL info"."""
        spec = parse_brief_to_spec(_make_setup(order_ttl_days=None))
        self.assertEqual(spec.order_ttl_days, 0)

    def test_order_ttl_days_zero_sentinel_when_brief_has_falsy_zero(self):
        spec = parse_brief_to_spec(_make_setup(order_ttl_days=0))
        self.assertEqual(spec.order_ttl_days, 0)


class TestUnplannableBriefsStillRaise(unittest.TestCase):
    def test_status_not_ok_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            parse_brief_to_spec(_make_setup(status="NO_STRUCTURE"))

    def test_unknown_schema_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            parse_brief_to_spec(_make_setup(schema_version="2.0.0"))

    def test_missing_suggested_size_rejected(self):
        setup = _make_setup()
        setup["suggested_size_pct"] = None
        with self.assertRaises(TradeSetupNotPlannableError):
            parse_brief_to_spec(setup)

    def test_missing_disaster_stop_rejected(self):
        setup = _make_setup()
        setup["disaster_stop"] = None
        with self.assertRaises(TradeSetupNotPlannableError):
            parse_brief_to_spec(setup)

    def test_empty_entry_tiers_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            parse_brief_to_spec(_make_setup(entry_tiers=[]))

    def test_all_tiers_non_positive_limit_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            parse_brief_to_spec(
                _make_setup(entry_tiers=[{"limit": 0.0, "alloc_pct": 100.0, "tag": "bad"}])
            )

    def test_non_dict_rejected(self):
        with self.assertRaises(TradeSetupNotPlannableError):
            parse_brief_to_spec("not a dict")


if __name__ == "__main__":
    unittest.main()

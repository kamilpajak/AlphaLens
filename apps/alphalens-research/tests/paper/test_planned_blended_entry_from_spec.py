"""Tests pinning ``planned_blended_entry_from_spec`` against ``planned_blended_entry``.

PR-7 (memo ``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
section 5): the daemon's geometry SHADOW stamp loses the brief dict at drain
time (arm-time now owns the parse) but still needs the planned blend, so it
consumes the already-parsed :class:`TradeSpec`. This must return the SAME
value as the dict-based ``planned_blended_entry`` for the equivalent spec —
``parse_brief_to_spec(setup)`` fed to ``planned_blended_entry_from_spec`` must
equal ``planned_blended_entry(setup)``.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.paper.sizing import (
    parse_brief_to_spec,
    planned_blended_entry,
    planned_blended_entry_from_spec,
)


def _setup(*, entry_tiers) -> dict:
    return {
        "schema_version": "1.0.0",
        "status": "OK",
        "disaster_stop": 90.0,
        "suggested_size_pct": 5.0,
        "entry_tiers": entry_tiers,
    }


class TestParityWithDictVersion(unittest.TestCase):
    def _assert_parity(self, entry_tiers: list[dict]) -> None:
        setup = _setup(entry_tiers=entry_tiers)
        spec = parse_brief_to_spec(setup)
        expected = planned_blended_entry(setup)
        actual = planned_blended_entry_from_spec(spec)
        if expected is None:
            self.assertIsNone(actual)
        else:
            self.assertIsNotNone(actual)
            self.assertAlmostEqual(actual, expected)

    def test_normal_tiers(self) -> None:
        self._assert_parity(
            [
                {"limit": 100.0, "alloc_pct": 60.0, "tag": "T1"},
                {"limit": 98.0, "alloc_pct": 40.0, "tag": "T2"},
            ]
        )

    def test_tier_with_non_positive_limit_dropped(self) -> None:
        self._assert_parity(
            [
                {"limit": 100.0, "alloc_pct": 50.0, "tag": "T1"},
                {"limit": 0.0, "alloc_pct": 50.0, "tag": "T2-bad"},
            ]
        )

    def test_all_zero_alloc_equal_weight_fallback(self) -> None:
        self._assert_parity(
            [
                {"limit": 100.0, "alloc_pct": 0.0, "tag": "T1"},
                {"limit": 98.0, "alloc_pct": 0.0, "tag": "T2"},
            ]
        )

    def test_empty_after_sanitisation_returns_none(self) -> None:
        # parse_brief_to_spec would reject all-non-positive-limit tiers at
        # validate_trade_setup, so this exercises the standalone function via
        # a spec built directly with a single non-positive tier.
        from broker_contract.trade_intent.schema import EntryTierSpec, TradeSpec

        spec = TradeSpec(
            entry_tiers=(EntryTierSpec(limit_price=0.0, alloc_pct=100.0, tag="bad"),),
            disaster_stop=90.0,
            tp_tranches=(),
            suggested_size_pct=5.0,
        )
        self.assertIsNone(planned_blended_entry_from_spec(spec))

    def test_no_entry_tiers_returns_none(self) -> None:
        from broker_contract.trade_intent.schema import TradeSpec

        spec = TradeSpec(
            entry_tiers=(),
            disaster_stop=90.0,
            tp_tranches=(),
            suggested_size_pct=5.0,
        )
        self.assertIsNone(planned_blended_entry_from_spec(spec))


if __name__ == "__main__":
    unittest.main()

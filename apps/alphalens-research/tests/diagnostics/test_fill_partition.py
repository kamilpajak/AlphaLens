"""The filled-tier partition and its reachability, for issue #1113.

The instrument under test measures ladder outcomes CONDITIONAL on which entry
tiers filled. This file pins the partition itself: the map from a filled-tier
SET to a named partition, table-driven over the whole power set of a three-tier
ladder, plus the property of the offline replay fill model that makes one of the
four partitions unreachable offline.
"""

from __future__ import annotations

import itertools
import unittest

from alphalens_pipeline.feedback.ladder_replay import replay_ladder
from alphalens_research.diagnostics import fill_partition as fp


def _three_tier_setup() -> dict:
    """A plain three-tier ladder: E1 100, E2 97, E3 95, disaster stop 90."""
    return {
        "status": "OK",
        "disaster_stop": 90.0,
        "entry_tiers": [
            {"limit": 100.0, "alloc_pct": 20.0},
            {"limit": 97.0, "alloc_pct": 30.0},
            {"limit": 95.0, "alloc_pct": 50.0},
        ],
        "tp_tranches": [{"target": 130.0, "tranche_pct": 100.0}],
    }


def _bar(ts_ms: int, low: float, high: float, close: float) -> dict:
    return {"t": ts_ms, "l": low, "h": high, "c": close, "o": close}


class TestPartitionOfFilledTierSet(unittest.TestCase):
    """Table-driven over every subset of a three-tier ladder (issue #1113)."""

    EXPECTED = {
        frozenset(): fp.PARTITION_UNFILLED,
        frozenset({"E1"}): fp.PARTITION_FIRST_ONLY,
        frozenset({"E1", "E2"}): fp.PARTITION_MIXED,
        frozenset({"E1", "E3"}): fp.PARTITION_MIXED,
        frozenset({"E1", "E2", "E3"}): fp.PARTITION_MIXED,
        frozenset({"E2"}): fp.PARTITION_DEEP_ONLY,
        frozenset({"E3"}): fp.PARTITION_DEEP_ONLY,
        frozenset({"E2", "E3"}): fp.PARTITION_DEEP_ONLY,
    }

    def test_every_subset_of_a_three_tier_ladder_maps_to_a_named_partition(self) -> None:
        for subset, expected in self.EXPECTED.items():
            with self.subTest(filled=sorted(subset)):
                self.assertEqual(fp.partition_of(subset), expected)

    def test_the_table_covers_the_powerset_exactly(self) -> None:
        table = fp.partition_table()
        self.assertEqual(len(table), 2 ** len(fp.TIER_IDS))
        self.assertEqual(set(table), set(self.EXPECTED))
        self.assertEqual(table, self.EXPECTED)

    def test_an_unknown_level_id_raises_rather_than_falling_into_a_bucket(self) -> None:
        with self.assertRaises(ValueError):
            fp.partition_of({"E1", "TP1"})

    def test_every_partition_name_is_declared_in_the_public_tuple(self) -> None:
        self.assertEqual(set(self.EXPECTED.values()) | {fp.PARTITION_UNFILLED}, set(fp.PARTITIONS))


class TestReachabilityUnderTheReplayFillModel(unittest.TestCase):
    """The offline bar walk can only produce PREFIX fill sets.

    ``_LadderWalk._fill_entries`` fills every unfilled tier whose limit satisfies
    ``low <= limit`` on the same bar, and the builder emits E1 > E2 > E3, so any
    low that reaches E2 has already reached E1. DEEP_ONLY is therefore structurally
    empty offline. It is NOT empty live: #1112 added an arm gate that refuses the
    SHALLOW tier when its own take-profit target sits below a realistic fill, which
    leaves exactly a deep-only fill set on the money rail.
    """

    def test_the_bar_walk_can_only_produce_prefix_sets(self) -> None:
        setup = _three_tier_setup()
        cases = (
            (98.0, ("E1",)),
            (96.0, ("E1", "E2")),
            (94.0, ("E1", "E2", "E3")),
        )
        for low, expected in cases:
            with self.subTest(low=low):
                out = replay_ladder(setup, [_bar(0, low, low + 1.0, low + 0.5)])
                self.assertEqual(out.entries_filled, expected)
                self.assertTrue(fp.is_prefix_fill_set(out.entries_filled))
                self.assertNotEqual(fp.partition_of(out.entries_filled), fp.PARTITION_DEEP_ONLY)

    def test_deep_only_is_a_real_partition_the_live_rail_can_produce(self) -> None:
        self.assertEqual(fp.partition_of({"E2", "E3"}), fp.PARTITION_DEEP_ONLY)
        self.assertFalse(fp.is_prefix_fill_set(("E2", "E3")))

    def test_the_offline_unreachable_partitions_are_named_so_a_zero_cell_carries_its_reason(
        self,
    ) -> None:
        self.assertEqual(fp.OFFLINE_UNREACHABLE_PARTITIONS, (fp.PARTITION_DEEP_ONLY,))

    def test_prefix_detection_agrees_with_a_brute_force_enumeration(self) -> None:
        prefixes = {
            frozenset(fp.TIER_IDS[:k]) for k in range(len(fp.TIER_IDS) + 1)
        }  # {}, {E1}, {E1,E2}, {E1,E2,E3}
        for r in range(len(fp.TIER_IDS) + 1):
            for subset in itertools.combinations(fp.TIER_IDS, r):
                with self.subTest(filled=subset):
                    self.assertEqual(fp.is_prefix_fill_set(subset), frozenset(subset) in prefixes)


if __name__ == "__main__":
    unittest.main()

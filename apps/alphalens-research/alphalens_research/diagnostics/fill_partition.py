"""Ladder outcomes conditional on WHICH entry tiers filled (issue #1113) -- no I/O.

The headline ladder metric is anchored to the blended average entry, which
averages away the one fact this measurement is about: which tiers actually
filled. A buy limit below the market is more likely to fill while price is
moving down toward it, so a fill is itself evidence the market moved against the
position. This module is the INSTRUMENT for that question, not an answer to it:
it ships the partition, the denominator and the fill model. The comparison is
pre-registered separately in issue #1115 and no number is reported here.

Pure and deterministic: every function takes already-loaded values and returns a
report. Reading the population-ladder store, the briefs and the cached minute
bars lives in ``apps/alphalens-research/scripts/measure_fill_partition.py``,
mirroring the ``nofill`` / ``fill_survival`` split in this package.

Three things this module refuses to do, each because the issue names it:

* It does not assume a bar low implies a fill -- :data:`FILL_MODELS` offers the
  replay engine's TOUCH model and a slippage-adverse THROUGH model, and the
  caller must pick one.
* It does not assume the whole ladder fills -- the walk records a per-tier fill
  set with timestamps, and the alloc-weighted :func:`filled_fraction` is the
  weight, never the tier count.
* It does not price a fill at the tier limit -- see
  :data:`ENTRY_TRAIL_OVERSHOOT_BPS`.

Bracket anchors (issue #1114): this instrument does NOT replay ATR brackets, so
it needs no ``AnchorMode``. It measures the ENTRY ladder exactly as the brief
specifies it, which is the layer issue #1113 is about; pulling the exit-side
bracket in would make the numbers depend on a take-profit policy that #1112
already moved on the live rail but deliberately did not retro-fit into the
replay.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable

# --------------------------------------------------------------------------
# The partition over the filled-tier set.
# --------------------------------------------------------------------------

TIER_IDS: tuple[str, ...] = ("E1", "E2", "E3")
"""The three entry tiers a brief_trade_setup emits, shallowest (E1) first."""

PARTITION_UNFILLED = "unfilled"
"""Nothing filled. The opportunity still counts -- see :class:`PartitionReport`."""

PARTITION_FIRST_ONLY = "first_only"
"""Only the shallowest tier filled."""

PARTITION_MIXED = "mixed"
"""The shallowest tier filled AND at least one deeper tier did."""

PARTITION_DEEP_ONLY = "deep_only"
"""At least one deeper tier filled and the shallowest did NOT."""

PARTITIONS: tuple[str, ...] = (
    PARTITION_UNFILLED,
    PARTITION_FIRST_ONLY,
    PARTITION_MIXED,
    PARTITION_DEEP_ONLY,
)

OFFLINE_UNREACHABLE_PARTITIONS: tuple[str, ...] = (PARTITION_DEEP_ONLY,)
"""Partitions the OFFLINE replay can never populate, so an ``n = 0`` cell for one
of them is a structural zero and not a measurement.

``ladder_replay._LadderWalk._fill_entries`` fills every unfilled tier whose limit
satisfies ``low <= limit`` on the same bar, and tiers descend in price, so only
the prefix sets can occur. Deep-only IS reachable on the live rail: #1112's
``arms_inside_exit_region`` refuses the SHALLOW tier when its own take-profit
target sits at or below a realistic fill, leaving the deeper tiers armed alone.
"""


def partition_of(filled: Iterable[str]) -> str:
    """Name the partition of a filled-tier set. Raises on an unknown level id."""
    ids = frozenset(filled)
    unknown = ids - frozenset(TIER_IDS)
    if unknown:
        raise ValueError(f"not entry tier ids: {sorted(unknown)}")
    if not ids:
        return PARTITION_UNFILLED
    if TIER_IDS[0] not in ids:
        return PARTITION_DEEP_ONLY
    return PARTITION_FIRST_ONLY if len(ids) == 1 else PARTITION_MIXED


def partition_table() -> dict[frozenset[str], str]:
    """The whole power set of :data:`TIER_IDS` mapped to its partition.

    Materialised so a test can assert coverage is EXACTLY the power set; a map
    that silently loses a subset would send those opportunities nowhere.
    """
    table: dict[frozenset[str], str] = {}
    for r in range(len(TIER_IDS) + 1):
        for subset in itertools.combinations(TIER_IDS, r):
            table[frozenset(subset)] = partition_of(subset)
    return table


def is_prefix_fill_set(filled: Iterable[str]) -> bool:
    """True when the filled set is a PREFIX of :data:`TIER_IDS` (E1, then E2, ...).

    The offline bar walk can only produce prefix sets; see
    :data:`OFFLINE_UNREACHABLE_PARTITIONS`.
    """
    ids = frozenset(filled)
    unknown = ids - frozenset(TIER_IDS)
    if unknown:
        raise ValueError(f"not entry tier ids: {sorted(unknown)}")
    return ids == frozenset(TIER_IDS[: len(ids)])

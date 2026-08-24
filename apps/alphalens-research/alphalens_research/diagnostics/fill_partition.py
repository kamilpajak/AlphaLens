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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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


# --------------------------------------------------------------------------
# The fill PRICE: measured trailing overshoot, never the bare tier limit.
# --------------------------------------------------------------------------

BPS_PER_UNIT = 1e4

# Measured on the one live trailing-entry round trip so far: SMG, 2026-08-24,
# from ~/.alphalens/broker_orders/live/entry_trails.jsonl on the VPS. The native
# trailing BUY rested against tier limit 59.786017 and filled at 59.9261 -- ABOVE
# its own limit. Both prices are also carried by tests/incident_1112_fixture.py,
# which is where a test re-derives the constant below from an independent copy.
#
# Issue #1113's prose calls this "+40 bps"; the two prices it quotes give 23.43,
# so the prices win (the same call tests/incident_1112_fixture.py already made).
SMG_TRAIL_TIER_LIMIT = 59.786017
SMG_TRAIL_FILL_PRICE = 59.9261

ENTRY_TRAIL_OVERSHOOT_BPS = (
    (SMG_TRAIL_FILL_PRICE - SMG_TRAIL_TIER_LIMIT) / SMG_TRAIL_TIER_LIMIT * BPS_PER_UNIT
)
"""How far above its own tier limit a trailing entry actually filled, in bps.

N = 1. One live round trip is a point calibration, not a distribution, which is
why :data:`OVERSHOOT_ARMS_BPS` exists and why every walk must state its arm.
"""

# The broker-enforced StopLimit CEILING for the same incident (#1112's
# entry_trail_geometry.entry_fill_estimate with reference = trough = the 59.77
# touch bid and d_bps = 50): an UPPER BOUND on where the order could have filled,
# not an expected fill. Kept as a separate, deliberately conservative arm so the
# two are never conflated.
SMG_TRAIL_CEILING_PRICE = 60.1889877

OVERSHOOT_ARM_LIMIT = "limit"
"""0 bps -- fill AT the tier limit, the assumption ``ladder_replay`` makes today."""

OVERSHOOT_ARM_MEASURED = "measured"
"""The single measured live overshoot (:data:`ENTRY_TRAIL_OVERSHOOT_BPS`)."""

OVERSHOOT_ARM_CEILING = "ceiling"
"""#1112's StopLimit ceiling over the same tier limit -- a conservative bound."""

OVERSHOOT_ARMS: tuple[str, ...] = (
    OVERSHOOT_ARM_LIMIT,
    OVERSHOOT_ARM_MEASURED,
    OVERSHOOT_ARM_CEILING,
)

OVERSHOOT_ARMS_BPS: dict[str, float] = {
    OVERSHOOT_ARM_LIMIT: 0.0,
    OVERSHOOT_ARM_MEASURED: ENTRY_TRAIL_OVERSHOOT_BPS,
    OVERSHOOT_ARM_CEILING: (
        (SMG_TRAIL_CEILING_PRICE - SMG_TRAIL_TIER_LIMIT) / SMG_TRAIL_TIER_LIMIT * BPS_PER_UNIT
    ),
}


def fill_price_from_limit(limit: float, overshoot_bps: float) -> float:
    """Estimated BUY fill price for a tier resting at ``limit``.

    ``overshoot_bps`` is required, with no default: an N = 1 calibration must be
    chosen by the caller, never inherited silently. A negative value is refused --
    a buy filling BELOW its own limit is price improvement, a different (and
    flattering) claim than overshoot.
    """
    if overshoot_bps < 0.0:
        raise ValueError(f"overshoot_bps must be >= 0, got {overshoot_bps}")
    return limit * (1.0 + overshoot_bps / BPS_PER_UNIT)


# --------------------------------------------------------------------------
# The fill CONDITION: a bar low does not have to imply a fill.
# --------------------------------------------------------------------------

TICK_USD = 0.01
"""US equity tick, mirroring ``scripts/whatif_trailing_entry.py``'s ``TICK``."""

FILL_MODEL_TOUCH = "touch"
"""``low <= limit`` -- exactly what ``ladder_replay._LadderWalk._fill_entries``
assumes. Optimistic: a bar whose low merely REACHES a resting limit may not have
traded enough size there to fill it."""

FILL_MODEL_THROUGH = "through"
"""``low <= limit - tick`` -- the bar must trade THROUGH the limit. The
slippage-adverse model ``scripts/whatif_trailing_entry.py`` already uses."""

FILL_MODELS: tuple[str, ...] = (FILL_MODEL_TOUCH, FILL_MODEL_THROUGH)


def tier_fills(*, low: float, limit: float, fill_model: str, tick: float = TICK_USD) -> bool:
    """Did a bar with this ``low`` fill a tier resting at ``limit``?"""
    if fill_model == FILL_MODEL_TOUCH:
        return low <= limit
    if fill_model == FILL_MODEL_THROUGH:
        return low <= limit - tick
    raise ValueError(f"unknown fill_model {fill_model!r}; expected one of {FILL_MODELS}")


# --------------------------------------------------------------------------
# The partial-fill walk: which tiers filled, WHEN, and at what price.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryTier:
    """One intended entry tier of a ``brief_trade_setup``."""

    tier_id: str
    limit: float
    alloc_pct: float


@dataclass(frozen=True)
class TierFill:
    """One tier that actually filled, with the bar it filled on."""

    tier_id: str
    limit: float
    alloc_pct: float
    fill_price: float
    bar_ts_ms: int


def entry_tiers_from_setup(trade_setup: Mapping[str, Any] | None) -> tuple[EntryTier, ...]:
    """Pull the entry tiers out of a ``brief_trade_setup``, in brief order.

    Deliberately does NOT re-implement ``ladder_replay.parse_ladder``'s
    plannability gate: the store already records plannability per row, and this
    instrument's exclusion buckets own that decision.
    """
    raw = (trade_setup or {}).get("entry_tiers") or []
    return tuple(
        EntryTier(
            tier_id=f"E{i + 1}",
            limit=float(t["limit"]),
            alloc_pct=float(t.get("alloc_pct", 0.0)),
        )
        for i, t in enumerate(raw)
    )


def walk_entry_fills(
    tiers: Sequence[EntryTier],
    bars: Sequence[Mapping[str, Any]],
    *,
    fill_model: str,
    overshoot_bps: float,
    entry_expiry_ms: int | None = None,
    tick: float = TICK_USD,
) -> tuple[TierFill, ...]:
    """Walk the bars and record which tiers filled, when, and at what price.

    Partial fills are the normal case, not the exception: every tier is tested
    independently on every bar, so a path that reaches E1 and stops leaves E2/E3
    unfilled. Several tiers CAN fill inside one bar (a gap down through them all),
    and they then share that bar's timestamp -- which is how
    :func:`conditional_fill_records` separates "deeper tier filled later" from
    "deeper tier filled in the same minute".

    ``entry_expiry_ms`` mirrors the engine's entry TTL: a limit reached at or
    after the cutoff is stale and does not fill. Bars are sorted by ``t``
    defensively, the same way ``replay_ladder`` does it.
    """
    if fill_model not in FILL_MODELS:
        raise ValueError(f"unknown fill_model {fill_model!r}; expected one of {FILL_MODELS}")
    fills: list[TierFill] = []
    filled_ids: set[str] = set()
    for bar in sorted(bars, key=lambda b: int(b["t"])):
        ts = int(bar["t"])
        if entry_expiry_ms is not None and ts >= entry_expiry_ms:
            break
        low = float(bar["l"])
        for tier in tiers:
            if tier.tier_id in filled_ids:
                continue
            if not tier_fills(low=low, limit=tier.limit, fill_model=fill_model, tick=tick):
                continue
            filled_ids.add(tier.tier_id)
            fills.append(
                TierFill(
                    tier_id=tier.tier_id,
                    limit=tier.limit,
                    alloc_pct=tier.alloc_pct,
                    fill_price=fill_price_from_limit(tier.limit, overshoot_bps),
                    bar_ts_ms=ts,
                )
            )
    return tuple(fills)


def filled_fraction(tiers: Sequence[EntryTier], filled_ids: Iterable[str]) -> float:
    """Alloc-weighted share of the FULL intended position that filled, in [0, 1].

    Mirrors ``ladder_replay._filled_frac``, including its equal-weight fallback
    for a ladder carrying no alloc weights. This is the weight a partition must
    use: on SMG's real allocs a top-tier-only fill deploys 21.07% of the intended
    position, not one third of it.
    """
    if not tiers:
        return 0.0
    ids = frozenset(filled_ids)
    total = sum(t.alloc_pct for t in tiers)
    if total > 0:
        frac = sum(t.alloc_pct for t in tiers if t.tier_id in ids) / total
    else:
        frac = sum(1 for t in tiers if t.tier_id in ids) / len(tiers)
    return min(max(frac, 0.0), 1.0)


def blended_fill_price(fills: Sequence[TierFill]) -> float | None:
    """Alloc-weighted blend over the OVERSHOOT-adjusted fill prices.

    ``None`` when nothing filled. Mirrors ``ladder_replay._blended_entry``
    (alloc weights, equal-weight fallback when they are absent or zero) but over
    the overshoot-adjusted prices rather than the tier limits -- which is the
    whole point: the limit blend is the number the store already carries.
    """
    if not fills:
        return None
    wsum = sum(f.alloc_pct for f in fills)
    if wsum > 0:
        return sum(f.fill_price * f.alloc_pct for f in fills) / wsum
    return sum(f.fill_price for f in fills) / len(fills)

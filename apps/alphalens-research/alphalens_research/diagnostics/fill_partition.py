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
import math
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


# --------------------------------------------------------------------------
# The DENOMINATOR: one report row per OPPORTUNITY, not per taken trade.
# --------------------------------------------------------------------------

EXCLUDE_NOT_PLANNABLE = "not_plannable"
"""The monitor never planned this candidate (no verified brief trade setup)."""

EXCLUDE_NOT_DECIDED = "not_decided"
"""The entry-TTL window is still open, so the filled-tier set can still grow.

Counting such a row now would understate every conditional fill rate -- the
immortal-time trap named in ``scripts/ml/2026_07_ladder_fill_depth_cr.py``.
"""

EXCLUDE_NO_REPLAY = "no_replay"
"""A plannable row that was never priced (placeholder / no cached bars)."""

EXCLUDE_SPLIT_INVALIDATED = "split_invalidated"
"""The replay window crossed a real corporate action, so the levels are stale."""

EXCLUDE_BAD_GEOMETRY = "bad_geometry"
"""Stop at or above the blended entry -- R units are undefined for the row."""

EXCLUSION_REASONS: tuple[str, ...] = (
    EXCLUDE_NOT_PLANNABLE,
    EXCLUDE_NOT_DECIDED,
    EXCLUDE_NO_REPLAY,
    EXCLUDE_SPLIT_INVALIDATED,
    EXCLUDE_BAD_GEOMETRY,
)


@dataclass(frozen=True)
class Opportunity:
    """One brief pick, filled or not. The unit of the denominator.

    ``realised_return`` is the return on the capital that was actually deployed
    and is ``None`` when nothing filled -- deliberately NOT 0.0, because a zero
    would fold an unfilled pick in as a costless miss, which is exactly the
    reading issue #1113 forbids. ``forgone_return`` is the market move over the
    same window and is the opportunity cost that makes the unfilled row readable.
    """

    brief_date: str
    ticker: str
    excluded_reason: str | None
    filled_tiers: tuple[str, ...]
    fill_bar_ts_ms: tuple[int, ...]
    filled_fraction: float
    realised_return: float | None
    forgone_return: float | None
    holding_days: int | None
    mae_r: float | None


@dataclass(frozen=True)
class PartitionStats:
    """One partition cell. Every count that could hide a dropped row is named."""

    partition: str
    n: int
    share_of_opportunities: float
    n_realised: int
    n_missing_realised: int
    realised_return_mean: float | None
    realised_return_median: float | None
    win_rate: float | None
    n_forgone: int
    n_missing_forgone: int
    forgone_return_mean: float | None
    forgone_return_median: float | None
    holding_days_median: float | None
    n_missing_holding: int
    mae_r_median: float | None
    n_missing_mae: int
    filled_fraction_median: float | None
    no_capital_deployed: bool
    offline_unreachable: bool


@dataclass(frozen=True)
class ConditionalFill:
    """Given tier k filled, how often did tier k+1 fill, and what happened next."""

    given_tier: str
    then_tier: str
    n_given: int
    n_then: int
    n_then_same_bar: int
    n_then_later: int
    then_realised_return_median: float | None
    n_missing_then_realised: int

    @property
    def rate(self) -> float | None:
        """P(tier k+1 filled | tier k filled). ``None`` on an empty denominator."""
        return self.n_then / self.n_given if self.n_given else None


@dataclass(frozen=True)
class PartitionReport:
    """The instrument's output. Carries no verdict -- issue #1115 owns that."""

    n_store_rows: int
    n_opportunities: int
    excluded: dict[str, int]
    partitions: tuple[PartitionStats, ...]
    conditional_fills: tuple[ConditionalFill, ...]
    fill_model: str
    overshoot_arm: str
    overshoot_bps: float


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _present(values: Iterable[float | None]) -> list[float]:
    return [v for v in values if v is not None]


def _partition_stats(
    partition: str, members: Sequence[Opportunity], n_opportunities: int
) -> PartitionStats:
    realised = _present(o.realised_return for o in members)
    forgone = _present(o.forgone_return for o in members)
    holding = _present(o.holding_days for o in members)
    mae = _present(o.mae_r for o in members)
    return PartitionStats(
        partition=partition,
        n=len(members),
        share_of_opportunities=(len(members) / n_opportunities) if n_opportunities else 0.0,
        n_realised=len(realised),
        n_missing_realised=len(members) - len(realised),
        realised_return_mean=_mean(realised),
        realised_return_median=_median(realised),
        win_rate=(sum(1 for r in realised if r > 0) / len(realised)) if realised else None,
        n_forgone=len(forgone),
        n_missing_forgone=len(members) - len(forgone),
        forgone_return_mean=_mean(forgone),
        forgone_return_median=_median(forgone),
        holding_days_median=_median(holding),
        n_missing_holding=len(members) - len(holding),
        mae_r_median=_median(mae),
        n_missing_mae=len(members) - len(mae),
        filled_fraction_median=_median([o.filled_fraction for o in members]),
        # An unfilled opportunity deployed no capital, so its realised return is
        # structurally absent rather than missing data. The flag exists so a
        # reader never mistakes the one for the other.
        no_capital_deployed=partition == PARTITION_UNFILLED,
        offline_unreachable=partition in OFFLINE_UNREACHABLE_PARTITIONS,
    )


def _conditional_fill(
    given: str, then: str, opportunities: Sequence[Opportunity]
) -> ConditionalFill:
    n_given = n_then = same_bar = later = 0
    then_returns: list[float | None] = []
    for opp in opportunities:
        ts_by_tier = dict(zip(opp.filled_tiers, opp.fill_bar_ts_ms, strict=True))
        if given not in ts_by_tier:
            continue
        n_given += 1
        if then not in ts_by_tier:
            continue
        n_then += 1
        then_returns.append(opp.realised_return)
        if ts_by_tier[then] > ts_by_tier[given]:
            later += 1
        else:
            same_bar += 1
    present = _present(then_returns)
    return ConditionalFill(
        given_tier=given,
        then_tier=then,
        n_given=n_given,
        n_then=n_then,
        n_then_same_bar=same_bar,
        n_then_later=later,
        then_realised_return_median=_median(present),
        n_missing_then_realised=len(then_returns) - len(present),
    )


def partition_report(
    opportunities: Sequence[Opportunity], *, fill_model: str, overshoot_arm: str
) -> PartitionReport:
    """Aggregate opportunities into the partitioned report.

    ``opportunities`` is the WHOLE store slice, excluded rows included, so the
    identity ``n_store_rows == n_opportunities + sum(excluded.values())`` holds by
    construction and a silently dropped row cannot hide.
    """
    if fill_model not in FILL_MODELS:
        raise ValueError(f"unknown fill_model {fill_model!r}; expected one of {FILL_MODELS}")
    if overshoot_arm not in OVERSHOOT_ARMS_BPS:
        raise ValueError(
            f"unknown overshoot_arm {overshoot_arm!r}; expected one of {OVERSHOOT_ARMS}"
        )

    excluded = dict.fromkeys(EXCLUSION_REASONS, 0)
    kept: list[Opportunity] = []
    for opp in opportunities:
        if opp.excluded_reason is None:
            kept.append(opp)
            continue
        if opp.excluded_reason not in excluded:
            raise ValueError(
                f"undeclared exclusion reason {opp.excluded_reason!r}; "
                f"expected one of {EXCLUSION_REASONS}"
            )
        excluded[opp.excluded_reason] += 1

    by_partition: dict[str, list[Opportunity]] = {p: [] for p in PARTITIONS}
    for opp in kept:
        by_partition[partition_of(opp.filled_tiers)].append(opp)

    return PartitionReport(
        n_store_rows=len(opportunities),
        n_opportunities=len(kept),
        excluded=excluded,
        partitions=tuple(_partition_stats(p, by_partition[p], len(kept)) for p in PARTITIONS),
        conditional_fills=tuple(
            _conditional_fill(TIER_IDS[i], TIER_IDS[i + 1], kept) for i in range(len(TIER_IDS) - 1)
        ),
        fill_model=fill_model,
        overshoot_arm=overshoot_arm,
        overshoot_bps=OVERSHOOT_ARMS_BPS[overshoot_arm],
    )


# --------------------------------------------------------------------------
# Re-anchoring the stored MAE to the overshoot fill.
# --------------------------------------------------------------------------


def mae_r_at_fill(
    *, stop_distance_pct: float | None, mae_pct: float | None, overshoot_bps: float
) -> float | None:
    """Maximum adverse excursion in R, anchored to the FILL rather than the limit.

    The store's ``mae`` is anchored to the tier-limit blend. Once the fill is
    known to land above that blend, both the numerator (distance travelled
    against the position) and the denominator (risk per share) change, and the
    two stored columns are enough to recompute it with no re-replay::

        stop         = blend * (1 - stop_distance_pct)
        in_trade_low = blend * (1 + mae_pct)
        fill         = blend * (1 + overshoot)
        mae_r        = (in_trade_low - fill) / (fill - stop)
                     = (mae_pct - overshoot) / (overshoot + stop_distance_pct)

    The blend cancels, so this needs only the two fractions. Callers MUST restrict
    it to TERMINAL rows: the identity holds on a settled row and breaks on an
    ongoing one, where the stored pair is a snapshot of a still-moving position.

    The direction is not uniformly adverse. An excursion shallower than the stop
    gets WORSE under the fill anchor; an excursion exactly at the stop is -1.0 R
    under either anchor (touching your own stop is one R by definition); an
    excursion past the stop is slightly LESS negative, because the fill anchor
    widens the risk unit. ``None`` on a missing or non-positive stop distance.
    """
    if mae_pct is None or stop_distance_pct is None or stop_distance_pct <= 0.0:
        return None
    overshoot = overshoot_bps / BPS_PER_UNIT
    return (mae_pct - overshoot) / (overshoot + stop_distance_pct)


def realized_r_at_fill(
    *, realized_r: float | None, stop_distance_pct: float | None, overshoot_bps: float
) -> float | None:
    """Realized R re-anchored to the overshoot FILL rather than the tier-limit blend.

    Same algebra as :func:`mae_r_at_fill`. The stored ``realized_r`` implies the
    weighted exit mark ``M = blend * (1 + realized_r * stop_distance_pct)``, and
    re-denominating that mark against the fill gives::

        realized_r' = (realized_r * stop_distance_pct - overshoot)
                      / (overshoot + stop_distance_pct)

    Two properties worth knowing before reading any number out of it: a stop-out
    stays exactly -1.0 R (you lose one risk unit whatever the anchor is), and
    every non-stop outcome moves DOWN, because the fill is worse than the blend
    and the risk unit is wider. Terminal rows only, for the same reason as
    :func:`mae_r_at_fill`.
    """
    if realized_r is None or stop_distance_pct is None or stop_distance_pct <= 0.0:
        return None
    overshoot = overshoot_bps / BPS_PER_UNIT
    return (realized_r * stop_distance_pct - overshoot) / (overshoot + stop_distance_pct)


# --------------------------------------------------------------------------
# Store rows -> opportunities.
# --------------------------------------------------------------------------

CLASSIFICATION_NO_FILL = "NO_FILL"
CLASSIFICATION_BAD_GEOMETRY = "BAD_GEOMETRY"
CLASSIFICATION_SPLIT_INVALIDATED = "SPLIT_INVALIDATED"
"""Mirrors ``corporate_actions.SPLIT_INVALIDATED_CLASSIFICATION``; kept as a local
literal so this pure module needs no import for one string."""


def _finite(value: Any) -> float | None:
    """Read a store cell as a float, treating ``None`` and NaN alike as missing.

    Parquet nulls arrive as float NaN through pandas. A NaN that reached a mean
    would poison a whole partition silently, which is exactly the failure this
    instrument exists to avoid.
    """
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    return as_float if math.isfinite(as_float) else None


def store_row_exclusion(row: Mapping[str, Any]) -> str | None:
    """Which exclusion bucket a store row falls in, or ``None`` when it counts.

    Order matters: a row can satisfy several conditions and must be counted once.
    The decidedness check is LAST so a row that is merely ongoing is reported as
    such rather than as a data problem.
    """
    if not bool(row.get("plannable")):
        return EXCLUDE_NOT_PLANNABLE
    classification = row.get("ladder_classification")
    if classification is None:
        return EXCLUDE_NO_REPLAY
    if classification == CLASSIFICATION_SPLIT_INVALIDATED:
        return EXCLUDE_SPLIT_INVALIDATED
    if classification == CLASSIFICATION_BAD_GEOMETRY:
        return EXCLUDE_BAD_GEOMETRY
    if not bool(row.get("terminal")):
        return EXCLUDE_NOT_DECIDED
    return None


def opportunity_from_store_row(
    row: Mapping[str, Any],
    *,
    fills: Sequence[TierFill],
    tiers: Sequence[EntryTier],
    overshoot_bps: float,
) -> Opportunity:
    """Build one :class:`Opportunity` from a store row plus its re-replayed fills.

    ``fills`` come from :func:`walk_entry_fills` over the cached minute bars, NOT
    from the row's ``sequence_str``: that column is order-only, so it cannot say
    whether a deeper tier filled in the same minute or three weeks later.

    Both re-anchored numbers are withheld on a non-terminal row. The identity they
    rely on (``stop = blend * (1 - stop_distance_pct)``) is exact once a row has
    settled and breaks while the position is still moving, so the honest answer
    for an ongoing row is nothing at all.
    """
    terminal = bool(row.get("terminal"))
    stop_distance_pct = _finite(row.get("stop_distance_pct"))
    holding = row.get("holding_days_elapsed")
    holding_days = None if _finite(holding) is None else int(float(holding))
    return Opportunity(
        brief_date=str(row.get("brief_date")),
        ticker=str(row.get("ticker")),
        excluded_reason=store_row_exclusion(row),
        filled_tiers=tuple(f.tier_id for f in fills),
        fill_bar_ts_ms=tuple(f.bar_ts_ms for f in fills),
        filled_fraction=filled_fraction(tiers, (f.tier_id for f in fills)),
        realised_return=(
            realized_r_at_fill(
                realized_r=_finite(row.get("realized_r")),
                stop_distance_pct=stop_distance_pct,
                overshoot_bps=overshoot_bps,
            )
            if terminal
            else None
        ),
        forgone_return=_finite(row.get("market_excess_return")),
        holding_days=holding_days,
        mae_r=(
            mae_r_at_fill(
                stop_distance_pct=stop_distance_pct,
                mae_pct=_finite(row.get("mae_pct")),
                overshoot_bps=overshoot_bps,
            )
            if terminal
            else None
        ),
    )

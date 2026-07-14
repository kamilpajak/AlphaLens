"""Pure position-sizing math for the paper-trade harness.

Translates a parsed ``brief_trade_setup`` dict into the concrete share
quantities a planner would route to Alpaca. No I/O, no Alpaca SDK reach —
this module is intentionally easy to test in isolation and easy to reason
about against the locked sizing formula in
``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §2.3 / §3.

v2 sizing math (per memo §2.3, supersedes v1's per-candidate cap):

  daily_target_notional = STEADY_STATE_GROSS_FRAC × equity
                            / EXPECTED_AVG_HOLD_DAYS
  aggregate_uncapped    = Σ_i suggested_size_pct_i / 100 × equity
                            (sum over plannable candidates today)
  scale_factor          = min(1.0, daily_target_notional / aggregate_uncapped)
  final_size_pct_i      = suggested_size_pct_i × scale_factor
  total_notional_i      = final_size_pct_i / 100 × equity
  per_tier_notional     = total_notional × (tier.alloc_pct / 100)
  per_tier_qty          = floor(per_tier_notional / tier.limit)

The scale factor preserves inter-candidate ratios while bounding aggregate
daily gross. ``compute_setup_plan`` takes the pre-computed ``scale_factor``
as an explicit argument; the planner runs a two-pass loop to derive it.

``alloc_pct`` already sums to ~100 across tiers (trade_setup §7.3); the
``total_notional × alloc_pct`` step honours the per-tier risk weighting
calibrated by the trade-setup generator.

The function does NOT skip tiers that round to 0 shares — it returns them
with ``qty=0`` so the planner can record the intent (and the reconciler in
PR 3 can decide whether to submit a zero-qty order at all). Silent skipping
would erase a real fact: that the effective size × alloc_pct can be below
the price of one share for very-low-allocation tiers at high prices, which
the analysis pipeline needs to be able to detect.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from alphalens_pipeline.paper.constants import (
    EXPECTED_AVG_HOLD_DAYS,
    STEADY_STATE_GROSS_FRAC,
)


@dataclass(frozen=True)
class TierPlan:
    """One entry-ladder tier rendered as a concrete share quantity."""

    tier_index: int
    limit_price: float
    qty: int
    alloc_pct: float
    tag: str


@dataclass(frozen=True)
class TpTranchePlan:
    """One take-profit tranche kept as a reference for the exit reconciler."""

    tranche_index: int
    target_price: float
    tranche_pct: float
    r_multiple: float
    tag: str


@dataclass(frozen=True)
class SetupPlan:
    """The full per-candidate plan: sizing scalars + ladder + exit references.

    ``scale_factor`` and ``final_size_pct`` reflect the v2 global-scaling
    decision: ``final_size_pct = suggested_size_pct × scale_factor``. The
    raw ``suggested_size_pct`` is preserved so the analysis report can
    attribute outcomes back to the brief's calibrated risk budget.
    """

    suggested_size_pct: float
    scale_factor: float
    final_size_pct: float
    total_notional: float
    paper_equity: float
    disaster_stop: float
    order_ttl_days: int
    entry_tiers: tuple[TierPlan, ...]
    tp_tranches: tuple[TpTranchePlan, ...]


class TradeSetupNotPlannableError(ValueError):
    """Raised when the brief_trade_setup cannot be turned into orders.

    Callers translate this into a shadow_log entry with a structured reason
    rather than propagating the exception (the planner is expected to handle
    many candidates, of which some are routinely unplannable).
    """


def validate_trade_setup(brief_trade_setup: dict) -> float:
    """Run the plannability checks and return ``suggested_size_pct``.

    Exposed so the planner's first pass can compute the aggregate uncapped
    notional without building a full :class:`SetupPlan` (which would require
    the not-yet-computed ``scale_factor``). The checks are the same ones
    :func:`compute_setup_plan` enforces; sharing them here avoids drift.
    """
    if not isinstance(brief_trade_setup, dict):
        raise TradeSetupNotPlannableError(
            f"brief_trade_setup is not a dict (got {type(brief_trade_setup).__name__})"
        )

    status = brief_trade_setup.get("status")
    if status != "OK":
        raise TradeSetupNotPlannableError(f"status={status!r} (only 'OK' is plannable)")

    # 1.1.0 only ADDS builder_config_version (ADR 0013); every field the planner
    # reads is unchanged, so both versions are plannable. Any other version means
    # a shape change nobody reviewed against this planner — reject loudly.
    schema = brief_trade_setup.get("schema_version")
    if schema not in ("1.0.0", "1.1.0"):
        raise TradeSetupNotPlannableError(
            f"unsupported schema_version={schema!r}; planner pinned to 1.0.0/1.1.0"
        )

    suggested_size_pct = brief_trade_setup.get("suggested_size_pct")
    if suggested_size_pct is None or suggested_size_pct <= 0:
        raise TradeSetupNotPlannableError(f"suggested_size_pct={suggested_size_pct!r} not usable")

    disaster_stop = brief_trade_setup.get("disaster_stop")
    if disaster_stop is None or disaster_stop <= 0:
        raise TradeSetupNotPlannableError(f"disaster_stop={disaster_stop!r} not usable")

    entry_tiers_raw = brief_trade_setup.get("entry_tiers") or ()
    if not entry_tiers_raw:
        raise TradeSetupNotPlannableError("entry_tiers empty")

    # Apply the same post-sanitisation tier-emptiness check that
    # :func:`compute_setup_plan` runs (it drops tiers with ``limit <= 0`` as
    # defense-in-depth). Without this alignment a candidate with all-zero-
    # limit tiers would pass pass 1 of the planner (contributing to the
    # aggregate that feeds compute_daily_scale_factor) then fail pass 2 with
    # "no usable entry tiers after sanitisation", introducing a downward
    # bias on the day's global scale factor. Per zen second-round review
    # 2026-05-28.
    usable_tiers = [
        t for t in entry_tiers_raw if isinstance(t, dict) and float(t.get("limit", 0) or 0) > 0
    ]
    if not usable_tiers:
        raise TradeSetupNotPlannableError("no usable entry tiers (all limits <= 0)")

    return float(suggested_size_pct)


def compute_daily_scale_factor(
    plannable_suggested_pcts: Iterable[float],
    paper_equity: float,
    *,
    steady_state_gross_frac: float = STEADY_STATE_GROSS_FRAC,
    expected_avg_hold_days: int = EXPECTED_AVG_HOLD_DAYS,
) -> float:
    """Daily global scale factor preserving inter-candidate ratios.

    Args:
        plannable_suggested_pcts: ``suggested_size_pct`` values from every
            candidate that passed :func:`validate_trade_setup` today
            (i.e. verified + has a plannable setup). Order does not matter.
        paper_equity: live paper-account equity in USD.

    Returns:
        ``min(1.0, daily_target / aggregate)``. When the candidate set is
        empty (no plannable candidates today) returns ``1.0`` — the value
        is moot since the planner won't apply it to anything.

    The formula computes a single multiplicative factor applied to every
    candidate's ``suggested_size_pct``. See memo §2.3 for the full
    derivation + why this preserves inter-candidate ratios (vs v1's
    per-candidate ``min(suggested, 100/N_FIXED)`` cap which flattened
    ~95% of candidates to uniform notional).
    """
    suggested_list = list(plannable_suggested_pcts)
    if not suggested_list or paper_equity <= 0:
        return 1.0
    aggregate_uncapped = sum(s / 100.0 * paper_equity for s in suggested_list)
    if aggregate_uncapped <= 0:
        return 1.0
    daily_target = steady_state_gross_frac * paper_equity / expected_avg_hold_days
    return min(1.0, daily_target / aggregate_uncapped)


def compute_setup_plan(
    *,
    brief_trade_setup: dict,
    paper_equity: float,
    scale_factor: float,
) -> SetupPlan:
    """Turn a parsed ``brief_trade_setup`` dict into a :class:`SetupPlan`.

    Args:
        brief_trade_setup: parsed JSON dict from the brief parquet row.
        paper_equity: live paper-account equity in USD.
        scale_factor: pre-computed daily scale factor from
            :func:`compute_daily_scale_factor`. Pass ``1.0`` for unit tests
            that want to inspect un-scaled sizing (rare; almost every prod
            day will scale < 1.0 given typical ``suggested_size_pct`` values).

    Raises :class:`TradeSetupNotPlannableError` for the documented
    unplannable cases (status != OK, no entry tiers, missing
    ``suggested_size_pct``, …). Shares its validation with
    :func:`validate_trade_setup` so the two cannot drift.
    """
    suggested_size_pct = validate_trade_setup(brief_trade_setup)
    disaster_stop = float(brief_trade_setup["disaster_stop"])
    entry_tiers_raw = brief_trade_setup["entry_tiers"]
    tp_tranches_raw = brief_trade_setup.get("tp_tranches") or ()

    final_size_pct = suggested_size_pct * float(scale_factor)
    total_notional = final_size_pct / 100.0 * float(paper_equity)

    entries: list[TierPlan] = []
    for idx, raw in enumerate(entry_tiers_raw):
        limit = float(raw["limit"])
        if limit <= 0:
            # Defense-in-depth — trade_setup generator already guards against
            # this. Skip the offending tier rather than the whole plan.
            continue
        alloc_pct = float(raw.get("alloc_pct", 0.0))
        tier_notional = total_notional * (alloc_pct / 100.0)
        qty = max(0, math.floor(tier_notional / limit))
        entries.append(
            TierPlan(
                tier_index=idx,
                limit_price=limit,
                qty=qty,
                alloc_pct=alloc_pct,
                tag=str(raw.get("tag", "")),
            )
        )

    if not entries:
        raise TradeSetupNotPlannableError("no usable entry tiers after sanitisation")

    tranches: list[TpTranchePlan] = []
    for idx, raw in enumerate(tp_tranches_raw):
        target = float(raw["target"])
        if target <= 0:
            continue
        tranches.append(
            TpTranchePlan(
                tranche_index=idx,
                target_price=target,
                tranche_pct=float(raw.get("tranche_pct", 0.0)),
                r_multiple=float(raw.get("r_multiple", 0.0)),
                tag=str(raw.get("tag", "")),
            )
        )

    order_ttl_days = int(
        brief_trade_setup.get("order_ttl_days") or 0
    )  # 0 sentinel → planner falls back to default

    return SetupPlan(
        suggested_size_pct=suggested_size_pct,
        scale_factor=float(scale_factor),
        final_size_pct=final_size_pct,
        total_notional=total_notional,
        paper_equity=float(paper_equity),
        disaster_stop=disaster_stop,
        order_ttl_days=order_ttl_days,
        entry_tiers=tuple(entries),
        tp_tranches=tuple(tranches),
    )


def setup_plan_gross_notional(plan: SetupPlan) -> float:
    """The dollar gross a planner would commit if every tier filled.

    Used by the gross safety guard in the planner (block if cumulative would
    push past ``GROSS_SAFETY_FRAC × equity``).
    """
    return sum(t.qty * t.limit_price for t in plan.entry_tiers)


__all__ = [
    "SetupPlan",
    "TierPlan",
    "TpTranchePlan",
    "TradeSetupNotPlannableError",
    "compute_daily_scale_factor",
    "compute_setup_plan",
    "setup_plan_gross_notional",
    "validate_trade_setup",
]

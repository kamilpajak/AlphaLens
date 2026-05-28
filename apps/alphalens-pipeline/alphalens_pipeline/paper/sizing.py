"""Pure position-sizing math for the paper-trade harness.

Translates a parsed ``brief_trade_setup`` dict into the concrete share
quantities a planner would route to Alpaca. No I/O, no Alpaca SDK reach —
this module is intentionally easy to test in isolation and easy to reason
about against the locked sizing formula in
``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §3.

Formula (per memo §3):

  effective_size_pct = min(suggested_size_pct, 100 / N_FIXED)
  total_notional     = effective_size_pct / 100 × paper_equity
  per_tier_notional  = total_notional × (tier.alloc_pct / 100)
  per_tier_qty       = floor(per_tier_notional / tier.limit)

``alloc_pct`` already sums to ~100 across tiers (trade_setup §7.3); the
``total_notional × alloc_pct`` step honours the per-tier risk weighting
calibrated by the trade-setup generator.

The function does NOT skip tiers that round to 0 shares — it returns
them with ``qty=0`` so the planner can record the intent (and the
reconciler in PR 3 can decide whether to submit a zero-qty order at
all). Silent skipping would erase a real fact: that the effective size
× alloc_pct can be below the price of one share for very-low-allocation
tiers at high prices, which the analysis pipeline needs to be able to
detect.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from alphalens_pipeline.paper.constants import N_FIXED


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
    """The full per-candidate plan: sizing scalars + ladder + exit references."""

    suggested_size_pct: float
    effective_size_pct: float
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


def _cap_pct() -> float:
    """100 / N_FIXED, expressed as a percent — i.e. the per-position weight
    expressed in the same units as ``suggested_size_pct`` (a percent, NOT a
    fraction). Pinned via N_FIXED so changing the constant flows through."""
    return 100.0 / N_FIXED


def compute_setup_plan(
    *,
    brief_trade_setup: dict,
    paper_equity: float,
    n_fixed: int = N_FIXED,
) -> SetupPlan:
    """Turn a parsed ``brief_trade_setup`` dict into a :class:`SetupPlan`.

    ``brief_trade_setup`` must be the JSON-decoded dict (already parsed by
    the caller); this module does NOT do parquet → JSON parsing. The
    decoupling lets the planner pass either a Django-side dict or a freshly-
    parsed parquet row through the same surface.

    Raises :class:`TradeSetupNotPlannableError` for the documented unplannable
    cases (status != OK, no entry tiers, missing suggested_size_pct, …).
    """
    if not isinstance(brief_trade_setup, dict):
        raise TradeSetupNotPlannableError(
            f"brief_trade_setup is not a dict (got {type(brief_trade_setup).__name__})"
        )

    status = brief_trade_setup.get("status")
    if status != "OK":
        raise TradeSetupNotPlannableError(f"status={status!r} (only 'OK' is plannable)")

    schema = brief_trade_setup.get("schema_version")
    if schema != "1.0.0":
        # Future schema → fail loudly rather than silently mis-interpret a new shape.
        raise TradeSetupNotPlannableError(
            f"unsupported schema_version={schema!r}; planner pinned to 1.0.0"
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

    tp_tranches_raw = brief_trade_setup.get("tp_tranches") or ()

    cap_pct = 100.0 / n_fixed
    effective_size_pct = min(float(suggested_size_pct), cap_pct)
    total_notional = effective_size_pct / 100.0 * float(paper_equity)

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
        suggested_size_pct=float(suggested_size_pct),
        effective_size_pct=effective_size_pct,
        total_notional=total_notional,
        paper_equity=float(paper_equity),
        disaster_stop=float(disaster_stop),
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
    "compute_setup_plan",
    "setup_plan_gross_notional",
]

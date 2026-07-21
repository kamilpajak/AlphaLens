"""Entry-only placement classifier (Stage-1 auto-manager).

Pure, stateless. Given a sized SetupPlan, produce one ENTRY-ONLY bracket per
non-zero tier: neither the in-band TP nor the disaster stop is ever a native
Saxo bracket child (`bracket.take_profit` AND `bracket.stop_loss` are always
None). This kills Bug-B at source — a bracket TP child is a live SELL Limit that,
paired with the standalone disaster stop (also a SELL), would commit 2x the owned
qty and trip `SellOrdersAlreadyExistForOwnedContracts` under FifoRealTime netting
(design memo §1 Bug-B, §10 placement_planner).

Each tier still SURFACES its in-band TP target + ORIGINAL `tier_index` so the
journal `planned` line can record them (design memo §7): `tp_planned_in_oco` is
True when the TP clears execution._MAX_CHILD_DISTANCE_FRAC (15%, inclusive <=) —
i.e. it is eligible to become a native OCO leg in Stage 2; a farther TP stays
operator-managed until phase B. The disaster stop is represented once at plan
level and placed later as ONE standalone StopIfTraded after fill, sized to
realized netted qty. Consumes execution.decompose_setup_plan.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

from alphalens_pipeline.brokers import execution as execution_policy
from alphalens_pipeline.brokers.contract import BracketOrderRequest, InstrumentRef
from alphalens_pipeline.brokers.execution import decompose_setup_plan
from alphalens_pipeline.paper.sizing import SetupPlan


@dataclasses.dataclass(frozen=True)
class TierPlacement:
    bracket: BracketOrderRequest  # entry-only: take_profit AND stop_loss always None
    tier_index: int  # ORIGINAL tier index (zero-qty skips do not shift it)
    tp: float | None  # in-band TP target — journaled, never placed as a bracket child
    tp_planned_in_oco: bool  # TP clears the child-distance guard (OCO rung-2 eligible, Stage 2)
    tp_operator_managed: float | None  # far TP beyond the guard — operator-managed until phase B


@dataclasses.dataclass(frozen=True)
class PlacementPlan:
    tiers: tuple[TierPlacement, ...]
    disaster_stop_price: float
    operator_report: str


def classify(
    setup_plan: SetupPlan,
    instrument: InstrumentRef,
    *,
    side: Literal["BUY", "SELL"] = "BUY",
) -> PlacementPlan:
    """Classify a sized plan into ENTRY-ONLY brackets + the surfaced in-band TPs."""
    limit_frac = execution_policy._MAX_CHILD_DISTANCE_FRAC
    brackets = decompose_setup_plan(setup_plan, instrument, side=side)
    # decompose_setup_plan drops zero-qty tiers while preserving order, so zip the
    # non-zero source tiers back onto the brackets to recover each bracket's
    # ORIGINAL tier_index (zero-qty skips must not shift it — the governing-TP
    # rule in §8 keys on the true tier_index). strict=True pins the 1:1 invariant.
    nonzero_tiers = [tier for tier in setup_plan.entry_tiers if tier.qty > 0]
    tiers: list[TierPlacement] = []
    for source_tier, bracket in zip(nonzero_tiers, brackets, strict=True):
        tp = bracket.take_profit  # decompose-computed in-band TP target for this tier
        tp_planned_in_oco = False
        tp_operator_managed: float | None = None
        if tp is not None:
            dist_frac = abs(tp - bracket.entry_limit) / bracket.entry_limit
            tp_planned_in_oco = dist_frac <= limit_frac
            if not tp_planned_in_oco:
                tp_operator_managed = tp
        placed = dataclasses.replace(
            bracket,
            take_profit=None,  # entry-only: the in-band TP is journaled, never a child (Bug-B source)
            stop_loss=None,  # disaster stop is NEVER a child — plan-level standalone
        )
        tiers.append(
            TierPlacement(
                bracket=placed,
                tier_index=source_tier.tier_index,
                tp=tp,
                tp_planned_in_oco=tp_planned_in_oco,
                tp_operator_managed=tp_operator_managed,
            )
        )
    report = _operator_report(instrument, tuple(tiers), setup_plan.disaster_stop, limit_frac)
    return PlacementPlan(
        tiers=tuple(tiers),
        disaster_stop_price=setup_plan.disaster_stop,
        operator_report=report,
    )


def _operator_report(
    instrument: InstrumentRef,
    tiers: tuple[TierPlacement, ...],
    disaster_stop: float,
    limit_frac: float,
) -> str:
    """Whole-plan report; the phrase 'disaster stop' appears EXACTLY ONCE.

    Stage 1 is entry-only: NO TP is placed as a bracket child, so every in-band
    TP is reported operator-managed (an in-band TP is flagged OCO-eligible — it is
    the Stage-2 upgrade candidate; a far TP is beyond the child-distance guard)."""
    lines = [f"{instrument.ticker} placement plan ({len(tiers)} non-zero tiers, entry-only):"]
    for tier in tiers:
        entry = tier.bracket.entry_limit
        if tier.tp is not None:
            tp = tier.tp
            pct = abs(tp - entry) / entry * 100.0
            band = "OCO-eligible" if tier.tp_planned_in_oco else f"beyond {limit_frac * 100:.0f}%"
            lines.append(
                f"  tier {tier.tier_index}: entry {entry:.2f} (entry-only); "
                f"TP {tp:.2f} operator-managed (+{pct:.1f}%, {band})"
            )
        else:
            lines.append(f"  tier {tier.tier_index}: entry {entry:.2f} (entry-only, no TP)")
    lines.append(
        f"  disaster stop {disaster_stop:.2f}: standalone StopIfTraded after fill (placed once)"
    )
    return "\n".join(lines)


__all__ = ["PlacementPlan", "TierPlacement", "classify"]

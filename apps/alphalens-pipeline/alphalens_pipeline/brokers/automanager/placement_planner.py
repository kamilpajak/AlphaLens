"""Option-C in-band-subset placement classifier (MVP auto-manager).

Pure, stateless. Given a sized SetupPlan, decide per non-zero tier which legs
place as native Saxo bracket children and which are reported operator-managed
(design memo §Place). A TP is a child only when it clears
execution._MAX_CHILD_DISTANCE_FRAC (15%, inclusive <=); a farther TP is reported
operator-managed and never POSTed (far standalone SELL LIMIT unproven — MVP risk
R1, phase B). The disaster stop is NEVER a child (bracket.stop_loss always None);
represented once at plan level and placed later as ONE standalone StopIfTraded
after fill, sized to realized qty (avoids the FifoRealTime partial-fill
over-hedge — Risk 2). Consumes execution.decompose_setup_plan.
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
    bracket: BracketOrderRequest
    tp_placed_as_child: bool
    tp_operator_managed: float | None


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
    """Classify a sized plan into the in-band placeable subset + operator report."""
    limit_frac = execution_policy._MAX_CHILD_DISTANCE_FRAC
    brackets = decompose_setup_plan(setup_plan, instrument, side=side)
    tiers: list[TierPlacement] = []
    for bracket in brackets:
        tp = bracket.take_profit
        tp_placed_as_child = False
        tp_operator_managed: float | None = None
        if tp is not None:
            dist_frac = abs(tp - bracket.entry_limit) / bracket.entry_limit
            tp_placed_as_child = dist_frac <= limit_frac
            if not tp_placed_as_child:
                tp_operator_managed = tp
        placed = dataclasses.replace(
            bracket,
            take_profit=tp if tp_placed_as_child else None,
            stop_loss=None,  # disaster stop is NEVER a child — plan-level standalone
        )
        tiers.append(
            TierPlacement(
                bracket=placed,
                tp_placed_as_child=tp_placed_as_child,
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
    """Whole-plan report; the phrase 'disaster stop' appears EXACTLY ONCE."""
    lines = [f"{instrument.ticker} placement plan ({len(tiers)} non-zero tiers):"]
    for idx, tier in enumerate(tiers):
        entry = tier.bracket.entry_limit
        if tier.tp_placed_as_child:
            tp = tier.bracket.take_profit
            # classify() only ever sets tp_placed_as_child=True on a tier whose
            # bracket was built with take_profit=tp (dataclasses.replace above),
            # so tp is not None here for every tier this private helper actually
            # receives (its sole caller, classify() itself).
            assert tp is not None, "tp_placed_as_child=True implies a non-None take_profit"
            pct = abs(tp - entry) / entry * 100.0
            lines.append(f"  tier {idx}: entry {entry:.2f} + TP {tp:.2f} child (+{pct:.1f}%)")
        elif tier.tp_operator_managed is not None:
            tp = tier.tp_operator_managed
            pct = abs(tp - entry) / entry * 100.0
            lines.append(
                f"  tier {idx}: entry {entry:.2f} (entry-only); "
                f"TP {tp:.2f} operator-managed (+{pct:.1f}%, beyond {limit_frac * 100:.0f}%)"
            )
        else:
            lines.append(f"  tier {idx}: entry {entry:.2f} (entry-only, no TP)")
    lines.append(
        f"  disaster stop {disaster_stop:.2f}: standalone StopIfTraded after fill (placed once)"
    )
    return "\n".join(lines)


__all__ = ["PlacementPlan", "TierPlacement", "classify"]

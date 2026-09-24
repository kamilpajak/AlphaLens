"""The plain-text account of a TradeIntent's exit declaration (#1530).

A brief pick's stop management used to be invisible before arming: `thematic
intent` wrote it into the JSON and `broker arm` left it out of its human output.
Both commands now print what this module says, so the author reads how the stop
will be managed before the pick is live.

Presentation only. The contract (`broker_contract.trade_intent`) stays free of
prose; the meaning of each declaration is the contract README's table.
"""

from __future__ import annotations

from collections.abc import Sequence

from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    ModelPush,
    ReanchorOnFill,
    TrailingStop,
)

_IMMEDIATE = "immediate"


def _share_weighted_average(tiers: Sequence[EntryTierSpec]) -> float:
    """The fill average if every one of ``tiers`` fills at its limit.

    ``alloc_pct`` splits the MONEY, so each tier buys ``alloc / limit`` shares and
    the average is total money over total shares."""
    money = sum(tier.alloc_pct for tier in tiers)
    shares = sum(tier.alloc_pct / tier.limit_price for tier in tiers)
    return money / shares


def _arm_price(tiers: Sequence[EntryTierSpec], disaster_stop: float, arm_trigger_r: float) -> str:
    """``average + a * (average - stop)``, prefixed "at most" when an immediate
    tier is in the fill: its limit is the cap, and a cheaper fill lowers both the
    average and the arm price."""
    average = _share_weighted_average(tiers)
    price = average + arm_trigger_r * (average - disaster_stop)
    bound = "at most " if any(tier.entry_mode == _IMMEDIATE for tier in tiers) else ""
    return f"{bound}{price:.2f}"


def _trailing_lines(
    primitive: TrailingStop, entry_tiers: Sequence[EntryTierSpec], disaster_stop: float
) -> list[str]:
    lines = [
        f"exit: trailing stop — arms at +{primitive.arm_trigger_r:g}R above the fill "
        f"average (R = average - stop), then keeps {primitive.trail_frac * 100:g}% of the "
        "gain above the average"
    ]
    if not entry_tiers:
        return lines
    first = _arm_price(entry_tiers[:1], disaster_stop, primitive.arm_trigger_r)
    if len(entry_tiers) == 1:
        lines.append(f"  arms near {first}")
        return lines
    full = _arm_price(entry_tiers, disaster_stop, primitive.arm_trigger_r)
    lines.append(f"  arms near {first} if only E1 fills, near {full} if the full ladder fills")
    return lines


def describe_exit(
    exit_spec: ExitGeometrySpec | None,
    *,
    entry_tiers: Sequence[EntryTierSpec],
    disaster_stop: float,
) -> list[str]:
    """How the stop will be managed after the fill, one line per fact.

    ``None`` and an empty plan mean the same thing to the daemon: the stop is
    never moved (contract README, "The exit declaration")."""
    lines: list[str] = []
    if exit_spec is not None and exit_spec.initial_levels is not None:
        levels = exit_spec.initial_levels
        lines.append(
            f"exit levels: places stop {levels.stop:g} and TP {levels.tp:g} instead of the ladder"
        )
    plan = () if exit_spec is None else exit_spec.reaction_plan
    if not plan:
        lines.append(f"exit: none — the stop stays at {disaster_stop:g} unless you move it")
        return lines
    for primitive in plan:
        if isinstance(primitive, TrailingStop):
            lines.extend(_trailing_lines(primitive, entry_tiers, disaster_stop))
        elif isinstance(primitive, ReanchorOnFill):
            lines.append(
                f"exit: re-anchor on fill — the stop moves to fill - {primitive.k_atr:g}×ATR "
                f"(ATR {primitive.atr:g})"
            )
        elif isinstance(primitive, ModelPush):
            lines.append("exit: model (reserved; the stop is not moved today)")
    return lines


__all__ = ["describe_exit"]

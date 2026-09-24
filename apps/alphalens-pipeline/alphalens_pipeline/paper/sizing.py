"""Read-side helpers over a brief's ``brief_trade_setup`` dict.

The brief row is a thematic-brief concern, so these stay client-side:
:func:`validate_trade_setup` answers "is this row plannable" for the population
monitor, and :func:`planned_blended_entry` / :func:`first_brief_tp_target` read
the planned blend and first target for the ``/edge`` replays.

Nothing here turns a brief into a pick any more. The brief producer
(``thematic intent``) was removed in #1552: every pick is a hand-written
TradeIntent document. The money math lives in the shared
``broker_contract.sizing`` leaf; import
:class:`~broker_contract.sizing.SetupPlan`,
:func:`~broker_contract.sizing.compute_setup_plan` and
:class:`~broker_contract.sizing.TradeSetupNotPlannableError` from there.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from broker_contract.sizing import (
    TradeSetupNotPlannableError,
    _blend_priced_tiers,
    planned_blended_entry_from_spec,
)


def validate_trade_setup(brief_trade_setup: dict) -> float:
    """Run the plannability checks and return the brief's ``suggested_size_pct``.

    Exposed so a caller can ask "is this brief row plannable" without building
    a :class:`~broker_contract.trade_intent.schema.TradeSpec` (the population
    monitor does). The percent is the BRIEF's field.
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
    # :func:`~broker_contract.sizing.compute_setup_plan` runs (it drops tiers
    # with ``limit <= 0`` as defense-in-depth), so a row this function accepts
    # is never refused later for having no usable tier.
    usable_tiers = [
        t for t in entry_tiers_raw if isinstance(t, dict) and float(t.get("limit", 0) or 0) > 0
    ]
    if not usable_tiers:
        raise TradeSetupNotPlannableError("no usable entry tiers (all limits <= 0)")

    return float(suggested_size_pct)


def planned_blended_entry(brief_trade_setup: Mapping[str, Any]) -> float | None:
    """Alloc-weighted mean price over ALL intended entry tiers (planned, pre-fill).

    Mirrors ``alphalens_pipeline.feedback.ladder_replay._blended_entry``'s formula
    (weighted by ``alloc_pct``, equal-weight fallback when weights sum to 0) but
    applies it to the FULL set of intended entry tiers rather than tiers that
    actually filled -- at placement time no bars / fills exist yet, so the
    "planned" blend (alloc-weighted tier limits) is the only anchor available
    (broker-manager extraction memo section 4.3). Tiers with a non-positive
    ``limit`` are dropped (mirrors :func:`validate_trade_setup`'s sanitisation).

    Returns ``None`` when there are no usable entry tiers, or the input is not a
    mapping / a tier is malformed -- never raises.
    """
    if not isinstance(brief_trade_setup, Mapping):
        return None
    raw_entries = brief_trade_setup.get("entry_tiers") or []
    priced: list[tuple[float, float]] = []
    for t in raw_entries:
        if not isinstance(t, Mapping):
            continue
        try:
            limit = float(t.get("limit", 0) or 0)
        except (TypeError, ValueError):
            continue
        if limit <= 0:
            continue
        try:
            alloc_pct = float(t.get("alloc_pct", 0.0))
        except (TypeError, ValueError):
            alloc_pct = 0.0
        priced.append((limit, alloc_pct))
    return _blend_priced_tiers(priced)


def first_brief_tp_target(brief_trade_setup: Mapping[str, Any]) -> float | None:
    """The brief's OWN first take-profit target, or ``None`` when there is none
    usable (issue #1112 step 3).

    ``None`` (never raises) when the input is not a mapping, ``tp_tranches`` is
    empty / not a sequence of mappings, or the first tranche's ``target`` is
    missing, unparseable, non-finite or non-positive — the same defensive
    contract as :func:`planned_blended_entry`.

    Only the FIRST tranche is read: it is the shallowest level the research
    committed to, so it is the floor. The deeper tranches say nothing about
    whether the geometry target is too low.
    """
    if not isinstance(brief_trade_setup, Mapping):
        return None
    tranches = brief_trade_setup.get("tp_tranches") or []
    try:
        first = tranches[0]
    except (IndexError, TypeError, KeyError):
        return None
    if not isinstance(first, Mapping):
        return None
    try:
        target = float(first.get("target"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(target) or target <= 0.0:
        return None
    return target


__all__ = [
    "first_brief_tp_target",
    "planned_blended_entry",
    "planned_blended_entry_from_spec",
    "validate_trade_setup",
]

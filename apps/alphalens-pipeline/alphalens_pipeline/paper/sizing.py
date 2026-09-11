"""Brief-parse / arm-time half of the paper-trade sizing pipeline.

Translates a raw ``brief_trade_setup`` dict (a thematic-brief concern) into
an unsized :class:`~broker_contract.trade_intent.schema.TradeSpec`, and
builds the ``atr_bracket_1p5`` exit-geometry spec off the same brief dict.
This module reads a thematic brief dict — a client concern — so it stays
client-side; the money-math half (the sizing VALUE TYPES, the FX-aware
notional/qty arithmetic) moved to the shared, dependency-free
``broker_contract.sizing`` leaf (broker-manager extraction 2A-4a, design memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
§2.1/§2.3). Import :class:`~broker_contract.sizing.SetupPlan`,
:func:`~broker_contract.sizing.compute_setup_plan`,
:class:`~broker_contract.sizing.TradeSetupNotPlannableError`, and friends
directly from ``broker_contract.sizing``.

See ``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §2.3 / §3
for the locked sizing formula this module's downstream consumers apply.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from broker_contract.exit_geometry import resolve_exit_policy
from broker_contract.exit_geometry.policy import BreakevenTrailPolicy
from broker_contract.sizing import (
    TradeSetupNotPlannableError,
    _blend_priced_tiers,
    planned_blended_entry_from_spec,
)
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    TpTrancheSpec,
    TradeSpec,
    TrailingStop,
)


def validate_trade_setup(brief_trade_setup: dict) -> float:
    """Run the plannability checks and return ``suggested_size_pct``.

    Exposed so the planner's first pass can compute the aggregate uncapped
    notional without building a full :class:`~broker_contract.sizing.SetupPlan`
    (which would require the not-yet-computed ``scale_factor``). The checks
    are the same ones :func:`~broker_contract.sizing.compute_setup_plan`
    enforces; sharing them here avoids drift.
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
    # with ``limit <= 0`` as defense-in-depth). Without this alignment a
    # candidate with all-zero- limit tiers would pass pass 1 of the planner
    # (contributing to the aggregate that feeds compute_daily_scale_factor)
    # then fail pass 2 with "no usable entry tiers after sanitisation",
    # introducing a downward bias on the day's global scale factor. Per zen
    # second-round review 2026-05-28.
    usable_tiers = [
        t for t in entry_tiers_raw if isinstance(t, dict) and float(t.get("limit", 0) or 0) > 0
    ]
    if not usable_tiers:
        raise TradeSetupNotPlannableError("no usable entry tiers (all limits <= 0)")

    return float(suggested_size_pct)


def parse_brief_to_spec(brief_trade_setup: dict) -> TradeSpec:
    """Parse a raw ``brief_trade_setup`` dict into an unsized :class:`TradeSpec`.

    Kept in ``paper/sizing.py`` (not ``thematic/intent_builder.py``) — the
    daemon still parses at drain time, so moving it now would introduce a
    transient brokers->thematic import edge; PR-7 relocates it when the
    parse moves to arm-time (memo section 2.3).

    Runs :func:`validate_trade_setup` FIRST so the same unplannable briefs
    raise :class:`~broker_contract.sizing.TradeSetupNotPlannableError` here as
    they did inside the pre-split ``compute_setup_plan``. Every raw entry
    tier / TP tranche is carried through IN ORDER (including non-positive
    ``limit``/``target`` rows) — the money half
    (:func:`~broker_contract.sizing.compute_setup_plan`) is what drops them,
    so ``tier_index``/``tranche_index`` downstream stay the raw enumerate
    index either way.
    """
    suggested_size_pct = validate_trade_setup(brief_trade_setup)

    entry_tiers_raw = brief_trade_setup["entry_tiers"]
    entry_tiers = tuple(
        EntryTierSpec(
            limit_price=float(raw["limit"]),
            alloc_pct=float(raw.get("alloc_pct", 0.0)),
            tag=str(raw.get("tag", "")),
        )
        for raw in entry_tiers_raw
    )

    tp_tranches_raw = brief_trade_setup.get("tp_tranches") or ()
    tp_tranches = tuple(
        TpTrancheSpec(
            price=float(raw["target"]),
            tranche_pct=float(raw.get("tranche_pct", 0.0)),
            r_multiple=float(raw.get("r_multiple", 0.0)),
            tag=str(raw.get("tag", "")),
        )
        for raw in tp_tranches_raw
    )

    disaster_stop = float(brief_trade_setup["disaster_stop"])
    order_ttl_days = int(
        brief_trade_setup.get("order_ttl_days") or 0
    )  # 0 sentinel preserved — must NOT fall through to TradeSpec's default (7)

    return TradeSpec(
        entry_tiers=entry_tiers,
        disaster_stop=disaster_stop,
        tp_tranches=tp_tranches,
        suggested_size_pct=suggested_size_pct,
        order_ttl_days=order_ttl_days,
        side="long",
    )


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


def _deployed_trail() -> BreakevenTrailPolicy:
    """The stop-management policy this deployment runs, and therefore the one the
    brief path declares.

    Read off the registry rather than retyped, so the declaration cannot drift
    from the policy an operator reads in a log line. The narrowing assert is what
    makes that read type-safe: the registry is declared as returning the
    ``ExitPolicy`` protocol, and the parameters below belong to this family."""
    policy = resolve_exit_policy("breakeven_trail")
    assert isinstance(policy, BreakevenTrailPolicy)
    return policy


def build_exit_declaration() -> ExitGeometrySpec:
    """What a brief pick DECLARES about its exit (#1414).

    A declaration and nothing else: how the stop is to be MANAGED after fill,
    with no ``initial_levels``. Since #1414 the presence of levels is the whole
    placement instruction — supply them and they are placed, omit them and the
    brief's own ladder is. This path omits them, which is what it has effectively
    done all along: it used to compute an ATR bracket that the deployed policy
    (``applies_geometry=False``) journaled and never placed, so the document said
    one thing and the broker saw another.

    Takes no arguments, which is the point rather than an oversight. The trail is
    read off the registry (``_deployed_trail``) instead of retyped, so the
    declaration cannot drift from the policy an operator reads in a log line, and
    it does not vary per brief: every brief pick runs the same stop management.
    The old builder returned ``None`` when the ATR was missing or the bracket was
    degenerate; there is no bracket left to fail to build, and declining to say
    how a stop is managed because a geometry we do not place could not be
    computed was never coherent. Measured before the change: over the 45 sessions
    to 2026-09-10, 287 of 287 plannable candidates had a usable ATR, so no live
    pick took that branch.

    The ceiling and the never-below-brief-TP1 clamp went with the bracket. They
    survive where they are still read: the ``/edge`` what-if lens
    (``feedback/ladder_replay``) and the research replay
    (``alphalens_research.diagnostics.exit_policy_replay``), both through the
    shared ``atr_bracket_levels`` leaf.
    """
    trail = _deployed_trail()
    return ExitGeometrySpec(
        reaction_plan=(
            TrailingStop(arm_trigger_r=trail.activation_r, trail_frac=trail.trail_frac),
        ),
    )


__all__ = [
    "build_exit_declaration",
    "first_brief_tp_target",
    "parse_brief_to_spec",
    "planned_blended_entry",
    "planned_blended_entry_from_spec",
    "validate_trade_setup",
]

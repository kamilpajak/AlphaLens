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

from broker_contract.exit_geometry import AtrBracketPolicy, resolve_exit_policy
from broker_contract.exit_geometry.levels import ceiling_from_52w_high
from broker_contract.sizing import TradeSetupNotPlannableError
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    ReanchorOnFill,
    TpTrancheSpec,
    TradeSpec,
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


def _blend_priced_tiers(priced: list[tuple[float, float]]) -> float | None:
    """Shared alloc-weighted-mean arithmetic for the dict and spec blend paths.

    Weighted by the second element (alloc weight); equal-weight fallback when
    weights sum to 0; ``None`` for an empty ``priced`` list. Extracted so
    :func:`planned_blended_entry` and :func:`planned_blended_entry_from_spec`
    cannot drift — both must produce identical results for
    ``parse_brief_to_spec(setup)`` vs ``setup`` (PR-7).
    """
    if not priced:
        return None
    wsum = sum(w for _, w in priced)
    if wsum > 0:
        return sum(p * w for p, w in priced) / wsum
    return sum(p for p, _ in priced) / len(priced)


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


def planned_blended_entry_from_spec(spec: TradeSpec) -> float | None:
    """Alloc-weighted mean price over an already-parsed :class:`TradeSpec`.

    The arm-time (PR-7) mirror of :func:`planned_blended_entry`: the daemon's
    geometry SHADOW stamp no longer has the raw brief dict at drain time (the
    parse moved to arm time), only the already-parsed ``TradeSpec`` carried on
    the :class:`~broker_contract.trade_intent.schema.TradeIntent`. Must
    return the SAME value ``planned_blended_entry(setup)`` would for the
    equivalent ``setup`` -- both routes share :func:`_blend_priced_tiers`.

    Returns ``None`` when there are no usable entry tiers (all non-positive
    ``limit_price``, or ``spec.entry_tiers`` is empty) -- never raises.
    """
    priced = [(t.limit_price, t.alloc_pct) for t in spec.entry_tiers if t.limit_price > 0]
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


def build_exit_geometry_spec(
    brief_trade_setup: dict, pct_off_52w_high: float | None = None
) -> ExitGeometrySpec | None:
    """Build the ``atr_bracket_1p5`` exit-geometry spec for one brief trade setup.

    The client-precomputed levels for the (currently dark) exit-geometry
    override at placement (broker-manager extraction memo section 4.1 / 4.3).
    Reads the SAME setup dict as
    :func:`alphalens_pipeline.feedback.ladder_replay.replay_ladder_atr_bracket`
    (the ``/edge`` what-if replay) for the anchor FACTS -- ATR is
    ``brief_trade_setup["atr"]`` (the identical brief key the replay leaf reads)
    and the 52w ceiling comes from the identical
    :func:`~broker_contract.exit_geometry.levels.ceiling_from_52w_high` leaf.

    LIVE AND THE REPLAY NO LONGER AGREE ON THE TAKE-PROFIT (issue #1112 step 3,
    the clamp below): live raises the target to the brief's own first tranche
    when the ATR bracket lands under it; the replay lens does not, on purpose,
    because clamping there would rewrite the historical what-if series issues
    #1114 / #1115 measure against. So an ``/edge`` ``atr_bracket_1p5`` what-if
    figure is NOT a prediction of what live will do on the take-profit side.
    The stop and the anchor blend match ONLY when every entry tier fills: the
    replay lens takes its anchor as an explicit argument since issue #1114, and
    on a partial fill ``anchor="realised"`` (the historical ``atr_bracket_1p5``
    lens) blends only the tiers that touched, so both the anchor AND the stop
    derived from it diverge -- on SMG, 59.786017 vs 55.5957 on the blend and
    55.754017 vs 51.5637 on the stop. ``anchor="planned"`` is the mode that
    mirrors this builder; it is registered as ``atr_bracket_1p5_planned``.
    Pinned by ``test_exit_geometry_spec.py
    ::test_multi_tier_blend_and_stop_match_replay_but_the_take_profit_does_not``
    (every tier fills) and by
    ``tests/feedback/test_atr_bracket_anchor_mode.py`` (partial fill).

    ``pct_off_52w_high`` is deliberately NOT read off ``brief_trade_setup``
    (it is a sibling column on the candidate/brief row, e.g.
    ``CandidateBrief.technical_pct_off_52w_high`` in ``paper/brief_loader.py``,
    never a key inside the ``trade_setup`` JSON blob itself -- confirmed against
    ``population_ladder_monitor.py``'s ``_replay_candidate`` call site, which
    threads it as a SEPARATE kwarg). Callers pass it in explicitly. This is
    exactly the "planned-vs-realized BLEND anchor" divergence the memo elevates
    to a P0 blocker (section 4.3) -- fixed by the PR-6b ``avg_price`` re-anchor,
    NOT by this function; :func:`~alphalens_pipeline.brokers.automanager.
    control_loop.build_default_deps`'s fail-fast guard keeps the flag from
    flipping live before that ships.

    Returns ``None`` (never raises) when there are no usable entry tiers, the
    ATR is missing / non-finite / non-positive, or the bracket is not
    constructible (degenerate ceiling, non-positive bracket stop) -- the same
    degenerate-input contract as :func:`~broker_contract.exit_geometry.
    levels.atr_bracket_levels`.
    """
    blended = planned_blended_entry(brief_trade_setup)
    if blended is None:
        return None
    raw_atr = brief_trade_setup.get("atr") if hasattr(brief_trade_setup, "get") else None
    try:
        atr = float(raw_atr) if raw_atr is not None else None
    except (TypeError, ValueError):
        atr = None
    if atr is None:
        return None
    ceiling = ceiling_from_52w_high(brief_trade_setup, pct_off_52w_high)
    exit_policy = resolve_exit_policy("atr_bracket_1p5")
    assert isinstance(exit_policy, AtrBracketPolicy)  # this builder handles only the ATR bracket
    levels = exit_policy.decide_placement_geometry(blended, atr, ceiling_price=ceiling)
    if levels is None:
        return None
    stop, tp = levels
    # NEVER BELOW THE BRIEF'S OWN FIRST TAKE-PROFIT (issue #1112 step 3) — the
    # take-profit-side mirror of the never-below-brief-floor rule
    # ``clamp_reanchor_target`` enforces on the stop side. On 2026-08-24 the
    # SMG policy target (blend + 1.5*ATR = 59.6277) landed BELOW the top entry
    # tier (59.786017) and far below the brief's own first tranche (65.25), so
    # the fill was past its take-profit the moment it happened.
    #
    # This is a FLOOR, never a cap (max, not min): a policy target above the
    # first tranche is left alone. It also outranks the 52w ceiling applied
    # inside ``atr_bracket_levels`` — the brief tranche is a level the research
    # committed to, the ceiling is a do-not-chase heuristic.
    #
    # Deliberately NOT pushed down into ``atr_bracket_levels``: that leaf is
    # shared with the ``/edge`` replay lens and ``feedback/ladder_replay``, and
    # clamping there would silently rewrite historical what-if measurements.
    first_target = first_brief_tp_target(brief_trade_setup)
    if first_target is not None:
        tp = max(tp, first_target)
    return ExitGeometrySpec(
        initial_levels=InitialLevels(stop=stop, tp=tp),
        reaction_plan=(
            ReanchorOnFill(k_atr=exit_policy.geom.stop_atr_mult, atr=atr, ceiling_price=ceiling),
        ),
    )


__all__ = [
    "build_exit_geometry_spec",
    "first_brief_tp_target",
    "parse_brief_to_spec",
    "planned_blended_entry",
    "planned_blended_entry_from_spec",
    "validate_trade_setup",
]

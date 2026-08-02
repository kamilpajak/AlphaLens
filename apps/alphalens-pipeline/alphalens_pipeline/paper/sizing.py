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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from broker_contract.exit_geometry.levels import ceiling_from_52w_high
from broker_contract.exit_geometry.registry import resolve_policy
from broker_contract.trade_intent.schema import (
    EntryTierSpec,
    ExitGeometrySpec,
    InitialLevels,
    ReanchorOnFill,
    TpTrancheSpec,
    TradeSpec,
)

from alphalens_pipeline.paper.constants import (
    EXPECTED_AVG_HOLD_DAYS,
    GROSS_SAFETY_FRAC,
    STEADY_STATE_GROSS_FRAC,
)
from alphalens_pipeline.paper.fx import FxConversion


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

    Currencies (FX-leg design memo §4.2): ``paper_equity`` and
    ``total_notional`` are ACCOUNT currency; ``entry_tiers`` limits,
    ``tp_tranches`` targets and ``disaster_stop`` are INSTRUMENT currency
    (prices are never converted). ``fx`` is ``None`` on the same-currency
    path (a strict no-op — the plan is byte-identical to the pre-FX-leg
    output); when set, :attr:`sizing_notional` is the buffered
    instrument-currency notional the qty division used.
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
    fx: FxConversion | None = None

    @property
    def sizing_notional(self) -> float:
        """The notional the qty division used, in INSTRUMENT currency.

        Identity (``total_notional``) when ``fx`` is None; otherwise the one
        FX line of math: ``total_notional × rate × (1 − buffer_pct/100)``.
        """
        if self.fx is None:
            return self.total_notional
        return self.total_notional * self.fx.rate * (1.0 - self.fx.sizing_buffer_pct / 100.0)


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


def parse_brief_to_spec(brief_trade_setup: dict) -> TradeSpec:
    """Parse a raw ``brief_trade_setup`` dict into an unsized :class:`TradeSpec`.

    Kept in ``paper/sizing.py`` (not ``thematic/intent_builder.py``) — the
    daemon still parses at drain time, so moving it now would introduce a
    transient brokers->thematic import edge; PR-7 relocates it when the
    parse moves to arm-time (memo section 2.3).

    Runs :func:`validate_trade_setup` FIRST so the same unplannable briefs
    raise :class:`TradeSetupNotPlannableError` here as they did inside the
    pre-split ``compute_setup_plan``. Every raw entry tier / TP tranche is
    carried through IN ORDER (including non-positive ``limit``/``target``
    rows) — the money half (:func:`compute_setup_plan`) is what drops them,
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


def build_exit_geometry_spec(
    brief_trade_setup: dict, pct_off_52w_high: float | None = None
) -> ExitGeometrySpec | None:
    """Build the ``atr_bracket_1p5`` exit-geometry spec for one brief trade setup.

    The client-precomputed levels for the (currently dark) exit-geometry
    override at placement (broker-manager extraction memo section 4.1 / 4.3).
    Mirrors exactly what
    :func:`alphalens_pipeline.feedback.ladder_replay.replay_ladder_atr_bracket`
    (the ``/edge`` what-if replay) reads off the SAME setup dict, so "live ==
    replay via shared formula" holds for the anchor FACTS -- ATR is
    ``brief_trade_setup["atr"]`` (the identical brief key the replay leaf reads)
    and the 52w ceiling comes from the identical
    :func:`~broker_contract.exit_geometry.levels.ceiling_from_52w_high` leaf.

    The one place this diverges from the replay's ``_blended_entry`` by
    necessity: replay anchors the bracket at the blend over tiers that actually
    TOUCHED in the bar-replay walk, but at PLACEMENT time no bars/fills exist
    yet -- ``pct_off_52w_high`` is deliberately NOT read off ``brief_trade_setup``
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
    policy = resolve_policy("atr_bracket_1p5")
    levels = policy.levels(blended, atr, ceiling_price=ceiling)
    if levels is None:
        return None
    stop, tp = levels
    return ExitGeometrySpec(
        initial_levels=InitialLevels(stop=stop, tp=tp),
        reaction_plan=(ReanchorOnFill(k_atr=policy.stop_atr_mult, atr=atr, ceiling_price=ceiling),),
    )


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
        paper_equity: live account equity in the ACCOUNT currency (whatever
            ``AccountSnapshot.currency`` says — the budget IS the account
            currency by operator decision, FX-leg memo §7 Q1).

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


def _build_tp_tranches(tp_tranches: Iterable[TpTrancheSpec]) -> list[TpTranchePlan]:
    """Render the take-profit tranches, dropping any with a non-positive target.

    Extracted verbatim from :func:`compute_setup_plan` to keep the exit-reference
    build a single self-contained pass. Prices are never converted — targets stay
    in INSTRUMENT currency. A tranche with ``price <= 0`` is skipped as
    defense-in-depth against a malformed brief row.
    """
    tranches: list[TpTranchePlan] = []
    for idx, t in enumerate(tp_tranches):
        if t.price <= 0:
            continue
        tranches.append(
            TpTranchePlan(
                tranche_index=idx,
                target_price=t.price,
                tranche_pct=t.tranche_pct,
                r_multiple=t.r_multiple,
                tag=t.tag,
            )
        )
    return tranches


def compute_setup_plan(
    spec: TradeSpec,
    *,
    paper_equity: float,
    scale_factor: float,
    fx: FxConversion | None = None,
) -> SetupPlan:
    """Turn an unsized :class:`TradeSpec` into a concrete :class:`SetupPlan`.

    Args:
        spec: parsed, unsized trade spec — see :func:`parse_brief_to_spec`.
        paper_equity: live account equity in the ACCOUNT currency.
        scale_factor: pre-computed daily scale factor from
            :func:`compute_daily_scale_factor`. Pass ``1.0`` for unit tests
            that want to inspect un-scaled sizing (rare; almost every prod
            day will scale < 1.0 given typical ``suggested_size_pct`` values).
        fx: ``None`` on the same-currency path (strict no-op — the plan is
            byte-identical to the pre-FX-leg output). When the instrument
            currency differs from the account currency the caller passes a
            policy-validated :class:`FxConversion`; the conversion is applied
            ONCE between the account-currency notional and the per-tier qty
            division. Prices (tier limits, targets, stop) are NEVER
            converted.

    Raises :class:`TradeSetupNotPlannableError` for the FX refusals
    (non-positive rate, same-currency ``FxConversion`` — same-currency must
    pass ``fx=None``) plus "no usable entry tiers after sanitisation" when
    every tier in ``spec`` has a non-positive ``limit_price``. The brief-side
    plannability checks (status != OK, missing ``suggested_size_pct``, …) now
    run earlier, inside :func:`parse_brief_to_spec` / :func:`validate_trade_setup`.
    """
    if fx is not None:
        if fx.account_currency == fx.instrument_currency:
            raise TradeSetupNotPlannableError(
                f"FxConversion for identical currencies ({fx.account_currency}) — "
                "the same-currency path must pass fx=None (strict no-op), never a rate"
            )
        if fx.rate <= 0:
            raise TradeSetupNotPlannableError(
                f"FxConversion rate {fx.rate!r} not usable "
                f"({fx.account_currency}->{fx.instrument_currency})"
            )

    suggested_size_pct = spec.suggested_size_pct
    disaster_stop = spec.disaster_stop

    final_size_pct = suggested_size_pct * float(scale_factor)
    total_notional = final_size_pct / 100.0 * float(paper_equity)
    if fx is None:
        # Same-currency: the account-ccy notional IS the sizing notional —
        # no float op applied, so the plan stays byte-exact vs pre-FX-leg.
        sizing_notional = total_notional
    else:
        # THE conversion (memo §4.2 step 5): account-ccy notional × rate ×
        # (1 − buffer). Applied to the NOTIONAL only, before the qty floor.
        sizing_notional = total_notional * fx.rate * (1.0 - fx.sizing_buffer_pct / 100.0)

    entries: list[TierPlan] = []
    for idx, t in enumerate(spec.entry_tiers):
        limit = t.limit_price
        if limit <= 0:
            # Defense-in-depth — trade_setup generator already guards against
            # this. Skip the offending tier rather than the whole plan.
            continue
        alloc_pct = t.alloc_pct
        tier_notional = sizing_notional * (alloc_pct / 100.0)
        qty = max(0, math.floor(tier_notional / limit))
        entries.append(
            TierPlan(
                tier_index=idx,
                limit_price=limit,
                qty=qty,
                alloc_pct=alloc_pct,
                tag=t.tag,
            )
        )

    if not entries:
        raise TradeSetupNotPlannableError("no usable entry tiers after sanitisation")

    tranches = _build_tp_tranches(spec.tp_tranches)

    order_ttl_days = spec.order_ttl_days  # 0 sentinel → planner falls back to default

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
        fx=fx,
    )


def setup_plan_gross_notional(plan: SetupPlan) -> float:
    """The INSTRUMENT-currency gross a planner would commit if every tier filled.

    Used by the gross safety guard in the planner (block if cumulative would
    push past :func:`setup_plan_gross_guard_limit`).
    """
    return sum(t.qty * t.limit_price for t in plan.entry_tiers)


def setup_plan_gross_guard_limit(
    plan: SetupPlan,
    *,
    gross_safety_frac: float = GROSS_SAFETY_FRAC,
) -> float:
    """The gross-guard ceiling in INSTRUMENT currency (memo §4.3 item 7).

    The gross guard must compare in ONE currency: the equity side is
    converted through the plan's OWN :class:`FxConversion` rate (no second
    fetch — two fetches could straddle a tick and disagree with the
    journal), WITHOUT the sizing buffer (the buffer shrinks the deployed
    notional, not the safety ceiling). Same-currency plans compare raw.
    """
    rate = plan.fx.rate if plan.fx is not None else 1.0
    return gross_safety_frac * plan.paper_equity * rate


__all__ = [
    "FxConversion",
    "SetupPlan",
    "TierPlan",
    "TpTranchePlan",
    "TradeSetupNotPlannableError",
    "build_exit_geometry_spec",
    "compute_daily_scale_factor",
    "compute_setup_plan",
    "parse_brief_to_spec",
    "planned_blended_entry",
    "planned_blended_entry_from_spec",
    "setup_plan_gross_guard_limit",
    "setup_plan_gross_notional",
    "validate_trade_setup",
]

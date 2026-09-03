"""Pure position-sizing math for the paper-trade harness (the money-math half).

Translates an already-parsed :class:`~broker_contract.trade_intent.schema.TradeSpec`
into the concrete share quantities a planner would route to a broker. No I/O,
no broker SDK reach — this module is intentionally easy to test in isolation
and easy to reason about against the locked sizing formula in
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

Broker-manager extraction 2A-4a (design memo
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
§2.1/§2.3) relocated this money-math half into the shared, dependency-free
``broker_contract`` leaf. The brief-parsing / arm-time half
(``parse_brief_to_spec``, ``validate_trade_setup``, ``build_exit_geometry_spec``,
``planned_blended_entry``/``planned_blended_entry_from_spec``) stays client-side
in ``alphalens_pipeline.paper.sizing`` — it reads a thematic brief dict, a
client concern that must not leak into this leaf.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from broker_contract.constants import (
    EXPECTED_AVG_HOLD_DAYS,
    GROSS_SAFETY_FRAC,
    STEADY_STATE_GROSS_FRAC,
)
from broker_contract.fx import FxConversion
from broker_contract.trade_intent.schema import TpTrancheSpec, TradeSpec


@dataclass(frozen=True)
class TierPlan:
    """One entry-ladder tier rendered as a concrete share quantity."""

    tier_index: int
    limit_price: float
    qty: int
    alloc_pct: float
    tag: str
    # Mirrors EntryTierSpec.entry_mode (#1247) so the drain can partition the
    # SIZED ladder (now vs pullback) without re-reading the spec. Trailing
    # with a default: one suite constructs TierPlan positionally.
    entry_mode: str = "pullback"


@dataclass(frozen=True)
class TpTranchePlan:
    """One take-profit tranche kept as a reference for the exit reconciler.

    ``tranche_frac`` is a FRACTION of the position (0-1), deliberately NOT the
    same unit as the brief-shaped
    :class:`~broker_contract.trade_intent.schema.TpTrancheSpec.tranche_pct`,
    which is a PERCENTAGE (0-100). The two names differ because the units do;
    sharing one name is what let a live sizer and a fee estimator read the same
    number 100x apart. The single conversion happens in
    :func:`_build_tp_tranches`, exactly where ``alloc_pct`` is already divided
    by 100 for the entry side.
    """

    tranche_index: int
    target_price: float
    tranche_frac: float
    r_multiple: float
    tag: str

    def __post_init__(self) -> None:
        # A fraction outside [0, 1] is not a small mistake, it is the wrong
        # UNIT — and the unit is exactly what was ambiguous here. Refusing at
        # construction is what makes the 100x class unrepresentable rather than
        # merely fixed: a percentage-shaped 33.3 can no longer become a plan.
        #
        # The exception TYPE is load-bearing, and reachable: a brief's
        # ``tranche_pct`` is LLM-authored and range-checked nowhere (``paper.
        # sizing`` parses a bare ``float(raw.get("tranche_pct", 0.0))``), so an
        # over-100 weight arrives from real data. ``TradeSetupNotPlannableError``
        # is what the rail already expects for a malformed setup:
        # ``control_loop._resolve_and_size`` catches it and refuses the pick,
        # and because it subclasses ``ValueError`` the journal fold skips a
        # corrupt line instead of dying on it. A bare ``ValueError`` would sail
        # past the first and take the tick down — and on this rail a dead tick
        # means the never-naked protection pass never runs.
        if not 0.0 <= self.tranche_frac <= 1.0:
            raise TradeSetupNotPlannableError(
                f"tranche_frac must be a FRACTION of the position in [0, 1], got "
                f"{self.tranche_frac!r} — a percentage (0-100) belongs on "
                f"TpTrancheSpec.tranche_pct and is converted once by compute_setup_plan"
            )


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
            candidate that passed :func:`~alphalens_pipeline.paper.sizing.
            validate_trade_setup` today (i.e. verified + has a plannable
            setup). Order does not matter.
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
                # THE conversion, percent -> fraction, in exactly one place.
                # Its absence was the defect: the entry side divides alloc_pct
                # by 100 below, while this copied the brief's percentage
                # verbatim into a field the live exit sizer multiplies by.
                tranche_frac=t.tranche_pct / 100.0,
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
    """Turn an unsized :class:`~broker_contract.trade_intent.schema.TradeSpec`
    into a concrete :class:`SetupPlan`.

    Args:
        spec: parsed, unsized trade spec — see
            :func:`~alphalens_pipeline.paper.sizing.parse_brief_to_spec`.
        paper_equity: live account equity in the ACCOUNT currency.
        scale_factor: pre-computed daily scale factor from
            :func:`compute_daily_scale_factor`. Pass ``1.0`` for unit tests
            that want to inspect un-scaled sizing (rare; almost every prod
            day will scale < 1.0 given typical ``suggested_size_pct`` values).
        fx: ``None`` on the same-currency path (strict no-op — the plan is
            byte-identical to the pre-FX-leg output). When the instrument
            currency differs from the account currency the caller passes a
            policy-validated :class:`~broker_contract.fx.FxConversion`; the
            conversion is applied ONCE between the account-currency notional
            and the per-tier qty division. Prices (tier limits, targets,
            stop) are NEVER converted.

    Raises :class:`TradeSetupNotPlannableError` for the FX refusals
    (non-positive rate, same-currency ``FxConversion`` — same-currency must
    pass ``fx=None``) plus "no usable entry tiers after sanitisation" when
    every tier in ``spec`` has a non-positive ``limit_price``. The brief-side
    plannability checks (status != OK, missing ``suggested_size_pct``, …) now
    run earlier, inside
    :func:`~alphalens_pipeline.paper.sizing.parse_brief_to_spec` /
    :func:`~alphalens_pipeline.paper.sizing.validate_trade_setup`.
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
                # getattr: some suites size duck-typed spec-tier stubs that
                # carry only limit_price/alloc_pct (#1247).
                entry_mode=getattr(t, "entry_mode", "pullback"),
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
    converted through the plan's OWN :class:`~broker_contract.fx.FxConversion`
    rate (no second fetch — two fetches could straddle a tick and disagree
    with the journal), WITHOUT the sizing buffer (the buffer shrinks the
    deployed notional, not the safety ceiling). Same-currency plans compare
    raw.
    """
    rate = plan.fx.rate if plan.fx is not None else 1.0
    return gross_safety_frac * plan.paper_equity * rate


__all__ = [
    "SetupPlan",
    "TierPlan",
    "TpTranchePlan",
    "TradeSetupNotPlannableError",
    "compute_daily_scale_factor",
    "compute_setup_plan",
    "setup_plan_gross_guard_limit",
    "setup_plan_gross_notional",
]

"""Pure position-sizing math for the paper-trade harness (the money-math half).

Translates an already-parsed :class:`~broker_contract.trade_intent.schema.TradeSpec`
into the concrete share quantities a planner would route to a broker. No I/O,
no broker SDK reach — this module is intentionally easy to test in isolation
and easy to reason about against the locked sizing formula in
``docs/research/paper_trading_capital_sizing_2026_05_28.md`` §2.3 / §3.

Sizing math (#1467 — the document states the amount):

  total_notional    = spec.size.notional_acct            (account currency)
  sizing_notional   = total_notional                     (same currency)
                    | total_notional × rate × (1 − buffer/100)   (FX path)
  per_tier_notional = sizing_notional × (tier.alloc_pct / 100)
  per_tier_qty      = floor(per_tier_notional / tier.limit)

Before #1467 the spec carried a percent that was multiplied by an equity frame
the daemon read from its environment, so the same document sized differently
per deployment and could change size between arming and draining.

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
``broker_contract`` leaf. The brief-reading helpers (``validate_trade_setup``,
``planned_blended_entry``) stay client-side in ``alphalens_pipeline.paper.sizing``
— they read a thematic brief dict, a client concern that must not leak into this
leaf. The brief-to-spec parse went with the brief producer in #1552: every pick
is a hand-written document now.

``planned_blended_entry_from_spec`` was originally listed alongside its
dict-reading sibling as client-side, but the reason given never applied to it:
it takes a ``TradeSpec``, a type this module already imports and consumes. It
moved here for #1404, where the intent validator needs the planned blend to
refuse a take-profit at or below it. Re-implementing the arithmetic there would
have FORKED it from the dict path — the divergence class of issue #1114 — so the
function moved instead, and ``paper.sizing`` re-exports it: every existing caller
imports it from the same place as before.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from broker_contract.fx import FxConversion
from broker_contract.trade_intent.schema import TpTrancheSpec, TradeSpec


def _blend_priced_tiers(priced: list[tuple[float, float]]) -> float | None:
    """Shared alloc-weighted-mean arithmetic for the dict and spec blend paths.

    Weighted by the second element (alloc weight); equal-weight fallback when
    weights sum to 0; ``None`` for an empty ``priced`` list. Extracted so
    ``alphalens_pipeline.paper.sizing.planned_blended_entry`` and
    :func:`planned_blended_entry_from_spec` cannot drift — both must produce
    identical results for a brief ``setup`` and the spec that states the same
    tiers (PR-7).
    """
    if not priced:
        return None
    wsum = sum(w for _, w in priced)
    if wsum > 0:
        return sum(p * w for p, w in priced) / wsum
    return sum(p for p, _ in priced) / len(priced)


def planned_blended_entry_from_spec(spec: TradeSpec) -> float | None:
    """Alloc-weighted mean price over an already-parsed :class:`TradeSpec`.

    The arm-time (PR-7) mirror of
    ``alphalens_pipeline.paper.sizing.planned_blended_entry``: the daemon's
    geometry SHADOW stamp no longer has the raw brief dict at drain time (the
    parse moved to arm time), only the already-parsed ``TradeSpec`` carried on
    the :class:`~broker_contract.trade_intent.schema.TradeIntent`. Must
    return the SAME value ``planned_blended_entry(setup)`` would for the
    equivalent ``setup`` -- both routes share :func:`_blend_priced_tiers`.

    Returns ``None`` when there are no usable entry tiers (all non-positive
    ``limit_price``, or ``spec.entry_tiers`` is empty) -- never raises. The
    intent validator relies on that: a ladder it is about to refuse for having
    a non-positive price must not make the blend rule explode first.
    """
    priced = [(t.limit_price, t.alloc_pct) for t in spec.entry_tiers if t.limit_price > 0]
    return _blend_priced_tiers(priced)


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
    """The full per-candidate plan: the amount + ladder + exit references.

    Currencies (FX-leg design memo §4.2): ``total_notional`` is ACCOUNT
    currency, copied from ``TradeSpec.size.notional_acct``; ``entry_tiers`` limits,
    ``tp_tranches`` targets and ``disaster_stop`` are INSTRUMENT currency
    (prices are never converted). ``fx`` is ``None`` on the same-currency
    path (a strict no-op — the plan is byte-identical to the pre-FX-leg
    output); when set, :attr:`sizing_notional` is the buffered
    instrument-currency notional the qty division used.
    """

    total_notional: float
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
    fx: FxConversion | None = None,
) -> SetupPlan:
    """Turn an unsized :class:`~broker_contract.trade_intent.schema.TradeSpec`
    into a concrete :class:`SetupPlan`.

    Args:
        spec: the unsized trade spec of a TradeIntent document.
        fx: ``None`` on the same-currency path (strict no-op — the plan is
            byte-identical to the pre-FX-leg output). When the instrument
            currency differs from the account currency the caller passes a
            policy-validated :class:`~broker_contract.fx.FxConversion`; the
            conversion is applied ONCE between the account-currency notional
            and the per-tier qty division. Prices (tier limits, targets,
            stop) are NEVER converted.

    Raises :class:`TradeSetupNotPlannableError` for the FX refusals
    (non-positive rate, same-currency ``FxConversion`` — same-currency must
    pass ``fx=None``), a ``disaster_stop`` that is not a finite positive
    price, plus "no usable entry tiers after sanitisation" when
    every tier in ``spec`` has a non-positive ``limit_price``. Whether the
    document itself is coherent is the door's question
    (``trade_intent.validate.validate_intent``), answered before anything
    reaches here.
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

    disaster_stop = spec.disaster_stop
    # The drain sizes a decoded spec, so only the arming schema stood between a
    # legacy or hand-edited journal line and an entry that rests with no usable
    # stop. bool is an int subclass, hence the explicit exclusion.
    if (
        isinstance(disaster_stop, bool)
        or not isinstance(disaster_stop, int | float)
        or not math.isfinite(disaster_stop)
        or disaster_stop <= 0
    ):
        raise TradeSetupNotPlannableError(
            f"disaster_stop {disaster_stop!r} is not a finite positive price"
        )

    total_notional = float(spec.size.notional_acct)
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
        total_notional=total_notional,
        disaster_stop=disaster_stop,
        order_ttl_days=order_ttl_days,
        entry_tiers=tuple(entries),
        tp_tranches=tuple(tranches),
        fx=fx,
    )


def setup_plan_gross_notional(plan: SetupPlan) -> float:
    """The INSTRUMENT-currency gross a planner would commit if every tier filled.

    The daemon's fee floor, portfolio gross cap and cash floor read it
    (``control_loop``).
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

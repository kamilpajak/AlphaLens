"""Semantic validation of a :class:`TradeIntent` (#1404).

Until this module existed, every refusal protecting a real-money arm lived in
``alphalens_pipeline/brokers/automanager/manual_intent.py`` — the CLI layer. A
ladder whose allocations summed to 250, or a levered size, was refused only if
you happened to come in through our Typer command. ``schema.py`` said so about
itself: *"These are pure data: no validation, no parsing, no I/O. The
client-side parse/validate step is deferred to a later PR."* This is that PR,
and #1406's ``arm --from-intent`` door is the consumer whose absence deferred it.

What belongs here, and what does not
------------------------------------
These are invariants of the DOCUMENT. Three kinds of rule deliberately stay in
the CLI, because they are not about the document at all:

* **Operator vocabulary** — the ``price[:alloc]`` mini-DSL, the ``now@`` prefix,
  the ``R`` suffix, ``--no-tp`` with ``--tp``, the ``--size-pct``/``--notional``
  XOR. A third party sends structured JSON and never sees the mini-DSL. A mix of
  bare and explicit allocations is not even representable here: after
  compilation ``alloc_pct`` is always set.
* **Rules about the INVOCATION** — ``--ttl-days > 0`` and the supported-venue
  list. ``order_ttl_days == 0`` is a LEGAL document value: ``brokers/execution.py``
  resolves that sentinel to a default, commented "the planner's 'field absent'
  sentinel". Refusing it here would make a document the brief path legitimately
  emits un-submittable. The venue list is Saxo deployment knowledge (venue map,
  fee card, market-data entitlement) and stays with the adapter — the #1122
  decision, "the ADAPTER reports, never the contract decides".
* **Single-field shape and format** — ``meta.trade_date`` parsing as a date,
  ``schema_version`` bounds. That is the JSON Schema's job (#1405).

The enum rules below (``side``, ``entry_mode``) are the exception to that last
line, and the reason is measured rather than assumed: ``Literal`` is NOT enforced
at runtime. ``TradeSpec(side="short")`` constructs happily, and a whole document
carrying it decodes cleanly through ``intent_from_jsonable`` — the door's actual
entry point. The annotation is documentation, not a gate, so the gate goes here.

Every violation, not just the first
-----------------------------------
``message`` carries the FIRST violation in the order below, so the operator's
text is unchanged; ``details["violations"]`` carries them all, so a producer that
generated the document machine-side can fix it in one pass instead of a
submit-fix-submit loop. Element-wise rules also carry ``tier_index`` /
``tranche_index``: a client told only that *a* price is wrong, without being told
which, has a debugging message rather than a machine-readable failure.

``details["reason"]`` is the field to branch on, and publishing it is sanctioned
rather than a loophole — ``failure.py`` states details keys are unstable "unless
the published table names them for that code", and the README's ``intent_invalid``
row names ``reason``, ``violations`` and the two index keys.

Stdlib only, like the rest of the package (``dependencies = []`` on purpose).
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from broker_contract.failure import ContractError, Failure
from broker_contract.sizing import planned_blended_entry_from_spec
from broker_contract.trade_intent.schema import (
    ReanchorOnFill,
    TradeIntent,
    TradeSpec,
    TrailingStop,
)

__all__ = [
    "INTENT_INVALID_REASONS",
    "IntentInvalidError",
    "Violation",
    "validate_intent",
]

# The failure code every refusal here reports. Registered in
# `broker_contract.failure.CONTRACT_FAILURE_CODES`; the per-rule discriminator
# is `details["reason"]`, not a code of its own — fifteen top-level codes for one
# failure mode would be a worse table for a client to read.
INTENT_INVALID_CODE: Final = "intent_invalid"

# Percentage sums are checked against float noise only (33.3+33.3+33.4 is not
# exactly 100.0) — NEVER against sloppy input. 60+30 is a refusal, not a rescale.
_PCT_SUM_TOL: Final = 1e-6

_ALLOWED_ENTRY_MODES: Final = ("pullback", "immediate")


@dataclass(frozen=True, slots=True)
class Violation:
    """One broken rule, with the pointer a machine needs to locate it."""

    reason: str
    message: str
    where: Mapping[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": self.message, **dict(self.where)}


class IntentInvalidError(ContractError):
    """The submitted document is internally inconsistent; nothing was queued."""


INTENT_INVALID_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "numeric_not_finite": "A numeric field is NaN or infinite; every comparison on it "
        "would silently pass.",
        "intent_id_empty": "The client-authored idempotency key is missing or blank.",
        "ticker_empty": "The instrument carries no ticker.",
        "side_not_long": "Only long entries are armed today.",
        "entry_tiers_empty": "The entry ladder has no rungs.",
        "entry_price_non_positive": "An entry tier's limit price is zero or negative.",
        "entry_alloc_non_positive": "An entry tier's allocation is zero or negative.",
        "entry_mode_unknown": "An entry tier's mode is outside the known vocabulary.",
        "entry_alloc_sum": "The entry allocations do not sum to 100 (no silent rescaling).",
        "entry_price_duplicate": "Two entry tiers sit at the same price.",
        "immediate_tier_count": "More than one immediate entry tier.",
        "immediate_tier_not_first": "The immediate entry tier is not listed first.",
        "stop_non_positive": "The disaster stop is zero or negative.",
        "stop_above_entry": "The disaster stop is not below every entry tier.",
        "size_pct_out_of_range": "The suggested size is outside (0, 100] — a pick is never levered.",
        "tp_pct_non_positive": "A take-profit tranche percentage is zero or negative.",
        "tp_pct_sum_exceeds_100": "The take-profit tranche percentages exceed 100.",
        "tp_price_duplicate": "Two take-profit tranches sit at the same price.",
        "tp_price_below_blend": "A take-profit sits at or below the planned blend entry.",
        # The exit declaration (#1236).
        "reaction_plan_ambiguous": "More than one stop-management primitive is declared.",
        "reaction_kind_unsupported": "A declared reaction primitive cannot be honoured.",
        "reanchor_without_levels": "A re-anchor is declared with no initial levels beside it.",
        "ceiling_price_unsupported": "ceiling_price caps a take-profit, which this contract "
        "does not yet place.",
        "arm_trigger_r_non_positive": "The trailing arm trigger is zero or negative.",
        "trail_frac_out_of_range": "The trailing giveback fraction is outside (0, 1].",
        "k_atr_non_positive": "The re-anchor ATR multiple is zero or negative.",
    }
)


def _numeric_fields(spec: TradeSpec) -> Iterator[tuple[str, float, dict[str, Any]]]:
    """Every float the rules below compare, with the pointer to where it came from."""
    for index, tier in enumerate(spec.entry_tiers):
        where = {"tier_index": index}
        yield f"entry_tiers[{index}].limit_price", tier.limit_price, where
        yield f"entry_tiers[{index}].alloc_pct", tier.alloc_pct, where
    yield "disaster_stop", spec.disaster_stop, {}
    yield "suggested_size_pct", spec.suggested_size_pct, {}
    for index, tranche in enumerate(spec.tp_tranches):
        where = {"tranche_index": index}
        yield f"tp_tranches[{index}].price", tranche.price, where
        yield f"tp_tranches[{index}].tranche_pct", tranche.tranche_pct, where
        yield f"tp_tranches[{index}].r_multiple", tranche.r_multiple, where


def _finiteness_violations(spec: TradeSpec) -> list[Violation]:
    """Refuse NaN / infinity BEFORE any rule compares against it.

    A NaN answers False to every ordering comparison, so `limit_price <= 0`,
    `stop >= lowest` and the duplicate check all pass it silently — the document
    would look valid and reach the placement path. This cannot arrive through the
    CLI (`_parse_float` requires `math.isfinite`), but it can arrive through a
    submitted document: `json.loads` accepts a bare `NaN` literal by default and
    the codec carries it through untouched.
    """
    return [
        Violation(
            "numeric_not_finite",
            f"{name} must be a finite number, got {value!r}",
            {"field": name, **where},
        )
        for name, value, where in _numeric_fields(spec)
        if not math.isfinite(value)
    ]


def _identity_violations(intent: TradeIntent) -> list[Violation]:
    found: list[Violation] = []
    if not str(intent.intent_id).strip():
        found.append(Violation("intent_id_empty", "intent_id must be non-empty"))
    if not str(intent.instrument.ticker).strip():
        found.append(Violation("ticker_empty", "ticker must be non-empty"))
    if intent.spec.side != "long":
        found.append(Violation("side_not_long", f"side must be 'long', got {intent.spec.side!r}"))
    return found


def _entry_tier_violations(spec: TradeSpec) -> list[Violation]:
    tiers = spec.entry_tiers
    if not tiers:
        return [Violation("entry_tiers_empty", "at least one entry tier is required")]

    found: list[Violation] = []
    for index, tier in enumerate(tiers):
        where = {"tier_index": index}
        if tier.limit_price <= 0:
            found.append(
                Violation(
                    "entry_price_non_positive",
                    f"entry tier price must be positive, got {tier.limit_price:g} at tier {index}",
                    where,
                )
            )
        if tier.alloc_pct <= 0:
            found.append(
                Violation(
                    "entry_alloc_non_positive",
                    f"entry tier alloc_pct must be positive, got {tier.alloc_pct:g} "
                    f"at tier {index}",
                    where,
                )
            )
        if tier.entry_mode not in _ALLOWED_ENTRY_MODES:
            found.append(
                Violation(
                    "entry_mode_unknown",
                    f"entry tier entry_mode must be one of "
                    f"{', '.join(_ALLOWED_ENTRY_MODES)}, got {tier.entry_mode!r} "
                    f"at tier {index}",
                    where,
                )
            )

    alloc_sum = sum(t.alloc_pct for t in tiers)
    if abs(alloc_sum - 100.0) > _PCT_SUM_TOL:
        found.append(
            Violation(
                "entry_alloc_sum",
                f"entry tier allocations must sum to 100, got {alloc_sum:g} — no silent rescaling",
            )
        )

    prices = [t.limit_price for t in tiers]
    if len(set(prices)) != len(prices):
        # The same price twice is almost certainly a pasted-twice typo — the
        # deeper rung of such a ladder can never fill separately (and an
        # immediate tier's cap colliding with a pullback rung is the same class).
        found.append(Violation("entry_price_duplicate", f"duplicate entry tier price: {prices}"))

    immediate = [i for i, t in enumerate(tiers) if t.entry_mode == "immediate"]
    if len(immediate) > 1:
        found.append(
            Violation(
                "immediate_tier_count",
                "at most one immediate entry tier per pick — a second one is a new signal, re-arm",
            )
        )
    if immediate and immediate[0] != 0:
        # The day-1 gate and the watch router key on the first PULLBACK tier, so
        # an immediate tier hiding mid-ladder would corrupt both.
        found.append(
            Violation("immediate_tier_not_first", "the immediate entry tier must be listed first")
        )
    return found


def _stop_and_size_violations(spec: TradeSpec) -> list[Violation]:
    found: list[Violation] = []
    if spec.disaster_stop <= 0:
        found.append(
            Violation(
                "stop_non_positive",
                f"disaster stop must be positive, got {spec.disaster_stop:g}",
            )
        )
    # Judge the stop against the REAL rungs only. A tier with a non-positive
    # price is already refused by `entry_price_non_positive`, and letting it into
    # `min()` would report a stop that sits correctly below every real tier as
    # `stop_above_entry` — a false entry in a `violations` list a client acts on.
    real_prices = [t.limit_price for t in spec.entry_tiers if t.limit_price > 0]
    if real_prices:
        lowest = min(real_prices)
        if spec.disaster_stop >= lowest:
            found.append(
                Violation(
                    "stop_above_entry",
                    f"disaster stop {spec.disaster_stop:g} must sit below every entry "
                    f"tier (lowest tier {lowest:g})",
                )
            )
    if not 0.0 < spec.suggested_size_pct <= 100.0:
        found.append(
            Violation(
                "size_pct_out_of_range",
                "suggested_size_pct must satisfy 0 < size_pct <= 100, got "
                f"{spec.suggested_size_pct:g}",
            )
        )
    return found


def _tp_violations(spec: TradeSpec) -> list[Violation]:
    tranches = spec.tp_tranches
    if not tranches:
        # An empty ladder is the `--no-tp` shape: a legitimate pick that runs to
        # its disaster stop. Not a violation.
        return []

    found: list[Violation] = []
    for index, tranche in enumerate(tranches):
        if tranche.tranche_pct <= 0:
            found.append(
                Violation(
                    "tp_pct_non_positive",
                    f"take-profit tranche_pct must be positive, got "
                    f"{tranche.tranche_pct:g} at tranche {index}",
                    {"tranche_index": index},
                )
            )

    pct_sum = sum(t.tranche_pct for t in tranches)
    # ONE-SIDED on purpose, unlike the entry ladder's: summing to LESS than 100
    # means the pick deliberately leaves a runner.
    if pct_sum - 100.0 > _PCT_SUM_TOL:
        found.append(
            Violation(
                "tp_pct_sum_exceeds_100",
                f"take-profit tranche percentages exceed 100, got {pct_sum:g}",
            )
        )

    prices = [t.price for t in tranches]
    if len(set(prices)) != len(prices):
        found.append(Violation("tp_price_duplicate", f"duplicate take-profit price: {prices}"))

    blend = planned_blended_entry_from_spec(spec)
    if blend is not None:
        # Skipped when the blend is uncomputable (every tier non-positive). That
        # ladder is already refused by `entry_price_non_positive`; reporting a
        # blend rule on top of it would describe a consequence, not the cause.
        for index, tranche in enumerate(tranches):
            if tranche.price <= blend:
                found.append(
                    Violation(
                        "tp_price_below_blend",
                        f"take-profit price must be above the planned blend entry "
                        f"{blend:g}, got {tranche.price:g} at tranche {index}",
                        {"tranche_index": index},
                    )
                )
    return found


def _declared_numeric_fields(exit_spec: Any) -> Iterator[tuple[str, float, dict[str, Any]]]:
    """Every float a declaration rule compares, with its pointer."""
    for index, primitive in enumerate(exit_spec.reaction_plan):
        where = {"reaction_index": index}
        if isinstance(primitive, TrailingStop):
            yield "exit.reaction_plan.arm_trigger_r", primitive.arm_trigger_r, where
            yield "exit.reaction_plan.trail_frac", primitive.trail_frac, where
        elif isinstance(primitive, ReanchorOnFill):
            yield "exit.reaction_plan.k_atr", primitive.k_atr, where
            yield "exit.reaction_plan.atr", primitive.atr, where


def _exit_violations(intent: TradeIntent) -> list[Violation]:
    """The exit DECLARATION rules (#1236).

    A declaration the daemon cannot honour must be refused here, at arm time and
    out loud, rather than quietly turning into something else downstream. The
    daemon-side resolution degrades an unknown primitive to "never move the
    stop", which is the right thing inside a protection pass — but silence is
    exactly what a door must not do.
    """
    exit_spec = intent.exit
    if exit_spec is None:
        return []

    # Finiteness first, for the reason `_finiteness_violations` gives: a NaN
    # answers False to every ordering comparison, so the bounds rules below would
    # pass it in full.
    non_finite = [
        Violation(
            reason="numeric_not_finite",
            message=f"{field_name} is not a finite number",
            where=where,
        )
        for field_name, value, where in _declared_numeric_fields(exit_spec)
        if not math.isfinite(value)
    ]
    if non_finite:
        return non_finite

    violations: list[Violation] = []
    managing = [p for p in exit_spec.reaction_plan if isinstance(p, ReanchorOnFill | TrailingStop)]
    unsupported = [
        index
        for index, p in enumerate(exit_spec.reaction_plan)
        if not isinstance(p, ReanchorOnFill | TrailingStop)
    ]
    if len(managing) > 1:
        violations.append(
            Violation(
                reason="reaction_plan_ambiguous",
                message=(
                    f"{len(managing)} stop-management primitives declared — "
                    "the daemon manages one stop, so exactly one may be declared"
                ),
                where={},
            )
        )
    violations.extend(
        Violation(
            reason="reaction_kind_unsupported",
            message=(
                f"reaction primitive {exit_spec.reaction_plan[index].kind!r} cannot be "
                "honoured — its levels would arrive through a call that does not exist"
            ),
            where={"reaction_index": index},
        )
        for index in unsupported
    )

    for index, primitive in enumerate(exit_spec.reaction_plan):
        where = {"reaction_index": index}
        if isinstance(primitive, TrailingStop):
            if primitive.arm_trigger_r <= 0:
                violations.append(
                    Violation(
                        reason="arm_trigger_r_non_positive",
                        message=(
                            f"arm_trigger_r {primitive.arm_trigger_r} must be > 0 — "
                            "a trail armed at or before entry is not a trail"
                        ),
                        where=where,
                    )
                )
            if not 0 < primitive.trail_frac <= 1:
                violations.append(
                    Violation(
                        reason="trail_frac_out_of_range",
                        message=(
                            f"trail_frac {primitive.trail_frac} must be in (0, 1] — "
                            "it is the fraction of the excursion the stop gives back"
                        ),
                        where=where,
                    )
                )
        elif isinstance(primitive, ReanchorOnFill):
            if primitive.k_atr <= 0:
                violations.append(
                    Violation(
                        reason="k_atr_non_positive",
                        message=f"k_atr {primitive.k_atr} must be > 0",
                        where=where,
                    )
                )
            if exit_spec.initial_levels is None:
                violations.append(
                    Violation(
                        reason="reanchor_without_levels",
                        message=(
                            "a re-anchor is declared with no initial levels — the two "
                            "halves of the document disagree about what is placed"
                        ),
                        where=where,
                    )
                )
            if primitive.ceiling_price is not None:
                violations.append(
                    Violation(
                        reason="ceiling_price_unsupported",
                        message=(
                            "ceiling_price caps the take-profit, not the stop, so it is a "
                            "placement instruction this contract does not yet carry — "
                            "omit it rather than have it silently discarded"
                        ),
                        where=where,
                    )
                )
    return violations


def _collect(intent: TradeIntent) -> list[Violation]:
    """Every violation, in the published order: identity, ladder, stop, size, TP.

    The order mirrors the sequence the CLI builder used to check in, so an input
    breaking two rules reports the same one it reported before.
    """
    spec = intent.spec
    # Finiteness comes FIRST: every rule below is a comparison, and a NaN makes
    # each one silently answer "fine". Reporting a downstream rule on a NaN would
    # describe a consequence rather than the cause.
    non_finite = _finiteness_violations(spec)
    if non_finite:
        return non_finite
    return [
        *_identity_violations(intent),
        *_entry_tier_violations(spec),
        *_stop_and_size_violations(spec),
        *_tp_violations(spec),
        *_exit_violations(intent),
    ]


def _assert_reasons_are_registered(violations: Sequence[Violation]) -> None:
    unknown = sorted({v.reason for v in violations} - set(INTENT_INVALID_REASONS))
    if unknown:
        raise ValueError(f"unregistered intent-invalid reason(s): {unknown}")


def validate_intent(intent: TradeIntent) -> None:
    """Refuse a :class:`TradeIntent` that is internally inconsistent.

    Raises :class:`IntentInvalidError` carrying the one published failure shape.
    Returns ``None`` for a valid document — it never normalises and never
    rescales: this arms real money, so a malformed level must explode loudly.
    """
    violations = _collect(intent)
    if not violations:
        return
    _assert_reasons_are_registered(violations)
    first = violations[0]
    raise IntentInvalidError(
        Failure(
            code=INTENT_INVALID_CODE,
            message=first.message,
            retryable=False,
            details={
                "reason": first.reason,
                **dict(first.where),
                "violations": [v.to_jsonable() for v in violations],
            },
        )
    )

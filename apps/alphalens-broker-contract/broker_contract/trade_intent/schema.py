"""Pure value types for the Boundary-2 wire schema (client <-> broker-manager).

Formalizes the shape that today crosses as an armed-pick dict plus the
brief-side setup dict computed by ``compute_setup_plan`` (memo section 2.3):
an unsized :class:`TradeSpec` (entry ladder + disaster stop + TP ladder +
suggested sizing), paired with an :class:`ExitGeometrySpec` describing the
client-precomputed initial stop/TP levels and an optional, bounded-vocabulary
reaction plan the executor evaluates after fill (memo revision R3). These are
pure data: no validation, no parsing, no I/O. The client-side parse/validate
step is deferred to a later PR in the broker-manager extraction sequence (see
``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``).

Immutability caveat: the tuple-typed fields (``entry_tiers``, ``tp_tranches``,
``reaction_plan``) are declared ``tuple`` and callers MUST pass actual tuples.
``@dataclass(frozen=True)`` freezes the reference, not the container, and no
``__post_init__`` coercion is done by design (pure data) — the deferred client
parse step is the single place that constructs these, so it is responsible for
passing tuples, not lists.
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, field
from typing import Any, Literal

from broker_contract.constants import DEFAULT_ORDER_TTL_DAYS

# Keywords the JSON Schema generator derives from the field itself (#1405). A
# `json_schema` block may add to the published node, never redefine these.
_GENERATOR_OWNED_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "enum",
        "items",
        "properties",
        "required",
        "description",
        "default",
        "$ref",
        "anyOf",
        "oneOf",
    }
)


def contract_field(
    meaning: str,
    *,
    default: Any = MISSING,
    json_schema: dict[str, Any] | None = None,
) -> Any:
    """Declare a contract field together with what its value MEANS.

    A type says ``float``; it does not say that ``alloc_pct`` is a percentage
    while ``trail_frac`` is a fraction, that ``order_ttl_days`` counts XNYS
    sessions, or that ``limit_price`` is a cap on an immediate tier. That is
    exactly what a third-party producer gets wrong, so it is stated here, on the
    field, and the JSON Schema generator (#1405) reads it from here rather than
    keeping a second table that can drift.

    ``json_schema`` adds keywords to the generated schema. It is deliberately
    rare: numeric bounds belong to ``validate_intent``, and mirroring one here
    would give a single rule two owners. Today exactly one field uses it, pinned
    by ``tests/trade_intent/test_contract_metadata.py``.

    An empty meaning raises, because a gate satisfiable by ``""`` is not a gate.
    A ``json_schema`` block that redefines something the generator already emits
    from the field itself raises too: ``description`` there would silently REPLACE
    the meaning in the published document, and the allowlist gate counts fields
    rather than reading inside them.
    """
    if not meaning.strip():
        raise ValueError("a contract field must say what its value means")
    generated = _GENERATOR_OWNED_KEYWORDS.intersection(json_schema or ())
    if generated:
        raise ValueError(
            f"json_schema must not set {sorted(generated)} — the generator emits "
            "those from the field itself, and an override here would be invisible"
        )
    metadata: dict[str, Any] = {"meaning": meaning}
    if json_schema:
        metadata["json_schema"] = dict(json_schema)
    if default is MISSING:
        return field(metadata=metadata)
    return field(default=default, metadata=metadata)


# Wire schema version stamped on every TradeSpec / IntentMeta instance so a
# future broker-manager can detect + reject stale client payloads.
# "2": EntryTierSpec.entry_mode added (#1247 immediate-entry tiers). Old "1"
# payloads decode unchanged (absent key -> the dataclass default applies).
SCHEMA_VERSION = "2"

# Reserved multi-tenant dimension. A single account is live today; the field
# exists so a future multi-account broker-manager does not need a schema
# migration to add it.
DEFAULT_ACCOUNT_ID = "default"


@dataclass(frozen=True)
class InstrumentHint:
    """Identifies the tradable instrument (memo section 2.3 Boundary-2 contract)."""

    ticker: str = contract_field('Exchange symbol, vendor-agnostic, e.g. "NVDA".')
    mic: str = contract_field(
        'ISO 10383 Market Identifier Code, e.g. "XNYS". WHICH venues a deployment '
        "can actually trade is the adapter's knowledge, not this contract's (#1122)."
    )


@dataclass(frozen=True)
class EntryTierSpec:
    """One rung of the client-computed entry ladder.

    ``alloc_pct`` is a PERCENTAGE (0-100), not a fraction; the tiers sum to
    ~100 across the ladder. This matches ``compute_setup_plan``, which sizes
    each tier as ``total_notional * (alloc_pct / 100)`` (paper/sizing.py).
    ``tag`` mirrors the brief entry tier's free-text label (e.g. "T1"); it
    carries no sizing semantics.

    ``entry_mode`` (#1247): ``"pullback"`` is a resting rung below the market
    (today's only shape); ``"immediate"`` marks an arm-manual "now" tranche —
    for it ``limit_price`` is the operator's CAP (max acceptable fill), not a
    pullback level. NOTE: the entry-trail journal's ``watch_open`` lines carry
    an unrelated top-level ``entry_mode`` cohort tag — different JSON
    namespace, name collision only.
    """

    limit_price: float = contract_field(
        "Price in the instrument's currency. For an immediate tier this is the "
        "operator's CAP (the worst acceptable fill), not a pullback level."
    )
    alloc_pct: float = contract_field(
        "PERCENTAGE of the pick's notional, 0-100 — not a fraction. The rungs sum to 100."
    )
    tag: str = contract_field('Free-text rung label, e.g. "T1". No sizing semantics.', default="")
    entry_mode: Literal["pullback", "immediate"] = contract_field(
        '"pullback" rests below the market; "immediate" is an arm-manual now tranche.',
        default="pullback",
    )


@dataclass(frozen=True)
class TpTrancheSpec:
    """One take-profit tranche of the client-computed TP ladder.

    Mirrors the brief TP tranche shape exactly: ``price`` is the target
    price, ``tranche_pct`` is a PERCENTAGE (0-100, matching the entry-tier
    convention and ``compute_setup_plan``'s tranche sizing), ``r_multiple``
    is the tranche's R-multiple label, and ``tag`` is the tranche's
    free-text label (e.g. "TP1"). ``r_multiple``/``tag`` carry no sizing
    semantics of their own.
    """

    price: float = contract_field("Take-profit price in the instrument's currency.")
    tranche_pct: float = contract_field(
        "PERCENTAGE of the position, 0-100 — not a fraction. The ladder MAY sum to "
        "less than 100: that is a deliberate runner, not an error."
    )
    r_multiple: float = contract_field(
        "Label only: the tranche's distance in R (multiples of initial risk). No sizing semantics.",
        default=0.0,
    )
    tag: str = contract_field(
        'Free-text tranche label, e.g. "TP1". No sizing semantics.', default=""
    )


@dataclass(frozen=True)
class TradeSpec:
    """Formalizes ``compute_setup_plan``'s UNSIZED dict input (memo section 2.3).

    ``suggested_size_pct`` is a PERCENTAGE (0-100) of account equity, matching
    ``compute_setup_plan``'s ``suggested_size_pct / 100 * equity`` sizing.
    """

    entry_tiers: tuple[EntryTierSpec, ...] = contract_field("The entry ladder, rung by rung.")
    disaster_stop: float = contract_field(
        "Stop price in the instrument's currency; sits below every entry rung."
    )
    tp_tranches: tuple[TpTrancheSpec, ...] = contract_field(
        "The take-profit ladder. Empty is the legitimate --no-tp shape: a pick that "
        "runs to its disaster stop."
    )
    suggested_size_pct: float = contract_field(
        "PERCENTAGE of account equity, 0-100 — not a fraction."
    )
    order_ttl_days: int = contract_field(
        "Entry-order lifetime in TRADING days (XNYS), not calendar days. 0 is the "
        'planner\'s "field absent" sentinel and resolves to the default downstream.',
        default=DEFAULT_ORDER_TTL_DAYS,
    )
    side: Literal["long"] = contract_field(
        "Only long entries are armed today. Pinned to one literal as a barrier "
        'against the client-side "Sell"->Buy footgun rather than left open.',
        default="long",
    )
    schema_version: str = contract_field(
        "Wire version of this spec. The same constant as meta.schema_version, not an "
        "independent dial.",
        default=SCHEMA_VERSION,
    )


@dataclass(frozen=True)
class InitialLevels:
    """Client-precomputed stop/TP pair (via the ``exit_geometry`` leaf)."""

    stop: float = contract_field("Stop price to place, in the instrument's currency.")
    tp: float = contract_field("Take-profit price to place, in the instrument's currency.")


@dataclass(frozen=True)
class ReanchorOnFill:
    """bezpazery + the memo section 4.3 P0 fix: re-anchor stop/TP on fill-complete.

    On fill-complete the executor sets stop/TP to ``avg_price +/- k_atr *
    atr`` (capped at ``ceiling_price`` when given). Carries the absolute
    ``atr`` snapshot (not just the multiplier) so the executor can recompute
    the new levels without a second data fetch.
    """

    k_atr: float = contract_field(
        "ATR multiple the re-anchored stop sits away from the fill price."
    )
    atr: float = contract_field(
        "Absolute ATR snapshot — a distance in the instrument's currency, not a "
        "multiple — so the executor recomputes the levels without a second fetch."
    )
    ceiling_price: float | None = contract_field(
        "Caps the TAKE-PROFIT (tp = min(tp, ceiling_price)); it never touches the "
        "stop. The door refuses a non-null value today: ceiling_price_unsupported.",
        default=None,
    )
    kind: Literal["reanchor_on_fill"] = contract_field(
        "Discriminator of the reaction-plan union.", default="reanchor_on_fill"
    )


@dataclass(frozen=True)
class TrailingStop:
    """be_0p5r_trail0p6: arm break-even at ``arm_trigger_r`` MFE, then trail.

    Once favorable excursion reaches ``arm_trigger_r`` multiples of initial
    risk, the executor arms a trailing stop that follows ``trail_frac`` of
    the peak favorable excursion.
    """

    arm_trigger_r: float = contract_field(
        "Favourable excursion, in R (multiples of initial risk), at which the trail arms."
    )
    trail_frac: float = contract_field(
        "FRACTION in (0, 1] of the peak excursion the stop gives back — a fraction "
        "here, unlike the _pct fields elsewhere in this contract."
    )
    kind: Literal["trailing_stop"] = contract_field(
        "Discriminator of the reaction-plan union.", default="trailing_stop"
    )


@dataclass(frozen=True)
class ModelPush:
    """Reserved escape hatch for a model/ML-pushed level (memo revision R3).

    Levels for this primitive arrive via a later ``amend_exit`` call, not via
    fields on this dataclass — it exists only to reserve the ``kind`` tag in
    the discriminated union.
    """

    kind: Literal["model"] = contract_field(
        "Discriminator of the reaction-plan union; reserves the tag and nothing else.",
        default="model",
    )


# Discriminated union of the reaction-plan primitives the executor evaluates
# post-fill. Bounded vocabulary by design (memo revision R3): the executor
# dispatches on the ``kind`` literal, never on an open-ended callable.
ReactionPrimitive = ReanchorOnFill | TrailingStop | ModelPush


@dataclass(frozen=True)
class ExitGeometrySpec:
    """Optional initial levels plus an optional reaction plan (memo revision R3).

    ``initial_levels`` is ``None`` when the document supplies no levels to place.
    It was required until #1236, which made "declares how the stop is managed,
    supplies no geometry" unrepresentable — and that is the shape of a pick whose
    exit is a trail rather than a client-computed bracket. The two halves are
    independent: levels say what to PLACE, the reaction plan says how the stop
    then MOVES.

    Consumers must therefore treat a present ``exit`` as no guarantee of levels.
    On the daemon side that check lives in exactly one predicate
    (``control_loop._places_client_geometry``) rather than at each of the sites
    that dereference them."""

    initial_levels: InitialLevels | None = contract_field(
        "Levels to PLACE, or null when the document supplies none.", default=None
    )
    reaction_plan: tuple[ReactionPrimitive, ...] = contract_field(
        "How the stop MOVES after fill. An empty plan means the stop is never "
        "moved. The door accepts at most ONE stop-management primitive and "
        "refuses a second with reaction_plan_ambiguous — that rule lives in "
        "validate_intent, not in this document's shape.",
        default=(),
    )


@dataclass(frozen=True)
class IntentMeta:
    """Wire-friendly provenance for a :class:`TradeIntent` (no datetime dep)."""

    armed_ts: str = contract_field("ISO-8601 timestamp of the moment the pick was armed.")
    trade_date: str = contract_field(
        "YYYY-MM-DD. Date key of the record — pick identity, TTL anchor, reconcile "
        "join. For a manual pick (no brief row) this is the arm date; provenance "
        "lives in `source`, not in this field's name (#1252)."
    )
    schema_version: str = contract_field(
        "Wire version of the document. A consumer reads this one.", default=SCHEMA_VERSION
    )
    source: Literal["brief", "manual"] = contract_field(
        'Where the intent came from: "brief" (parsed from a brief row by `broker arm`) '
        'or "manual" (operator-provided levels via `broker arm-manual`, #1235). '
        "Journals and later measurement separate the two populations on this marker; "
        'legacy payloads without the key decode to "brief".',
        default="brief",
    )
    # `broker arm-manual` assigns 1 + the highest generation already queued for
    # (ticker, trade_date); a disarmed generation never comes back. Generation 1
    # keeps the pre-#1371 identity strings byte-for-byte, so every journal line
    # written before the field existed — and every payload that omits it — is
    # generation 1.
    generation: int = contract_field(
        "Same-day re-arm counter, 1-based (#1371). Pick identity everywhere "
        "downstream — queue fold, submissions join, entry-watch crids, stop refs — "
        "is (ticker, trade_date, generation), and the queue keeps the LATEST, so a "
        "resubmission REPLACES rather than duplicates.",
        default=1,
        json_schema={"minimum": 1},
    )


@dataclass(frozen=True)
class TradeIntent:
    """The Boundary-2 wire type: client-armed pick handed to the broker-manager.

    See ``docs/research/broker_manager_extraction_and_exit_geometry_2026_07_31.md``
    section 2.3 for the contract this formalizes.

    ``exit`` is ``None`` when no geometry bracket is buildable from the source
    brief (missing/degenerate ATR, no usable entry tiers, a non-constructible
    bracket) — mirrors the daemon's pre-PR-7 ``exit_spec=None`` path, where the
    placement falls back to the brief's static disaster-stop / tier TP levels
    (memo section 5, PR-7).
    """

    intent_id: str = contract_field("Client-authored idempotency key.")
    instrument: InstrumentHint = contract_field("What is being traded, and where.")
    spec: TradeSpec = contract_field("The unsized trade: entry ladder, stop, TP ladder, size.")
    meta: IntentMeta = contract_field("Provenance and identity of this document.")
    # Field name "exit" per the memo contract — deliberately shadows the
    # builtin `exit` as an attribute (safe: instance attribute, never called).
    exit: ExitGeometrySpec | None = contract_field(
        "Optional exit geometry: the levels to place, and how the stop is managed "
        "afterwards. Null when the source brief yields no buildable bracket.",
        default=None,
    )
    account_id: str = contract_field(
        "Reserved tenant dimension; one value today.", default=DEFAULT_ACCOUNT_ID
    )

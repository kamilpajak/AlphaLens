"""The document DECLARED as pending orders and reactions (spec sections 4.2, 4.3.1).

Four steps, in this order, and the order is the contract:

  1. the reading pass   every path whose value the plan carries is read through
                        a recorder, which is what the gate below is given
  2. the fifth gate     `check_classified`: a path the replay neither reads,
                        translates nor lists as out of scope is `path_unclassified`
  3. the entry mode     a tranche that is not a resting rung is refused
                        (`entry_mode_unsupported`, `details.tiers`)
  4. the plan           what the document declares, and nothing else

The gate precedes the refusal because section 4.3.1 calls it a FIFTH gate of
the door's chain: a document is understood before it is interpreted. That is
also why the reading pass reads every entry tier, including one this version
refuses — otherwise a document whose only tier is `immediate` would be answered
with a path it declared rather than with the code that names its real problem.

**This module does not say which declared level RESTS at a broker, and that is
deliberate.** Spec section 5.1 says a document supplying `exit.initial_levels`
rests that stop; run against the deployed daemon it does not. The `planned`
journal line is the one the protection pass places and both stop arms read as
the never-below floor and the 1R denominator, and on the entry-trail path that
line is written from `spec.disaster_stop` (`control_loop._journal_entry_planned_disaster`);
the geometry-aware writer lives only on the classic bracket path, which a
document supplying levels cannot reach on either deployment. So the plan
carries the floor AND the declared levels, and the walk chooses once the
contradiction is decided.

The invariant that makes the gate's proof honest: **every path the reader
records lands in a field of the plan, and every field of the plan comes from a
recorded path.** Under it no read here is an echo in the sense of section
4.3.1 — in any shape of document — so the classes of that section need no
widening and the gate keeps its refuting power for every path it covers.

**The stated conversion reaches exactly two things here (#1592).** The sizing
buffer comes off the budget ONCE, on the total, before the ladder splits it —
which is where ``broker_contract.sizing`` applies it, so this is the only
split that reproduces the drain's. And ``spec.size.currency`` is READ: its
value is the account code the configuration was parsed against, so it decides
the fx key set and can therefore cause a refusal, which is what moves it out of
section 4.3.1's out-of-scope list.

The RATE reaches nothing here, and that is a property rather than an omission.
Every sizing site divides a notional by a price, and an account-currency
notional over an instrument-currency price is a share count scaled by the rate,
so the rate cancels in every ratio the walk takes. Only the cost gate needs it,
because only the gate compares ``min_commission`` — an instrument-currency
magnitude — against a notional.

What this module still leaves out: whole-share flooring, which is a venue fact
the configuration does not carry and the step-1 plan's recorded scope cut; the
bar walk; the tie convention; and the result envelope.

This module takes a document the DOOR has already admitted, and says so here
because it relies on that: the entry ladder is non-empty, the allocations sum
to 100, the stop sits below every rung, at most one stop-management primitive
is declared and every declared kind is one the contract honours. None of those
rules is re-checked here — a stricter copy would refuse a document the door
admits. A path this module cannot navigate, or a value of the wrong type, is a
programming error and surfaces as a traceback rather than as a refusal, the
rule ``refusal.py`` states for the package.

ENGINE module: stdlib and ``broker_contract`` only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from broker_contract.failure import ContractError
from broker_contract.trade_intent.schema import ReactionPrimitive, TradeIntent

from intent_replay.classification import check_classified
from intent_replay.fx import Fx
from intent_replay.refusal import refuse

__all__ = [
    "ENTRY_MODE_UNSUPPORTED_CODE",
    "HONOURED_REACTION_KINDS",
    "PULLBACK_MODE",
    "DeclaredTranche",
    "EntryModeUnsupportedError",
    "PendingEntry",
    "Plan",
    "interpret",
]

ENTRY_MODE_UNSUPPORTED_CODE: Final = "entry_mode_unsupported"

# The one entry shape this version models: a rung resting below the market,
# which a bar walk can test. `immediate` is bought AT DRAIN, where the limit is
# the operator's cap rather than a level, so a walk asking "did the low touch
# the limit" would replay a different order (spec section 8.1).
PULLBACK_MODE: Final = "pullback"

REANCHOR_KIND: Final = "reanchor_on_fill"
TRAILING_KIND: Final = "trailing_stop"

# The inputs each honoured arm needs, read so the gate of section 4.3.1 sees
# them. A kind absent from this table is refused rather than ignored: the
# contract's `resolve_declared_policy` DEGRADES a primitive it cannot honour to
# the inert policy, which would turn "this is not implemented" into the
# plausible number "the stop never moved".
_REACTION_INPUTS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        REANCHOR_KIND: ("k_atr", "atr"),
        TRAILING_KIND: ("arm_trigger_r", "trail_frac"),
    }
)

HONOURED_REACTION_KINDS: Final[frozenset[str]] = frozenset(_REACTION_INPUTS)


class EntryModeUnsupportedError(ContractError):
    """The document declares an entry tranche v1 does not model; nothing was computed."""


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """One rung of the entry ladder, resting at its limit.

    ``notional`` is this rung's share of the BUFFERED budget in the ACCOUNT
    currency — not a share quantity. Turning it into shares needs the stated
    rate (``Fx.to_shares``) and the venue's quantity lattice, and the lattice is
    the one of the two the configuration still does not carry.
    """

    tier_index: int
    limit_price: float
    notional: float


@dataclass(frozen=True, slots=True)
class DeclaredTranche:
    """One take-profit tranche the document declared.

    ``fraction`` is ``tranche_pct / 100`` — a FRACTION, the unit the daemon's
    own ``TpTranchePlan`` uses, because the 100x confusion between the two
    units has cost this project a live defect before. It carries no range check
    of its own: ``validate_intent`` owns that rule and tolerates 1e-6 of float
    noise on the ladder's sum, so a bound here would refuse a document the door
    admits, and would do it with a traceback instead of a published refusal.
    """

    tranche_index: int
    price: float
    fraction: float


@dataclass(frozen=True, slots=True)
class Plan:
    """What the document DECLARES. Nothing here claims to rest at a broker.

    ``declared_floor`` is ``spec.disaster_stop``; ``declared_stop`` and
    ``declared_take_profit`` are the pair the document supplied itself, or
    ``None``; ``declared_tranches`` is the author's take-profit ladder;
    ``reaction`` is the decoded primitive the walk hands to
    ``broker_contract.stop_decision.decide_stop``. ``read`` is the set of wire
    paths this plan was built from — the universe the classification gate of
    section 4.3.1 subtracts.

    ``notional`` is the budget the document STATES and nothing rescales it;
    ``sizing_notional`` is what the entry ladder actually splits, after the
    settlement-drift buffer the drain also withholds. Two fields rather than
    one, because section 2 forbids rescaling the stated size and a single field
    would make the two indistinguishable in the result — and because the walk
    needs both: it accumulates against the second and the published
    ``filled_fraction`` is a fraction OF the second.
    """

    entries: tuple[PendingEntry, ...]
    notional: float
    sizing_notional: float
    account_currency: str
    declared_floor: float
    declared_stop: float | None
    declared_take_profit: float | None
    declared_tranches: tuple[DeclaredTranche, ...]
    reaction: ReactionPrimitive | None
    read: frozenset[str]


class _Reader:
    """Reads a value off the decoded document and RECORDS the wire path it came from.

    The path string is the navigation instruction, so it cannot drift from the
    value: a wrong path is an ``AttributeError`` here rather than a quiet
    disagreement between a hand-typed list and the code. Only LEAVES are
    recorded — a container is classified by ``classification.CONTAINERS``, so
    entering one is not a read.
    """

    __slots__ = ("_node", "_prefix", "_read")

    def __init__(self, node: Any, prefix: str, read: set[str]) -> None:
        self._node = node
        self._prefix = prefix
        self._read = read

    @property
    def node(self) -> Any:
        """The object this reader reads. Handing it on is not a read of its fields.

        It is the decoded object itself, shared with the caller's intent and
        frozen; the plan carries it rather than a copy, so a field the copy
        would have dropped cannot go missing.
        """
        return self._node

    def _path(self, path: str) -> str:
        return f"{self._prefix}.{path}" if self._prefix else path

    def _navigate(self, path: str) -> Any:
        node = self._node
        for name in path.split("."):
            node = getattr(node, name)
        return node

    def number(self, path: str) -> float:
        """A numeric leaf, as a float. An int is a number; ``True`` is not.

        ``json.loads`` gives an int for ``1500`` and the codec keeps it, so the
        int case is a document the arming door admits. ``bool`` is excluded
        deliberately — it is an ``int`` in Python, and a budget of ``True``
        would read as 1.0.
        """
        value = self._navigate(path)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"{self._path(path)} is not a number: {value!r}")
        self._read.add(self._path(path))
        return float(value)

    def text(self, path: str) -> str:
        """A string leaf."""
        value = self._navigate(path)
        self._read.add(self._path(path))
        return str(value)

    def optional(self, path: str) -> _Reader | None:
        """A reader over a container, or ``None`` when the document has none."""
        node = self._navigate(path)
        return None if node is None else _Reader(node, self._path(path), self._read)

    def each(self, path: str) -> Iterator[_Reader]:
        """A reader per item of a sequence; every item shares one ``[]`` path."""
        for item in self._navigate(path):
            yield _Reader(item, f"{self._path(path)}[]", self._read)


def _entry_ladder(
    reader: _Reader, notional: float
) -> tuple[tuple[PendingEntry, ...], tuple[int, ...]]:
    """The resting rungs, and the tiers this version does not model.

    Every tier's three fields are read, including an unmodelled tier's: the
    gate runs before the refusal, and a tier left unread would hand the caller
    a path refusal instead of the entry-mode one.
    """
    entries: list[PendingEntry] = []
    unsupported: list[int] = []
    for index, tier in enumerate(reader.each("spec.entry_tiers")):
        limit_price = tier.number("limit_price")
        alloc_pct = tier.number("alloc_pct")
        mode = tier.text("entry_mode")
        if mode != PULLBACK_MODE:
            unsupported.append(index)
            continue
        entries.append(
            PendingEntry(
                tier_index=index, limit_price=limit_price, notional=notional * alloc_pct / 100.0
            )
        )
    return tuple(entries), tuple(unsupported)


def _declared_tranches(reader: _Reader) -> tuple[DeclaredTranche, ...]:
    return tuple(
        DeclaredTranche(
            tranche_index=index,
            price=tranche.number("price"),
            fraction=tranche.number("tranche_pct") / 100.0,
        )
        for index, tranche in enumerate(reader.each("spec.tp_tranches"))
    )


def _declared_levels(exit_reader: _Reader | None) -> tuple[float | None, float | None]:
    """The stop/take-profit pair the document supplied itself, or two ``None``."""
    levels = None if exit_reader is None else exit_reader.optional("initial_levels")
    if levels is None:
        return None, None
    return levels.number("stop"), levels.number("tp")


def _declared_reaction(exit_reader: _Reader | None) -> ReactionPrimitive | None:
    """The declared stop-management primitive, with its own inputs read.

    ``validate_intent`` admits at most one such primitive and refuses every
    kind this table does not carry, so the loop returns the only one there is.
    The refusal below is therefore a programming error rather than a published
    code — and it must stay loud, because the alternative is the inert policy.
    """
    if exit_reader is None:
        return None
    declared: ReactionPrimitive | None = None
    for item in exit_reader.each("reaction_plan"):
        if declared is not None:
            # The plan holds ONE primitive. Reading a second one's inputs would
            # record paths whose values land in no field, which is the invariant
            # this module rests on, and skipping them would hide a declaration
            # the walk will not honour. The door refuses the shape
            # (`reaction_plan_ambiguous`), so this is a programming error — and
            # it stays local, because the engine's API is callable without it.
            raise ValueError(
                "the plan holds one stop-management primitive and this document declares "
                "more; the door refuses that shape, so reaching here would read the second "
                "primitive's inputs into no field at all"
            )
        kind = item.text("kind")
        inputs = _REACTION_INPUTS.get(kind)
        if inputs is None:
            raise ValueError(
                f"reaction kind {kind!r} is not one of {sorted(HONOURED_REACTION_KINDS)}: "
                "resolving it would DEGRADE to the inert policy and report a stop that "
                "never moved (spec section 4.3.1)"
            )
        for name in inputs:
            item.number(name)
        if declared is None:
            declared = item.node
    return declared


def _check_one_account(account_currency: str, fx: Fx) -> None:
    """The document and the conversion must name ONE account currency.

    They cannot disagree through the CLI, which derives both from this same
    document. ``interpret`` is public, and a configuration parsed against
    another account currency chose a different fx key set, so the conversion it
    produced describes a different run. A programming error rather than a
    published refusal, which is the rule ``refusal.py`` states for the package.
    """
    if account_currency != fx.account_currency:
        raise ValueError(
            f"the document states spec.size.currency {account_currency!r} and the conversion "
            f"was parsed against {fx.account_currency!r}; the two decide the fx key set "
            "together, so one of them is from another run"
        )


def _refuse_unsupported_entry_modes(tiers: tuple[int, ...]) -> None:
    if not tiers:
        return
    raise refuse(
        EntryModeUnsupportedError,
        ENTRY_MODE_UNSUPPORTED_CODE,
        f"entry tier(s) {', '.join(str(tier) for tier in tiers)} declare an entry_mode this "
        f"version does not model; only {PULLBACK_MODE!r} rests as a rung a bar walk can test",
        tiers=list(tiers),
    )


def interpret(intent: TradeIntent, document: Mapping[str, Any], *, fx: Fx) -> Plan:
    """Read ``intent``, check ``document`` against the path classes, return the plan.

    ``document`` is the COMPLETED wire document (``door.Admitted.document``),
    which is the classification gate's universe; the values all come from
    ``intent``, because decoding is the contract codec's job and this module
    does not parse a second time.

    ``fx`` is keyword-only and has no default. A default would be a conversion
    this module states on the caller's behalf, which is what section 2.1 rules
    out — and the same-currency value is not a safe default either: it would
    silently drop a stated buffer and publish a budget the drain never spends.
    """
    read: set[str] = set()
    reader = _Reader(intent, "", read)
    notional = reader.number("spec.size.notional_acct")
    account_currency = reader.text("spec.size.currency")
    _check_one_account(account_currency, fx)
    sizing_notional = fx.sizing_notional(notional)
    entries, unsupported = _entry_ladder(reader, sizing_notional)
    declared_floor = reader.number("spec.disaster_stop")
    declared_tranches = _declared_tranches(reader)
    exit_reader = reader.optional("exit")
    declared_stop, declared_take_profit = _declared_levels(exit_reader)
    reaction = _declared_reaction(exit_reader)
    check_classified(document, read)
    _refuse_unsupported_entry_modes(unsupported)
    return Plan(
        entries=entries,
        notional=notional,
        sizing_notional=sizing_notional,
        account_currency=account_currency,
        declared_floor=declared_floor,
        declared_stop=declared_stop,
        declared_take_profit=declared_take_profit,
        declared_tranches=declared_tranches,
        reaction=reaction,
        read=frozenset(read),
    )

"""The result envelope and the stream line (spec sections 5, 5.1, 5.2 and 5.3).

ENGINE module: stdlib, ``broker_contract`` and this package only.

What this module publishes is ONE DECLARED RULE and a FREQUENCY, and nothing
wider. ``intrabar_rule`` names the order the walk resolves a bar in;
``snu_bars`` counts the bars on which that order had to decide something the
tape does not answer. Neither makes ``pnl_cash`` a bound: section 4.4 carries
the citations for why a laddered document need not have a unique worst case at
all, and whether a best/worst envelope is even well defined here is an OPEN
question (section 8, issue #1616). So no name, comment or docstring in this
package calls the cash conservative, pessimistic, a bound or a worst case, and
none reads ``snu_bars == 0`` as a certificate: zero means no SNU was DETECTED,
which is weaker than none occurring.

The honest use of the count is deciding WHETHER to trust the result, never
correcting it (section 4.4). It is a frequency and never a magnitude -- two
runs can both report 1 while the bar decided 18.05 units in one and 30.92 in
the other -- and it has no upper bound, because a tranche that sells PART of
the position leaves the run alive to meet the situation again.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from broker_contract.exit_geometry.registry import resolve_declared_policy
from broker_contract.trade_intent.schema import ReanchorOnFill, TradeIntent

from intent_replay.bars import Bar
from intent_replay.config import RunConfig
from intent_replay.interpreter import Plan
from intent_replay.measures import Measures
from intent_replay.trace import to_jsonable
from intent_replay.units import INSTRUMENT_CURRENCY, PERCENT, R_UNIT, Quantity, Translated
from intent_replay.walk import WalkResult, resolve_ladder

__all__ = [
    "DIVERGENCES",
    "INTRABAR_RULE",
    "SCHEMA",
    "STREAM_SCHEMA",
    "build",
    "divergences",
    "stream_line",
    "stream_summary",
]

SCHEMA: Final = "intent_replay.result/v1"
STREAM_SCHEMA: Final = "intent_replay.stream/v1"

# The per-bar order of section 4.4, named rather than claimed. A v1 CONSTANT:
# nothing derives it, because the order is not a switch.
INTRABAR_RULE: Final = "entries_then_stop_then_ladder"

# The section 5.1 denominator, whose four text fields the spec fixes.
_DENOMINATOR_KIND: Final = "placed_stop"
_DENOMINATOR_SOURCE: Final = "spec.disaster_stop"
_DENOMINATOR_FORMULA: Final = "avg_entry_price - placed_stop"

# The five entries of the section 5.2 table, in its order. An addition is an
# edit of that section first: a divergence that can appear without anyone
# writing it down is a divergence nobody will find.
DIVERGENCES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "daemon_trail_guards": (
            "orders, so neither order-state guard can be evaluated. Covers BOTH stop arms: "
            "the re-anchor arm checks the same two guards as the trail arm."
        ),
        "daemon_reanchor_latch_is_journal_lifetime": (
            "the journal, so the daemon's idempotence latch cannot be reproduced. The replay "
            "computes the same predicate from its own trace, with exact equality and only "
            "within one run, so it differs in lifetime and in tolerance."
        ),
        "native_entry_trail_is_a_broker_model": (
            "any local implementation to compare against: once the order rests, the server "
            "owns the ratchet and the fire. The watch that PLACES it is ours, and the replay "
            "lacks the quotes, the session boundaries and the account state its gates read, "
            "so its sampling, its rejections, its DayOrder lifetime and its money gates are "
            "outside the model too."
        ),
        "take_profit_observation_time": (
            "the poll time and the quote, so the moment a tranche is observed is not the "
            "moment the daemon would have observed it."
        ),
        "cost_gate_prices_the_account_currency": (
            "the instrument's currency, so the replay prices the stated budget in the ACCOUNT "
            "currency while the daemon prices the whole-share notional in the instrument's."
        ),
    }
)


def divergences(plan: Plan, config: RunConfig) -> tuple[str, ...]:
    """Which entries this run reports, in registry order.

    Every predicate is the section 5.2 table's own "emitted when" column. The
    ladder questions go through ``walk.resolve_ladder`` rather than through a
    second reading of section 5.1: the two sides have to answer with ONE
    function, or the published list stops describing the run the day that rule
    changes. ``WalkResult.ladder`` cannot answer them -- it carries the NAME,
    and ``tp_tranches`` comes back for an empty tuple too.
    """
    policy = resolve_declared_policy(plan.reaction)
    _, ladder = resolve_ladder(plan)
    laddered = bool(ladder)
    fires = {
        "daemon_trail_guards": policy.trails or policy.requires_amend_stop,
        "daemon_reanchor_latch_is_journal_lifetime": isinstance(plan.reaction, ReanchorOnFill),
        "native_entry_trail_is_a_broker_model": config.entry_trail_bps is not None,
        "take_profit_observation_time": laddered,
        "cost_gate_prices_the_account_currency": laddered and config.costs.min_commission_applies,
    }
    return tuple(name for name in DIVERGENCES if fires[name])


def _window(bars: tuple[Bar, ...]) -> dict[str, Any]:
    """The INPUT series, not the sub-window the walk read.

    A CHOICE, and the spec does not make it: in the section 5 example
    ``from_t`` equals ``config.walk_start`` and ``to_t`` equals
    ``config.entry_deadline``, so that block reads equally well as "the
    configuration's boundaries". The series is what a caller can check against
    the file they handed in. It can be wider than what the walk looked at in
    both directions: bars before ``walk_start`` are skipped, and the loop
    breaks when the position closes. The README says so.

    ``bars`` is never empty here: ``bars_empty`` refused that upstream.
    """
    return {"from_t": bars[0].t, "to_t": bars[-1].t, "bars": len(bars)}


def _denominator(measures: Measures) -> dict[str, Any]:
    """Carried even when there is no value to divide by, so a reader sees WHY
    rather than a missing key (section 5.1)."""
    return Translated(
        kind=_DENOMINATOR_KIND,
        value=measures.denominator,
        unit=INSTRUMENT_CURRENCY,
        source=_DENOMINATOR_SOURCE,
        formula=_DENOMINATOR_FORMULA,
    ).to_jsonable()


def _priced(value: float | None, unit: str) -> dict[str, Any] | None:
    """A measure that carries a unit, or a BARE null when there is no value.

    Section 5.1 prints ``mfe`` and ``mae`` as bare nulls while ``r_multiple``
    keeps its object, and the asymmetry has a reason rather than being an
    oversight: what the object buys is the ``denominator`` provenance, which is
    the answer to "why is this null". A wrapper with nothing but a unit in it
    answers nothing, so every other measure nulls bare.
    """
    return None if value is None else Quantity(value=value, unit=unit).to_jsonable()


def _summary(*, measures: Measures, result: WalkResult, currency: str) -> dict[str, Any]:
    """The nine keys of section 5, in the order it prints them."""
    return {
        "filled_fraction": result.filled_fraction,
        "snu_bars": result.snu_bars,
        "notional_spent": _priced(measures.notional_spent, currency),
        "avg_entry_price": _priced(measures.avg_entry_price, INSTRUMENT_CURRENCY),
        "pnl_cash": _priced(measures.pnl_cash, currency),
        "pnl_pct_of_spent": _priced(measures.pnl_pct_of_spent, PERCENT),
        "r_multiple": {
            "value": measures.r_multiple,
            "unit": R_UNIT,
            "denominator": _denominator(measures),
        },
        "mfe": _priced(measures.mfe, R_UNIT),
        "mae": _priced(measures.mae, R_UNIT),
    }


def build(
    *,
    intent: TradeIntent,
    plan: Plan,
    config: RunConfig,
    result: WalkResult,
    measures: Measures,
    bars: tuple[Bar, ...],
) -> dict[str, Any]:
    """The section 5 envelope for one document, in the key order it prints.

    ``intent_id`` comes off the ADMITTED intent and never off ``door``: the
    engine may not import the door (pinned by ``test_module_dependencies``),
    and the sentinel is the door's fact about the document rather than this
    module's constant.

    The trace is always present, in both formats. Section 5.3 gives ``data`` in
    a stream line as byte-identical to what ``--format json`` prints, and
    routing the trace by format would break that. A flag to omit it belongs
    with the multi-document input that has not been decided (#1611); in v1
    there is no run longer than one document, so there is nothing to omit for.
    """
    return {
        "schema": SCHEMA,
        "intent_id": intent.intent_id,
        "instrument": {"ticker": intent.instrument.ticker, "mic": intent.instrument.mic},
        "window": _window(bars),
        "config": config.to_jsonable(),
        "divergences": list(divergences(plan, config)),
        "intrabar_rule": INTRABAR_RULE,
        "outcome": result.outcome,
        "summary": _summary(measures=measures, result=result, currency=intent.spec.size.currency),
        "trace": [to_jsonable(event) for event in result.events],
    }


def stream_line(envelope: Mapping[str, Any], *, sequence: int) -> dict[str, Any]:
    """One `result` line of the section 5.3 stream.

    The line is a TRANSPORT envelope and the section 5 result rides inside it
    as ``data``, so one identifier describes one shape. ``sequence`` is the
    line's own 1-based index and never ``intent_id``: two variants of one pick
    collide on the arming door's ``TICKER:DATE:manual`` key.
    """
    return {
        "schema": STREAM_SCHEMA,
        "type": "result",
        "sequence": sequence,
        "data": dict(envelope),
    }


def stream_summary(*, sequence: int, documents: int) -> dict[str, Any]:
    """The closing `summary` line. Section 5.3 prints it as a COMPLETE literal
    with these four keys and no ellipsis, so the set is fixed rather than
    chosen here. It carries ``schema`` like every other line, so its own shape
    can be versioned when a stream longer than one document arrives."""
    return {
        "schema": STREAM_SCHEMA,
        "type": "summary",
        "sequence": sequence,
        "documents": documents,
    }

"""Build intent-replay's inputs from a LIVE pick's own production record.

Assembling a run by hand is where the errors were: over two days three of the
seven values came out wrong, and every one of them is recorded in the pick's own
journals. This module maps the record onto the two inputs the replay takes.

The replay stays clean. It learns nothing about ``~/.alphalens``: this module
reads the keeper's record and hands the replay a document and a configuration,
which is why it lives in the laboratory rather than in the engine.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import pathlib
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.brokers import execution
from alphalens_pipeline.brokers.automanager import costs, state_paths
from alphalens_pipeline.brokers.automanager.costs import fee_card_for
from alphalens_pipeline.brokers.automanager.entry_trails import fold_entry_trail_lines
from alphalens_pipeline.brokers.automanager.pick_window import effective_ttl_days, pick_window
from alphalens_pipeline.brokers.automanager.state_paths import entry_trails_path
from alphalens_pipeline.brokers.automanager.trades import format_time
from alphalens_pipeline.feedback.bar_window import BarFetch, default_bar_fetch
from alphalens_pipeline.feedback.corporate_actions import (
    CachedCorporateActionsLookup,
    PolygonCorporateActionsLookup,
    PreExCloseFn,
)
from alphalens_pipeline.market.calendar import (
    advance_trading_sessions,
    previous_trading_day,
    session_close_utc,
    session_on_or_after,
    session_open_utc,
)
from broker_contract.trade_intent.codec import supplied_derived_paths
from intent_replay import units
from intent_replay.fx import Fx

__all__ = [
    "BarWindow",
    "DeadlineCoverage",
    "ExitTimeUnknownError",
    "ReplayInputs",
    "StatedFacts",
    "authored_document",
    "bar_cache_root",
    "bar_window",
    "deadline_coverage",
    "entry_trail_bps_by_pick",
    "exit_instant",
    "main",
    "replay_inputs",
    "run_configuration",
    "stated_facts",
]

SCHEMA = "alphalens.research.replay_from_pick/v1"

# ``meta.source`` of a hand-written pick. Anything else -- "brief", or the key
# absent -- is the legacy producer shape, whose day-1 anchor is a session later.
_MANUAL_SOURCE = "manual"

_SEGMENT = re.compile(r"([^\[\]]+)((?:\[\d+\])*)")

# The corporate-action answer for one (ticker, window) is cached inside the bar
# cache root, beside the bars whose basis it vouches for.
_ACTIONS_CACHE_FILENAME = "corporate_actions_cache.json"

# The bar cache lives beside the rest of this machine's alphalens state.
_BAR_CACHE_DIRNAME = "replay_bars"

# (bar cache root, the dividend-materiality denominator) -> an object carrying
# ``lookup(ticker, start, end)``. A FACTORY rather than the lookup itself: the
# disk cache lives under this run's own ``--bar-cache`` root, which no import
# may resolve -- resolving the operator's home at import time would refuse this
# module inside any test that installed the #1696 operator-state guard first.
ActionsLookupFactory = Callable[[pathlib.Path, PreExCloseFn], Any]

# The clock, as a seam: whether the vendor can serve a window yet is a question
# about now, and a test must be able to ask it on a fixed instant.
Clock = Callable[[], dt.datetime]


def _utc_now() -> dt.datetime:
    """Production clock: the current instant, aware, in UTC."""
    return dt.datetime.now(dt.UTC)


def _segments(path: str) -> Iterator[str | int]:
    """``spec.tp_tranches[0].r_multiple`` -> ``spec``, ``tp_tranches``, 0,
    ``r_multiple``. The grammar is the one ``supplied_derived_paths`` writes."""
    for part in path.split("."):
        match = _SEGMENT.fullmatch(part)
        if match is None:  # pragma: no cover - the producer writes this grammar
            raise ValueError(f"cannot read the path {path!r}")
        yield match.group(1)
        for index in re.findall(r"\[(\d+)\]", match.group(2)):
            yield int(index)


def _remove(document: Any, path: str) -> None:
    *parents, leaf = _segments(path)
    node = document
    for segment in parents:
        node = node[segment]
    del node[leaf]


def _epoch_ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def _instant(epoch_ms: int) -> dt.datetime:
    """The inverse of :func:`_epoch_ms`: the configuration states its two
    anchors as epochs, and the calendar is asked in instants."""
    return dt.datetime.fromtimestamp(epoch_ms / 1000, dt.UTC)


def authored_document(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The pick's plan as its AUTHOR wrote it: the armed document minus every
    field a door computes.

    A journalled pick is the armed form, so it carries ``intent_id``,
    ``meta.armed_ts`` and each tranche's ``r_multiple``. The replay refuses
    exactly those (``derived_field_supplied``), and WHICH they are is not
    repeated here -- ``supplied_derived_paths`` is asked, the same function the
    replay's own door asks, so this cannot drift from the refusal it exists to
    avoid.

    The argument is not mutated: a pick's record is read by other callers too.
    """
    document = json.loads(json.dumps(plan))
    for path in supplied_derived_paths(document):
        _remove(document, path)
    return document


def run_configuration(
    document: Mapping[str, Any],
    *,
    fx_rate: float | None,
    fx_rate_asof: str | None,
    fx_rate_source: str | None,
    instrument_currency: str,
    entry_trail_bps: int | None,
) -> dict[str, Any]:
    """The replay's configuration block for ``document``, from stated facts.

    Five facts arrive as arguments because no document path carries them, and
    every one of them is recorded in the pick's own journals: the rate the
    daemon sized with, its as-of time and its source, the currency the broker
    resolved for the instrument, and the entry-trail distance the drain priced.
    They are arguments rather than reads so this function stays pure and the
    reader that fetches them is testable on its own.

    Everything else is DERIVED, and the three values that came out wrong when
    this block was assembled by hand are each derived from their real source:

    * the calendar is asked for ``instrument.mic``, never for a venue the
      author had in mind. XNAS and XNYS share their session times, so a wrong
      MIC between those two moves nothing and is invisible;
    * the cost constants are imported from the daemon that applies them, so the
      design memo's worked example (``exit_edge_min_bps`` 5.0, where production
      declares 50.0) cannot come back;
    * the fee card is the venue's own (``fee_card_for``), so a non-US pick is
      not priced on the US schedule.

    Three keys are stated as absent, each for a reason rather than for want of
    a value. ``ceiling_price`` because the replay accepts it and does not read
    it, and only the producer-side bracket builder reads one on the live side.
    ``time_stop_t`` because this rail places no live time stop. ``oco`` false
    because v1 models no OCO pair and the block travels in the result.
    """
    meta = document["meta"]
    spec = document["spec"]
    mic = document["instrument"]["mic"]
    account_currency = spec["size"]["currency"]
    trade_date = dt.date.fromisoformat(meta["trade_date"])
    ttl_days = spec["order_ttl_days"]

    # The daemon's own two anchors (``control_loop._day1_anchor``: step 0 for a
    # manual pick whose trade_date IS the arm date, step 1 for a brief, which
    # holds T-1 data and trades the next session; an absent source is a brief
    # per the LEGACY(source_brief) entry).
    manual = meta.get("source") == _MANUAL_SOURCE
    day1 = advance_trading_sessions(trade_date, 0 if manual else 1, exchange=mic)
    # The daemon's own window (#1734): ttl_days sessions from trade_date, with
    # a stated 0 read as the default, exactly as the keeper rested the entries.
    window = pick_window(trade_date, ttl_days, mic)
    anchor_rule = (
        "source=manual counts trade_date itself as day 1"
        if manual
        else "source=brief counts the session after trade_date as day 1"
    )

    card = fee_card_for(instrument_currency, exchange_mic=mic)
    if card is None:
        raise ValueError(f"no fee card for {mic} / {instrument_currency}; the venue is not priced")

    fx = Fx(
        account_currency=account_currency,
        instrument_currency=instrument_currency,
        rate=fx_rate,
        round_trip_cost_rate=costs.FX_ROUND_TRIP_RATE,
        sizing_buffer_pct=execution._FX_SIZING_BUFFER_PCT,
    )
    fx_block: dict[str, Any] = {
        "instrument_currency": {
            "kind": "venue_settlement_currency",
            "value": instrument_currency,
            "unit": units.ISO_4217,
            "source": "keeper.instrument.instrument_currency",
            "formula": f"the currency the broker resolved for this pick on {mic}",
        }
    }
    if fx.applies:
        fx_block["mid_rate"] = {
            "kind": "fx_mid",
            "value": fx_rate,
            # The direction token comes from the engine's own helper, so the
            # pair cannot be spelled one way here and read the other way there.
            "unit": fx.pair_unit(),
            "source": fx_rate_source,
            "formula": f"the rate this pick's own sizing record carries, as of {fx_rate_asof}",
        }
        fx_block["round_trip_cost_rate"] = {
            "value": costs.FX_ROUND_TRIP_RATE,
            "unit": units.FRACTION,
        }
        fx_block["sizing_buffer_pct"] = {
            "value": execution._FX_SIZING_BUFFER_PCT,
            "unit": units.PERCENT,
        }

    return {
        "entry_deadline": {
            "kind": "order_ttl_sessions",
            "value": _epoch_ms(window.window_end),
            "unit": units.EPOCH_MS_UTC,
            "source": "spec.order_ttl_days",
            "formula": (
                f"pick_window({trade_date}, {ttl_days}, {mic}).window_end "
                f"(0 = {effective_ttl_days(0)} sessions)"
            ),
        },
        "walk_start": {
            "kind": "day1_session_open",
            "value": _epoch_ms(session_open_utc(day1, exchange=mic)),
            "unit": units.EPOCH_MS_UTC,
            "source": "meta.source + meta.trade_date",
            "formula": f"session_open_utc({day1}, {mic}); {anchor_rule}",
        },
        "entry_trail_bps": entry_trail_bps,
        "ceiling_price": None,
        "time_stop_t": None,
        "oco": False,
        "fx": fx_block,
        "costs": {
            "commission_rate": {"value": card.commission_rate, "unit": units.FRACTION},
            # The card is the VENUE's, so its minimum is already denominated in
            # the notional's own currency -- which is what makes the flag true.
            "min_commission": {"value": card.min_commission, "unit": instrument_currency},
            "min_commission_applies": True,
            "exit_edge_min_bps": {"value": costs.EXIT_EDGE_MIN_BPS, "unit": units.BPS},
        },
    }


@dataclass(frozen=True, slots=True)
class StatedFacts:
    """The facts no document path carries, read off the pick's own record.

    The three FX fields are ``None`` exactly when the instrument settles in the
    account's own currency, which is also when the configuration refuses them:
    the engine's key set is conditional on the two codes agreeing.
    """

    fx_rate: float | None
    fx_rate_asof: str | None
    fx_rate_source: str | None
    instrument_currency: str


@dataclass(frozen=True, slots=True)
class ReplayInputs:
    """The two arguments a replay run takes."""

    document: dict[str, Any]
    configuration: dict[str, Any]


def _measured(node: Mapping[str, Any], path: str) -> Any:
    """One ``Measured`` field's value, refusing a hole.

    A record publishes an absent fact as ``{"value": null, "null_reason":
    ...}``, so the reason is part of the refusal: "the daemon never journalled
    it" and "the journal line was malformed" send a reader to different places.
    """
    value = node.get("value")
    if value is None:
        raise ValueError(f"{path} is not in this pick's record ({node.get('null_reason')})")
    return value


def _measured_number(node: Mapping[str, Any], path: str) -> float:
    """One ``Measured`` number, refusing a value nothing can compute with.

    The null check alone let a rate stated as TEXT through: it would be written
    into a configuration file and refused only when the replay read it back, so
    the command would report success on a run that cannot start. ``bool`` is a
    subclass of ``int`` and is excluded for the same reason the arming door
    excludes it.
    """
    value = _measured(node, path)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{path} is not a number in this pick's record ({value!r})")
    if not math.isfinite(value):
        raise ValueError(f"{path} is not finite in this pick's record ({value!r})")
    return float(value)


def _measured_text(node: Mapping[str, Any], path: str) -> str:
    value = _measured(node, path)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} is not a non-empty string in this pick's record ({value!r})")
    return value


def stated_facts(trade: Mapping[str, Any]) -> StatedFacts:
    """The stated facts of one ``broker trades`` record.

    The account side is the record's own ``instrument.sizing_currency`` rather
    than the document's ``spec.size.currency``: a pre-#1467 percent-size
    document states no currency at all, and this function is read by callers
    that have not yet refused such a record.
    """
    instrument = trade["instrument"]
    instrument_currency = _measured_text(
        instrument["instrument_currency"], "instrument.instrument_currency"
    )
    account_currency = _measured_text(instrument["sizing_currency"], "instrument.sizing_currency")
    if instrument_currency == account_currency:
        return StatedFacts(
            fx_rate=None,
            fx_rate_asof=None,
            fx_rate_source=None,
            instrument_currency=instrument_currency,
        )
    sizing_fx = trade["sizing_fx"]
    return StatedFacts(
        fx_rate=_measured_number(sizing_fx["rate"], "sizing_fx.rate"),
        fx_rate_asof=_measured_text(sizing_fx["asof"], "sizing_fx.asof"),
        fx_rate_source=_measured_text(sizing_fx["source"], "sizing_fx.source"),
        instrument_currency=instrument_currency,
    )


_EXIT_INSTANT_PATH = "exits[-1].venue_time"


class ExitTimeUnknownError(ValueError):
    """The pick's last exit carries no instant the window can be anchored on.

    A typed exception rather than prose, because the refusal publishes a closed
    ``reason`` vocabulary and the record's own ``null_reason`` beside it, and
    parsing either back out of a message is how one code comes to mean two
    different things.

    ``reason`` is the MODE: ``not_journaled`` when the record states no instant
    at all, ``non_finite`` when it states one that cannot be read as an
    instant. WHICH hole the first of those was stays the record's own word, in
    ``null_reason`` -- "the daemon never journalled it" and "the fill could not
    be attributed" send a reader to different places, and neither earns a
    top-level reason of its own.
    """

    def __init__(self, reason: str, message: str, *, null_reason: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.null_reason = null_reason
        self.path = _EXIT_INSTANT_PATH


def exit_instant(trade: Mapping[str, Any]) -> dt.datetime | None:
    """When the pick's LAST exit filled, or ``None`` when it never filled.

    The LAST, because a record's exits arrive in ascending fill order and a
    two-exit pick's window has to reach the second one; ``exits[0]`` would
    anchor it on the first partial.

    An exit WITH a null ``venue_time`` is REFUSED rather than read as absent:
    "the pick never filled" and "the record does not say when it filled" are
    different facts, and only the first of them is a window anchored on the
    entry deadline alone.
    """
    exits = trade.get("exits") or []
    if not exits:
        return None
    node = exits[-1].get("venue_time") or {}
    value = node.get("value")
    if value is None:
        raise ExitTimeUnknownError(
            "not_journaled",
            f"{trade.get('pick_key')}: {_EXIT_INSTANT_PATH} carries no instant "
            f"({node.get('null_reason')}), so the window has no exit to reach",
            null_reason=node.get("null_reason"),
        )
    moment = None
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            moment = dt.datetime.fromisoformat(value)
    if moment is None or moment.tzinfo is None:
        raise ExitTimeUnknownError(
            "non_finite",
            f"{trade.get('pick_key')}: {_EXIT_INSTANT_PATH} is not an instant with a "
            f"zone ({value!r}); comparing it against the entry deadline would raise "
            "rather than refuse",
            null_reason=node.get("null_reason"),
        )
    return moment


# How far past the window's anchor the bars are asked for. A count of
# sessions, and the only number this join adds: the grid itself adds none, and
# the two edges are comparisons against instants the calendar supplies.
#
# The margin is SOFT. The replay can enter later than reality -- that is the
# divergence a trailing-entry question is about -- so its own exit can fall
# later than the real one. Truncating it is safe because it is VISIBLE: a
# never-filled run has already published ``entry_expired``, and a filled one
# publishes the terminal state ``open`` ("the bars ran out with the position
# still open"), which reads as "re-run with more bars".
_WINDOW_MARGIN_SESSIONS = 2


@dataclass(frozen=True, slots=True)
class BarWindow:
    """The bar window for one pick, with the provenance of both its edges.

    ONE function computes it for the filled and the never-filled case, because
    a module that computes one quantity on two sides ends up describing two
    different panels.

    The two edges are not the same kind of fact. ``required_end`` is HARD: the
    entry ladder was legally alive through ``entry_deadline``, and whether the
    replay fills is the run's OUTPUT, so the requirement cannot be made
    conditional on it. ``end`` is soft, and is the margin ``min``'d against
    what the vendor can serve -- a shortened margin is stated
    (``margin_sessions_served``) rather than inferred.
    """

    start: dt.datetime
    end: dt.datetime
    required_end: dt.datetime
    entry_deadline: dt.datetime
    entry_deadline_session: dt.date
    expiry_observable_session: dt.date
    anchor: dt.datetime
    last_servable_session: dt.date
    servable_end: dt.datetime
    margin_sessions_requested: int
    margin_sessions_served: int

    @property
    def servable(self) -> bool:
        """Whether the vendor can serve a window that reaches ``required_end``.

        Asked of the REQUIREMENT and never of ``end``: ``end`` is already
        capped at what the vendor serves, so asking it would always answer yes
        and the short window would run.
        """
        return self.servable_end >= self.required_end

    @property
    def servable_from(self) -> dt.date:
        """The first calendar day on which the vendor can serve this window.

        The day AFTER the session the expiry is observable on, because the
        restriction is a calendar-day one: once that session's day is past,
        ``previous_trading_day`` of the session on-or-after today reaches it.
        It is a calendar date and not a session, so a Saturday is a correct
        answer -- the weekend run resolves forward to Monday and then back to
        the Friday it needs.
        """
        return self.expiry_observable_session + dt.timedelta(days=1)


def bar_window(
    *,
    walk_start: dt.datetime,
    entry_deadline: dt.datetime,
    exit_instant: dt.datetime | None,
    mic: str,
    now: dt.datetime,
) -> BarWindow:
    """The window the kept series has to cover for this pick.

    ``required_end`` is the close of the session AFTER the deadline session,
    and that is the whole of the half-open grid's effect here: the engine
    expires the ladder on the first bar at or after ``entry_deadline``
    (``walk.py:171``), under this grid no bar carries that instant, so the
    first bar that can fire it is the NEXT session's open. A window ending at
    the deadline session publishes ``no_fill`` with an empty trace instead.

    ``exit_instant`` is ``None`` for a pick that never filled.
    """
    deadline_session = session_on_or_after(entry_deadline.date(), exchange=mic)
    expiry_observable_session = advance_trading_sessions(deadline_session, 1, exchange=mic)
    required_end = session_close_utc(expiry_observable_session, exchange=mic)

    # The LATER of the two, because each alone truncates real picks: of the
    # four comparable closed picks, VST exited after its deadline and the other
    # three before theirs. The deadline alone would cut VST while the position
    # was still open; the exit alone would cut the other three while their
    # entry ladder was still live.
    anchor = max(entry_deadline, exit_instant) if exit_instant else entry_deadline
    anchor_session = session_on_or_after(anchor.date(), exchange=mic)
    desired_end = session_close_utc(
        advance_trading_sessions(anchor_session, _WINDOW_MARGIN_SESSIONS, exchange=mic),
        exchange=mic,
    )

    # The cap is the vendor's CALENDAR DAY, not the last closed session: the
    # free plan serves only past-day minute aggregates, so a cap built on
    # ``session_not_closed`` would cross into the current calendar day after
    # the close and ask for a session the vendor will not serve.
    last_servable_session = previous_trading_day(
        session_on_or_after(now.date(), exchange=mic), exchange=mic
    )
    servable_end = session_close_utc(last_servable_session, exchange=mic)

    # The margin is capped at what the vendor can serve; the REQUIREMENT is
    # not, and is checked by the caller instead. A ``min`` over both would
    # silently run a window that cannot observe the ladder's expiry.
    end = min(desired_end, servable_end)
    return BarWindow(
        start=walk_start,
        end=end,
        required_end=required_end,
        entry_deadline=entry_deadline,
        entry_deadline_session=deadline_session,
        expiry_observable_session=expiry_observable_session,
        anchor=anchor,
        last_servable_session=last_servable_session,
        servable_end=servable_end,
        margin_sessions_requested=_WINDOW_MARGIN_SESSIONS,
        margin_sessions_served=_margin_served(anchor_session, end, mic=mic),
    )


@dataclass(frozen=True, slots=True)
class DeadlineCoverage:
    """Whether a kept series reaches the entry deadline, and where it stops.

    Both facts from one traversal, because a refusal that names a last bar the
    verdict was not taken from describes a different series than the one it
    refused.
    """

    reaches: bool
    last_bar_t: int | None


def deadline_coverage(
    bars: Sequence[Mapping[str, Any]], *, entry_deadline: dt.datetime
) -> DeadlineCoverage:
    """Does this kept series carry a bar at or after ``entry_deadline``?

    Asked of the series' GREATEST ``t`` rather than of its final element: the
    engine refuses a mis-ordered series (``bars_unordered``) and does so after
    this check, so reading the last element would let a shuffled file decide
    the right edge with a bar that is not its latest.

    Total on an empty series, which the empty-window refusal reaches first:
    raising here would turn that refusal into a traceback the day the two
    steps are reordered.
    """
    stamps = [bar["t"] for bar in bars]
    if not stamps:
        return DeadlineCoverage(reaches=False, last_bar_t=None)
    last = max(stamps)
    return DeadlineCoverage(reaches=last >= _epoch_ms(entry_deadline), last_bar_t=last)


def _unservable_message(pick: str, window: BarWindow) -> str:
    """Why this window cannot be run yet, and when it can be.

    It names both sessions, because naming only the deadline reads as "the
    vendor is behind" when the real reason is that the expiry is a session
    later, and it names what the short run WOULD have published, because that
    output is indistinguishable from a ladder that genuinely expired.
    """
    return (
        f"{pick}'s entry ladder was live through session {window.entry_deadline_session} "
        f"(entry_deadline {format_time(window.entry_deadline)}). The session grid is "
        f"half-open at the close, so the ladder's expiry is only observable on the NEXT "
        f"session, {window.expiry_observable_session} (close "
        f"{format_time(window.required_end)}), and the price vendor serves only past "
        f"calendar days. The last session it can serve today is "
        f"{window.last_servable_session} (close {format_time(window.servable_end)}). "
        f"Re-run on {window.servable_from} or later. Replaying the short window would "
        f"publish outcome no_fill with an empty trace, which is what a ladder that "
        f"genuinely expired also publishes."
    )


def _served_short_message(pick: str, window: BarWindow, coverage: DeadlineCoverage) -> str:
    """Why a series that was fetched, or supplied, is still too short.

    The post-fetch half of the same requirement. It names no vendor, because
    the identical refusal fires on a supplied ``--bars`` file, and it states
    the deadline beside the last bar: "the series is short" is only readable
    against the instant it had to reach.
    """
    reached = (
        f"ends at {format_time(_instant(coverage.last_bar_t))}"
        if coverage.last_bar_t is not None
        else "carries no bar"
    )
    return (
        f"{pick}: the kept series {reached}, before the entry deadline "
        f"{format_time(window.entry_deadline)}, so this run cannot observe the ladder's "
        f"expiry. The session grid is half-open at the close, so observing it needs a bar "
        f"from session {window.expiry_observable_session} (close "
        f"{format_time(window.required_end)}). Replaying the short series would publish "
        f"outcome no_fill with an empty trace, which is what a ladder that genuinely "
        f"expired also publishes."
    )


def _window_details(
    pick: str,
    window: BarWindow,
    *,
    mic: str,
    now: dt.datetime,
    last_bar_t: int | None = None,
) -> dict[str, Any]:
    """The refusal's machine-readable half, for both of its reasons.

    One builder for the pre-fetch and the post-fetch refusal, so a caller
    branching on ``code`` reads the same keys either way. ``last_bar_t`` is the
    one that only the post-fetch reason can state.
    """
    details: dict[str, Any] = {
        "pick": pick,
        "exchange": mic,
        "entry_deadline_t": _epoch_ms(window.entry_deadline),
        "entry_deadline_session": window.entry_deadline_session.isoformat(),
        "expiry_observable_session": window.expiry_observable_session.isoformat(),
        "required_end_t": _epoch_ms(window.required_end),
        "last_servable_session": window.last_servable_session.isoformat(),
        "last_servable_end_t": _epoch_ms(window.servable_end),
        "servable_from_date": window.servable_from.isoformat(),
        "now": format_time(now),
    }
    if last_bar_t is not None:
        details["last_bar_t"] = last_bar_t
    return details


def _margin_served(anchor_session: dt.date, end: dt.datetime, *, mic: str) -> int:
    """How many of the requested margin sessions ``end`` actually covers.

    Counted from the window's own end rather than tracked alongside it, so the
    number and the edge it describes cannot disagree.
    """
    served = 0
    for step in range(1, _WINDOW_MARGIN_SESSIONS + 1):
        session = advance_trading_sessions(anchor_session, step, exchange=mic)
        if session_close_utc(session, exchange=mic) > end:
            break
        served = step
    return served


def _assert_comparable(trade: Mapping[str, Any]) -> None:
    """Refuse a record the report itself marks as not comparable.

    One function for two callers, so the command can refuse BEFORE it reads any
    journal: a pick that can never be replayed is never reported as a missing
    trail distance instead.
    """
    exclusions = trade.get("replay_exclusions") or []
    if exclusions:
        raise ValueError(
            f"{trade['pick_key']} cannot be compared with a replay: {', '.join(exclusions)}"
        )


def replay_inputs(trade: Mapping[str, Any], *, entry_trail_bps: int | None) -> ReplayInputs:
    """Both replay inputs for one ``broker trades`` record.

    A record carrying any ``replay_exclusions`` is REFUSED, and the reasons are
    named. That list is the record's own verdict on comparability: a position
    closed by hand, one still open, a plan shape the replay does not model.
    Replaying one of those produces a number that reads as a divergence of the
    tool and is a divergence of the trade.

    ``entry_trail_bps`` stays an argument because it is the one configuration
    value this record does NOT carry; it comes from the entry-trail journal via
    :func:`entry_trail_bps_by_pick`.
    """
    _assert_comparable(trade)
    facts = stated_facts(trade)
    document = authored_document(trade["plan"])
    return ReplayInputs(
        document=document,
        configuration=run_configuration(
            document,
            fx_rate=facts.fx_rate,
            fx_rate_asof=facts.fx_rate_asof,
            fx_rate_source=facts.fx_rate_source,
            instrument_currency=facts.instrument_currency,
            entry_trail_bps=entry_trail_bps,
        ),
    )


def entry_trail_bps_by_pick(lines: Iterable[str]) -> dict[str, int | None]:
    """The trail distance each pick's own ``watch_open`` line journalled.

    The value is frozen at DRAIN time. The arm itself re-reads the ambient flag
    (``entry_trails.KEY_DISTANCE``), so the absolute distance an order actually
    got is the ``trail_armed`` line's ``distance`` and is not reproduced by
    dividing it back by a price -- which is why the stated bps is READ here and
    not reconstructed.

    A journalled ``0`` is the live flag's way of saying OFF (the three-limit
    ladder), and the configuration states OFF as ``None`` and refuses a ``0``.
    So a pick WITH a line and no trail maps to ``None``, which a caller tells
    apart from a pick with no line at all by the key being present.

    A CORRUPT ``watch_open`` is refused rather than skipped, because skipping
    it reported the pick as having no line at all. One unusable line refuses
    the whole mapping, which is what the daemon's own gross-cap consumer does
    with a record it cannot attribute: a reservation it cannot see.
    """
    distances: dict[str, int] = {}
    for state in fold_entry_trail_lines(lines).tiers.values():
        record = state.watch_open
        if record is None:
            continue
        pick = record.get("pick_key")
        distance = record.get("d_bps")
        if not isinstance(pick, str) or isinstance(distance, bool) or not isinstance(distance, int):
            raise ValueError(
                f"watch_open {state.crid}: pick_key {pick!r} and d_bps {distance!r} are not a "
                "pick and a distance; the line is there and unusable, which is not the same as "
                "a pick having no line"
            )
        earlier = distances.get(pick)
        if earlier is not None and earlier != distance:
            raise ValueError(
                f"{pick}: its tiers journalled two trail distances ({earlier} and "
                f"{distance}); no single value is this run's"
            )
        distances[pick] = distance
    return {pick: distance or None for pick, distance in distances.items()}


# ---------------------------------------------------------------------------
# The command. See ``_USAGE`` for the published surface.
# ---------------------------------------------------------------------------

_USAGE = """USAGE
  python -m scripts.replay_from_pick TRADES_JSON PICK_KEY [options]

  TRADES_JSON is the output of `alphalens broker trades --format json`, or `-`
  to read it from stdin. The report decides comparability, so the command needs
  no broker of its own and runs offline.

OPTIONS
  --out DIR              where the two files are written (default: .)
  --entry-trails PATH    the entry-trail journal (default: the report env's own)
  --entry-trail-bps N    state the distance instead of reading it; 0 means OFF
  --bars PATH            bars to put in the suggested command
  --bar-cache DIR        the bar cache root (default: ~/.alphalens/replay_bars)
  --format human|json    default human

OUTPUT
  json: one object on stdout with `document_path`, `config_path`, the stated
  `facts`, the whole `configuration` and a runnable `replay_argv`.

EXIT CODES
  0 ok, 1 the pick cannot be replayed, 2 usage, 4 the report has no such pick,
  7 a transient vendor failure or a window the vendor cannot serve yet; re-run

EXAMPLES
  alphalens broker trades --env live --format json > /tmp/trades.json
  python -m scripts.replay_from_pick /tmp/trades.json VST:2026-09-21 --out /tmp/vst
"""


def _report_trades(source: str) -> tuple[str, dict[str, Mapping[str, Any]]]:
    text = sys.stdin.read() if source == "-" else pathlib.Path(source).read_text()
    report = json.loads(text)
    trades = report.get("trades")
    if not isinstance(trades, list):
        raise ValueError(f"{source}: not a `broker trades` report (no `trades` array)")
    try:
        picks = {trade["pick_key"]: trade for trade in trades}
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{source}: a row of `trades` carries no `pick_key`") from exc
    return report.get("env") or "live", picks


def _trail_distance(
    pick: str, *, stated: int | None, journal: pathlib.Path, env: str
) -> int | None:
    """The distance for ``pick``: stated if the caller stated one, else the
    pick's own ``watch_open`` line.

    An absent line is REFUSED rather than read as OFF. A run with the wrong
    entry policy answers a different question, and the ladder and the trail are
    different policies -- not a present and an absent setting.
    """
    if stated is not None:
        return stated or None
    lines = journal.read_text().splitlines() if journal.exists() else []
    distances = entry_trail_bps_by_pick(lines)
    if pick not in distances:
        raise ValueError(
            f"{pick}: {journal} holds no watch_open line for it, so the entry-trail "
            f"distance is unknown (env={env}); state it with --entry-trail-bps (0 = OFF)"
        )
    return distances[pick]


def bar_cache_root(stated: str | None) -> pathlib.Path:
    """The bar cache root: the stated path, else ``<alphalens home>/replay_bars``.

    Derived through ``state_paths._alphalens_home()`` rather than a
    ``Path.home()`` join of its own, because that seam is the one the #1696
    operator-state guard wraps: a broker-tier test that runs this command
    without ``--bar-cache`` is then refused loudly instead of reading -- and
    writing -- the operator's live cache.

    Resolved LAZILY, at the call and never while the parser is built: the
    refusals the command owes its caller come first, and two of them are
    reached by invocations that state no cache at all.
    """
    if stated:
        return pathlib.Path(stated)
    return state_paths._alphalens_home() / _BAR_CACHE_DIRNAME


# The refusal envelope. One schema for every code, so `--format json` has a
# shape a caller can parse and `--format human` names the same code in prose.
_ERROR_SCHEMA = "alphalens.research.replay_from_pick.error/v1"

# The reserved transient status: a vendor failure, or a window the vendor
# cannot serve yet. It is the only status worth re-running on, which is what
# ``retryable`` reports -- so nothing has to state the two separately.
_EXIT_TRANSIENT = 7


def _fail(
    code: str,
    reason: str,
    message: str,
    status: int,
    *,
    fmt: str,
    suggestions: list[dict[str, list[str]]] | None = None,
    **details: Any,
) -> int:
    """Refuse: nothing on stdout, one shaped refusal on stderr, ``status`` back.

    ``--format human`` writes the message as prose behind its code, so the
    operator still reads a sentence. ``--format json`` writes ONE object as the
    LAST line of stderr -- the machine rule the `alphalens broker` CLI
    publishes, which lets a caller read the last line and parse it.

    ``code`` is the field to branch on; ``reason`` narrows it inside one
    failure mode, the shape `broker arm` uses rather than one top-level code
    per rule.
    """
    if fmt == "json":
        print(
            json.dumps(
                {
                    "schema": _ERROR_SCHEMA,
                    "code": code,
                    "reason": reason,
                    "message": message,
                    "retryable": status == _EXIT_TRANSIENT,
                    "details": details,
                    "suggestions": suggestions or [],
                }
            ),
            file=sys.stderr,
        )
    else:
        print(f"replay_from_pick: {code}: {message}", file=sys.stderr)
    return status


def main(
    argv: list[str] | None = None,
    *,
    bar_fetch: BarFetch = default_bar_fetch,
    actions_lookup: ActionsLookupFactory = lambda cache_root, pre_ex_close: (
        CachedCorporateActionsLookup(
            PolygonCorporateActionsLookup(pre_ex_close=pre_ex_close),
            cache_root / _ACTIONS_CACHE_FILENAME,
        )
    ),
    now: Clock = _utc_now,
) -> int:
    """Write both replay inputs for one pick of a `broker trades` report.

    ``bar_fetch`` is the price-vendor seam, stated in the signature with its
    production default rather than defaulted to ``None`` and resolved inside:
    a ``None`` would leave the vendor call untested in every direction, because
    the no-live-network guard forbids running the real one. ``actions_lookup``
    is the same seam for the corporate-action question, and is a factory
    because its disk cache sits under the run's own bar-cache root. ``now`` is
    the clock the servable-window question is asked against.
    """
    parser = argparse.ArgumentParser(
        prog="replay_from_pick",
        usage=argparse.SUPPRESS,
        description=_USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("trades_json")
    parser.add_argument("pick_key")
    parser.add_argument("--out", default=".")
    parser.add_argument("--entry-trails", default=None)
    parser.add_argument("--entry-trail-bps", type=int, default=None)
    parser.add_argument("--bars", default=None)
    # Resolved by ``bar_cache_root`` at the call, never here: a default built
    # with the parser would resolve the operator's home ahead of every refusal.
    parser.add_argument("--bar-cache", default=None)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)

    if args.entry_trail_bps is not None and args.entry_trail_bps < 0:
        return _fail(
            "usage",
            "negative_trail_distance",
            f"--entry-trail-bps must be 0 (OFF) or above; got {args.entry_trail_bps}",
            2,
            fmt=args.format,
            stated=args.entry_trail_bps,
        )
    try:
        env, trades = _report_trades(args.trades_json)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _fail(
            "report_unreadable",
            "not_a_trades_report",
            str(exc),
            2,
            fmt=args.format,
            path=args.trades_json,
        )
    trade = trades.get(args.pick_key)
    if trade is None:
        return _fail(
            "pick_not_in_report",
            "no_such_pick",
            f"{args.pick_key} is not in this report ({len(trades)} picks, env={env})",
            4,
            fmt=args.format,
            pick=args.pick_key,
            picks=len(trades),
            env=env,
        )

    journal = pathlib.Path(args.entry_trails) if args.entry_trails else entry_trails_path(env)
    # Three refusals, not one: comparability is the report's own verdict, an
    # unknown trail distance is a journal gap, and the mapping's own failures
    # are neither. One `except` could only report the first of the three.
    try:
        _assert_comparable(trade)
    except (OSError, ValueError) as exc:
        return _fail(
            "not_comparable",
            "report_excluded",
            str(exc),
            1,
            fmt=args.format,
            pick=args.pick_key,
            reasons=list(trade.get("replay_exclusions") or []),
        )
    try:
        distance = _trail_distance(
            args.pick_key, stated=args.entry_trail_bps, journal=journal, env=env
        )
    except (OSError, ValueError) as exc:
        return _fail(
            "entry_trail_unknown",
            "unknown_distance",
            str(exc),
            1,
            fmt=args.format,
            pick=args.pick_key,
            path=str(journal),
        )
    try:
        last_exit = exit_instant(trade)
    except ExitTimeUnknownError as exc:
        return _fail(
            "exit_time_unknown",
            exc.reason,
            str(exc),
            1,
            fmt=args.format,
            pick_key=args.pick_key,
            null_reason=exc.null_reason,
            path=exc.path,
        )
    try:
        inputs = replay_inputs(trade, entry_trail_bps=distance)
    except (OSError, ValueError) as exc:
        return _fail(
            "unclassified",
            "refusal_not_yet_coded",
            str(exc),
            1,
            fmt=args.format,
            pick=args.pick_key,
        )

    mic = inputs.document["instrument"]["mic"]
    # Asked ONCE: a clock is a seam, and two calls can straddle a session's
    # close, which is exactly the boundary the servable cap is about.
    moment = now()
    window = bar_window(
        walk_start=_instant(inputs.configuration["walk_start"]["value"]),
        entry_deadline=_instant(inputs.configuration["entry_deadline"]["value"]),
        exit_instant=last_exit,
        mic=mic,
        now=moment,
    )
    # Before any vendor call, so an unanswerable window spends no quota.
    if not window.servable:
        return _fail(
            "bars_window_unservable",
            "deadline_not_yet_servable",
            _unservable_message(args.pick_key, window),
            _EXIT_TRANSIENT,
            fmt=args.format,
            suggestions=[
                {
                    "argv": [
                        sys.executable,
                        "-m",
                        "scripts.replay_from_pick",
                        args.trades_json,
                        args.pick_key,
                        "--out",
                        args.out,
                    ]
                }
            ],
            **_window_details(args.pick_key, window, mic=mic, now=moment),
        )

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = args.pick_key.replace(":", "_")
    document_path = out / f"{stem}.document.json"
    config_path = out / f"{stem}.config.json"
    document_path.write_text(json.dumps(inputs.document, indent=1) + "\n")
    config_path.write_text(json.dumps(inputs.configuration, indent=1) + "\n")

    facts = stated_facts(trade)
    answer = {
        "schema": SCHEMA,
        "env": env,
        "pick": args.pick_key,
        "document_path": str(document_path),
        "config_path": str(config_path),
        "facts": {
            "fx_rate": facts.fx_rate,
            "fx_rate_asof": facts.fx_rate_asof,
            "fx_rate_source": facts.fx_rate_source,
            "instrument_currency": facts.instrument_currency,
            "entry_trail_bps": distance,
        },
        "configuration": inputs.configuration,
        "replay_argv": [
            sys.executable,
            "-m",
            "intent_replay",
            "run",
            str(document_path),
            "--config",
            str(config_path),
            "--bars",
            args.bars or "<BARS.json>",
        ],
    }
    if args.format == "json":
        print(json.dumps(answer))
        return 0
    print(f"{args.pick_key} ({env}) -> {document_path} + {config_path}")
    for name, value in answer["facts"].items():
        print(f"  {name}: {value}")
    print("  next: " + " ".join(answer["replay_argv"]))
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point
    raise SystemExit(main())

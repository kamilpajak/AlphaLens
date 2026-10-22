"""Episodes: one instrument, flat to flat, at the account (#1689, memo 1.5).

An episode is the span over which the cumulative signed quantity on one ``uic``
leaves zero and returns to zero. Derived, never stored. A position opened by
hand and one opened by the keeper land in an episode identically, because the
derivation refers to no order reference, no pick and no ``ExternalReference``.

**The sign source is ``amount``.** ``Direction`` is the literal string
``'None'`` on all 41 rows of the capture, so a reader that branches on it gets
nothing. ``ToOpenOrClose`` is populated (24 ``ToOpen`` / 17 ``ToClose``) and it
agrees with ``sign(amount)`` on all 41 rows, which is exactly why it is NOT
read: a cross-check between two collinear columns is a tautology with no
resolving power, and the venue pairs nothing anyway -- 1 of 41 rows carries a
``RelatedPositionId``, because the account nets at end of day. So the pairing
is FIFO over the account's own fills and every episode says so
(:data:`ATTRIBUTION_FIFO_ASSUMED`).

**``window_clipped`` is detected by the quantity walk going NEGATIVE**, never
by "the first row is a close". The discriminating case is a PARTIAL over-close
-- buy 2, then sell 5 -- which the first-row check passes and the walk refuses.
Three rules, in order: a fill the walk places in full is assigned; a close
whose quantity exceeds the running position is unassigned, and the episode it
would have closed is ``window_clipped`` and excluded; a close with no open
quantity at all produces no episode. A fourth rule falls out of the epsilon:
a fill INSIDE ``QTY_PRECISION`` -- zero included -- arriving with no open
position closes nothing and opens nothing, so it is unassigned rather than an
episode that was never a round trip.

**This walk is LONG-ONLY, and says so rather than guessing.** An instrument
whose walk opens short and later returns to flat -- sell 10, buy 10 back -- is
a short round trip, and the pairing here has no notion of one. The whole
instrument is refused with :data:`UNASSIGNED_SHORT_OPEN` and publishes no
episode, instead of reading the opening sell as store incompleteness and the
buy-back as a fresh long position that never closes. A lone sell with no
buy-back is NOT this case: it cannot be told apart from a close whose entry
predates the sweep, so it stays ``no_open_in_store``.

**An unreadable quantity is STICKY for the rest of that instrument.** Once one
fill's quantity cannot be read, the running position is unknowable from that
row on -- the fill may have been a buy -- so every LATER episode on that
``uic`` is ``imbalanced`` and not eligible, not just the one in flight or the
one that opens next.

**What this cannot see, and the report says so rather than hiding it:** an
episode whose entry is PARTLY before the store -- bought 10 before, bought 10
inside, sold 10 inside -- never takes the walk negative and reads as a
complete round trip with half the position. No fixture case is written for a
detection that does not exist. The containment is the coverage block:
``history_begins`` is published on every total and a ``--since`` earlier than
it is a refusal, never a smaller number.

**This takes no window.** An episode clipped by a presentation window is
indistinguishable from one clipped by the store, and only the second is a real
incompleteness, so the derivation runs over every row it is given and the
caller filters by ``closed_date`` afterwards.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from broker_contract.constants import QTY_PRECISION

from alphalens_pipeline.account_cash.money import (
    NULL_NOT_CLOSED,
    AcctAmount,
    acct_null,
    acct_sum,
)
from alphalens_pipeline.brokers.account_cash import TradeRow

#: Back to flat: the realized net exists and the episode is eligible.
STATE_CLOSED = "closed"
#: Still holding quantity. Not realized, so there is no net to publish.
STATE_OPEN = "open"
#: Quantity that will not pair -- a fill whose quantity could not be read, a
#: partial close outside the window, overlapping round trips. A first-class
#: state (memo section 5's table), not a flavour of ``open``.
STATE_IMBALANCED = "imbalanced"
#: A close larger than everything the store knows was opened: the entry
#: predates what was swept, so the span is not a round trip.
STATE_WINDOW_CLIPPED = "window_clipped"

EPISODE_STATES: tuple[str, ...] = (
    STATE_CLOSED,
    STATE_OPEN,
    STATE_IMBALANCED,
    STATE_WINDOW_CLIPPED,
)

#: Pre-registered precedence. The FIRST hit is the stamped reason, so counts by
#: reason ADD UP to the number of excluded episodes -- the shape that keeps
#: ``episodes_summed + episodes_excluded == len(episodes)`` arithmetically
#: true. Listing every reason that applies would make counts over-add.
EXCLUSION_ORDER: tuple[str, ...] = (STATE_WINDOW_CLIPPED, STATE_IMBALANCED, STATE_OPEN)

#: A close the walk cannot place: nothing, or not enough, was ever opened.
UNASSIGNED_NO_OPEN = "no_open_in_store"
#: The vendor sent no quantity on this fill, so the walk cannot place it.
UNASSIGNED_QUANTITY_NOT_READ = "quantity_not_read"
#: A fill whose quantity is inside ``QTY_PRECISION`` -- zero included --
#: arriving with no open position: it closes nothing and opens nothing. Its
#: own reason rather than :data:`UNASSIGNED_NO_OPEN`, for the same argument
#: as :data:`UNASSIGNED_SHORT_OPEN`: nothing here says an entry was swept
#: away, because nothing was being closed.
UNASSIGNED_CLOSES_NOTHING = "closes_nothing"
#: The instrument's walk OPENS short and later returns to flat -- a short
#: round trip. Long-only is a premise of this derivation (memo section 1.5),
#: so the shape is named rather than modelled: a short episode's cost basis,
#: its sign conventions and its realized net are all different quantities.
#: Distinct from :data:`UNASSIGNED_NO_OPEN` on purpose -- that one means the
#: entry predates the sweep, and reporting a short as incompleteness points
#: the reader at a store gap that does not exist.
UNASSIGNED_SHORT_OPEN = "short_open_not_modelled"
UNASSIGNED_REASONS: tuple[str, ...] = (
    UNASSIGNED_NO_OPEN,
    UNASSIGNED_QUANTITY_NOT_READ,
    UNASSIGNED_CLOSES_NOTHING,
    UNASSIGNED_SHORT_OPEN,
)

#: Stated on every episode. The pairing is an assumption about the account's
#: end-of-day netting, not a measurement, and publishing it as measured would
#: be a claim nobody made.
ATTRIBUTION_FIFO_ASSUMED = "fifo_assumed"

__all__ = [
    "ATTRIBUTION_FIFO_ASSUMED",
    "EPISODE_STATES",
    "EXCLUSION_ORDER",
    "STATE_CLOSED",
    "STATE_IMBALANCED",
    "STATE_OPEN",
    "STATE_WINDOW_CLIPPED",
    "UNASSIGNED_CLOSES_NOTHING",
    "UNASSIGNED_NO_OPEN",
    "UNASSIGNED_QUANTITY_NOT_READ",
    "UNASSIGNED_REASONS",
    "UNASSIGNED_SHORT_OPEN",
    "CashEpisode",
    "EpisodeFold",
    "UnassignedFill",
    "derive_episodes",
]

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


@dataclass(frozen=True)
class CashEpisode:
    """One flat-to-flat span on one instrument, in the ACCOUNT currency."""

    uic: int | None
    instrument_symbol: str | None
    state: str
    eligible: bool
    exclusion_reason: str | None
    #: The vendor's ``TradeDate``, never ``ValueDate``: memo section 1.7 P9
    #: settles the date basis, and the two differ on 41 of 41 trade rows.
    opened_date: str | None
    #: Set only when the walk brought the instrument flat. An episode that was
    #: terminated by an unplaceable close, or that is still open, has no
    #: closing row and therefore no closing date.
    closed_date: str | None
    opened_qty: float
    closed_qty: float
    trade_ids: tuple[str, ...]
    attribution: str
    #: The realized net. Null ``not_closed`` on anything but a closed episode.
    net_acct: AcctAmount
    #: The ENTRY side only -- every fill that ADDED quantity.
    cost_basis_acct: AcctAmount
    #: Every fill assigned to this episode, closed or not.
    money_acct: AcctAmount

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "uic": self.uic,
            "instrument_symbol": self.instrument_symbol,
            "state": self.state,
            "eligible": self.eligible,
            "exclusion_reason": self.exclusion_reason,
            "opened_date": self.opened_date,
            "closed_date": self.closed_date,
            "opened_qty": self.opened_qty,
            "closed_qty": self.closed_qty,
            "trade_ids": list(self.trade_ids),
            "attribution": self.attribution,
            "net_acct": self.net_acct.to_jsonable(),
            "cost_basis_acct": self.cost_basis_acct.to_jsonable(),
            "money_acct": self.money_acct.to_jsonable(),
        }


@dataclass(frozen=True)
class UnassignedFill:
    """A fill no episode could hold. It still contributes its money.

    Listed rather than dropped: ``assigned + unassigned == fills_read`` is the
    memo's coverage partition, and it is what makes ``if not lots: continue``
    impossible to reintroduce quietly.
    """

    trade_id: str
    uic: int | None
    instrument_symbol: str | None
    reason: str
    quantity: float | None
    money_acct: AcctAmount

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "uic": self.uic,
            "instrument_symbol": self.instrument_symbol,
            "reason": self.reason,
            "quantity": self.quantity,
            "money_acct": self.money_acct.to_jsonable(),
        }


@dataclass(frozen=True)
class EpisodeFold:
    """Every episode, every fill that reached none, and the two counts."""

    episodes: tuple[CashEpisode, ...]
    unassigned: tuple[UnassignedFill, ...]
    fills_read: int
    fills_assigned: int

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "episodes": [episode.to_jsonable() for episode in self.episodes],
            "unassigned": [fill.to_jsonable() for fill in self.unassigned],
            "fills_read": self.fills_read,
            "fills_assigned": self.fills_assigned,
        }


class _Accumulator:
    """One episode under construction, on one ``uic``."""

    def __init__(self, row: TradeRow, *, imbalanced: bool = False) -> None:
        self.uic = row.uic
        self.instrument_symbol = row.instrument_symbol
        self.opened_date = row.trade_date
        self.closed_date: str | None = None
        self.opened_qty = 0.0
        self.closed_qty = 0.0
        self.trade_ids: list[str] = []
        self.entry_pairs: list[tuple[float | None, str | None]] = []
        self.all_pairs: list[tuple[float | None, str | None]] = []
        self.imbalanced = imbalanced
        self.clipped = False

    def assign(self, row: TradeRow, quantity: float) -> None:
        self.trade_ids.append(row.trade_id)
        pair = (row.booked_amount_account_currency, row.account_currency)
        self.all_pairs.append(pair)
        if quantity > 0.0:
            self.opened_qty += quantity
            self.entry_pairs.append(pair)
        else:
            self.closed_qty += -quantity

    def finish(self) -> CashEpisode:
        flat = abs(self.opened_qty - self.closed_qty) <= QTY_PRECISION
        applicable = {
            STATE_WINDOW_CLIPPED: self.clipped,
            STATE_IMBALANCED: self.imbalanced,
            STATE_OPEN: not flat,
        }
        reason = next((name for name in EXCLUSION_ORDER if applicable[name]), None)
        state = reason or STATE_CLOSED
        currency = _stated_currency(self.all_pairs)
        money = acct_sum(self.all_pairs, rows=len(self.all_pairs), currency=currency)
        return CashEpisode(
            uic=self.uic,
            instrument_symbol=self.instrument_symbol,
            state=state,
            eligible=state == STATE_CLOSED,
            exclusion_reason=reason,
            opened_date=self.opened_date,
            closed_date=self.closed_date if state == STATE_CLOSED else None,
            opened_qty=self.opened_qty,
            closed_qty=self.closed_qty,
            trade_ids=tuple(self.trade_ids),
            attribution=ATTRIBUTION_FIFO_ASSUMED,
            net_acct=(
                money
                if state == STATE_CLOSED
                else acct_null(NULL_NOT_CLOSED, currency, rows=len(self.all_pairs))
            ),
            cost_basis_acct=acct_sum(
                self.entry_pairs, rows=len(self.entry_pairs), currency=currency
            ),
            money_acct=money,
        )


def _stated_currency(pairs: Sequence[tuple[float | None, str | None]]) -> str | None:
    stated = {currency for _, currency in pairs if currency is not None}
    return next(iter(stated)) if len(stated) == 1 else None


def _walk_order(row: TradeRow) -> tuple[bool, dt.datetime, str]:
    """Execution order, with the trade id breaking a tie to the microsecond.

    Two ``ALB:xnys`` closes are stamped 20 ms apart, so the stamp alone orders
    the capture; the trade id keeps the order total for a vendor that repeats
    one. A row with no stamp sorts last rather than raising.
    """
    return (row.execution_time is None, row.execution_time or _EPOCH, row.trade_id)


def _opens_a_short_round_trip(ordered: Sequence[TradeRow]) -> bool:
    """True when this instrument's walk opens SHORT and later returns to flat.

    Two conditions, and both are needed. **Opens short**: the first fill is a
    close beyond the epsilon, so there is nothing of the account's own for it
    to close. **Returns to flat**: the signed quantity comes back to zero
    later, which is the one thing a clipped entry cannot do -- a close whose
    entry predates the sweep leaves the signed walk short forever, and on a
    long-only account that is the likelier reading of a lone sell. So the two
    together are what separate a bought-back short from
    :data:`UNASSIGNED_NO_OPEN`, and dropping either would relabel a real
    store-incompleteness signal.

    Reads the signed quantity only to RECOGNISE the shape; nothing downstream
    models a short position. An unreadable quantity makes the signed walk
    unknowable, so this answers ``False`` and the instrument goes to the long
    walk, where the sticky imbalanced flag refuses its episodes instead.

    Called with one ``uic``'s own fills, which are never none.
    """
    quantities: list[float] = []
    for row in ordered:
        if row.amount is None:
            return False
        quantities.append(row.amount)

    if quantities[0] >= -QTY_PRECISION:
        return False

    position = 0.0
    for quantity in quantities:
        position += quantity
        if abs(position) <= QTY_PRECISION:
            return True
    return False


def _unassigned(row: TradeRow, reason: str, *, quantity: float | None) -> UnassignedFill:
    pair = (row.booked_amount_account_currency, row.account_currency)
    return UnassignedFill(
        trade_id=row.trade_id,
        uic=row.uic,
        instrument_symbol=row.instrument_symbol,
        reason=reason,
        quantity=quantity,
        money_acct=acct_sum([pair], rows=1, currency=row.account_currency),
    )


class _InstrumentWalk:
    """The quantity walk over ONE ``uic``'s fills, in execution order.

    Extracted from :func:`derive_episodes`, where the same logic read as one
    function of cognitive complexity 28 against this project's S3776 limit of
    15 -- four pieces of mutable state (the running position, the episode in
    flight, the two output lists) threaded through four nested branches. Each
    decision is now its own method and the state is this object's, so no
    method is deeper than one branch. Nothing here decides anything the
    inlined walk did not.

    One instance per instrument: the position and the episode in flight are
    per-``uic`` by construction, so a shared instance could not leak one
    instrument's position into another's.
    """

    def __init__(self) -> None:
        self.episodes: list[CashEpisode] = []
        self.unassigned: list[UnassignedFill] = []
        self.assigned = 0
        self._position = 0.0
        self._current: _Accumulator | None = None
        #: STICKY. Once one quantity on this instrument could not be read the
        #: position is unknowable for the REST of the walk, so every later
        #: episode is imbalanced too -- see :meth:`_quantity_not_read`.
        self._quantity_unreadable = False

    def run(self, uic_rows: Sequence[TradeRow]) -> None:
        """Walk one instrument's fills, oldest first.

        An instrument whose walk opens short is refused WHOLE rather than
        walked: every fill is listed with its own reason and no episode is
        published, so the instrument states no eligible realized net. Pairing
        a short long-only would publish a cost basis and a net that are not
        the quantities those words name.
        """
        ordered = sorted(uic_rows, key=_walk_order)
        if _opens_a_short_round_trip(ordered):
            self.unassigned.extend(
                _unassigned(row, UNASSIGNED_SHORT_OPEN, quantity=row.amount) for row in ordered
            )
            return
        for row in ordered:
            self._step(row)
        if self._current is not None:
            self.episodes.append(self._current.finish())

    def _step(self, row: TradeRow) -> None:
        """The four rules, in the order the module docstring states them.

        The order is load-bearing: a fill inside the epsilon is checked AFTER
        the over-close guard, because a close that both exceeds the position
        and sits inside the epsilon is the over-close the walk already names.
        """
        quantity = row.amount
        if quantity is None:
            self._quantity_not_read(row)
            return
        if quantity < 0.0 and -quantity > self._position + QTY_PRECISION:
            self._over_close(row, quantity)
            return
        if self._current is None and abs(quantity) <= QTY_PRECISION:
            self._closes_nothing(row, quantity)
            return
        self._place(row, quantity)

    def _closes_nothing(self, row: TradeRow, quantity: float) -> None:
        """A fill inside the epsilon arriving with no open position.

        It opens nothing and closes nothing, so it is no more an episode than
        a close the walk cannot place is. Each of the other guards lets it
        through for its own reason: the quantity IS readable; the over-close
        threshold compares against the same epsilon the quantity sits inside,
        so it does not fire; and the flat check then reads a position of zero
        as "back to flat" and publishes a ``closed`` eligible episode whose
        money becomes a realized net.

        The discriminator is the open position, never the quantity: the same
        fill BETWEEN a buy and its sell belongs to that round trip, and
        refusing it there would move real money out of a real net.
        """
        self.unassigned.append(_unassigned(row, UNASSIGNED_CLOSES_NOTHING, quantity=quantity))

    def _quantity_not_read(self, row: TradeRow) -> None:
        """The vendor sent no quantity, so the walk cannot place this fill.

        Its money still counts, on the fill rather than on the episode, so the
        money partition stays exact; the episode it belonged to cannot pair
        its quantity any more.

        The flag is STICKY, and that is the point: the unreadable fill may
        have been a BUY, in which case the instrument is not flat where the
        walk thinks it is and nothing after this row is a flat round trip
        either. Marking only the episode in flight -- or only the one that
        opens next -- publishes the one after that as ``closed`` and eligible,
        which is money published as realized on an unknowable position.
        """
        self.unassigned.append(_unassigned(row, UNASSIGNED_QUANTITY_NOT_READ, quantity=None))
        self._quantity_unreadable = True
        if self._current is not None:
            self._current.imbalanced = True

    def _over_close(self, row: TradeRow, quantity: float) -> None:
        """The close exceeds everything the store knows was opened.

        The position is RESET afterwards, so a clean round trip later on the
        same instrument is still real realized money (amendment W8).
        """
        self.unassigned.append(_unassigned(row, UNASSIGNED_NO_OPEN, quantity=quantity))
        if self._current is not None:
            self._current.clipped = True
            self.episodes.append(self._current.finish())
            self._current = None
        self._position = 0.0

    def _place(self, row: TradeRow, quantity: float) -> None:
        """Assign the fill, and close the episode if it brought the walk flat."""
        if self._current is None:
            self._current = _Accumulator(row, imbalanced=self._quantity_unreadable)
        self._current.assign(row, quantity)
        self.assigned += 1
        self._position += quantity
        if abs(self._position) <= QTY_PRECISION:
            self._current.closed_date = row.trade_date
            self.episodes.append(self._current.finish())
            self._current = None
            self._position = 0.0


def derive_episodes(rows: Sequence[TradeRow]) -> EpisodeFold:
    """Flat-to-flat spans per ``uic``, FIFO over the account's own fills.

    Reads exactly five columns: ``uic`` (the grouping), ``amount`` (the sign
    AND the quantity), ``execution_time`` (the order),
    ``booked_amount_account_currency`` with ``account_currency`` (the money),
    and ``trade_date`` (the dates). ``to_open_or_close`` and
    ``trade_event_type`` are deliberately not read -- see the module docstring.

    The per-instrument walk itself is :class:`_InstrumentWalk`; this function
    groups and concatenates. The published order is the input's own order of
    first appearance per ``uic``, which is what it was when the walk was
    inlined here.
    """
    by_uic: dict[int | None, list[TradeRow]] = {}
    for row in rows:
        by_uic.setdefault(row.uic, []).append(row)

    episodes: list[CashEpisode] = []
    unassigned: list[UnassignedFill] = []
    assigned = 0

    for uic_rows in by_uic.values():
        walk = _InstrumentWalk()
        walk.run(uic_rows)
        episodes.extend(walk.episodes)
        unassigned.extend(walk.unassigned)
        assigned += walk.assigned

    return EpisodeFold(
        episodes=tuple(episodes),
        unassigned=tuple(unassigned),
        fills_read=len(rows),
        fills_assigned=assigned,
    )

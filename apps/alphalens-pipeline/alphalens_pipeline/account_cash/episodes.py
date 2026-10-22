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
quantity at all produces no episode.

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
UNASSIGNED_REASONS: tuple[str, ...] = (UNASSIGNED_NO_OPEN, UNASSIGNED_QUANTITY_NOT_READ)

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
    "UNASSIGNED_NO_OPEN",
    "UNASSIGNED_QUANTITY_NOT_READ",
    "UNASSIGNED_REASONS",
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

    def __init__(self, row: TradeRow) -> None:
        self.uic = row.uic
        self.instrument_symbol = row.instrument_symbol
        self.opened_date = row.trade_date
        self.closed_date: str | None = None
        self.opened_qty = 0.0
        self.closed_qty = 0.0
        self.trade_ids: list[str] = []
        self.entry_pairs: list[tuple[float | None, str | None]] = []
        self.all_pairs: list[tuple[float | None, str | None]] = []
        self.imbalanced = False
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


def derive_episodes(rows: Sequence[TradeRow]) -> EpisodeFold:
    """Flat-to-flat spans per ``uic``, FIFO over the account's own fills.

    Reads exactly five columns: ``uic`` (the grouping), ``amount`` (the sign
    AND the quantity), ``execution_time`` (the order),
    ``booked_amount_account_currency`` with ``account_currency`` (the money),
    and ``trade_date`` (the dates). ``to_open_or_close`` and
    ``trade_event_type`` are deliberately not read -- see the module docstring.
    """
    by_uic: dict[int | None, list[TradeRow]] = {}
    for row in rows:
        by_uic.setdefault(row.uic, []).append(row)

    episodes: list[CashEpisode] = []
    unassigned: list[UnassignedFill] = []
    assigned = 0

    for uic_rows in by_uic.values():
        position = 0.0
        current: _Accumulator | None = None
        pending_imbalance = False

        for row in sorted(uic_rows, key=_walk_order):
            quantity = row.amount
            if quantity is None:
                # Its money still counts, on the fill rather than on the
                # episode, so the money partition stays exact; the episode it
                # belonged to cannot pair its quantity any more.
                unassigned.append(_unassigned(row, UNASSIGNED_QUANTITY_NOT_READ, quantity=None))
                if current is not None:
                    current.imbalanced = True
                else:
                    pending_imbalance = True
                continue

            if quantity < 0.0 and -quantity > position + QTY_PRECISION:
                # The close exceeds everything the store knows was opened.
                unassigned.append(_unassigned(row, UNASSIGNED_NO_OPEN, quantity=quantity))
                if current is not None:
                    current.clipped = True
                    episodes.append(current.finish())
                    current = None
                position = 0.0
                continue

            if current is None:
                current = _Accumulator(row)
                current.imbalanced = pending_imbalance
                pending_imbalance = False
            current.assign(row, quantity)
            assigned += 1
            position += quantity
            if abs(position) <= QTY_PRECISION:
                current.closed_date = row.trade_date
                episodes.append(current.finish())
                current = None
                position = 0.0

        if current is not None:
            episodes.append(current.finish())

    return EpisodeFold(
        episodes=tuple(episodes),
        unassigned=tuple(unassigned),
        fills_read=len(rows),
        fills_assigned=assigned,
    )

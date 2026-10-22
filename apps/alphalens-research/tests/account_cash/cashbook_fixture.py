"""The rows the cash book's derivation is tested against (#1689, memo Phase 4).

Real rows come from the LIVE account on 2026-10-03, the capture
``tests/brokers/automanager/fixtures/trades/live_2026_10_03/venue.json``:
104 bookings rows and 41 trades rows, mapped through the adapter's own
``from_vendor_row`` so the tests exercise the same records the store holds.

**Synthetic rows exist only where the capture is uniform in a column a test
claims to read.** The capture is PLN on 104 of 104 bookings rows and 41 of 41
trade rows, every open episode on it is buy-only, no close anywhere exceeds its
open, and ``ToOpenOrClose`` agrees with ``sign(Amount)`` on all 41 rows -- so
each of those four claims needs a row the capture does not contain, or the test
cannot fail. Each helper below names which claim it is the tie-breaker for.

The record-level helpers take DATACLASS field names and go through
``dataclasses.replace``, not vendor keys: the three modules under test take
mapped records and never see a vendor row, and the mapping's own absent-versus-
null distinction is ``tests/brokers/test_saxo_broker_account_cash.py``'s
subject, not this one's.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.account_cash import BalanceSnapshot, CashEvent, TradeRow

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "brokers"
    / "automanager"
    / "fixtures"
    / "trades"
    / "live_2026_10_03"
    / "venue.json"
)

VENUE: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))

#: The 104 bookings rows, mapped. Every cash movement the account made over
#: 2026-08-01..2026-10-03, unfiltered.
BOOKINGS: tuple[CashEvent, ...] = tuple(
    CashEvent.from_vendor_row(dict(row)) for row in VENUE["bookings"]
)

#: The 41 trades rows, mapped. One row per execution.
TRADES: tuple[TradeRow, ...] = tuple(TradeRow.from_vendor_row(dict(row)) for row in VENUE["trades"])

#: The account's own balance, as read. ``CashBalance`` is memo section 1.6 P1
#: (LIVE 2026-10-05) and is the figure the cumulative cross-check reads in P4-b;
#: here it is one more money field whose absence must null its own total.
BALANCE = BalanceSnapshot.from_vendor_body(
    {
        "Currency": "PLN",
        "CashBalance": 20738.68,
        "NonMarginPositionsValue": 3422.56,
        "TotalValue": 24161.24,
        "TransactionsNotBooked": -4319.07,
        "CalculationReliability": "Ok",
    },
    asof=dt.datetime(2026, 10, 3, 6, 12, tzinfo=dt.UTC),
)


def event_like(base: CashEvent, **overrides: Any) -> CashEvent:
    """One real booking row with named fields changed."""
    return replace(base, **overrides)


def trade_like(base: TradeRow, **overrides: Any) -> TradeRow:
    """One real trades row with named fields changed."""
    return replace(base, **overrides)


def events_with_one_changed(index: int, **overrides: Any) -> tuple[CashEvent, ...]:
    """:data:`BOOKINGS` with row ``index`` changed, the row count unchanged."""
    rows = list(BOOKINGS)
    rows[index] = event_like(rows[index], **overrides)
    return tuple(rows)


def trades_with_one_changed(index: int, **overrides: Any) -> tuple[TradeRow, ...]:
    """:data:`TRADES` with row ``index`` changed, the row count unchanged."""
    rows = list(TRADES)
    rows[index] = trade_like(rows[index], **overrides)
    return tuple(rows)


def events_of_type(bk_amount_type: str) -> tuple[CashEvent, ...]:
    """Every real booking row the vendor stamped with this ``BkAmountType``."""
    return tuple(event for event in BOOKINGS if event.bk_amount_type == bk_amount_type)


def acct_pairs(events: tuple[CashEvent, ...]) -> list[tuple[float | None, str | None]]:
    """``(amount_account_currency, account_currency)`` per row, in order.

    The ONLY two fields of a booking row that any sum in this layer may read:
    ``conversion_cost_account_currency`` is already inside the account amount
    via the rate (memo section 1.3), so adding it double counts at about 0.25%
    of notional per leg.
    """
    return [(event.amount_account_currency, event.account_currency) for event in events]


# --- Synthetic trade rows, one per claim the capture cannot settle -----------

_SYNTHETIC_BASE = TRADES[0]
_SYNTHETIC_OPENED = dt.datetime(2026, 9, 15, 13, 40, tzinfo=dt.UTC)


def _synthetic_fill(
    *,
    uic: int,
    symbol: str,
    trade_id: str,
    amount: float,
    booked: float,
    minute: int,
    trade_date: str = "2026-09-15",
    **overrides: Any,
) -> TradeRow:
    """One fill on an instrument the capture does not contain."""
    return trade_like(
        _SYNTHETIC_BASE,
        trade_id=trade_id,
        order_id=f"O{trade_id}",
        uic=uic,
        instrument_symbol=symbol,
        amount=amount,
        booked_amount_account_currency=booked,
        trade_date=trade_date,
        value_date="2026-09-16",
        execution_time=_SYNTHETIC_OPENED + dt.timedelta(minutes=minute),
        **overrides,
    )


#: One ``uic``, one sell of 5, no buy anywhere. The quantity walk goes to -5 at
#: the first row, so there is no open position for the close to land on: 0
#: episodes and 1 unassigned fill. Half of the ``window_clipped`` pair.
SYNTHETIC_CLIPPED_SELL_ONLY_UIC = 990001


def synthetic_clipped_sell_only() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_CLIPPED_SELL_ONLY_UIC,
            symbol="SELLONLY:xnas",
            trade_id="9900010001",
            amount=-5.0,
            booked=480.00,
            minute=0,
        ),
    )


#: Buy 2, then sell 5. The walk goes 2 and then -3, so the close exceeds the
#: open it would have closed. This is the case a "the first row is a close"
#: check cannot see, and it is therefore the tie-breaker between that detector
#: and the quantity walk.
SYNTHETIC_CLIPPED_PARTIAL_UIC = 990002


def synthetic_clipped_partial() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_CLIPPED_PARTIAL_UIC,
            symbol="PARTIAL:xnas",
            trade_id="9900020001",
            amount=2.0,
            booked=-200.00,
            minute=0,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_CLIPPED_PARTIAL_UIC,
            symbol="PARTIAL:xnas",
            trade_id="9900020002",
            amount=-5.0,
            booked=480.00,
            minute=5,
        ),
    )


#: Buy 10, buy 5, sell 4 -- still long 11. Every open episode in the capture is
#: buy-only, so an implementation summing EVERY fill into ``cost_basis_acct``
#: passes on the real rows; this is the only case where the entry side and the
#: whole episode are two different numbers.
SYNTHETIC_PARTLY_CLOSED_UIC = 990003
SYNTHETIC_PARTLY_CLOSED_COST_BASIS = -1500.00
SYNTHETIC_PARTLY_CLOSED_MONEY = -1090.00


def synthetic_partly_closed() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_PARTLY_CLOSED_UIC,
            symbol="PARTLY:xnas",
            trade_id="9900030001",
            amount=10.0,
            booked=-1000.00,
            minute=0,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_PARTLY_CLOSED_UIC,
            symbol="PARTLY:xnas",
            trade_id="9900030002",
            amount=5.0,
            booked=-500.00,
            minute=5,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_PARTLY_CLOSED_UIC,
            symbol="PARTLY:xnas",
            trade_id="9900030003",
            amount=-4.0,
            booked=410.00,
            minute=10,
        ),
    )


#: Buy 1.5, sell 1.2 -- a residual of 0.3 shares, INSIDE ``QTY_PRECISION``
#: (0.5, half a share on a whole-share venue). Every quantity in the capture is
#: a whole share, so the epsilon in the walk's flat check and the one in
#: ``finish()`` are both unexercised there: ``== 0.0`` and ``opened == closed``
#: pass on all 41 real rows.
SYNTHETIC_FRACTIONAL_UIC = 990005


def synthetic_fractional_round_trip() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_FRACTIONAL_UIC,
            symbol="FRACTION:xnas",
            trade_id="9900050001",
            amount=1.5,
            booked=-150.00,
            minute=0,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_FRACTIONAL_UIC,
            symbol="FRACTION:xnas",
            trade_id="9900050002",
            amount=-1.2,
            booked=130.00,
            minute=5,
        ),
    )


#: Buy 2, sell 2.4 -- an over-close of 0.4, also inside ``QTY_PRECISION``, so
#: the walk places it rather than refusing it. This is the OTHER side of the
#: clip threshold from :func:`synthetic_clipped_partial`: that one must clip
#: and this one must not.
SYNTHETIC_OVER_CLOSE_INSIDE_EPSILON_UIC = 990006


def synthetic_over_close_inside_epsilon() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_OVER_CLOSE_INSIDE_EPSILON_UIC,
            symbol="EPSILON:xnas",
            trade_id="9900060001",
            amount=2.0,
            booked=-200.00,
            minute=0,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_OVER_CLOSE_INSIDE_EPSILON_UIC,
            symbol="EPSILON:xnas",
            trade_id="9900060002",
            amount=-2.4,
            booked=250.00,
            minute=5,
        ),
    )


#: A quantity-less fill arriving BEFORE any open on its ``uic``, then a clean
#: buy 4 / sell 4. The capture's one null-amount fill (``MARA`` 6827730144) is
#: not the first on its instrument, so the branch that remembers an unpairable
#: quantity until the next episode opens is never taken there.
SYNTHETIC_UNREAD_QUANTITY_FIRST_UIC = 990007


def synthetic_unread_quantity_first() -> tuple[TradeRow, ...]:
    return (
        trade_like(
            _synthetic_fill(
                uic=SYNTHETIC_UNREAD_QUANTITY_FIRST_UIC,
                symbol="UNREAD:xnas",
                trade_id="9900070001",
                amount=0.0,
                booked=-10.00,
                minute=0,
            ),
            amount=None,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_UNREAD_QUANTITY_FIRST_UIC,
            symbol="UNREAD:xnas",
            trade_id="9900070002",
            amount=4.0,
            booked=-400.00,
            minute=5,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_UNREAD_QUANTITY_FIRST_UIC,
            symbol="UNREAD:xnas",
            trade_id="9900070003",
            amount=-4.0,
            booked=440.00,
            minute=10,
        ),
    )


#: A complete round trip, then a fill whose quantity cannot be read, then TWO
#: more round trips -- all on one ``uic``. The capture's one null-amount fill
#: (``MARA`` 6827730144) has a single episode after it, and
#: :func:`synthetic_unread_quantity_first` has one too, so neither can tell a
#: flag that marks ONE episode from a flag that marks the rest of the
#: instrument's walk. Once a quantity is unreadable the position is unknowable
#: from that point on: the unreadable fill may have been a buy, in which case
#: the later rows are not flat round trips at all.
SYNTHETIC_UNREAD_QUANTITY_MIDWAY_UIC = 990014

#: The realized net of the FIRST round trip, which is before the unreadable
#: fill and therefore still eligible, and of each of the two after it, which
#: are not. Two different literals, so a fix that nulls the whole instrument
#: is told apart from one that nulls only what follows the fill.
SYNTHETIC_UNREAD_MIDWAY_FIRST_NET = 10.00
SYNTHETIC_UNREAD_MIDWAY_LATER_MONEY = 3.00


def _synthetic_unread_midway(*, unreadable_minute: int) -> tuple[TradeRow, ...]:
    """Round trip, two more round trips, and one unreadable fill between them.

    ``unreadable_minute`` places the unreadable fill: 10 puts it BETWEEN two
    episodes, 17 puts it INSIDE the second one. The stamp is what the walk
    sorts on, so the position in this tuple would not move it.
    """
    rows = [
        _synthetic_fill(
            uic=SYNTHETIC_UNREAD_QUANTITY_MIDWAY_UIC,
            symbol="MIDWAY:xnas",
            trade_id=trade_id,
            amount=amount,
            booked=booked,
            minute=minute,
        )
        for trade_id, amount, booked, minute in (
            ("9900140001", 10.0, -1000.00, 0),
            ("9900140002", -10.0, 1010.00, 5),
            ("9900140004", 5.0, -500.00, 15),
            ("9900140005", -5.0, 503.00, 20),
            ("9900140006", 5.0, -500.00, 25),
            ("9900140007", -5.0, 503.00, 30),
        )
    ]
    unreadable = trade_like(
        _synthetic_fill(
            uic=SYNTHETIC_UNREAD_QUANTITY_MIDWAY_UIC,
            symbol="MIDWAY:xnas",
            trade_id="9900140003",
            amount=0.0,
            booked=-1.00,
            minute=unreadable_minute,
        ),
        amount=None,
    )
    return (*rows[:2], unreadable, *rows[2:])


def synthetic_unread_quantity_midway() -> tuple[TradeRow, ...]:
    """The unreadable fill BETWEEN the first episode and the second."""
    return _synthetic_unread_midway(unreadable_minute=10)


def synthetic_unread_quantity_inside_episode() -> tuple[TradeRow, ...]:
    """The unreadable fill INSIDE the second episode, between its two legs."""
    return _synthetic_unread_midway(unreadable_minute=17)


#: Buy 2, sell 5, then buy 1 / sell 1 -- a clipped close followed by a clean
#: round trip on the same ``uic``. The shape amendment W8 discusses: the second
#: round trip is real money, and it only survives if the walk's position is
#: RESET after a close it could not place.
SYNTHETIC_CLIP_THEN_ROUND_TRIP_UIC = 990008


def synthetic_clip_then_round_trip() -> tuple[TradeRow, ...]:
    return tuple(
        _synthetic_fill(
            uic=SYNTHETIC_CLIP_THEN_ROUND_TRIP_UIC,
            symbol="CLIPTHEN:xnas",
            trade_id=trade_id,
            amount=amount,
            booked=booked,
            minute=minute,
        )
        for trade_id, amount, booked, minute in (
            ("9900080001", 2.0, -200.00, 0),
            ("9900080002", -5.0, 480.00, 5),
            ("9900080003", 1.0, -100.00, 10),
            ("9900080004", -1.0, 105.00, 15),
        )
    )


#: Two different ``uic`` values sharing one ``instrument_symbol`` -- a ticker
#: reuse or a dual listing. The capture is 1:1 between the two, so grouping by
#: symbol instead of by ``uic`` answers the same thing there; here it would
#: fold a buy on one instrument and a sell on another into one round trip.
SYNTHETIC_SHARED_SYMBOL = "SAME:xnas"
SYNTHETIC_SHARED_SYMBOL_BUY_UIC = 990009
SYNTHETIC_SHARED_SYMBOL_SELL_UIC = 990010


def synthetic_two_uics_one_symbol() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_SHARED_SYMBOL_BUY_UIC,
            symbol=SYNTHETIC_SHARED_SYMBOL,
            trade_id="9900090001",
            amount=3.0,
            booked=-300.00,
            minute=0,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_SHARED_SYMBOL_SELL_UIC,
            symbol=SYNTHETIC_SHARED_SYMBOL,
            trade_id="9900100001",
            amount=-3.0,
            booked=320.00,
            minute=5,
        ),
    )


#: Buy 3, a fill whose quantity is exactly ZERO, sell 3. No zero-quantity fill
#: exists in the capture, so a walk that counts one as an ENTRY -- and puts its
#: money in the cost basis -- is invisible on the real rows.
SYNTHETIC_ZERO_QUANTITY_UIC = 990011


def synthetic_zero_quantity_fill() -> tuple[TradeRow, ...]:
    return tuple(
        _synthetic_fill(
            uic=SYNTHETIC_ZERO_QUANTITY_UIC,
            symbol="ZEROQTY:xnas",
            trade_id=trade_id,
            amount=amount,
            booked=booked,
            minute=minute,
        )
        for trade_id, amount, booked, minute in (
            ("9900110001", 3.0, -300.00, 0),
            ("9900110002", 0.0, -7.00, 5),
            ("9900110003", -3.0, 350.00, 10),
        )
    )


#: The buy is stamped EARLIER and carries the HIGHER trade id. On the capture
#: the trade ids happen to be monotone in ``TradeExecutionTime``, so a walk
#: ordered by trade id alone answers the same thing there; here the two orders
#: disagree and only one of them is a round trip.
SYNTHETIC_ID_AGAINST_TIME_UIC = 990012


def synthetic_trade_id_disagrees_with_time() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_ID_AGAINST_TIME_UIC,
            symbol="IDVSTIME:xnas",
            trade_id="9900129999",
            amount=3.0,
            booked=-300.00,
            minute=0,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_ID_AGAINST_TIME_UIC,
            symbol="IDVSTIME:xnas",
            trade_id="9900120001",
            amount=-3.0,
            booked=310.00,
            minute=5,
        ),
    )


#: A round trip, plus a later buy with NO execution stamp. Every real row
#: carries one, so the "no stamp sorts last" half of the walk's key is
#: unexercised on the capture; sorting it first would merge the round trip into
#: the open position.
SYNTHETIC_UNSTAMPED_UIC = 990013


def synthetic_unstamped_fill_sorts_last() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_UNSTAMPED_UIC,
            symbol="NOSTAMP:xnas",
            trade_id="9900130001",
            amount=2.0,
            booked=-200.00,
            minute=0,
        ),
        _synthetic_fill(
            uic=SYNTHETIC_UNSTAMPED_UIC,
            symbol="NOSTAMP:xnas",
            trade_id="9900130002",
            amount=-2.0,
            booked=210.00,
            minute=5,
        ),
        trade_like(
            _synthetic_fill(
                uic=SYNTHETIC_UNSTAMPED_UIC,
                symbol="NOSTAMP:xnas",
                trade_id="9900130003",
                amount=1.0,
                booked=-100.00,
                minute=10,
            ),
            execution_time=None,
        ),
    )


#: An opening fill the venue calls ``ToOpen`` while its ``Amount`` is NEGATIVE.
#: On the capture the word and the sign agree on 41 of 41 rows, so only a row
#: where they disagree can show which of the two the walk reads.
SYNTHETIC_WORD_DISAGREES_UIC = 990004


def synthetic_word_disagrees_with_sign() -> tuple[TradeRow, ...]:
    return (
        _synthetic_fill(
            uic=SYNTHETIC_WORD_DISAGREES_UIC,
            symbol="DISAGREE:xnas",
            trade_id="9900040001",
            amount=6.0,
            booked=-600.00,
            minute=0,
            to_open_or_close="ToOpen",
            trade_event_type="Bought",
        ),
        _synthetic_fill(
            uic=SYNTHETIC_WORD_DISAGREES_UIC,
            symbol="DISAGREE:xnas",
            trade_id="9900040002",
            amount=-6.0,
            booked=610.00,
            minute=5,
            to_open_or_close="ToOpen",
            trade_event_type="Bought",
        ),
    )

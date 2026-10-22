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

"""The Saxo read behind :class:`SupportsAccountCash` (#1689, memo Phase 2).

A free function, not a method: ``saxo/broker.py`` sits on its own size
baseline, so the read body lives here and the adapter keeps the handful of
lines that resolve the two keys and delegate. It takes the client and both
keys as arguments and therefore touches no ``SaxoBroker`` attribute.

Three GETs, never a write, and no row-level filtering of any kind -- that is
the whole point of this capability (memo section 1.4). The client's three
readers already return every row of every page with no filtering of their own
(``saxo/client.py``), so the unfiltered read needs no client change.

This module imports nothing from ``saxo/broker.py``. That is structural, not
stylistic: the adapter imports this module at the top of its own import block,
so an import back the other way is a cycle. It is why the SIM refusal stays in
the adapter, where ``list_fill_history`` already makes the same check.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from alphalens_pipeline.brokers.account_cash import (
    AccountCashRead,
    CashEvent,
    TradeRow,
)
from alphalens_pipeline.brokers.fill_history import ReadWindow


def read_account_cash(
    client: Any,
    *,
    client_key: str,
    account_key: str,
    since: dt.date,
    until: dt.date,
) -> AccountCashRead:
    """Every cash movement booked between two dates, plus the account balance.

    ``since`` and ``until`` are DATES and are passed through unpadded: both
    report endpoints filter on a venue-stamped date (memo section 1.7 P9), so
    the window asked for is the window published."""
    from_date = since.isoformat()
    to_date = until.isoformat()
    booking_rows = client.get_bookings_report(
        client_key, account_key=account_key, from_date=from_date, to_date=to_date
    )
    trade_rows = client.get_trades_report(
        client_key, account_key=account_key, from_date=from_date, to_date=to_date
    )
    events = tuple(CashEvent.from_vendor_row(row) for row in booking_rows if isinstance(row, dict))
    trades = tuple(TradeRow.from_vendor_row(row) for row in trade_rows if isinstance(row, dict))
    return AccountCashRead(
        events=events,
        trades=trades,
        balance=None,
        events_window=ReadWindow(from_date, to_date, len(events)),
        trades_window=ReadWindow(from_date, to_date, len(trades)),
    )

"""The Saxo read behind :class:`SupportsAccountCash` (#1689, memo Phase 2).

A free function, not a method: ``saxo/broker.py`` sits on its own size
baseline, so the read body lives here and the adapter keeps the handful of
lines that resolve the two keys and delegate. It takes the client and both
keys as arguments and therefore touches no ``SaxoBroker`` attribute.

Three GETs, never a write, and no row-level filtering of any kind -- that is
the whole point of this capability (memo section 1.4). The client's three
readers already return every row of every page with no filtering of their own
(``saxo/client.py``), so the unfiltered read needs no client change.

Unfiltered means every row or no rows: a payload this mapping cannot place
raises :class:`VendorPayloadShapeError` and the whole window is refused,
rather than publishing the rows around it under a count that agrees with
them. The client type-checks nothing inside ``Data`` (it returns
``list(payload.get("Data") or [])``), so that shape is the vendor's to send.

This module imports nothing from ``saxo/broker.py``. That is structural, not
stylistic: the adapter imports this module at the top of its own import block,
so an import back the other way is a cycle. It is why the SIM refusal stays in
the adapter, where ``list_fill_history`` already makes the same check.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any

from alphalens_pipeline.brokers.account_cash import (
    AccountCashRead,
    BalanceSnapshot,
    CashEvent,
    TradeRow,
    VendorPayloadShapeError,
)
from alphalens_pipeline.brokers.fill_history import ReadWindow


def _map_every_row[Row](
    rows: Sequence[Any],
    mapper: Callable[[dict[str, Any]], Row],
    *,
    report: str,
) -> tuple[Row, ...]:
    """Map every row the vendor sent, or refuse the read naming the one that stopped it.

    Not ``if isinstance(row, dict)`` in a comprehension, which is what the
    fill-history read does: that one is filtered by design, so one more
    dropped row is one more filtered row, while here a dropped row is a cash
    movement that vanishes from a read whose published count then agrees with
    the rows that survived. ``VendorPayloadShapeError`` says why that is
    refused rather than skipped."""
    mapped: list[Row] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VendorPayloadShapeError(
                f"the {report} report row at index {index} of {len(rows)} is "
                f"{type(row).__name__}, not an object, so the read refuses the whole "
                f"window rather than publishing the rows around it"
            )
        mapped.append(mapper(row))
    return tuple(mapped)


def _require_plain_date(name: str, value: dt.date) -> dt.date:
    """One window edge, refused unless it is a plain ``date``.

    ``datetime`` SUBCLASSES ``date``, so the annotation on the read cannot
    stop one arriving and a static checker cannot flag the call. Passed
    through, its ``isoformat()`` carries a time of day into the vendor's
    ``FromDate`` / ``ToDate`` AND into the published ``ReadWindow``, so the
    window read would differ from the window asked for while reporting itself
    as the window asked for. The likely caller is a sweep that also holds
    ``list_fill_history``, which takes DATETIMES over these same two
    endpoints.

    A refusal rather than a normalisation to the date inside, which is this
    repo's habit on a window edge: a clamp publishes a number that reads as
    the thing asked for. A plain ``ValueError``, which is what every other
    argument refusal under ``brokers/`` raises (``execution.py``'s
    ``entry_ttl_days``, ``reconcile.py``'s audit budget): the mistake is the
    CALLER's, and a ``BrokerError`` would classify it at the CLI as
    ``broker_failed`` -- blaming the vendor for a bad call.
    """
    if isinstance(value, dt.datetime):
        raise ValueError(
            f"{name} must be a date, not a datetime ({value!r}): its time of day would "
            f"travel to the report endpoints as the window edge and be published as the "
            f"window's own edge, so the window read would not be the window asked for"
        )
    if not isinstance(value, dt.date):
        raise ValueError(f"{name} must be a datetime.date, got {type(value).__name__}")
    return value


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
    the window asked for is the window published. Both edges are checked
    before the first read: a ``datetime`` is refused rather than normalised
    (see :func:`_require_plain_date`)."""
    _require_plain_date("since", since)
    _require_plain_date("until", until)
    from_date = since.isoformat()
    to_date = until.isoformat()
    booking_rows = client.get_bookings_report(
        client_key, account_key=account_key, from_date=from_date, to_date=to_date
    )
    trade_rows = client.get_trades_report(
        client_key, account_key=account_key, from_date=from_date, to_date=to_date
    )
    events = _map_every_row(booking_rows, CashEvent.from_vendor_row, report="bookings")
    trades = _map_every_row(trade_rows, TradeRow.from_vendor_row, report="trades")
    balance_body = client.get_balances(client_key, account_key=account_key)
    if not isinstance(balance_body, dict):
        raise VendorPayloadShapeError(
            f"the balance body is {type(balance_body).__name__}, not an object; mapped "
            f"anyway it would read as a balance of nothing, stamped with a fresh asof"
        )
    balance = BalanceSnapshot.from_vendor_body(balance_body, asof=dt.datetime.now(dt.UTC))
    return AccountCashRead(
        events=events,
        trades=trades,
        balance=balance,
        events_window=ReadWindow(from_date, to_date, len(events)),
        trades_window=ReadWindow(from_date, to_date, len(trades)),
    )

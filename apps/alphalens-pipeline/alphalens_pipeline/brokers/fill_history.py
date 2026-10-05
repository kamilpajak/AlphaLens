"""The fill-history capability: what the broker says happened to orders (#1701).

``alphalens broker trades`` rebuilds one record per pick from the keeper's
journals AND from the broker's own records. This module is the broker half's
shape: a ``@runtime_checkable`` extension Protocol, checked with ``isinstance``
like the reconcile capabilities (``brokers/reconcile.py``), and the typed rows
an adapter returns.

The adapter REPORTS and the builder decides (#1122). Rows carry the vendor's
values as sent: a signed amount stays signed, a booking type stays its raw
string, a missing field stays ``None``. Nothing here classifies an exit,
allocates a fee or picks an owner.

Times are timezone-aware UTC datetimes. :func:`parse_utc` is the one parser,
shared with the builder, so a vendor timestamp and a journal timestamp go
through the same rules.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ISO-8601 with a fraction longer than Python's six digits (Saxo can send
# seven). The fraction is cut to microseconds before parsing.
_LONG_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def parse_utc(value: Any) -> dt.datetime | None:
    """A timestamp as an aware UTC datetime, or ``None`` when it is not one.

    Accepts an epoch number (the standalone-stop journal writes floats), an
    ISO-8601 string with ``Z`` or an offset, or a naive ISO string, which is
    read as UTC (every writer in this repo stamps UTC). ``bool`` and any
    non-finite number are refused rather than coerced."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(float(value)):
            return None
        try:
            return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        text = _LONG_FRACTION_RE.sub(r"\1", value.strip())
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class OrderActivity:
    """One audit row: one state change of one order."""

    order_id: str
    external_reference: str | None
    uic: int | None
    buy_sell: str | None
    order_type: str | None
    status: str
    sub_status: str | None
    price: float | None
    activity_time: dt.datetime | None
    amount: float | None
    filled_amount: float | None
    execution_price: float | None
    average_price: float | None
    position_id: str | None
    related_position_id: str | None
    log_id: int | None

    @classmethod
    def from_vendor_row(cls, row: dict[str, Any]) -> OrderActivity:
        """Map one ``/cs/v1/audit/orderactivities`` row, field by field."""
        return cls(
            order_id=str(row.get("OrderId", "")),
            external_reference=_opt_str(row.get("ExternalReference")),
            uic=_opt_int(row.get("Uic")),
            buy_sell=_opt_str(row.get("BuySell")),
            order_type=_opt_str(row.get("OrderType")),
            status=str(row.get("Status", "")),
            sub_status=_opt_str(row.get("SubStatus")),
            price=_opt_float(row.get("Price")),
            activity_time=parse_utc(row.get("ActivityTime")),
            amount=_opt_float(row.get("Amount")),
            filled_amount=_opt_float(row.get("FilledAmount")),
            execution_price=_opt_float(row.get("ExecutionPrice")),
            average_price=_opt_float(row.get("AveragePrice")),
            position_id=_opt_str(row.get("PositionId")),
            related_position_id=_opt_str(row.get("RelatedPositionId")),
            log_id=_opt_int(row.get("LogId")),
        )


@dataclass(frozen=True)
class Execution:
    """One trades-report row: one execution (TradeId) of one order."""

    trade_id: str
    order_id: str
    execution_time: dt.datetime | None
    price: float | None
    amount: float | None  # signed as sent: negative = sold
    to_open_or_close: str | None
    trade_date: str | None
    booked_amount_account_currency: float | None
    account_currency: str | None

    @classmethod
    def from_vendor_row(cls, row: dict[str, Any]) -> Execution:
        """Map one ``/cs/v1/reports/trades`` row.

        ``booked_amount_account_currency`` is the raw
        ``BookedAmountAccountCurrency``: the venue's own net for this
        execution, in the account currency. On the 2026-10-03 LIVE capture it
        equals the execution's ``Share Amount`` plus its ``Commission`` plus
        its ``Exchange Fee`` in ``AmountAccountCurrency`` on 41 of 41 rows, and
        equals the ``Share Amount`` alone on none. That reading is the
        builder's, not this row's; this row only reports the field."""
        return cls(
            trade_id=str(row.get("TradeId", "")),
            order_id=str(row.get("OrderId", "")),
            execution_time=parse_utc(row.get("TradeExecutionTime")),
            price=_opt_float(row.get("Price")),
            amount=_opt_float(row.get("Amount")),
            to_open_or_close=_opt_str(row.get("ToOpenOrClose")),
            trade_date=_opt_str(row.get("TradeDate")),
            booked_amount_account_currency=_opt_float(row.get("BookedAmountAccountCurrency")),
            account_currency=_opt_str(row.get("AccountCurrency")),
        )


@dataclass(frozen=True)
class CostBooking:
    """One bookings-report row, with the vendor's raw type and signs."""

    related_trade_id: str
    bk_amount_type: str
    amount: float | None
    currency: str | None
    amount_account_currency: float | None
    account_currency: str | None
    conversion_rate: float | None
    conversion_cost_account_currency: float | None
    date: str | None

    @classmethod
    def from_vendor_row(cls, row: dict[str, Any]) -> CostBooking:
        """Map one ``/cs/v1/reports/bookings`` row.

        ``conversion_cost_account_currency`` is the raw
        ``ConversionRateAccountCurrency``. The LIVE probe of 2026-10-03 read it
        as the FX conversion charge on a ``Share Amount`` row (about 0.25 % of
        the traded value on all 25 rows) and as rounding (±0.01) on a
        ``Commission`` row; that reading is the builder's, not this row's."""
        return cls(
            related_trade_id=str(row.get("RelatedTradeId", "")),
            bk_amount_type=str(row.get("BkAmountType", "")),
            amount=_opt_float(row.get("Amount")),
            currency=_opt_str(row.get("Currency")),
            amount_account_currency=_opt_float(row.get("AmountAccountCurrency")),
            account_currency=_opt_str(row.get("AccountCurrency")),
            conversion_rate=_opt_float(row.get("ConversionRate")),
            conversion_cost_account_currency=_opt_float(row.get("ConversionRateAccountCurrency")),
            date=_opt_str(row.get("Date")),
        )


@dataclass(frozen=True)
class ReadWindow:
    """What one read asked for, so the output can say so."""

    start: str
    end: str
    rows: int


@dataclass(frozen=True)
class FillHistory:
    """Everything one ``list_fill_history`` call read.

    ``executions`` and ``bookings`` are ``None`` when the reports were not read
    at all, and ``reports_skipped_reason`` then says why (for example
    ``sim_reports_unusable``: the SIM report endpoints answer with canned data
    from other accounts). An empty tuple means "read, and nothing was there"."""

    activities: tuple[OrderActivity, ...]
    executions: tuple[Execution, ...] | None
    bookings: tuple[CostBooking, ...] | None
    audit_window: ReadWindow
    trades_window: ReadWindow | None = None
    bookings_window: ReadWindow | None = None
    reports_skipped_reason: str | None = None


@runtime_checkable
class SupportsFillHistory(Protocol):
    """Extension capability: the broker's own record of orders and fills."""

    def list_fill_history(self, since: dt.datetime, until: dt.datetime) -> FillHistory: ...

    def tick_size(self, uic: int, price: float) -> float | None: ...


__all__ = [
    "CostBooking",
    "Execution",
    "FillHistory",
    "OrderActivity",
    "ReadWindow",
    "SupportsFillHistory",
    "parse_utc",
]

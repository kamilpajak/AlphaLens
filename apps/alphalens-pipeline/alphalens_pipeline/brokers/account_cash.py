"""The account-cash capability: what the account's money actually did (#1689).

The cash book's source of truth is ``/cs/v1/reports/bookings`` read
**unfiltered** (memo section 2): every booking row is a cash movement in the
account's own currency, so a sum over all rows for a period is that period's
whole money movement by construction, and nothing can be missing without the
row count saying so.

This is a SECOND capability over the same endpoints as
:mod:`alphalens_pipeline.brokers.fill_history`, not a widening of it. That one
keeps only bookings whose ``related_trade_id`` is in the trades it kept
(``saxo/broker.py:1707``); on the 2026-10-03 LIVE capture that drops 5 of 104
rows worth 24 004.05 PLN -- the three deposits and the two corporate-action
rows (memo section 1.4). It is fill-shaped by construction and correctly so;
the cash book needs the rows it drops.

The adapter REPORTS and the caller decides (#1122). Rows carry the vendor's
values as sent: a signed amount stays signed, a booking type stays its raw
string, a missing field stays ``None``. **Nothing here classifies.** There is
no bucket, no ``BkAmountType`` vocabulary and no total -- those are memo
Phase 4, and a classifier keying on "a non-zero ``RelatedTradeId`` means a
trade" folds 24 000 PLN of funding into trading P&L, because the three
deposits carry non-trade ``RelatedTradeId`` values (memo section 1.1).

The coercers come from :mod:`alphalens_pipeline.brokers.fill_history` by their
private names, deliberately: there is ONE definition of the coercion rules for
both mappings of the same vendor rows, and it is a sibling module in this same
package. ``saxo/broker.py`` defines its own ``_opt_float`` with different
semantics -- ``float(value)``, which RAISES on a non-numeric string and accepts
``bool`` -- and binding that one would put a traceback on the money path.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from alphalens_pipeline.brokers.fill_history import (
    ReadWindow,
    _opt_float,
    _opt_int,
    _opt_str,
)

# ``AccountCashRead.skipped_reason`` on SIM: the SIM report endpoints answer
# with canned rows from other accounts, and storing fiction in a money store is
# worse than storing nothing (memo section 3). This is the capability's own
# public vocabulary, declared beside the field that carries it; the adapter's
# ``_SIM_REPORTS_UNUSABLE`` keeps its own spelling for the fill-history read.
SIM_REPORTS_UNUSABLE = "sim_reports_unusable"


@dataclass(frozen=True)
class CashEvent:
    """One bookings-report row, UNFILTERED, with the vendor's raw type and signs."""

    bk_amount_id: str
    bk_amount_type: str
    amount_class: str | None
    amount: float | None
    currency: str | None
    amount_account_currency: float | None
    account_currency: str | None
    conversion_rate: float | None
    conversion_cost_account_currency: float | None
    date: str | None
    value_date: str | None
    uic: int | None
    instrument_symbol: str | None
    related_trade_id: str
    ca_event_name: str | None

    @classmethod
    def from_vendor_row(cls, row: dict[str, Any]) -> CashEvent:
        """Map one ``/cs/v1/reports/bookings`` row, field by field."""
        return cls(
            bk_amount_id=str(row.get("BkAmountId", "")),
            bk_amount_type=str(row.get("BkAmountType", "")),
            amount_class=_opt_str(row.get("AmountClass")),
            amount=_opt_float(row.get("Amount")),
            currency=_opt_str(row.get("Currency")),
            amount_account_currency=_opt_float(row.get("AmountAccountCurrency")),
            account_currency=_opt_str(row.get("AccountCurrency")),
            conversion_rate=_opt_float(row.get("ConversionRate")),
            conversion_cost_account_currency=_opt_float(row.get("ConversionRateAccountCurrency")),
            date=_opt_str(row.get("Date")),
            value_date=_opt_str(row.get("ValueDate")),
            uic=_opt_int(row.get("Uic")),
            instrument_symbol=_opt_str(row.get("InstrumentSymbol")),
            related_trade_id=str(row.get("RelatedTradeId", "")),
            ca_event_name=_opt_str(row.get("CaEventName")),
        )


@dataclass(frozen=True)
class TradeRow:
    """One trades-report row: one execution (TradeId) of one order."""

    trade_id: str
    order_id: str
    uic: int | None
    instrument_symbol: str | None
    amount: float | None
    price: float | None
    trade_event_type: str | None
    to_open_or_close: str | None
    trade_date: str | None
    value_date: str | None
    execution_time: dt.datetime | None
    booked_amount_account_currency: float | None
    account_currency: str | None
    venue: str | None

    @classmethod
    def from_vendor_row(cls, row: dict[str, Any]) -> TradeRow:
        """Map one ``/cs/v1/reports/trades`` row, field by field."""
        return cls(
            trade_id=str(row.get("TradeId", "")),
            order_id="",
            uic=None,
            instrument_symbol=None,
            amount=None,
            price=None,
            trade_event_type=None,
            to_open_or_close=None,
            trade_date=None,
            value_date=None,
            execution_time=None,
            booked_amount_account_currency=None,
            account_currency=None,
            venue=None,
        )


@dataclass(frozen=True)
class BalanceSnapshot:
    """One ``/port/v1/balances`` body, as read."""

    currency: str | None
    cash_balance: float | None
    non_margin_positions_value: float | None
    total_value: float | None
    transactions_not_booked: float | None
    calculation_reliability: str | None
    asof: dt.datetime

    @classmethod
    def from_vendor_body(cls, body: dict[str, Any], *, asof: dt.datetime) -> BalanceSnapshot:
        """Map one ``/port/v1/balances`` body, field by field."""
        raise NotImplementedError


@dataclass(frozen=True)
class AccountCashRead:
    """Everything one ``list_account_cash`` call read.

    ``events``, ``trades`` and ``balance`` are ``None`` when nothing was read
    at all, and ``skipped_reason`` then says why. An empty tuple means "read,
    and nothing was there"."""

    events: tuple[CashEvent, ...] | None
    trades: tuple[TradeRow, ...] | None
    balance: BalanceSnapshot | None
    events_window: ReadWindow | None = None
    trades_window: ReadWindow | None = None
    skipped_reason: str | None = None


@runtime_checkable
class SupportsAccountCash(Protocol):
    """Extension capability: the account's own cash movements, UNFILTERED."""

    def list_account_cash(self, since: dt.date, until: dt.date) -> AccountCashRead: ...


__all__ = [
    "SIM_REPORTS_UNUSABLE",
    "AccountCashRead",
    "BalanceSnapshot",
    "CashEvent",
    "SupportsAccountCash",
    "TradeRow",
]

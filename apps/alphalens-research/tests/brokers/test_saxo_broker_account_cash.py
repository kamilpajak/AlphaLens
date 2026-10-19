"""The Saxo adapter's account-cash capability (#1689, memo Phase 2).

``list_account_cash`` is the cash book's read: the SAME two report endpoints
``list_fill_history`` uses, read UNFILTERED, plus the account's own balance.
The fill-history capability cannot serve as the cash book — it keeps only
bookings whose ``related_trade_id`` is in the trades it kept
(``saxo/broker.py:1707``), which on the captured window drops 5 of 104 rows
worth 24 004.05 PLN (memo section 1.4).

Rows come from the LIVE account on 2026-10-03, the fixture
``automanager/fixtures/trades/live_2026_10_03/venue.json`` (104 bookings rows,
41 trades rows), and the balance body from the LIVE probes of 2026-10-05
(memo section 1.6 P1) and 2026-10-08 (memo section 1.7 P5).
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.account_cash import (
    CashEvent,
    SupportsAccountCash,
    TradeRow,
)
from alphalens_pipeline.brokers.fill_history import CostBooking, Execution, ReadWindow
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from alphalens_pipeline.brokers.saxo.client import SIM_BASE_URL, SaxoRateLimitError
from broker_contract.contract import BrokerRateLimitError

_VENUE = json.loads(
    (
        Path(__file__).parent
        / "automanager"
        / "fixtures"
        / "trades"
        / "live_2026_10_03"
        / "venue.json"
    ).read_text(encoding="utf-8")
)
_LIVE_BASE = "https://gateway.saxobank.com/" + "openapi"

# The balance body: memo section 1.6 P1 (LIVE 2026-10-05) for the five
# position fields, and memo section 1.7 P5 (LIVE 2026-10-08, 23:46:52 UTC,
# three trades in flight) for ``TransactionsNotBooked`` -- the only state in
# which that field is non-zero, and the one P1's quiet window could not show.
_BALANCE_BODY: dict[str, Any] = {
    "Currency": "PLN",
    "CashBalance": 20738.68,
    "NonMarginPositionsValue": 3422.56,
    "TotalValue": 24161.24,
    "TransactionsNotBooked": -4319.07,
    "CalculationReliability": "Ok",
}

_SINCE = dt.date(2026, 8, 1)
_UNTIL = dt.date(2026, 10, 3)


class _FakeClient:
    """Answers the reads ``list_account_cash`` makes, and records them."""

    def __init__(self, *, base_url: str = _LIVE_BASE) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bookings: list[dict[str, Any]] = [dict(r) for r in _VENUE["bookings"]]
        self.trades: list[dict[str, Any]] = [dict(r) for r in _VENUE["trades"]]
        self.balance: dict[str, Any] = dict(_BALANCE_BODY)
        self.rate_limited = False

    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": "CK-1"}

    def get_accounts(self) -> dict[str, Any]:
        return {"Data": [{"AccountKey": "AK-1"}]}

    def get_trades_report(self, client_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("trades", kwargs))
        return list(self.trades)

    def get_bookings_report(self, client_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("bookings", kwargs))
        if self.rate_limited:
            raise SaxoRateLimitError("429")
        return list(self.bookings)

    def get_balances(self, client_key: str, account_key: str | None = None) -> dict[str, Any]:
        self.calls.append(("balances", {"account_key": account_key}))
        return dict(self.balance)


class TheCapability(unittest.TestCase):
    def test_the_broker_offers_the_capability(self) -> None:
        broker = SaxoBroker(_FakeClient())  # type: ignore[arg-type]
        self.assertIsInstance(broker, SupportsAccountCash)


class TheRead(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]

    def test_every_booking_row_comes_back_unfiltered(self) -> None:
        """All 104 rows, including the 5 the fill-history filter drops.

        ``list_fill_history`` keeps only bookings whose ``related_trade_id`` is
        in the trades it kept (``saxo/broker.py:1707``). The five ids below are
        exactly the rows that filter removes: three deposits, whose
        ``RelatedTradeId`` is a non-trade value, and the two corporate-action
        rows, whose ``RelatedTradeId`` is ``"0"``."""
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.events is not None
        self.assertEqual(len(read.events), 104)
        ids = {event.bk_amount_id for event in read.events}
        for dropped in (
            "58226097441",
            "58612863938",
            "59102022834",
            "60187389516",
            "60187389517",
        ):
            with self.subTest(bk_amount_id=dropped):
                self.assertIn(dropped, ids)

    def test_the_window_asked_for_is_the_window_published(self) -> None:
        """Dates in, dates out, no pad.

        ``list_fill_history`` pads the report window by one day on both ends
        because its caller passes datetimes and the 2026-10-03 probe could not
        tell whether the report date was UTC or account-local. Memo section 1.7
        P9 settled that: ``Date`` is the venue-stamped trade date. So the
        window asked for IS the window read, and a sweep of ``2026-08-01`` is
        not silently two days wider."""
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        kwargs = dict(self.client.calls)["bookings"]
        self.assertEqual(kwargs["from_date"], "2026-08-01")
        self.assertEqual(kwargs["to_date"], "2026-10-03")
        self.assertEqual(kwargs["account_key"], "AK-1")
        self.assertEqual(read.events_window, ReadWindow("2026-08-01", "2026-10-03", 104))

    def test_the_trades_window_is_published_too(self) -> None:
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        kwargs = dict(self.client.calls)["trades"]
        self.assertEqual(kwargs["from_date"], "2026-08-01")
        self.assertEqual(kwargs["to_date"], "2026-10-03")
        self.assertEqual(read.trades_window, ReadWindow("2026-08-01", "2026-10-03", 41))

    def test_a_vendor_error_leaves_as_a_contract_error(self) -> None:
        """The network calls sit INSIDE ``_translate_saxo_errors()``.

        Outside it a raw ``SaxoError`` escapes the adapter boundary and the CLI
        classifies it as the base ``broker_failed`` instead of the retryable
        rate-limit code."""
        self.client.rate_limited = True
        with self.assertRaises(BrokerRateLimitError):
            self.broker.list_account_cash(_SINCE, _UNTIL)


class TheCashEventMapping(unittest.TestCase):
    """The bookings-row mapping, field by field, on real LIVE rows."""

    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]

    def _event(self, bk_amount_id: str) -> CashEvent:
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.events is not None
        return next(e for e in read.events if e.bk_amount_id == bk_amount_id)

    def test_a_share_amount_row_is_mapped_as_sent(self) -> None:
        self.assertEqual(
            self._event("58330463544"),
            CashEvent(
                bk_amount_id="58330463544",
                bk_amount_type="Share Amount",
                amount_class="NotAssigned",
                amount=-13.78,
                currency="USD",
                amount_account_currency=-51.49,
                account_currency="PLN",
                conversion_rate=3.73625234,
                conversion_cost_account_currency=-0.130000000000003,
                date="2026-08-10",
                value_date="2026-08-11",
                uic=486,
                instrument_symbol="F:xnys",
                related_trade_id="6824247527",
                ca_event_name=None,
            ),
        )

    def test_a_corporate_action_row_carries_its_event_name(self) -> None:
        """``CaEventName`` is present on exactly 2 of the 104 rows.

        102 rows carry 36 keys and these two carry 37, so a mapping written
        against the common shape alone never reads the field."""
        event = self._event("60187389516")
        self.assertEqual(event.bk_amount_type, "Corporate Actions - Cash Dividends")
        self.assertEqual(event.ca_event_name, "Cash Dividend")
        self.assertEqual(event.related_trade_id, "0")

    def test_a_deposit_keeps_its_non_trade_related_trade_id_as_sent(self) -> None:
        """Memo section 1.1's trap, carried rather than hidden.

        The three deposits carry NON-ZERO ``RelatedTradeId`` values that are
        not trade ids. A classifier keying on "non-zero means a trade" folds
        24 000 PLN of funding into trading P&L. This layer reports the value as
        sent and classifies nothing.

        Selected by ``BkAmountId``, never by ``InstrumentSymbol``: all three
        deposits carry the symbol ``CASHDEPWI``, so a symbol selector is
        under-determined, and the symbol is itself one half of a uniform
        key pair (see the twin-key test)."""
        event = self._event("58226097441")
        self.assertEqual(event.bk_amount_type, "Cash Amount")
        self.assertEqual(event.related_trade_id, "6820472599")
        self.assertEqual(event.amount_account_currency, 2000.0)

    def test_the_left_hand_key_of_each_uniform_pair_is_the_one_read(self) -> None:
        """Four vendor key pairs are byte-identical on all 104 real rows.

        Measured on the fixture: ``RelatedTradeId == RelatedPositionId``,
        ``Uic == UnderlyingInstrumentUic``,
        ``InstrumentSymbol == UnderlyingInstrumentSymbol`` and
        ``AccountCurrency == ClientCurrency``, each 104 of 104 with both keys
        present on every row. So reading the right-hand key instead is
        invisible on the whole real capture, and only a row whose twins
        DISAGREE can tell the two apart. The parity test against
        ``CostBooking`` cannot see this either: that mapping reads the same
        left-hand keys, so both agree under the swap, and it carries neither
        ``uic`` nor ``instrument_symbol`` at all."""
        self.client.bookings = [
            {
                **next(r for r in _VENUE["bookings"] if r["BkAmountId"] == "58330463544"),
                "RelatedPositionId": "999",
                "UnderlyingInstrumentUic": 999,
                "UnderlyingInstrumentSymbol": "WRONG:xnas",
                "ClientCurrency": "EUR",
            }
        ]
        event = self._event("58330463544")
        self.assertEqual(event.related_trade_id, "6824247527")
        self.assertEqual(event.uic, 486)
        self.assertEqual(event.instrument_symbol, "F:xnys")
        self.assertEqual(event.account_currency, "PLN")


class TheTradeRowMapping(unittest.TestCase):
    """The trades-row mapping, field by field, on real LIVE rows."""

    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]

    def _row(self, trade_id: str) -> TradeRow:
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.trades is not None
        return next(r for r in read.trades if r.trade_id == trade_id)

    def test_a_trade_row_is_mapped_as_sent(self) -> None:
        """Without this and the three tests beside it, an implementation that
        returns ``trades=None`` always, or drops ``TradeRow.from_vendor_row``
        entirely, passes every other test in this file."""
        self.assertEqual(
            self._row("6824247527"),
            TradeRow(
                trade_id="6824247527",
                order_id="5432611806",
                uic=486,
                instrument_symbol="F:xnys",
                amount=1.0,
                price=13.78,
                trade_event_type="Bought",
                to_open_or_close="ToOpen",
                trade_date="2026-08-10",
                value_date="2026-08-11",
                execution_time=dt.datetime(2026, 8, 10, 13, 58, 40, 720000, tzinfo=dt.UTC),
                booked_amount_account_currency=-55.23,
                account_currency="PLN",
                venue="Exchange",
            ),
        )

    def test_the_trade_rows_left_hand_keys_are_the_ones_read(self) -> None:
        """The trades side carries four uniform pairs of its own, and one of
        them is the money field.

        Measured on the fixture, each 41 of 41:
        ``BookedAmountAccountCurrency == BookedAmountClientCurrency``,
        ``TradeDate == AdjustedTradeDate``,
        ``InstrumentSymbol == UnderlyingInstrumentSymbol`` and
        ``AccountCurrency == ClientCurrency``."""
        self.client.trades = [
            {
                **next(r for r in _VENUE["trades"] if r["TradeId"] == "6824247527"),
                "UnderlyingInstrumentSymbol": "WRONG:xnas",
                "AdjustedTradeDate": "1999-01-01",
                "BookedAmountClientCurrency": 999.99,
                "ClientCurrency": "EUR",
            }
        ]
        row = self._row("6824247527")
        self.assertEqual(row.instrument_symbol, "F:xnys")
        self.assertEqual(row.trade_date, "2026-08-10")
        self.assertEqual(row.booked_amount_account_currency, -55.23)
        self.assertEqual(row.account_currency, "PLN")


class MoneyThatIsAbsentStaysDistinctFromZero(unittest.TestCase):
    """Both money fields, on both row types.

    This is what discriminates ``fill_history``'s coercers from the ones
    ``saxo/broker.py`` defines under the same names. That module's
    ``_opt_float`` is ``None if value is None else float(value)``: it RAISES
    on a non-numeric string, so binding it puts a traceback on the money path.
    It also catches an ``or 0.0`` in either mapping, which would turn a vendor
    null into a real ``0.0`` on the field Phase 4 will sum."""

    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]

    def _booking_with(self, value: Any) -> CashEvent:
        base = next(r for r in _VENUE["bookings"] if r["BkAmountId"] == "58330463544")
        self.client.bookings = [{**base, "AmountAccountCurrency": value}]
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.events is not None
        return read.events[0]

    def _trade_with(self, value: Any) -> TradeRow:
        base = next(r for r in _VENUE["trades"] if r["TradeId"] == "6824247527")
        self.client.trades = [{**base, "BookedAmountAccountCurrency": value}]
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.trades is not None
        return read.trades[0]

    def test_an_unparseable_booking_amount_maps_to_none_and_does_not_raise(self) -> None:
        self.assertIsNone(self._booking_with("oops").amount_account_currency)

    def test_a_zero_booking_amount_stays_zero(self) -> None:
        self.assertEqual(self._booking_with(0.0).amount_account_currency, 0.0)

    def test_a_null_trade_amount_maps_to_none(self) -> None:
        """``BookedAmountAccountCurrency`` is non-null on all 41 fixture rows,
        so only a constructed null can tell ``None`` from ``0.0`` here."""
        self.assertIsNone(self._trade_with(None).booked_amount_account_currency)

    def test_a_zero_trade_amount_stays_zero(self) -> None:
        self.assertEqual(self._trade_with(0.0).booked_amount_account_currency, 0.0)


class TheBalance(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]

    def test_a_balance_body_is_mapped_including_the_field_account_snapshot_drops(self) -> None:
        """Driven through the READ, not through the classmethod.

        A read that never calls ``get_balances`` and returns ``balance=None``
        satisfies every other test in this file, and nothing downstream would
        notice until Phase 4, where the balance cross-check is the single
        independent check on the money. So the subject here is
        ``list_account_cash``.

        ``contract.AccountSnapshot`` cannot be reused: it has no
        ``TransactionsNotBooked`` field at all, so it would not construct.
        That field is what probe 3 showed matters -- it parks a whole day's
        trading before the nightly batch (memo section 1.7 P5) -- and it is
        also the field the memo warns is NOT a completeness signal (P7).

        The arithmetic identity ``cash_balance + non_margin_positions_value ==
        total_value`` is deliberately NOT asserted: there is no balances
        fixture in the repo, so the implementer would be authoring the numbers
        that make it true, and it is a float equality on money. The mapping is
        the half with power."""
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        balance = read.balance
        assert balance is not None
        self.assertEqual(balance.currency, "PLN")
        self.assertEqual(balance.cash_balance, 20738.68)
        self.assertEqual(balance.non_margin_positions_value, 3422.56)
        self.assertEqual(balance.total_value, 24161.24)
        self.assertEqual(balance.transactions_not_booked, -4319.07)
        self.assertEqual(balance.calculation_reliability, "Ok")
        self.assertIsNotNone(balance.asof.tzinfo)


class OnSim(unittest.TestCase):
    def test_on_sim_no_report_endpoint_is_called(self) -> None:
        """The SIM report endpoints answer with canned rows belonging to other
        accounts, so reading them and discarding the rows is not good enough:
        the request still goes across the wire.

        The refusal covers the balance too, although ``/port/v1/balances`` is
        not a report endpoint and the daemon reads it every tick. A balance
        with no rows beside it answers nothing, and memo section 3 says there
        is no SIM half of this layer -- so the refusal stays one fact rather
        than a partial result."""
        client = _FakeClient(base_url=SIM_BASE_URL)
        read = SaxoBroker(client).list_account_cash(_SINCE, _UNTIL)  # type: ignore[arg-type]
        self.assertEqual(client.calls, [])
        self.assertIsNone(read.events)
        self.assertIsNone(read.trades)
        self.assertIsNone(read.balance)
        self.assertEqual(read.skipped_reason, "sim_reports_unusable")


class TheTwoMappingsOfTheSameRows(unittest.TestCase):
    """Drift containment between this capability and fill-history.

    STATED LIMIT, so it is not discovered later: this is drift containment,
    not mapping verification. It is blind to all four uniform key pairs on
    each side -- ``CostBooking`` reads the same left-hand keys, so both
    mappings still agree under a swap -- and ``CostBooking`` and ``Execution``
    carry neither ``uic`` nor ``instrument_symbol`` at all, so two of the four
    booking swaps fall outside its field set entirely. The twin-key tests are
    the only checks with power there."""

    def test_the_bookings_mappings_agree_on_every_shared_field(self) -> None:
        shared = (
            "bk_amount_type",
            "amount",
            "currency",
            "amount_account_currency",
            "account_currency",
            "conversion_rate",
            "conversion_cost_account_currency",
            "date",
            "related_trade_id",
        )
        # Without this floor the whole test passes vacuously on an empty
        # fixture list -- a property that is true AND empty.
        self.assertEqual(len(_VENUE["bookings"]), 104)
        for row in _VENUE["bookings"]:
            event = CashEvent.from_vendor_row(row)
            booking = CostBooking.from_vendor_row(row)
            for field in shared:
                with self.subTest(bk_amount_id=row["BkAmountId"], field=field):
                    self.assertEqual(getattr(event, field), getattr(booking, field))

    def test_the_trades_mappings_agree_on_every_shared_field(self) -> None:
        shared = (
            "trade_id",
            "order_id",
            "price",
            "amount",
            "to_open_or_close",
            "trade_date",
            "execution_time",
            "booked_amount_account_currency",
            "account_currency",
        )
        self.assertEqual(len(_VENUE["trades"]), 41)
        for row in _VENUE["trades"]:
            mine = TradeRow.from_vendor_row(row)
            theirs = Execution.from_vendor_row(row)
            for field in shared:
                with self.subTest(trade_id=row["TradeId"], field=field):
                    self.assertEqual(getattr(mine, field), getattr(theirs, field))


if __name__ == "__main__":
    unittest.main()

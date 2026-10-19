"""The Saxo adapter's account-cash capability (#1689, memo Phase 2).

``list_account_cash`` is the cash book's read: the SAME two report endpoints
``list_fill_history`` uses, read UNFILTERED, plus the account's own balance.
The fill-history capability cannot serve as the cash book -- it keeps only
bookings whose ``related_trade_id`` is in the trades it kept (the
``kept_trades`` filter in ``SaxoBroker.list_fill_history``), which on the
captured window drops 5 of 104 rows worth 24 004.05 PLN (memo section 1.4).

Rows come from the LIVE account on 2026-10-03, the fixture
``automanager/fixtures/trades/live_2026_10_03/venue.json`` (104 bookings rows,
41 trades rows), and the balance body from the LIVE probes of 2026-10-05
(memo section 1.6 P1) and 2026-10-08 (memo section 1.7 P5).
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import unittest
from pathlib import Path
from typing import Any, get_type_hints

from alphalens_pipeline.brokers.account_cash import (
    BalanceSnapshot,
    CashEvent,
    SupportsAccountCash,
    TradeRow,
    VendorPayloadShapeError,
)
from alphalens_pipeline.brokers.fill_history import CostBooking, Execution, ReadWindow, _opt_int
from alphalens_pipeline.brokers.saxo.account_cash_read import read_account_cash
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from alphalens_pipeline.brokers.saxo.client import SIM_BASE_URL, SaxoClient, SaxoRateLimitError
from broker_contract.contract import BrokerError, BrokerRateLimitError

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

#: Marks a key the vendor did NOT send, so "absent" is expressible beside
#: "sent as null". A dict override can only say the second, and the whole
#: subject of the identity tests below is that the two are different facts.
_ABSENT: Any = object()


def _vendor_row(base: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """``base`` with ``overrides`` applied, an ``_ABSENT`` value removing its key."""
    row = {**base, **overrides}
    for key, value in overrides.items():
        if value is _ABSENT:
            del row[key]
    return row


class _FakeClient:
    """Answers the reads ``list_account_cash`` makes, and records them."""

    def __init__(self, *, base_url: str = _LIVE_BASE) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Typed loosely on purpose: the vendor can hand over a row, or a
        # whole body, that is not an object, and a fake that cannot express
        # that shape cannot test what the read does with it.
        self.bookings: list[Any] = [dict(r) for r in _VENUE["bookings"]]
        self.trades: list[Any] = [dict(r) for r in _VENUE["trades"]]
        self.balance: Any = dict(_BALANCE_BODY)
        self.rate_limited = False

    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": "CK-1"}

    def get_accounts(self) -> dict[str, Any]:
        return {"Data": [{"AccountKey": "AK-1"}]}

    def get_trades_report(self, client_key: str, **kwargs: Any) -> list[Any]:
        self.calls.append(("trades", {"client_key": client_key, **kwargs}))
        return list(self.trades)

    def get_bookings_report(self, client_key: str, **kwargs: Any) -> list[Any]:
        self.calls.append(("bookings", {"client_key": client_key, **kwargs}))
        if self.rate_limited:
            raise SaxoRateLimitError("429")
        return list(self.bookings)

    def get_balances(self, client_key: str, account_key: str | None = None) -> Any:
        self.calls.append(("balances", {"client_key": client_key, "account_key": account_key}))
        return self.balance


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
        in the trades it kept -- the ``kept_trades`` filter. The five ids below are
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

    def test_every_endpoint_is_asked_about_this_client_and_this_account(self) -> None:
        """Both keys, on all three reads.

        The two keys are different strings with the same shape, and the client
        takes the client key positionally and the account key by name, so
        passing one where the other belongs is a type-correct call. On a client
        with more than one account that reads another account's money and
        publishes it as this one's. The fixture's balance body is the same
        whichever account it is asked for, so only the recorded call can tell."""
        self.broker.list_account_cash(_SINCE, _UNTIL)
        calls = dict(self.client.calls)
        for endpoint in ("bookings", "trades", "balances"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(calls[endpoint]["client_key"], "CK-1")
                self.assertEqual(calls[endpoint]["account_key"], "AK-1")

    def test_a_datetime_is_refused_rather_than_read_as_the_date_it_is_inside(self) -> None:
        """``datetime`` subclasses ``date``, so the annotation cannot stop this.

        Measured before the refusal: ``dt.datetime(2026, 8, 1, 12, 0,
        tzinfo=dt.UTC)`` sent ``from_date='2026-08-01T12:00:00+00:00'`` to the
        vendor AND published that same string as ``events_window.start``. The
        likely caller mistake is the sibling capability: ``list_fill_history``
        takes DATETIMES over the same two endpoints, so a sweep holding both
        has two argument kinds for one pair of reports.

        A refusal and not a normalisation to the date inside: the window
        published would read as the window asked for, and this repo's habit on
        a window edge is to refuse rather than clamp, because a clamp publishes
        a number that reads as the thing asked for."""
        noon = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC)
        for since, until in ((noon, _UNTIL), (_SINCE, noon)):
            with self.subTest(since=since, until=until):
                with self.assertRaises(ValueError) as caught:
                    self.broker.list_account_cash(since, until)
                self.assertIn("datetime", str(caught.exception))
                self.assertEqual(self.client.calls, [])

    def test_a_window_edge_that_is_not_a_date_at_all_is_refused_too(self) -> None:
        """The other arm of the same guard, measured on its own.

        A caller holding ISO strings (the shape the two report endpoints
        actually take) reaches ``since.isoformat()`` and raises
        ``AttributeError`` from inside the read instead of being told which
        argument was wrong."""
        for since, until in (("2026-08-01", _UNTIL), (_SINCE, "2026-10-03")):
            with self.subTest(since=since, until=until):
                with self.assertRaises(ValueError) as caught:
                    self.broker.list_account_cash(since, until)  # type: ignore[arg-type]
                self.assertIn("date", str(caught.exception))
                self.assertEqual(self.client.calls, [])

    def test_a_window_that_ends_before_it_starts_is_refused(self) -> None:
        """Swapped arguments, which read as a period that moved no money.

        Measured before the refusal: ``since=2026-10-03, until=2026-08-01``
        returned ``ReadWindow(start='2026-10-03', end='2026-08-01', rows=0)``
        with ``events=()``. Stored, that is a two-month period the account
        booked nothing in -- and the row-count claim this capability rests on
        says nothing is missing, because nothing was dropped. The only thing
        that could ever tell is the pair of dates, which is why the refusal is
        here and not in a later reader."""
        with self.assertRaises(ValueError) as caught:
            self.broker.list_account_cash(_UNTIL, _SINCE)
        self.assertIn("2026-08-01", str(caught.exception))
        self.assertIn("2026-10-03", str(caught.exception))
        self.assertEqual(self.client.calls, [])

    def test_a_single_day_window_is_read_and_not_refused(self) -> None:
        """``since == until`` is the shape a daily sweep asks for.

        The endpoints take an INCLUSIVE ``ToDate``, so one date on both edges
        is one day, not an empty window. Without this the ordering check could
        be ``<=`` and every daily sweep would refuse."""
        read = self.broker.list_account_cash(_UNTIL, _UNTIL)
        self.assertEqual(read.events_window, ReadWindow("2026-10-03", "2026-10-03", 104))

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

    def test_the_instrument_id_is_an_int_and_not_a_float_that_compares_equal(self) -> None:
        """``486.0 == 486`` is ``True``, and so is the dataclass comparison.

        So the field-by-field test above cannot tell ``_opt_int`` from
        ``_opt_float`` on this field. pyright does today -- the declared type
        is ``int | None`` and pyright over the tree is a blocking CI step --
        and it would stop the moment the annotation is loosened. The uic is a
        key: Phase 4 groups rows by instrument and Phase 6 joins them to a
        pick, and a float key misses a dict lookup that an equal int finds."""
        self.assertIsInstance(self._event("58330463544").uic, int)

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
        """SEVEN mapped bookings fields have a twin key the capture cannot tell apart.

        Re-measured on the fixture on 2026-10-19, because the count was
        stated as four and the scan that produced it covered only the fields
        whose twin is named ``Underlying*`` or ``Client*``:

        ======================================================  ==================
        pair                                                    identical rows
        ======================================================  ==================
        ``RelatedTradeId == RelatedPositionId``                 104 / 104
        ``Uic == UnderlyingInstrumentUic``                      104 / 104
        ``InstrumentSymbol == UnderlyingInstrumentSymbol``       104 / 104
        ``AccountCurrency == ClientCurrency``                   104 / 104
        ``AmountClass == CostClass``                             103 / 104
        ``ConversionRateAccountCurrency == …ClientCurrency``     103 / 104
        ``AmountClass == CostSubClass``                           44 / 104
        ======================================================  ==================

        So reading the right-hand key instead is invisible on the whole real
        capture for the first four, and only a row whose twins DISAGREE can
        tell the two apart. The last three disagree on at least one row, and
        the criterion that matters is still the same: all seven agree on
        ``58330463544``, the row the field-by-field test above asserts, so no
        swap among them shows up there.

        What the other tests see, measured by making each swap:

        * ``AmountClass -> CostClass`` is invisible to the ENTIRE brokers test
          package (3 652 tests green). ``CostBooking`` carries no
          ``amount_class``, so the parity test is blind to it by field set.
        * ``ConversionRateAccountCurrency -> …ClientCurrency`` is caught by the
          parity test, but only on row ``58422790702`` and only by
          ``-0.00999999999999978 != -0.00999999999999979`` -- a 1e-17 float
          difference on one of 104 rows. That is an accident of this capture,
          not a guard, and the next capture may not have it.
        * The four ``Underlying*`` / ``Client*`` pairs stay invisible to the
          parity test: ``CostBooking`` reads the same left-hand keys, so both
          mappings agree under the swap, and it carries neither ``uic`` nor
          ``instrument_symbol`` at all.

        Hence every twin goes in the synthetic row below, uniform or not."""
        self.client.bookings = [
            {
                **next(r for r in _VENUE["bookings"] if r["BkAmountId"] == "58330463544"),
                "RelatedPositionId": "999",
                "UnderlyingInstrumentUic": 999,
                "UnderlyingInstrumentSymbol": "WRONG:xnas",
                "ClientCurrency": "EUR",
                "CostClass": "WrongClass",
                "CostSubClass": "WrongSubClass",
                "ConversionRateClientCurrency": -999.99,
            }
        ]
        event = self._event("58330463544")
        self.assertEqual(event.related_trade_id, "6824247527")
        self.assertEqual(event.uic, 486)
        self.assertEqual(event.instrument_symbol, "F:xnys")
        self.assertEqual(event.account_currency, "PLN")
        self.assertEqual(event.amount_class, "NotAssigned")
        self.assertEqual(event.conversion_cost_account_currency, -0.130000000000003)


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

    def test_the_instrument_id_is_an_int_and_not_a_float_that_compares_equal(self) -> None:
        """The same swap on the trades side, invisible to the same comparison."""
        self.assertIsInstance(self._row("6824247527").uic, int)

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


#: The balance's four money fields, vendor key to mapped attribute. Every one
#: is non-zero in ``_BALANCE_BODY``, so a truthiness fallback (``or 0.0``) in
#: the mapping is invisible on that body: only a constructed null can tell
#: "the vendor did not say" from a real zero.
_BALANCE_MONEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("CashBalance", "cash_balance"),
    ("NonMarginPositionsValue", "non_margin_positions_value"),
    ("TotalValue", "total_value"),
    ("TransactionsNotBooked", "transactions_not_booked"),
)


class MoneyThatIsAbsentStaysDistinctFromZero(unittest.TestCase):
    """Every money field, on all three payload shapes.

    This is what discriminates ``fill_history``'s coercers from the ones
    ``saxo/broker.py`` defines under the same names. That module's
    ``_opt_float`` is ``None if value is None else float(value)``: it RAISES
    on a non-numeric string, so binding it puts a traceback on the money path.
    It also catches an ``or 0.0`` in any of the three mappings, which would
    turn a vendor null into a real ``0.0`` on a field Phase 4 will sum.

    The balance half matters for a reason the row halves do not share:
    ``TransactionsNotBooked`` is the field memo section 1.7 P5 singles out. A
    real ``0.0`` there means "nothing is in flight" and an absent value means
    "the vendor did not say", and Phase 4's balance cross-check is the single
    independent check on the money."""

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

    def _balance_with(self, key: str, value: Any) -> BalanceSnapshot:
        self.client.balance = {**_BALANCE_BODY, key: value}
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.balance is not None
        return read.balance

    def test_a_null_balance_money_field_maps_to_none(self) -> None:
        for key, attribute in _BALANCE_MONEY_FIELDS:
            with self.subTest(field=key):
                self.assertIsNone(getattr(self._balance_with(key, None), attribute))

    def test_a_zero_balance_money_field_stays_zero(self) -> None:
        for key, attribute in _BALANCE_MONEY_FIELDS:
            with self.subTest(field=key):
                self.assertEqual(getattr(self._balance_with(key, 0.0), attribute), 0.0)

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


class APayloadTheMappingCannotPlaceIsRefused(unittest.TestCase):
    """All of the account's money or none of it.

    Memo section 2 is the claim this holds up: a sum over all rows is the
    period's whole money movement "by construction: nothing can be missing
    without the row count saying so". A read that skips the row it cannot map
    and then publishes the count of the rows that survived breaks that claim
    twice over -- the movement is gone AND the count agrees with itself.
    Section 5(b) states the mechanism the memo wants instead: a dropped row
    does not vanish, it makes an identity fail.

    So the read refuses. The refusal is a ``BrokerError`` subclass, which is
    what makes it classify as ``broker_failed`` at the CLI boundary instead of
    arriving there as an unclassified traceback -- PR-A registers no failure
    code of its own, and the base class is the taxonomy's documented answer
    for exactly that.

    What this is NOT: a mapping that refuses a VALUE it cannot read. A
    non-numeric amount still maps to ``None`` and still does not raise (see
    the money tests). The refusal is about a payload that is not an object at
    all, which is the one shape no coercer can be asked about."""

    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]

    def test_a_booking_row_that_is_not_an_object_refuses_the_whole_read(self) -> None:
        """One unreadable row among 104 refuses, rather than publishing 103.

        ``get_bookings_report`` returns ``list(payload.get("Data") or [])``
        and type-checks nothing inside it, so a non-object row is a shape the
        vendor can hand over."""
        self.client.bookings = [*self.client.bookings[:3], "not an object"]
        with self.assertRaises(VendorPayloadShapeError) as caught:
            self.broker.list_account_cash(_SINCE, _UNTIL)
        self.assertIn("bookings", str(caught.exception))
        self.assertIn("3", str(caught.exception))

    def test_a_trade_row_that_is_not_an_object_refuses_the_whole_read(self) -> None:
        self.client.trades = [*self.client.trades[:2], None]
        with self.assertRaises(VendorPayloadShapeError) as caught:
            self.broker.list_account_cash(_SINCE, _UNTIL)
        self.assertIn("trades", str(caught.exception))

    def test_a_balance_body_that_is_not_an_object_refuses_the_whole_read(self) -> None:
        """A body of every field ``None`` is the same defect in the other half.

        Mapped from a payload that is not an object it reads as a balance the
        vendor answered with nothing, stamped with a fresh ``asof``. Phase 4's
        cross-check is the only independent check on the money, and it would
        run against that."""
        self.client.balance = []
        with self.assertRaises(VendorPayloadShapeError):
            self.broker.list_account_cash(_SINCE, _UNTIL)

    def test_the_refusal_classifies_as_a_broker_failure(self) -> None:
        self.client.bookings = ["not an object"]
        with self.assertRaises(BrokerError):
            self.broker.list_account_cash(_SINCE, _UNTIL)

    def test_the_published_count_is_the_rows_published(self) -> None:
        """The identity the refusal buys, on the real fixture.

        With no row droppable, the count of rows published and the count the
        vendor sent are the same number, and the memo's claim holds by
        construction rather than by a caller comparing two fields."""
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.events is not None
        assert read.trades is not None
        assert read.events_window is not None
        assert read.trades_window is not None
        self.assertEqual(read.events_window.rows, len(read.events))
        self.assertEqual(read.events_window.rows, len(self.client.bookings))
        self.assertEqual(read.trades_window.rows, len(read.trades))
        self.assertEqual(read.trades_window.rows, len(self.client.trades))


class TheRowsOwnIdentityIsRefusedRatherThanCoerced(unittest.TestCase):
    """The store key, and the two fields beside it that must not invent a value.

    Memo section 3 keys ``cash_events.jsonl`` on ``BkAmountId`` and folds the
    journal latest-line-wins. ``str(row.get("BkAmountId", ""))`` turns an
    absent key into ``''`` and a null into the string ``'None'``, so two rows
    the vendor sent without a usable id fold onto ONE key: one cash movement
    disappears while ``events_window.rows`` still equals the number published.
    That is the claim :class:`VendorPayloadShapeError` exists to hold up, and
    until now the refusal covered the payload SHAPE but not the one field the
    store is keyed by.

    The three fields are decided differently, on purpose:

    * ``BkAmountId`` is the store key, so an absent, null or empty value is
      refused and the whole window goes with it -- exactly as a row that is
      not an object does.
    * ``related_trade_id`` is not a key here, and must not invent a value
      either: absent maps to ``None``, while a ``"0"`` the vendor SENT (the
      two corporate-action rows) survives untouched. ``''`` was both at once.
    * ``uic`` absent is legitimate -- a cash deposit has no instrument -- and
      stays ``None``. A PRESENT value that cannot be read as an int is a
      present-but-unusable fact, and Phase 4 groups rows by instrument, so it
      is refused rather than nulled into the same bucket as "no instrument".
    """

    def setUp(self) -> None:
        self.client = _FakeClient()
        self.broker = SaxoBroker(self.client)  # type: ignore[arg-type]
        self.booking_base = next(r for r in _VENUE["bookings"] if r["BkAmountId"] == "58330463544")
        self.trade_base = next(r for r in _VENUE["trades"] if r["TradeId"] == "6824247527")

    def _event(self, **overrides: Any) -> CashEvent:
        self.client.bookings = [_vendor_row(self.booking_base, **overrides)]
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.events is not None
        return read.events[0]

    def _trade(self, **overrides: Any) -> TradeRow:
        self.client.trades = [_vendor_row(self.trade_base, **overrides)]
        read = self.broker.list_account_cash(_SINCE, _UNTIL)
        assert read.trades is not None
        return read.trades[0]

    def test_a_row_the_vendor_sent_without_a_usable_id_refuses_the_whole_read(self) -> None:
        """Absent, null and empty, each named in the refusal.

        All 104 captured rows carry a non-empty ``BkAmountId``, so none of
        these three shapes appears in the fixture: they are the shapes a
        coercion to ``str`` makes indistinguishable from each other, and the
        store cannot tell them apart afterwards."""
        for value in (_ABSENT, None, "", "   "):
            with self.subTest(bk_amount_id=value):
                with self.assertRaises(VendorPayloadShapeError) as caught:
                    self._event(BkAmountId=value)
                self.assertIn("BkAmountId", str(caught.exception))

    def test_two_id_less_rows_do_not_arrive_as_one_folded_record(self) -> None:
        """The collapse the refusal prevents, in the shape the store would see.

        Two rows, two different amounts, both with no id: coerced they share
        the key ``''``, the fold keeps the last of them, and
        ``events_window.rows`` still says 2."""
        self.client.bookings = [
            _vendor_row(self.booking_base, BkAmountId=_ABSENT, AmountAccountCurrency=-51.49),
            _vendor_row(self.booking_base, BkAmountId=_ABSENT, AmountAccountCurrency=-2000.0),
        ]
        with self.assertRaises(VendorPayloadShapeError):
            self.broker.list_account_cash(_SINCE, _UNTIL)

    def test_an_absent_related_trade_id_is_none_and_not_the_empty_string(self) -> None:
        """``''`` is a value the vendor never sends, and ``"0"`` is one it does.

        Mapping absent to ``''`` puts the two one string apart on a field
        Phase 4 reads to tell a trade booking from a corporate action."""
        self.assertIsNone(self._event(RelatedTradeId=_ABSENT).related_trade_id)
        self.assertIsNone(self._event(RelatedTradeId=None).related_trade_id)

    def test_a_zero_related_trade_id_survives_as_the_vendor_sent_it(self) -> None:
        """The two corporate-action rows carry ``"0"`` (see the CA row test).

        So ``"0"`` must not be folded into the absent case: it is a value, and
        this layer classifies nothing."""
        self.assertEqual(self._event(RelatedTradeId="0").related_trade_id, "0")

    def test_the_coercer_cannot_report_an_unreadable_instrument_id(self) -> None:
        """Why the uic needs a refusal of its own rather than the coercer's.

        ``_opt_int`` answers ``None`` for a present-but-unreadable value and
        ``None`` for an absent one, so the mapping cannot tell "no instrument"
        from "an instrument id this layer could not read" by its return value
        alone. Measured: the string form of a whole number is the first case
        and a float of the same number is not."""
        self.assertIsNone(_opt_int("486.0"))
        self.assertIsNone(_opt_int("oops"))
        self.assertIsNone(_opt_int(None))
        self.assertEqual(_opt_int(486.0), 486)

    def test_a_present_instrument_id_that_is_not_an_int_refuses_the_read(self) -> None:
        """On both row types: the capability reads ``Uic`` from both reports."""
        for value in ("486.0", "oops", True, [486]):
            with self.subTest(uic=value):
                with self.assertRaises(VendorPayloadShapeError) as caught:
                    self._event(Uic=value)
                self.assertIn("Uic", str(caught.exception))
                with self.assertRaises(VendorPayloadShapeError):
                    self._trade(Uic=value)

    def test_an_instrument_id_the_vendor_did_not_send_stays_none(self) -> None:
        """A cash deposit has no instrument, so absent is an answer, not a defect.

        Not observed on the capture -- all 104 bookings rows and all 41 trades
        rows carry an int ``Uic``, the deposits included -- so only a
        constructed row can tell this branch from the refusal beside it."""
        for value in (_ABSENT, None):
            with self.subTest(uic=value):
                self.assertIsNone(self._event(Uic=value).uic)
                self.assertIsNone(self._trade(Uic=value).uic)


class TheReadIsTypedAgainstTheRealClient(unittest.TestCase):
    """``client: Any`` forfeited the only coupling this module has.

    ``read_account_cash`` is a free function because ``saxo/broker.py`` sits
    on its own size baseline, and its docstring explains the shape by the
    cycle an import back into the adapter would make. But the three methods it
    calls belong to ``SaxoClient``, in ``saxo/client.py``, which imports only
    ``saxo.errors`` and ``saxo.tokens`` (verified) -- so naming that type
    creates no cycle, and ``Any`` only hid a rename of ``account_key``,
    ``from_date`` or ``to_date`` from pyright, which is a blocking CI step.

    The fake stays a fake and is NOT swapped for the real client: it has to
    answer a row that is not an object, a shape the real client's return type
    forbids. What the second test does instead is check the fake's recorded
    call against the real class, so the suite has power here too.
    """

    def test_the_client_parameter_names_the_saxo_client(self) -> None:
        hints = get_type_hints(read_account_cash)
        self.assertIs(hints["client"], SaxoClient)

    def test_every_keyword_the_read_passes_is_one_the_real_client_declares(self) -> None:
        """Derived from the call the read actually made, never from a literal.

        The fake declares ``**kwargs: Any``, so it accepts any spelling and
        the suite stays green under a rename on either side. These are the
        names the read passed on a real run, checked against the signature of
        the method it passed them to."""
        client = _FakeClient()
        SaxoBroker(client).list_account_cash(_SINCE, _UNTIL)  # type: ignore[arg-type]
        methods = {
            "bookings": SaxoClient.get_bookings_report,
            "trades": SaxoClient.get_trades_report,
            "balances": SaxoClient.get_balances,
        }
        self.assertEqual(sorted(dict(client.calls)), sorted(methods))
        for endpoint, kwargs in client.calls:
            declared = set(inspect.signature(methods[endpoint]).parameters)
            for keyword in kwargs:
                with self.subTest(endpoint=endpoint, keyword=keyword):
                    self.assertIn(keyword, declared)


class TheTwoMappingsOfTheSameRows(unittest.TestCase):
    """Drift containment between this capability and fill-history.

    STATED LIMIT, so it is not discovered later: this is drift containment,
    not mapping verification. It is blind to the four uniform key pairs on
    each side -- ``CostBooking`` reads the same left-hand keys, so both
    mappings still agree under a swap -- and ``CostBooking`` and ``Execution``
    carry neither ``uic`` nor ``instrument_symbol`` nor ``amount_class`` at
    all, so three of the seven booking swaps fall outside its field set
    entirely. On the ``ConversionRate`` twins it is red by accident rather
    than by design: see the table in the bookings twin-key test. The twin-key
    tests are the checks with power there."""

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

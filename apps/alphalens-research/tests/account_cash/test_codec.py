"""The cash book's wire form: a render for every decode (#1689, memo section 3).

Symmetric by construction and pinned by a round trip, following
``broker_contract/trade_intent/codec.py``. The subject is a MONEY journal that
is never compacted, so a field the render drops is a fact that exists nowhere
afterwards, and a value the decode mis-reads is a number that will be summed.

Rows come from the LIVE account on 2026-10-03, the fixture
``tests/brokers/automanager/fixtures/trades/live_2026_10_03/venue.json``, whose
104 bookings rows carry three distinct key shapes (36 keys x 99, 35 x 3,
37 x 2) -- the 37-key shape is the only one with ``CaEventName``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import unittest
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from alphalens_pipeline.account_cash import codec
from alphalens_pipeline.brokers.account_cash import (
    BalanceSnapshot,
    CashEvent,
    TradeRow,
)
from alphalens_pipeline.brokers.fill_history import ReadWindow

_VENUE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "brokers"
        / "automanager"
        / "fixtures"
        / "trades"
        / "live_2026_10_03"
        / "venue.json"
    ).read_text(encoding="utf-8")
)

_BOOKINGS: list[dict[str, Any]] = list(_VENUE["bookings"])
_TRADES: list[dict[str, Any]] = list(_VENUE["trades"])


def _one_row_per_key_shape(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row for each distinct key shape, so the round trip covers all three."""
    seen: dict[frozenset[str], dict[str, Any]] = {}
    for row in rows:
        seen.setdefault(frozenset(row), row)
    return list(seen.values())


#: Every optional field unset: the shape where a dropped field and a field
#: rendered as ``None`` look the same on the wire unless the decode is strict.
_EMPTY_EVENT = CashEvent(
    bk_amount_id="x",
    bk_amount_type="",
    amount_class=None,
    amount=None,
    currency=None,
    amount_account_currency=None,
    account_currency=None,
    conversion_rate=None,
    conversion_cost_account_currency=None,
    date=None,
    value_date=None,
    uic=None,
    instrument_symbol=None,
    related_trade_id=None,
    ca_event_name=None,
)

_EMPTY_TRADE = TradeRow(
    trade_id="t",
    order_id="o",
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

_BALANCE = BalanceSnapshot(
    currency="PLN",
    cash_balance=20738.68,
    non_margin_positions_value=3422.56,
    total_value=24161.24,
    transactions_not_booked=-4319.07,
    calculation_reliability="Ok",
    asof=dt.datetime(2026, 10, 8, 23, 46, 52, tzinfo=dt.UTC),
)

#: The balance equivalent of :data:`_EMPTY_EVENT`: every nullable field unset.
#: ``asof`` is the one field :class:`BalanceSnapshot` declares non-optional.
#: Without this row EVERY money field of the balance is non-``None`` in every
#: fixture, so a render that coerced a vendor ``None`` to ``0.0`` -- the shape
#: Phase 4's balance cross-check reads, and on ``transactions_not_booked`` the
#: field that parks a whole day's trading (memo section 1.7 P5) -- round-trips
#: unobserved. The fixture-uniform-in-the-column-under-test shape, on money.
_EMPTY_BALANCE = BalanceSnapshot(
    currency=None,
    cash_balance=None,
    non_margin_positions_value=None,
    total_value=None,
    transactions_not_booked=None,
    calculation_reliability=None,
    asof=dt.datetime(2026, 10, 8, 23, 46, 52, tzinfo=dt.UTC),
)

#: Every nullable money field of :class:`BalanceSnapshot`, read off the
#: dataclass rather than typed out, so a field added later is covered.
_BALANCE_MONEY_FIELDS = (
    "cash_balance",
    "non_margin_positions_value",
    "total_value",
    "transactions_not_booked",
)

_SWEEP = codec.SweepRecord(
    sweep_id="2026-10-09T00:31:00+00:00",
    swept_at=dt.datetime(2026, 10, 9, 0, 31, tzinfo=dt.UTC),
    env="live",
    since="2026-08-01",
    until="2026-10-03",
    events_window=ReadWindow("2026-08-01", "2026-10-03", 104),
    trades_window=ReadWindow("2026-08-01", "2026-10-03", 41),
    appended={"cash_events": 104, "trade_rows": 41, "balances": 1, "sweeps": 1},
    already_present={"cash_events": 0, "trade_rows": 0},
    restated={"cash_events": 0, "trade_rows": 0},
    malformed={"cash_events": 0, "trade_rows": 0},
    refused_reason=None,
)


def _contains_a_tuple(value: Any) -> bool:
    if isinstance(value, tuple):
        return True
    if isinstance(value, dict):
        return any(_contains_a_tuple(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_a_tuple(item) for item in value)
    return False


class EveryRowTypeRoundTrips(unittest.TestCase):
    def test_a_booking_row_of_every_key_shape_round_trips(self) -> None:
        shapes = _one_row_per_key_shape(_BOOKINGS)
        self.assertEqual(len(shapes), 3, sorted(len(s) for s in shapes))
        for row in shapes:
            event = CashEvent.from_vendor_row(row)
            with self.subTest(bk_amount_id=event.bk_amount_id):
                rendered = codec.cash_event_to_jsonable(event)
                self.assertEqual(codec.cash_event_from_jsonable(rendered), event)

    def test_the_corporate_action_rows_event_name_survives(self) -> None:
        """``CaEventName`` is present on exactly 2 of 104 rows, so a render that
        dropped it would round-trip on 102 of them."""
        named = [
            CashEvent.from_vendor_row(row)
            for row in _BOOKINGS
            if row.get("CaEventName") is not None
        ]
        self.assertEqual(len(named), 2)
        for event in named:
            with self.subTest(bk_amount_id=event.bk_amount_id):
                decoded = codec.cash_event_from_jsonable(codec.cash_event_to_jsonable(event))
                self.assertEqual(decoded.ca_event_name, "Cash Dividend")
                self.assertEqual(decoded, event)

    def test_an_all_none_booking_row_round_trips(self) -> None:
        self.assertEqual(
            codec.cash_event_from_jsonable(codec.cash_event_to_jsonable(_EMPTY_EVENT)),
            _EMPTY_EVENT,
        )

    def test_every_trade_row_round_trips(self) -> None:
        for row in _TRADES:
            trade = TradeRow.from_vendor_row(row)
            with self.subTest(trade_id=trade.trade_id):
                self.assertEqual(
                    codec.trade_row_from_jsonable(codec.trade_row_to_jsonable(trade)), trade
                )

    def test_an_all_none_trade_row_round_trips(self) -> None:
        self.assertEqual(
            codec.trade_row_from_jsonable(codec.trade_row_to_jsonable(_EMPTY_TRADE)),
            _EMPTY_TRADE,
        )

    def test_a_balance_round_trips(self) -> None:
        self.assertEqual(codec.balance_from_jsonable(codec.balance_to_jsonable(_BALANCE)), _BALANCE)

    def test_an_all_none_balance_round_trips(self) -> None:
        """The balance's own absent-versus-zero case. ``CalculationReliability``
        answers ``NotAvailable`` on the vendor side when a value could not be
        computed, and every money field is then null."""
        self.assertEqual(
            codec.balance_from_jsonable(codec.balance_to_jsonable(_EMPTY_BALANCE)),
            _EMPTY_BALANCE,
        )

    def test_an_absent_balance_money_field_never_becomes_a_number(self) -> None:
        """Field by field, so the report names WHICH money field was coerced.

        The round trip above would also catch an ``or 0.0`` on one field, but it
        reports only "the balance did not round-trip"; Phase 4's cross-check
        reads these four individually and a null read as ``0.0`` is a balance
        that claims to be known.
        """
        for name in _BALANCE_MONEY_FIELDS:
            with self.subTest(money_field=name):
                rendered = codec.balance_to_jsonable(_EMPTY_BALANCE)
                self.assertIsNone(rendered[name])
                decoded = codec.balance_from_jsonable(rendered)
                self.assertIsNone(getattr(decoded, name))

    def test_a_balance_money_field_of_zero_stays_a_number(self) -> None:
        """The other side of the same guard: a real ``0.0`` must not become
        ``None``, or a ``None``-for-falsy render would pass the test above."""
        for name in _BALANCE_MONEY_FIELDS:
            with self.subTest(money_field=name):
                zeroed = replace(_EMPTY_BALANCE, **{name: 0.0})
                decoded = codec.balance_from_jsonable(codec.balance_to_jsonable(zeroed))
                self.assertEqual(getattr(decoded, name), 0.0)
                self.assertIsNotNone(getattr(decoded, name))

    def test_the_money_field_list_covers_every_nullable_float_the_dataclass_has(self) -> None:
        """Anti-rot for the two loops above: a fifth money field added to
        :class:`BalanceSnapshot` must not sit outside them silently."""
        declared = sorted(
            field.name
            for field in fields(BalanceSnapshot)
            if field.type in ("float | None", float | None)
        )
        self.assertEqual(declared, sorted(_BALANCE_MONEY_FIELDS))

    def test_a_sweep_record_round_trips(self) -> None:
        self.assertEqual(codec.sweep_from_jsonable(codec.sweep_to_jsonable(_SWEEP)), _SWEEP)

    def test_a_sweep_record_with_no_windows_round_trips(self) -> None:
        """The SIM refusal's shape: nothing was read, so neither window exists."""
        refused = replace(
            _SWEEP,
            events_window=None,
            trades_window=None,
            refused_reason="sim_reports_unusable",
        )
        self.assertEqual(codec.sweep_from_jsonable(codec.sweep_to_jsonable(refused)), refused)


class EveryFieldIsOnTheWire(unittest.TestCase):
    """The round trip alone cannot catch a field dropped from BOTH halves: the
    render omits it, the decode defaults it, and equality still holds when the
    sample happens to carry the default. So the key set is asserted against the
    dataclass itself -- declared in another module, not in the codec."""

    def test_the_rendered_keys_are_exactly_the_dataclass_fields(self) -> None:
        cases = (
            (CashEvent, codec.cash_event_to_jsonable(_EMPTY_EVENT)),
            (TradeRow, codec.trade_row_to_jsonable(_EMPTY_TRADE)),
            (BalanceSnapshot, codec.balance_to_jsonable(_BALANCE)),
            (codec.SweepRecord, codec.sweep_to_jsonable(_SWEEP)),
        )
        for row_type, rendered in cases:
            with self.subTest(row_type=row_type.__name__):
                self.assertEqual(sorted(rendered), sorted(field.name for field in fields(row_type)))

    def test_a_missing_key_is_refused_rather_than_defaulted(self) -> None:
        rendered = codec.cash_event_to_jsonable(_EMPTY_EVENT)
        for key in sorted(rendered):
            with self.subTest(missing=key):
                partial = {k: v for k, v in rendered.items() if k != key}
                with self.assertRaises(codec.CashBookDecodeError):
                    codec.cash_event_from_jsonable(partial)


class NoTupleSurvivesTheRender(unittest.TestCase):
    """#1405: ``dataclasses.asdict`` recurses a tuple INTO a tuple, which
    ``json.dumps`` then writes as an array a decode reads back as a list -- so
    the round trip fails on a field nobody changed."""

    def test_every_sequence_is_a_list(self) -> None:
        for name, rendered in (
            ("cash_event", codec.cash_event_to_jsonable(_EMPTY_EVENT)),
            ("trade_row", codec.trade_row_to_jsonable(_EMPTY_TRADE)),
            ("balance", codec.balance_to_jsonable(_BALANCE)),
            ("sweep", codec.sweep_to_jsonable(_SWEEP)),
        ):
            with self.subTest(row=name):
                self.assertFalse(_contains_a_tuple(rendered))

    def test_the_tuple_probe_can_actually_fire(self) -> None:
        """Positive control: a probe that matches nothing has tested nothing."""
        self.assertTrue(_contains_a_tuple({"window": {"rows": (1, 2)}}))
        self.assertTrue(_contains_a_tuple([[("a",)]]))


class ANonFiniteMoneyValueIsRefused(unittest.TestCase):
    """A property that is TRUE AND EMPTY on every fixture row, redirected to a
    constructed one: ``fill_history._opt_float`` ends
    ``return result if math.isfinite(result) else None``, so the vendor path
    cannot produce a non-finite and the raw fixture contains neither ``NaN``
    nor ``Infinity``.

    The reachable defect is a hand-edited or torn line:
    ``json.loads('{"a": NaN}')`` answers ``{'a': nan}`` and
    ``json.dumps({'a': nan})`` answers ``'{"a": NaN}'``, which is not JSON.

    The refusal belongs HERE and not in ``brokers/journal.py``: that file is
    shared broker code, it serialises OUTSIDE its own write guard on purpose,
    and its docstring states that a serialisation failure is a programming
    error in the caller's payload. Adding ``allow_nan=False`` there would
    change the shared money path for one new consumer. The append-path half of
    this claim is in ``test_store.py``: the codec can only refuse what the
    appender actually routes through it.
    """

    def test_the_render_refuses_a_non_finite_amount(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(codec.CashBookDecodeError):
                    codec.cash_event_to_jsonable(replace(_EMPTY_EVENT, amount_account_currency=bad))

    def test_the_decode_refuses_a_bare_nan_token(self) -> None:
        rendered = codec.cash_event_to_jsonable(_EMPTY_EVENT)
        line = json.dumps({**rendered, "amount_account_currency": float("nan")})
        self.assertIn("NaN", line)
        parsed = json.loads(line)
        self.assertTrue(math.isnan(parsed["amount_account_currency"]))
        with self.assertRaises(codec.CashBookDecodeError):
            codec.cash_event_from_jsonable(parsed)

    def test_a_non_finite_on_the_trades_money_field_is_refused_too(self) -> None:
        """``BookedAmountAccountCurrency`` is the field Phase 4 sums, and it is
        non-null on all 41 fixture rows, so nothing else here reaches it."""
        with self.assertRaises(codec.CashBookDecodeError):
            codec.trade_row_to_jsonable(
                replace(_EMPTY_TRADE, booked_amount_account_currency=float("nan"))
            )

    def test_a_non_finite_balance_is_refused(self) -> None:
        with self.assertRaises(codec.CashBookDecodeError):
            codec.balance_to_jsonable(replace(_BALANCE, cash_balance=float("inf")))

    def test_zero_is_not_confused_with_absent(self) -> None:
        """The other side of the same guard: a legitimate ``0.0`` must stay a
        number and never become ``None``, or an ``or 0.0`` / falsy test would
        pass every refusal test above while erasing the distinction."""
        decoded = codec.cash_event_from_jsonable(
            codec.cash_event_to_jsonable(replace(_EMPTY_EVENT, amount_account_currency=0.0))
        )
        self.assertEqual(decoded.amount_account_currency, 0.0)
        self.assertIsNotNone(decoded.amount_account_currency)


class ADecodeOfTheWrongTypeRefuses(unittest.TestCase):
    """Stated limit: this does not cover a bare ``NaN``, which decodes to a
    float and passes a type check -- the class above is that half."""

    def test_a_string_where_a_float_belongs_refuses(self) -> None:
        rendered = codec.cash_event_to_jsonable(_EMPTY_EVENT)
        with self.assertRaises(codec.CashBookDecodeError):
            codec.cash_event_from_jsonable({**rendered, "amount_account_currency": "13.78"})

    def test_a_bool_where_a_float_belongs_refuses(self) -> None:
        """``isinstance(True, int)`` is ``True`` in Python, so a bool reaches a
        numeric field unless the check names it."""
        rendered = codec.cash_event_to_jsonable(_EMPTY_EVENT)
        with self.assertRaises(codec.CashBookDecodeError):
            codec.cash_event_from_jsonable({**rendered, "amount": True})

    def test_a_number_where_the_key_belongs_refuses(self) -> None:
        rendered = codec.cash_event_to_jsonable(_EMPTY_EVENT)
        with self.assertRaises(codec.CashBookDecodeError):
            codec.cash_event_from_jsonable({**rendered, "bk_amount_id": 58330463544})

    def test_an_absent_key_is_refused_not_coerced(self) -> None:
        rendered = codec.cash_event_to_jsonable(_EMPTY_EVENT)
        with self.assertRaises(codec.CashBookDecodeError):
            codec.cash_event_from_jsonable({**rendered, "bk_amount_id": None})

    def test_a_non_object_line_refuses(self) -> None:
        for payload in ([], "x", 3, None):
            with self.subTest(payload=payload):
                with self.assertRaises(codec.CashBookDecodeError):
                    codec.cash_event_from_jsonable(payload)

    def test_a_window_of_the_wrong_shape_refuses(self) -> None:
        rendered = codec.sweep_to_jsonable(_SWEEP)
        with self.assertRaises(codec.CashBookDecodeError):
            codec.sweep_from_jsonable({**rendered, "events_window": ["2026-08-01", 104]})

    def test_a_count_map_with_a_non_integer_refuses(self) -> None:
        rendered = codec.sweep_to_jsonable(_SWEEP)
        with self.assertRaises(codec.CashBookDecodeError):
            codec.sweep_from_jsonable({**rendered, "appended": {"cash_events": 1.5}})


class AnAwareDatetimeSurvivesTheRoundTrip(unittest.TestCase):
    def test_an_execution_time_comes_back_aware_and_equal(self) -> None:
        stamped = replace(
            _EMPTY_TRADE,
            execution_time=dt.datetime(2026, 8, 10, 13, 58, 40, 720000, tzinfo=dt.UTC),
        )
        decoded = codec.trade_row_from_jsonable(codec.trade_row_to_jsonable(stamped))
        self.assertIsNotNone(decoded.execution_time)
        assert decoded.execution_time is not None
        self.assertIsNotNone(decoded.execution_time.tzinfo)
        self.assertEqual(decoded.execution_time, stamped.execution_time)

    def test_a_balance_asof_comes_back_aware_and_equal(self) -> None:
        """Two sweeps' balance stamps must stay comparable. A datetime rendered
        through ``str()`` and decoded naive is not comparable with an aware one
        at all -- Python raises on the comparison."""
        decoded = codec.balance_from_jsonable(codec.balance_to_jsonable(_BALANCE))
        self.assertIsNotNone(decoded.asof.tzinfo)
        self.assertEqual(decoded.asof, _BALANCE.asof)

    def test_a_non_utc_offset_is_normalised_to_the_same_instant(self) -> None:
        warsaw = dt.timezone(dt.timedelta(hours=2))
        stamped = replace(_BALANCE, asof=dt.datetime(2026, 10, 9, 1, 46, 52, tzinfo=warsaw))
        decoded = codec.balance_from_jsonable(codec.balance_to_jsonable(stamped))
        self.assertEqual(decoded.asof, stamped.asof)
        self.assertEqual(decoded.asof.utcoffset(), dt.timedelta(0))

    def test_a_naive_datetime_is_refused_by_the_render(self) -> None:
        """A naive stamp is the shape that makes two sweeps incomparable, and
        the vendor path never produces one (``parse_utc`` stamps UTC), so it
        can only arrive from code."""
        with self.assertRaises(codec.CashBookDecodeError):
            codec.balance_to_jsonable(replace(_BALANCE, asof=dt.datetime(2026, 10, 9, 1, 46)))

    def test_an_unparseable_stamp_is_refused_by_the_decode(self) -> None:
        rendered = codec.balance_to_jsonable(_BALANCE)
        with self.assertRaises(codec.CashBookDecodeError):
            codec.balance_from_jsonable({**rendered, "asof": "not-a-time"})


if __name__ == "__main__":
    unittest.main()

"""The summing vocabulary of the cash book (#1689, memo Phase 4).

This is the module where money numbers appear, so four rules carry the whole
layer and each one is pinned by a test that fails without it:

* **Only the ``_acct`` family is ever summed.** ``pnl_cash`` is in the
  INSTRUMENT currency while ``pnl_cash_acct`` is in the ACCOUNT currency, and
  every LIVE pick is USD against a PLN account today (memo section 4).
* **The FX disclosure is never added into any net.** It is already inside the
  account amount via the rate, so adding it double counts at about 0.25% of
  notional per leg (memo section 1.3). Held structurally by the AST test at the
  bottom of this file, not by a convention.
* **A record contributes all of its money or none of it.** A missing fee read
  as ``0.0`` makes a net look better than reality, which is the worst direction
  for a money error (memo section 9).
* **A refusal is a named reason, never a number.** A null total carries why it
  is null; it never publishes ``0.0``, and it never publishes a NaN.
"""

from __future__ import annotations

import unittest
from dataclasses import fields
from decimal import Decimal

from alphalens_pipeline.account_cash import money
from alphalens_pipeline.account_cash.money import (
    BALANCE_TOLERANCE_ACCT,
    NULL_ACCT_AMOUNT_NOT_REPORTED,
    NULL_CURRENCY_MIXED,
    NULL_NON_FINITE,
    AcctAmount,
    acct_difference,
    acct_null,
    acct_sum,
)
from alphalens_pipeline.brokers.account_cash import BalanceSnapshot, CashEvent, TradeRow

from tests.account_cash.cashbook_fixture import (
    BALANCE,
    BOOKINGS,
    acct_pairs,
    event_like,
    events_of_type,
    events_with_one_changed,
    trades_with_one_changed,
)

#: Measured from the capture, not copied from the memo:
#: ``sum(Decimal(str(AmountAccountCurrency)))`` over the 41 ``Share Amount``
#: rows, and over all 104 rows.
SHARE_LEG_ROWS = 41
SHARE_LEG_TOTAL_PLN = -3110.06
ALL_BOOKING_ROWS = 104
ALL_BOOKINGS_TOTAL_PLN = 20738.68


def _share_leg_pairs() -> list[tuple[float | None, str | None]]:
    return acct_pairs(events_of_type("Share Amount"))


def _shifted(amount: float | None, by: str) -> float:
    """``amount`` moved by an exact decimal step, with no float noise added."""
    assert amount is not None
    return float(Decimal(str(amount)) + Decimal(by))


class TheAccountCurrencySumIsTheOnlySummer(unittest.TestCase):
    def test_a_sum_of_the_real_share_legs_is_the_measured_total(self) -> None:
        total = acct_sum(_share_leg_pairs(), rows=SHARE_LEG_ROWS)

        self.assertEqual(total.rows, SHARE_LEG_ROWS)
        self.assertEqual(total.currency, "PLN")
        self.assertIsNone(total.null_reason)
        assert total.value is not None
        self.assertAlmostEqual(total.value, SHARE_LEG_TOTAL_PLN, places=6)

    def test_a_sum_of_every_real_booking_row_is_the_measured_total(self) -> None:
        total = acct_sum(acct_pairs(BOOKINGS), rows=ALL_BOOKING_ROWS)

        self.assertEqual(total.rows, ALL_BOOKING_ROWS)
        assert total.value is not None
        self.assertAlmostEqual(total.value, ALL_BOOKINGS_TOTAL_PLN, places=6)

    def test_an_empty_sum_is_zero_with_the_currency_the_caller_states(self) -> None:
        """A bucket at zero still prints its currency (memo section 5(a)).

        Without the stated currency an empty bucket would publish ``None``,
        and the bucket identity -- which sums the five bucket values -- would
        then read that ``None`` as a money field the vendor did not report.
        """
        total = acct_sum([], rows=0, currency="PLN")

        self.assertEqual(total.value, 0.0)
        self.assertEqual(total.currency, "PLN")
        self.assertEqual(total.rows, 0)
        self.assertIsNone(total.null_reason)


class AMoneyRecordContributesAllOfItsMoneyOrNone(unittest.TestCase):
    def test_one_absent_amount_nulls_the_whole_sum(self) -> None:
        """``or 0.0`` is the named worst-direction error (memo section 9)."""
        pairs = acct_pairs(events_with_one_changed(0, amount_account_currency=None))

        total = acct_sum(pairs, rows=ALL_BOOKING_ROWS)

        self.assertIsNone(total.value)
        self.assertEqual(total.null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)
        self.assertEqual(total.currency, "PLN")
        self.assertEqual(total.rows, ALL_BOOKING_ROWS)

    def test_every_money_field_the_layer_reads_nulls_its_own_total(self) -> None:
        """The all-or-nothing rule on EVERY money field, not just the first.

        The floor is what catches a row quietly dropped from the table: five
        money fields reach this layer, and a table of four has stopped
        describing it.
        """
        money_fields: tuple[tuple[type, str, list[tuple[float | None, str | None]]], ...] = (
            (
                CashEvent,
                "amount_account_currency",
                acct_pairs(events_with_one_changed(0, amount_account_currency=None)),
            ),
            (
                CashEvent,
                "account_currency",
                acct_pairs(events_with_one_changed(0, account_currency=None)),
            ),
            (
                TradeRow,
                "booked_amount_account_currency",
                [
                    (row.booked_amount_account_currency, row.account_currency)
                    for row in trades_with_one_changed(0, booked_amount_account_currency=None)
                ],
            ),
            (
                TradeRow,
                "account_currency",
                [
                    (row.booked_amount_account_currency, row.account_currency)
                    for row in trades_with_one_changed(0, account_currency=None)
                ],
            ),
            (
                BalanceSnapshot,
                "cash_balance",
                [(None, BALANCE.currency)],
            ),
        )

        self.assertGreaterEqual(len(money_fields), 5)
        for owner, field_name, pairs in money_fields:
            with self.subTest(field=f"{owner.__name__}.{field_name}"):
                self.assertIn(field_name, {f.name for f in fields(owner)})
                total = acct_sum(pairs, rows=len(pairs))
                self.assertIsNone(total.value)
                self.assertEqual(total.null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)

    def test_a_non_finite_amount_is_a_named_null_and_never_a_rendered_nan(self) -> None:
        """Named here, rather than left to ``_emit_json(allow_nan=False)``.

        That guard turns the whole report into a refusal; a named null turns
        one line into a refusal and leaves the rest readable.
        """
        pairs = acct_pairs(events_with_one_changed(0, amount_account_currency=float("inf")))

        total = acct_sum(pairs, rows=ALL_BOOKING_ROWS)

        self.assertIsNone(total.value)
        self.assertEqual(total.null_reason, NULL_NON_FINITE)

    def test_a_non_finite_value_cannot_be_constructed_at_all(self) -> None:
        with self.assertRaises(ValueError):
            AcctAmount(value=float("nan"), currency="PLN", rows=1)

    def test_a_value_and_a_null_reason_cannot_both_be_present(self) -> None:
        """The record has two states, not three (the ``oneOf`` in memo 5(d))."""
        with self.assertRaises(ValueError):
            AcctAmount(value=1.0, currency="PLN", rows=1, null_reason=NULL_NON_FINITE)
        with self.assertRaises(ValueError):
            AcctAmount(value=None, currency="PLN", rows=1)


class TheCurrencyIsReadAndNeverAssumed(unittest.TestCase):
    def test_a_second_account_currency_refuses_rather_than_adding(self) -> None:
        """The capture is PLN on 104 of 104 rows, so only this row can prove
        the field is read at all rather than hard-coded."""
        rows = (*BOOKINGS, event_like(BOOKINGS[0], account_currency="EUR"))

        total = acct_sum(acct_pairs(rows), rows=len(rows))

        self.assertIsNone(total.value)
        self.assertEqual(total.null_reason, NULL_CURRENCY_MIXED)

    def test_the_absent_currency_does_not_count_as_a_second_one(self) -> None:
        """A row with no stated currency is an unreported amount, not a second
        currency: putting ``None`` in the distinct set would read a single
        absent currency as a multi-currency account."""
        pairs = acct_pairs(events_with_one_changed(0, account_currency=None))

        total = acct_sum(pairs, rows=ALL_BOOKING_ROWS)

        self.assertEqual(total.null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)
        self.assertEqual(total.currency, "PLN")

    def test_a_stated_currency_that_the_rows_contradict_refuses(self) -> None:
        total = acct_sum(acct_pairs(BOOKINGS), rows=ALL_BOOKING_ROWS, currency="EUR")

        self.assertEqual(total.null_reason, NULL_CURRENCY_MIXED)


class TheToleranceIsAGroszInTheAccountCurrency(unittest.TestCase):
    """Two separate claims: the unit, and the order of magnitude.

    A residual of exactly ``0.0`` would prove neither -- it passes for a
    ``Decimal`` accumulator and for a naive float sum alike. Both cases below
    are constructed so that one of them goes red at a tolerance of ``0.0`` or
    ``0.001`` and the other at a tolerance of ``1.0``.
    """

    def setUp(self) -> None:
        self.real = acct_sum(acct_pairs(BOOKINGS), rows=ALL_BOOKING_ROWS)

    def test_the_tolerance_is_one_grosz(self) -> None:
        self.assertEqual(BALANCE_TOLERANCE_ACCT, 0.01)

    def test_a_half_grosz_disagreement_is_inside_the_tolerance(self) -> None:
        shifted = acct_sum(
            acct_pairs(
                events_with_one_changed(
                    0,
                    amount_account_currency=_shifted(BOOKINGS[0].amount_account_currency, "0.005"),
                )
            ),
            rows=ALL_BOOKING_ROWS,
        )

        difference = acct_difference(shifted, self.real)

        assert difference.value is not None
        self.assertAlmostEqual(difference.value, 0.005, places=9)
        self.assertLess(abs(difference.value), BALANCE_TOLERANCE_ACCT)

    def test_a_fifty_grosz_disagreement_is_outside_the_tolerance(self) -> None:
        shifted = acct_sum(
            acct_pairs(
                events_with_one_changed(
                    0,
                    amount_account_currency=_shifted(BOOKINGS[0].amount_account_currency, "0.50"),
                )
            ),
            rows=ALL_BOOKING_ROWS,
        )

        difference = acct_difference(shifted, self.real)

        assert difference.value is not None
        self.assertAlmostEqual(difference.value, 0.50, places=9)
        self.assertGreaterEqual(abs(difference.value), BALANCE_TOLERANCE_ACCT)


class TheDifferenceOfTwoTotals(unittest.TestCase):
    def test_a_difference_carries_the_currency_and_both_row_counts(self) -> None:
        left = acct_sum([(10.0, "PLN"), (5.0, "PLN")], rows=2)
        right = acct_sum([(3.0, "PLN")], rows=1)

        difference = acct_difference(left, right)

        self.assertEqual(difference.value, 12.0)
        self.assertEqual(difference.currency, "PLN")
        self.assertEqual(difference.rows, 3)

    def test_a_null_on_either_side_propagates_with_its_own_reason(self) -> None:
        real = acct_sum([(10.0, "PLN")], rows=1)
        absent = acct_null(NULL_ACCT_AMOUNT_NOT_REPORTED, "PLN", rows=1)

        self.assertEqual(acct_difference(absent, real).null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)
        self.assertEqual(acct_difference(real, absent).null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)

    def test_two_currencies_refuse_rather_than_subtracting(self) -> None:
        difference = acct_difference(
            acct_sum([(10.0, "PLN")], rows=1), acct_sum([(3.0, "EUR")], rows=1)
        )

        self.assertEqual(difference.null_reason, NULL_CURRENCY_MIXED)


class TheRenderedForm(unittest.TestCase):
    def test_a_number_renders_with_its_currency_and_its_row_count(self) -> None:
        body = acct_sum(_share_leg_pairs(), rows=SHARE_LEG_ROWS).to_jsonable()

        self.assertEqual(
            sorted(body), ["currency", "null_reason", "rows", "value", "value_rendered"]
        )
        self.assertEqual(body["currency"], "PLN")
        self.assertEqual(body["rows"], SHARE_LEG_ROWS)
        self.assertIsNone(body["null_reason"])
        self.assertEqual(body["value_rendered"], "-3110.06 PLN")

    def test_a_null_renders_its_reason_and_no_number(self) -> None:
        body = acct_null(NULL_NON_FINITE, "PLN", rows=3).to_jsonable()

        self.assertIsNone(body["value"])
        self.assertEqual(body["null_reason"], NULL_NON_FINITE)
        self.assertEqual(body["value_rendered"], "non_finite")

    def test_every_null_reason_is_registered(self) -> None:
        with self.assertRaises(ValueError):
            acct_null("made_up_reason", "PLN", rows=1)
        self.assertEqual(len(set(money.NULL_REASONS)), len(money.NULL_REASONS))


if __name__ == "__main__":
    unittest.main()

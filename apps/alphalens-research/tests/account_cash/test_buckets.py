"""Every booking row lands in exactly one bucket (#1689, memo section 5(a)).

The bucket identity -- ``sum(buckets) == sum(all rows given)`` -- is the first
of the memo's three mechanisms by which an incomplete number announces itself.
A ``BkAmountType`` nobody has seen cannot shrink the total; it can only move
money INTO ``unclassified``, which is printed even at zero, so a non-zero one
reads as a changed number rather than as a new row.

Every figure below was re-derived from the capture
(``fixtures/trades/live_2026_10_03/venue.json``) with ``Decimal(str(...))``
over ``AmountAccountCurrency``, grouped by ``BkAmountType``, not copied from
the memo:

====================================  ====  ===========
``BkAmountType``                      rows   sum (PLN)
====================================  ====  ===========
``Share Amount``                        41     -3110.06
``Commission``                          41      -154.41
``Exchange Fee``                        17        -0.90
``Cash Amount``                          3    +24000.00
``Corporate Actions - Cash Dividends``   1        +4.74
``Corporate Actions - Withholding Tax``  1        -0.69
all rows                               104    +20738.68
====================================  ====  ===========
"""

from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.account_cash import buckets
from alphalens_pipeline.account_cash.buckets import (
    BUCKET_DEPOSITS,
    BUCKET_FEES,
    BUCKET_INCOME,
    BUCKET_ORDER,
    BUCKET_SHARE_LEGS,
    BUCKET_UNCLASSIFIED,
    bucket_of,
    classify_buckets,
)
from alphalens_pipeline.account_cash.money import (
    NULL_CURRENCY_NOT_STATED,
    acct_difference,
    acct_sum,
)

from tests.account_cash.cashbook_fixture import BOOKINGS, event_like, events_of_type

ALL_BOOKING_ROWS = 104
ALL_BOOKINGS_TOTAL_PLN = 20738.68

#: bucket -> (sum in PLN, rows). Hand-written from the table in the docstring;
#: none of the six figures is computed by the code under test.
MEASURED: dict[str, tuple[float, int]] = {
    BUCKET_SHARE_LEGS: (-3110.06, 41),
    BUCKET_FEES: (-155.31, 58),
    BUCKET_INCOME: (4.05, 2),
    BUCKET_DEPOSITS: (24000.00, 3),
    BUCKET_UNCLASSIFIED: (0.00, 0),
}


def _by_name(fold: buckets.BucketFold) -> dict[str, buckets.Bucket]:
    return {bucket.name: bucket for bucket in fold.buckets}


class TheMeasuredTypesLandInTheirBuckets(unittest.TestCase):
    def setUp(self) -> None:
        self.fold = classify_buckets(BOOKINGS)
        self.named = _by_name(self.fold)

    def test_the_six_measured_types_land_in_their_buckets_with_the_measured_sums(self) -> None:
        for name, (total, rows) in MEASURED.items():
            with self.subTest(bucket=name):
                bucket = self.named[name]
                self.assertEqual(bucket.rows, rows)
                self.assertEqual(bucket.total.rows, rows)
                self.assertEqual(bucket.total.currency, "PLN")
                assert bucket.total.value is not None
                self.assertAlmostEqual(bucket.total.value, total, places=6)

    def test_the_fold_states_the_account_currency_and_the_row_count(self) -> None:
        self.assertEqual(self.fold.rows, ALL_BOOKING_ROWS)
        self.assertEqual(self.fold.all_rows.rows, ALL_BOOKING_ROWS)
        assert self.fold.all_rows.value is not None
        self.assertAlmostEqual(self.fold.all_rows.value, ALL_BOOKINGS_TOTAL_PLN, places=6)

    def test_every_bucket_names_the_types_it_actually_holds(self) -> None:
        self.assertEqual(
            self.named[BUCKET_FEES].bk_amount_types, (buckets.COMMISSION, buckets.EXCHANGE_FEE)
        )
        self.assertEqual(
            self.named[BUCKET_INCOME].bk_amount_types,
            (buckets.CA_CASH_DIVIDENDS, buckets.CA_WITHHOLDING_TAX),
        )
        self.assertEqual(self.named[BUCKET_UNCLASSIFIED].bk_amount_types, ())


class TheClassifierReadsTheBookingTypeAndNothingElse(unittest.TestCase):
    def setUp(self) -> None:
        self.named = _by_name(classify_buckets(BOOKINGS))

    def test_the_deposits_do_not_follow_their_non_zero_related_trade_id(self) -> None:
        """Memo section 1.1's measured trap.

        The three ``Cash Amount`` rows carry non-zero ``RelatedTradeId`` values
        (6820472599 / 6833231458 / 6849524900) that are not trade ids. A
        classifier keying on "non-zero means a trade" folds +24 000.00 PLN of
        funding into trading, reporting +20 734.63 where the account's own
        trading figure is -3 265.37: a sign flip on the headline number.
        """
        deposits = events_of_type(buckets.CASH_AMOUNT)
        self.assertEqual(len(deposits), 3)
        self.assertEqual(
            sorted(event.related_trade_id or "" for event in deposits),
            ["6820472599", "6833231458", "6849524900"],
        )
        self.assertTrue(all(event.related_trade_id not in (None, "0") for event in deposits))

        self.assertEqual(self.named[BUCKET_DEPOSITS].rows, 3)
        assert self.named[BUCKET_DEPOSITS].total.value is not None
        self.assertAlmostEqual(self.named[BUCKET_DEPOSITS].total.value, 24000.00, places=6)
        assert self.named[BUCKET_SHARE_LEGS].total.value is not None
        self.assertAlmostEqual(self.named[BUCKET_SHARE_LEGS].total.value, -3110.06, places=6)

    def test_amount_class_is_not_the_classifier(self) -> None:
        """The real rows discriminate the two classifiers on their own.

        ``AmountClass`` measures ``TransactionCosts`` 59 -- it folds the
        withholding tax in with the commissions -- ``NotAssigned`` 44, which
        folds the three deposits in with the 41 share legs, and
        ``OngoingPayment`` 1 for the dividend alone. So an ``AmountClass``
        classifier answers 59 fee rows and 1 income row where the booking type
        answers 58 and 2.
        """
        self.assertEqual(self.named[BUCKET_FEES].rows, 58)
        self.assertEqual(self.named[BUCKET_INCOME].rows, 2)
        by_class = {event.amount_class for event in BOOKINGS}
        self.assertEqual(by_class, {"TransactionCosts", "NotAssigned", "OngoingPayment"})
        self.assertEqual(
            sum(1 for event in BOOKINGS if event.amount_class == "TransactionCosts"), 59
        )
        self.assertEqual(sum(1 for event in BOOKINGS if event.amount_class == "NotAssigned"), 44)

    def test_the_withholding_tax_row_keeps_its_zero_related_trade_id(self) -> None:
        """``"0"`` is a value the vendor sends, never an absence.

        Treating it as absent, or ``or 0.0``-ing the string, moves +4.74 and
        -0.69 out of ``income``.
        """
        corporate = events_of_type(buckets.CA_WITHHOLDING_TAX) + events_of_type(
            buckets.CA_CASH_DIVIDENDS
        )
        self.assertEqual([event.related_trade_id for event in corporate], ["0", "0"])
        assert self.named[BUCKET_INCOME].total.value is not None
        self.assertAlmostEqual(self.named[BUCKET_INCOME].total.value, 4.05, places=6)

    def test_bucket_of_answers_unclassified_for_a_type_nobody_has_seen(self) -> None:
        self.assertEqual(bucket_of(buckets.SHARE_AMOUNT), BUCKET_SHARE_LEGS)
        self.assertEqual(bucket_of(buckets.CASH_AMOUNT), BUCKET_DEPOSITS)
        self.assertEqual(bucket_of("Financing Charge"), BUCKET_UNCLASSIFIED)
        self.assertEqual(bucket_of(""), BUCKET_UNCLASSIFIED)


class AnUnseenTypeCanOnlyMoveMoneyIntoUnclassified(unittest.TestCase):
    def test_an_unseen_type_moves_money_into_unclassified_and_the_total_follows(self) -> None:
        """``if t in BUCKET_OF_TYPE:`` with no ``else`` drops the row.

        The bucket identity would still hold with the row dropped -- it would
        be absent from both sides -- so this test carries the hand-written
        total and dies on its own.
        """
        rows = (
            *BOOKINGS,
            event_like(
                BOOKINGS[0],
                bk_amount_id="synthetic-financing-charge",
                bk_amount_type="Financing Charge",
                amount_class="NotAssigned",
                amount_account_currency=-12.34,
            ),
        )

        fold = classify_buckets(rows)
        named = _by_name(fold)

        self.assertEqual(fold.rows, 105)
        self.assertEqual(named[BUCKET_UNCLASSIFIED].rows, 1)
        self.assertEqual(named[BUCKET_UNCLASSIFIED].bk_amount_types, ("Financing Charge",))
        assert named[BUCKET_UNCLASSIFIED].total.value is not None
        self.assertAlmostEqual(named[BUCKET_UNCLASSIFIED].total.value, -12.34, places=6)
        assert fold.all_rows.value is not None
        self.assertAlmostEqual(fold.all_rows.value, 20726.34, places=6)

    def test_one_absent_amount_nulls_its_bucket_and_the_row_wise_total(self) -> None:
        """The all-or-nothing rule has to hold HERE too, not only in ``acct_sum``.

        A ``or 0.0`` written into the bucket's own comprehension never reaches
        ``acct_sum``'s guard: the fold publishes a share-legs total that is
        too HIGH by exactly the absent amount, with every row count still
        right. Measured mutation 2026-10-22: with ``or 0.0`` in the bucket
        comprehension and no test here, all 12 bucket tests stayed green.
        """
        rows = list(BOOKINGS)
        share_leg = next(
            index
            for index, event in enumerate(rows)
            if event.bk_amount_type == buckets.SHARE_AMOUNT
        )
        rows[share_leg] = event_like(rows[share_leg], amount_account_currency=None)

        fold = classify_buckets(tuple(rows))
        named = _by_name(fold)

        self.assertIsNone(named[BUCKET_SHARE_LEGS].total.value)
        self.assertEqual(named[BUCKET_SHARE_LEGS].total.null_reason, "account_amount_not_reported")
        self.assertEqual(named[BUCKET_SHARE_LEGS].rows, 41)
        self.assertIsNone(fold.all_rows.value)
        assert named[BUCKET_FEES].total.value is not None
        self.assertAlmostEqual(named[BUCKET_FEES].total.value, -155.31, places=6)

    def test_one_absent_currency_nulls_its_bucket_the_same_way(self) -> None:
        """The all-or-nothing rule covers the CURRENCY, not only the amount.

        An amount nobody said was in the account currency is not an account
        amount. A bucket comprehension that writes ``event.account_currency or
        "PLN"`` never reaches ``acct_sum``'s guard: measured 2026-10-22, it
        publishes ``share_legs`` as a complete -3110.06 PLN while the row-wise
        total refuses, so the report would print numeric buckets beside a
        refusing total and the identity would be unreadable rather than red.
        """
        rows = list(BOOKINGS)
        share_leg = next(
            index
            for index, event in enumerate(rows)
            if event.bk_amount_type == buckets.SHARE_AMOUNT
        )
        rows[share_leg] = event_like(rows[share_leg], account_currency=None)

        fold = classify_buckets(tuple(rows))
        named = _by_name(fold)

        self.assertIsNone(named[BUCKET_SHARE_LEGS].total.value)
        self.assertEqual(named[BUCKET_SHARE_LEGS].total.null_reason, "account_amount_not_reported")
        self.assertEqual(named[BUCKET_SHARE_LEGS].rows, 41)
        self.assertIsNone(fold.all_rows.value)
        assert named[BUCKET_FEES].total.value is not None
        self.assertAlmostEqual(named[BUCKET_FEES].total.value, -155.31, places=6)

    def test_unclassified_is_present_at_zero(self) -> None:
        """Printed even when empty, so a non-zero one is a CHANGED number."""
        named = _by_name(classify_buckets(BOOKINGS))

        self.assertIn(BUCKET_UNCLASSIFIED, named)
        self.assertEqual(named[BUCKET_UNCLASSIFIED].rows, 0)
        self.assertEqual(named[BUCKET_UNCLASSIFIED].total.value, 0.0)
        self.assertEqual(named[BUCKET_UNCLASSIFIED].total.currency, "PLN")

    def test_no_rows_at_all_still_publishes_every_bucket(self) -> None:
        fold = classify_buckets(())

        self.assertEqual([bucket.name for bucket in fold.buckets], list(BUCKET_ORDER))
        self.assertEqual(fold.rows, 0)
        self.assertEqual(fold.all_rows.value, 0.0)

    def test_an_empty_window_publishes_unit_less_zeros_and_that_is_the_contract(self) -> None:
        """Stated, because the alternative readings are both worse.

        With no rows there is no currency to read, so all five buckets and
        ``all_rows`` publish ``0.00`` with no unit -- six money lines with no
        currency on them. Refusing instead would turn the bucket identity into
        a refusal on any window with an empty bucket, which is every window
        with no dividend; defaulting to ``"PLN"`` would print zlotys on an
        account that may hold none. So the zero stays a neutral element and
        naming the currency is the caller's job. What makes that safe is the
        other half of the same fix: a unit-less NUMBER can no longer be
        differenced against a stated one, so it cannot adopt a currency
        downstream.
        """
        fold = classify_buckets(())

        self.assertIsNone(fold.all_rows.currency)
        self.assertEqual(fold.all_rows.rendered, "0.00")
        for bucket in fold.buckets:
            with self.subTest(bucket=bucket.name):
                self.assertEqual(bucket.total.value, 0.0)
                self.assertIsNone(bucket.total.currency)
                self.assertEqual(bucket.total.rendered, "0.00")

        self.assertEqual(
            acct_difference(fold.all_rows, acct_sum([(5.0, "PLN")], rows=1)).null_reason,
            NULL_CURRENCY_NOT_STATED,
        )


class TheAccountCurrencyComesOffTheRows(unittest.TestCase):
    def test_a_non_pln_account_publishes_its_own_currency_on_every_bucket(self) -> None:
        """The capture is PLN on 104 of 104 rows, so nothing here reads the field.

        Measured 2026-10-22: with the fold's account currency hard-coded to
        ``"PLN"`` all 13 bucket tests stayed green. The direction of that
        mutation is safe on a EUR account -- every bucket refuses with
        ``currency_mixed`` -- but an EMPTY bucket publishes ``0.00 PLN`` on an
        account that holds no zlotys, which is a money line with the wrong
        unit on it. The empty bucket is therefore the assertion that proves
        the read: its currency can come from nowhere but the rows.
        """
        rows = tuple(event_like(event, account_currency="EUR") for event in BOOKINGS)

        fold = classify_buckets(rows)
        named = _by_name(fold)

        self.assertEqual(fold.all_rows.currency, "EUR")
        for name in BUCKET_ORDER:
            with self.subTest(bucket=name):
                self.assertEqual(named[name].total.currency, "EUR")
        self.assertEqual(named[BUCKET_UNCLASSIFIED].total.rendered, "0.00 EUR")
        assert fold.all_rows.value is not None
        self.assertAlmostEqual(fold.all_rows.value, ALL_BOOKINGS_TOTAL_PLN, places=6)


class TheBucketIdentity(unittest.TestCase):
    def test_the_bucket_identity_holds_over_the_real_rows(self) -> None:
        """With floors: without them a fold returning NO buckets and a zero
        ``all_rows`` satisfies the identity -- true and empty."""
        fold = classify_buckets(BOOKINGS)

        over_buckets = acct_sum(
            [(bucket.total.value, bucket.total.currency) for bucket in fold.buckets],
            rows=fold.rows,
            currency=fold.all_rows.currency,
        )

        self.assertEqual(fold.rows, ALL_BOOKING_ROWS)
        self.assertEqual(len(fold.buckets), 5)
        self.assertGreaterEqual(
            sum(1 for bucket in fold.buckets if bucket.total.value not in (None, 0.0)), 4
        )
        assert over_buckets.value is not None and fold.all_rows.value is not None
        self.assertAlmostEqual(over_buckets.value, fold.all_rows.value, places=6)
        self.assertEqual(over_buckets.currency, fold.all_rows.currency)

    def test_all_rows_is_summed_over_the_rows_and_not_over_the_buckets(self) -> None:
        """The one mutation that kills the identity's power.

        With ``all_rows`` computed FROM the buckets the identity above is a
        tautology and every bucket number stays right. Dropping a bucket from
        the published order is what tells the two apart: the row-wise sum does
        not move, the bucket-wise one does.
        """
        shortened = tuple(name for name in BUCKET_ORDER if name != BUCKET_DEPOSITS)

        with mock.patch.object(buckets, "BUCKET_ORDER", shortened):
            fold = classify_buckets(BOOKINGS)

        self.assertEqual([bucket.name for bucket in fold.buckets], list(shortened))
        self.assertEqual(fold.rows, ALL_BOOKING_ROWS)
        assert fold.all_rows.value is not None
        self.assertAlmostEqual(fold.all_rows.value, ALL_BOOKINGS_TOTAL_PLN, places=6)

        over_buckets = acct_sum(
            [(bucket.total.value, bucket.total.currency) for bucket in fold.buckets],
            rows=fold.rows,
        )
        assert over_buckets.value is not None
        self.assertAlmostEqual(over_buckets.value, -3261.32, places=6)
        self.assertNotAlmostEqual(over_buckets.value, fold.all_rows.value, places=2)


if __name__ == "__main__":
    unittest.main()

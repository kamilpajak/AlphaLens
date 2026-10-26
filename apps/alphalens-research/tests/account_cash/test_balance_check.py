"""The balance cross-check, in both versions (#1689, memo section 5(c)).

Two versions, and the difference matters more than it looks. The CUMULATIVE
check -- the cash book since ``history_begins`` against the latest
``CashBalance`` -- is known to hold, and it is fully real here: the 104-row sum
is +20 738.68 and the balance body committed at
``tests/brokers/test_saxo_broker_account_cash.py`` carries
``CashBalance: 20738.68``, measured on LIVE 2026-10-05 (memo section 1.6 P1).
Both sides come from real reads.

The INCREMENTAL check -- the change between two snapshots against the events
settling between them -- is the one that catches a late restatement, which is
the entire reason the store is an append-only journal. Its money is real too
and its identity fields are AUTHORED, and the test that uses it says so in its
name: memo section 1.7 P8 states the cash pair (17 131.59 to 12 812.52), the
difference (-4 319.07), the six rows that explain it and their ``Date`` stamp,
and states none of their ``BkAmountId``, ``Uic`` or ``ConversionRate`` -- none
of which this check reads. It reads the date, the account amount and the
account currency, and nothing else.

What is NOT tested here, and is not built: the ``TotalValue`` half of the
account identity (memo section 1.6 P2). The market value of the three open
positions needs a price, and ``venue.json``'s ``instruments`` dict carries no
price field, so there is no offline way to exercise it.
"""

from __future__ import annotations

import datetime as dt
import unittest
from dataclasses import replace

from alphalens_pipeline.account_cash import balance_check
from alphalens_pipeline.account_cash.codec import SweepRecord
from alphalens_pipeline.account_cash.money import BALANCE_TOLERANCE_ACCT, acct_difference
from alphalens_pipeline.account_cash.store import BalanceAt
from alphalens_pipeline.brokers.account_cash import BalanceSnapshot, CashEvent

from tests.account_cash.cashbook_fixture import BALANCE, BOOKINGS

_LIVE = "live"

ALL_ROWS = 104
ALL_ROWS_TOTAL_PLN = 20738.68
#: The span the 104 rows THEMSELVES cover, which is not the window the one
#: sweep asked for (2026-08-01..2026-10-03): the account's first booking row
#: is five days after the asked start and its last is two days before the
#: asked end. That is what makes the two readings of the cumulative basis
#: distinguishable at all.
ROWS_FIRST_DATE = "2026-08-06"
ROWS_LAST_DATE = "2026-10-01"

# --- Memo section 1.7 P8: a real two-snapshot pair, three trades in flight ---

P8_EARLIER_CASH_PLN = 17131.59
P8_LATER_CASH_PLN = 12812.52
P8_DIFFERENCE_PLN = -4319.07
#: Three share legs and three commissions, all stamped ``Date=2026-10-08``.
P8_SHARE_LEGS_PLN = (-1056.86, -1646.78, -1603.67)
P8_COMMISSION_PLN = -3.92
P8_TRADE_DATE = "2026-10-08"
#: The venue stamps the NEXT day as ``ValueDate`` on all six rows (P9). The
#: settlement crosses midnight UTC, so a reader inferring the basis from the
#: observation instant gets 2026-10-09; the field says 2026-10-08.
P8_VALUE_DATE = "2026-10-09"
P8_EARLIER_ASOF = dt.datetime(2026, 10, 8, 23, 46, 52, tzinfo=dt.UTC)
P8_LATER_ASOF = dt.datetime(2026, 10, 9, 0, 14, 17, tzinfo=dt.UTC)


def _event(bk_amount_id: str, amount: float, **overrides: object) -> CashEvent:
    """One booking row. ``bk_amount_id`` and ``uic`` are AUTHORED, and this
    check reads neither -- only ``date``, ``amount_account_currency`` and
    ``account_currency``."""
    base = CashEvent(
        bk_amount_id=bk_amount_id,
        bk_amount_type="Share Amount",
        amount_class="NotAssigned",
        amount=-250.0,
        currency="USD",
        amount_account_currency=amount,
        account_currency="PLN",
        conversion_rate=3.73625234,
        conversion_cost_account_currency=None,
        date=P8_TRADE_DATE,
        value_date=P8_VALUE_DATE,
        uic=1,
        instrument_symbol="BE:xnys",
        related_trade_id="0",
        ca_event_name=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _p8_events() -> tuple[CashEvent, ...]:
    """The six rows memo section 1.7 P8 measured, summing -4 319.07."""
    legs = tuple(_event(f"leg{index}", amount) for index, amount in enumerate(P8_SHARE_LEGS_PLN))
    fees = tuple(
        _event(f"fee{index}", P8_COMMISSION_PLN, bk_amount_type="Commission")
        for index in range(len(P8_SHARE_LEGS_PLN))
    )
    return (*legs, *fees)


def _sweep(sweep_id: str, since: str, until: str) -> SweepRecord:
    return SweepRecord(
        sweep_id=sweep_id,
        swept_at=dt.datetime.fromisoformat(f"{until}T23:00:00+00:00"),
        env=_LIVE,
        since=since,
        until=until,
        events_window=None,
        trades_window=None,
        appended={},
        already_present={},
        restated={},
        malformed={},
        refused_reason=None,
    )


def _snapshot(sweep_id: str, cash: float, asof: dt.datetime) -> BalanceAt:
    return BalanceAt(
        sweep_id=sweep_id,
        snapshot=BalanceSnapshot(
            currency="PLN",
            cash_balance=cash,
            non_margin_positions_value=None,
            total_value=None,
            transactions_not_booked=None,
            calculation_reliability="Ok",
            asof=asof,
        ),
    )


def _p8_pair() -> tuple[tuple[BalanceAt, ...], tuple[SweepRecord, ...]]:
    """Two snapshots and the two sweeps whose windows produced them."""
    snapshots = (
        _snapshot("s1", P8_EARLIER_CASH_PLN, P8_EARLIER_ASOF),
        _snapshot("s2", P8_LATER_CASH_PLN, P8_LATER_ASOF),
    )
    sweeps = (
        _sweep("s1", "2026-10-01", "2026-10-07"),
        _sweep("s2", "2026-10-08", "2026-10-08"),
    )
    return snapshots, sweeps


class TheCumulativeCheckOnTheRealCapture(unittest.TestCase):
    """Both sides real: the 104-row sum and the committed ``CashBalance``."""

    def setUp(self) -> None:
        self.latest = BalanceAt(sweep_id="s1", snapshot=BALANCE)
        self.check = balance_check.cumulative_check(BOOKINGS, self.latest)

    def test_the_premise_is_that_both_sides_are_read_and_not_authored(self) -> None:
        """Stated rather than assumed. If these two came from one source the
        test below would be asserting that a number equals itself."""
        self.assertEqual(BALANCE.cash_balance, ALL_ROWS_TOTAL_PLN)
        self.assertEqual(len(BOOKINGS), ALL_ROWS)

    def test_it_reconciles_on_the_real_window(self) -> None:
        self.assertEqual(self.check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertEqual(self.check.expected.value, ALL_ROWS_TOTAL_PLN)
        self.assertEqual(self.check.observed.value, ALL_ROWS_TOTAL_PLN)
        self.assertIsNotNone(self.check.difference.value)
        self.assertLess(abs(self.check.difference.value or 0.0), BALANCE_TOLERANCE_ACCT)
        self.assertEqual(self.check.kind, balance_check.CHECK_CUMULATIVE)

    def test_it_says_on_its_face_that_it_cannot_see_a_restatement(self) -> None:
        """Memo section 5(c) makes this sentence load-bearing: a check that
        only ever says yes cannot detect a correction to an old row, and
        without the field "reconciles" reads as proof the history is intact.

        **Mutation it catches:** shipping it without the statement.
        """
        self.assertIs(self.check.detects_restatement, False)

    def test_it_publishes_its_own_row_count_and_its_own_interval(self) -> None:
        """Amendment W1: this check is STORE-WIDE by definition, so it carries
        its own basis and can never be read as the window's figure.

        **Mutation it catches:** dropping the three fields, which leaves a
        104-row store-wide total sitting under a header that states a 64-row
        window.
        """
        self.assertEqual(self.check.rows, ALL_ROWS)
        self.assertEqual(self.check.asof, BALANCE.asof.isoformat())

    def test_the_difference_rests_on_the_rows_it_summed_and_not_on_both_counts(self) -> None:
        """A DIFFERENCE between a 104-row sum and one balance snapshot has no
        row count of its own, and the one it used to publish counted nothing:
        ``acct_difference`` adds both sides' counts, so the record carried
        ``rows: 105`` and the operator read "difference 0.00 PLN over 105
        rows". There are 104 booking rows and no 105th anything.

        The published count is the booking rows the cash-book side summed,
        which is this check's own basis -- so the record cannot state two
        different counts for one check. ``acct_difference``'s rule is NOT
        changed: adding both counts is right for a difference of two
        independent sums over disjoint rows, which is what it is for, and it
        is relied on in ``money.py``'s own tests.

        **Mutation it catches:** publishing the raw difference, which is the
        last assertion's 105; and publishing 0, which would read like the
        ``not_checkable`` records that really do rest on no row.
        """
        self.assertEqual(self.check.expected.rows, ALL_ROWS)
        self.assertEqual(self.check.observed.rows, 1)
        self.assertEqual(self.check.difference.rows, ALL_ROWS)
        self.assertEqual(self.check.difference.rows, self.check.rows)
        self.assertNotEqual(self.check.difference.rows, 0)
        self.assertEqual(
            acct_difference(self.check.expected, self.check.observed).rows,
            ALL_ROWS + 1,
        )
        self.assertNotEqual(
            self.check.difference.rows,
            acct_difference(self.check.expected, self.check.observed).rows,
        )

    def test_the_interval_is_the_span_of_the_rows_it_summed(self) -> None:
        """F5. The basis is read off the rows SUMMED, not handed in.

        The caller used to pass the coverage watermarks, and ``coverage_of``
        stops at the end of the FIRST contiguous run so that a hole cannot be
        claimed as covered. This check is handed EVERY stored row, so with a
        hole in the coverage it published a row count covering everything
        under an ``until`` that stopped at the first run -- money from after
        the hole inside a number whose stated basis excluded it.

        **Mutation it catches:** taking the interval from anywhere but the
        rows. The real capture separates the two readings: the rows span
        2026-08-06..2026-10-01 while the one sweep asked for
        2026-08-01..2026-10-03.
        """
        self.assertEqual(
            (
                min(event.date or "" for event in BOOKINGS),
                max(event.date or "" for event in BOOKINGS),
            ),
            (ROWS_FIRST_DATE, ROWS_LAST_DATE),
        )

        self.assertEqual(self.check.since, ROWS_FIRST_DATE)
        self.assertEqual(self.check.until, ROWS_LAST_DATE)
        self.assertNotEqual(self.check.since, "2026-08-01")
        self.assertNotEqual(self.check.until, "2026-10-03")

    def test_no_row_at_all_is_no_span_rather_than_a_zero_length_one(self) -> None:
        """A first sweep over a quiet window. There is nothing to state, and a
        stated pair of dates would be a basis nobody measured."""
        check = balance_check.cumulative_check((), self.latest)

        self.assertIsNone(check.since)
        self.assertIsNone(check.until)
        self.assertEqual(check.rows, 0)

    def test_an_undated_row_is_still_summed_and_bounds_no_span(self) -> None:
        """``CashEvent.date`` is optional, and this check sums EVERY stored
        row rather than the dated ones -- that is what makes it store-wide.

        So the span covers the rows that carry a readable date while ``rows``
        counts every row summed, and the two differing is the honest answer:
        an undated row has no date to put in a span.

        The row emptied here is ``BOOKINGS[1]``, dated 2026-08-10, which is
        strictly INSIDE the span -- so the span must not move at all.

        **Mutation it catches:** dropping the undated row from the SUM to keep
        the span and the count agreeing, which would move real money out of
        the only check that compares against the account's own cash.
        """
        undated = (BOOKINGS[0], replace(BOOKINGS[1], date=None), *BOOKINGS[2:])

        check = balance_check.cumulative_check(undated, self.latest)

        self.assertEqual(check.rows, ALL_ROWS)
        self.assertEqual(check.expected.value, ALL_ROWS_TOTAL_PLN)
        self.assertEqual(check.since, ROWS_FIRST_DATE)
        self.assertEqual(check.until, ROWS_LAST_DATE)

    def test_the_span_is_read_off_the_dated_rows_and_is_not_a_constant(self) -> None:
        """The other direction, and the reason the test above has power.

        ``BOOKINGS[0]`` is the ONLY row dated 2026-08-06, so emptying its date
        moves the span's left bound to the next date the rows carry,
        2026-08-10. A span hard-coded to the capture's own dates, or taken
        from anywhere but the rows, stays at 2026-08-06 here.
        """
        without_the_edge = (replace(BOOKINGS[0], date=None), *BOOKINGS[1:])

        check = balance_check.cumulative_check(without_the_edge, self.latest)

        self.assertEqual(check.since, "2026-08-10")
        self.assertEqual(check.until, ROWS_LAST_DATE)
        self.assertEqual(check.rows, ALL_ROWS)


class TheToleranceIsAGroszAndIsTwoClaims(unittest.TestCase):
    """Amendment W7: the residual between the ``Decimal`` accumulation and its
    ``float`` publication is exactly 0.0 on these rows, so a test resting on
    it passes for a naive float sum too and says nothing about the tolerance.
    Both directions are constructed instead."""

    def _with_first_row_shifted(self, to: float) -> balance_check.BalanceCheck:
        events = (replace(BOOKINGS[0], amount_account_currency=to), *BOOKINGS[1:])
        return balance_check.cumulative_check(events, BalanceAt(sweep_id="s1", snapshot=BALANCE))

    def test_a_half_grosz_inside_the_tolerance_still_reconciles(self) -> None:
        """2000.00 becomes 2000.005, so the cash book sums to 20 738.685
        against a balance of 20 738.68.

        **Mutation it catches:** a tolerance narrowed to 0.0, and one narrowed
        to 0.001. Both make this red.
        """
        check = self._with_first_row_shifted(2000.005)

        self.assertEqual(check.expected.value, 20738.685)
        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)

    def test_a_disagreement_of_exactly_one_grosz_still_reconciles(self) -> None:
        """The third claim a tolerance makes, after its unit and its order of
        magnitude: which side of the boundary the boundary itself is on.

        2000.00 becomes 2000.01, so the cash book sums to 20 738.69 against a
        balance of 20 738.68 and the difference is exactly
        ``BALANCE_TOLERANCE_ACCT``. "Agree to within one grosz" includes the
        grosz, so this reconciles.

        **Mutation it catches:** ``<`` in place of ``<=``. Measured
        2026-10-26: green before this test, because the two constructed cases
        were 0.005 -- strictly inside -- and 0.50, far outside, leaving the
        edge itself unasserted.
        """
        check = self._with_first_row_shifted(2000.01)

        self.assertEqual(check.expected.value, 20738.69)
        self.assertAlmostEqual(check.difference.value or 0.0, BALANCE_TOLERANCE_ACCT, places=9)
        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)

    def test_a_fifty_grosz_disagreement_is_content_and_not_a_crash(self) -> None:
        """**Mutation it catches:** raising on a disagreement, and a tolerance
        widened to 1.0, which this 0.50 would swallow."""
        check = self._with_first_row_shifted(2000.50)

        self.assertEqual(check.verdict, balance_check.VERDICT_DIFFERS)
        self.assertAlmostEqual(check.difference.value or 0.0, 0.50, places=9)
        self.assertEqual(check.expected.value, 20739.18)


class TheIncrementalCheckOnTheP8PairWithAuthoredRowIds(unittest.TestCase):
    """Memo section 1.7 P8's real pair. The cash figures, the difference, the
    six amounts and their ``Date`` are measured; each row's ``BkAmountId``,
    ``Uic`` and ``ConversionRate`` are AUTHORED by this test, and the check
    reads none of the three."""

    def setUp(self) -> None:
        self.snapshots, self.sweeps = _p8_pair()
        self.check = balance_check.incremental_check(
            _p8_events(), snapshots=self.snapshots, sweeps=self.sweeps
        )

    def test_the_premise_is_that_the_six_rows_explain_the_cash_movement(self) -> None:
        """Stated first: if the six amounts did not sum to the cash change,
        every assertion below would be about a different claim."""
        self.assertAlmostEqual(
            sum(P8_SHARE_LEGS_PLN) + 3 * P8_COMMISSION_PLN, P8_DIFFERENCE_PLN, places=9
        )
        self.assertAlmostEqual(P8_LATER_CASH_PLN - P8_EARLIER_CASH_PLN, P8_DIFFERENCE_PLN, places=9)

    def test_it_reconciles_over_the_six_rows(self) -> None:
        self.assertEqual(self.check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertAlmostEqual(self.check.expected.value or 0.0, P8_DIFFERENCE_PLN, places=9)
        self.assertAlmostEqual(self.check.observed.value or 0.0, P8_DIFFERENCE_PLN, places=9)
        self.assertIsNotNone(self.check.difference.value)
        self.assertLess(abs(self.check.difference.value or 0.0), BALANCE_TOLERANCE_ACCT)
        self.assertEqual(self.check.rows, 6)

    def test_the_difference_rests_on_the_six_rows_and_not_on_eight(self) -> None:
        """The same rule on the other check, where both sides are sums: six
        booking rows against a change over two balance snapshots. The raw
        difference adds them to eight, and there is no eighth row.

        The ``observed`` line keeps its own count of 2, deliberately. It is a
        sum over two balance snapshots and two is how many it read -- a count
        of something that exists, in the balances journal rather than in the
        cash book. What the difference had was a count of two SPACES added
        together, which counts nothing in either.
        """
        self.assertEqual(self.check.expected.rows, 6)
        self.assertEqual(self.check.observed.rows, 2)
        self.assertEqual(self.check.difference.rows, 6)
        self.assertEqual(self.check.difference.rows, self.check.rows)
        self.assertNotEqual(self.check.difference.rows, 8)

    def test_it_says_on_its_face_that_it_can_see_a_restatement(self) -> None:
        """**Mutation it catches:** copying the flag from the cumulative
        check, which makes the two indistinguishable to a machine reader."""
        self.assertIs(self.check.detects_restatement, True)

    def test_the_interval_is_in_trade_dates_from_the_sweep_and_not_in_instants(self) -> None:
        """The pairing rule, and the reason ``BalanceAt`` exists.

        A cash event carries a ``Date`` and no timestamp, while the settlement
        batch fires at about 23:48 UTC and the report completes about 26
        minutes later -- across midnight. So the earlier snapshot is stamped
        2026-10-08T23:46:52Z and the later one 2026-10-09T00:14:17Z, while
        every row that explains the movement is dated 2026-10-08.

        **Mutation it catches:** keying the interval on the ``asof``
        instants. Measured 2026-10-26, and the two shapes of that slip are not
        equally loud:

        - comparing a row's date against the INSTANTS -- midnight 2026-10-08
          falls before the earlier snapshot's 23:46:52 -- sums 0 of the 6 rows
          and publishes ``differs`` with +4 319.07 on a pair that reconciles
          exactly. Five assertions in this class go red.
        - taking ``asof.date()`` on both ends instead keeps the one-day
          offset's effect by accident, because the earlier snapshot is stamped
          2026-10-08 and that IS the earlier window's ``until`` plus a day. It
          still sums all six and still answers ``reconciles``, so the only
          assertion with power over it is the published ``until`` below --
          2026-10-09 against 2026-10-08. The money verdict moves only with a
          row past the later window in the store, which is what
          ``test_a_row_past_the_later_window_is_not_summed_either`` adds.
        """
        self.assertEqual(self.check.since, P8_TRADE_DATE)
        self.assertEqual(self.check.until, P8_TRADE_DATE)
        self.assertLess(self.snapshots[0].snapshot.asof.date().isoformat(), "2026-10-09")
        self.assertEqual(self.snapshots[1].snapshot.asof.date().isoformat(), "2026-10-09")

    def test_it_keys_on_the_trade_date_and_not_on_the_value_date(self) -> None:
        """Memo section 1.7 P9 settles the basis. ``Date`` and ``ValueDate``
        agree on only 5 of 104 rows of the real capture, so the choice changes
        which rows fall inside.

        **Mutation it catches:** reading ``value_date``, which puts all six
        rows on 2026-10-09 -- outside the interval -- and reports a full day's
        trading as a disagreement.
        """
        moved = tuple(replace(event, value_date="2026-10-11") for event in _p8_events())

        check = balance_check.incremental_check(moved, snapshots=self.snapshots, sweeps=self.sweeps)

        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)

    def test_a_row_the_earlier_snapshot_already_reflects_is_not_summed_again(self) -> None:
        """The negative control, on the interval's left EDGE.

        A check that summed EVERYTHING would satisfy every assertion above,
        because the fixture holds only interval rows. The extra row is dated
        2026-10-07 -- the last day the earlier sweep covered, so its money is
        already inside that snapshot's cash balance.

        **Mutation it catches:** dropping the one-day offset and starting the
        interval at the earlier window's own ``until``, which double counts
        every row of the earlier snapshot's last day. A row dated further back
        would not catch it.
        """
        with_extra = (*_p8_events(), _event("outside", -500.00, date="2026-10-07"))

        check = balance_check.incremental_check(
            with_extra, snapshots=self.snapshots, sweeps=self.sweeps
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertEqual(check.rows, 6)
        self.assertEqual(check.since, P8_TRADE_DATE)

    def test_a_row_past_the_later_window_is_not_summed_either(self) -> None:
        """The same control on the interval's RIGHT edge, and the one that
        gives the MONEY a stake in the interval's basis.

        The store can hold a row dated after the latest sweep's window: a wide
        back-fill (say 2026-10-01..2026-10-10) read it, and the two narrower
        sweeps that produced these snapshots end 10-07 and 10-08. So the extra
        row is dated 2026-10-09 and carries -500.00.

        **Mutation it catches:** ``earlier.snapshot.asof.date()`` and
        ``later.snapshot.asof.date()`` in place of the two sweep windows.
        Measured 2026-10-26: that slip keeps the one-day offset's effect by
        accident -- the earlier snapshot is stamped 2026-10-08, which is the
        earlier window's ``until`` plus a day -- so it still sums all six real
        rows and still answers ``reconciles``. Its only visible trace was the
        published ``until``, 2026-10-09 against 2026-10-08. With this row in
        the store it sums seven and answers ``differs`` by 500.00 PLN.
        """
        with_extra = (*_p8_events(), _event("later", -500.00, date="2026-10-09"))

        check = balance_check.incremental_check(
            with_extra, snapshots=self.snapshots, sweeps=self.sweeps
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertEqual(check.rows, 6)
        self.assertEqual(check.until, P8_TRADE_DATE)

    def test_it_pairs_the_LAST_two_snapshots_and_not_the_first_and_the_last(self) -> None:
        """**Mutation it catches:** ``snapshots[0], snapshots[-1]``, which on
        a two-snapshot store answers identically and on a real store compares
        today's cash against the first sweep's."""
        older = _snapshot("s0", 99999.99, dt.datetime(2026, 9, 1, 0, 31, tzinfo=dt.UTC))

        check = balance_check.incremental_check(
            _p8_events(),
            snapshots=(older, *self.snapshots),
            sweeps=(_sweep("s0", "2026-08-01", "2026-08-31"), *self.sweeps),
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertAlmostEqual(check.observed.value or 0.0, P8_DIFFERENCE_PLN, places=9)

    def test_a_disagreement_is_published_as_a_number(self) -> None:
        shifted = (
            replace(_p8_events()[0], amount_account_currency=P8_SHARE_LEGS_PLN[0] + 0.50),
            *_p8_events()[1:],
        )

        check = balance_check.incremental_check(
            shifted, snapshots=self.snapshots, sweeps=self.sweeps
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_DIFFERS)
        self.assertAlmostEqual(check.difference.value or 0.0, 0.50, places=9)


class TheIncrementalIntervalRunsForwardOrIsNotAnInterval(unittest.TestCase):
    """F1: the interval is DERIVED, so it can come out backwards.

    It runs from the day AFTER the earlier sweep's window ended to the day the
    later one's ended. Two ordinary operator actions make ``first > last``: a
    re-run of a sweep over the same window, and a backfill of an older one.
    No row can fall inside a backwards pair, so the sum is empty and the check
    compares ``0.00`` against a real balance change -- publishing ``differs``
    with a money number beside an interval whose own stated bounds are in the
    wrong order.
    """

    def setUp(self) -> None:
        self.snapshots, self.sweeps = _p8_pair()

    #: Both sweeps over one window. The reviewer's probe shape.
    def _same_window(self) -> tuple[SweepRecord, ...]:
        return (
            _sweep("s1", "2026-10-01", "2026-10-08"),
            _sweep("s2", "2026-10-01", "2026-10-08"),
        )

    #: The later SNAPSHOT belongs to a sweep that asked for an EARLIER window.
    def _backfill(self) -> tuple[SweepRecord, ...]:
        return (
            _sweep("s1", "2026-10-01", "2026-10-08"),
            _sweep("s2", "2026-09-01", "2026-09-30"),
        )

    def test_a_re_run_of_the_same_window_forms_no_interval_at_all(self) -> None:
        """The loudest shape, with the money moving under it: the account's
        cash falls 5 000.00 between the two snapshots while the derived
        interval is ``[2026-10-09, 2026-10-08]`` and holds nothing.

        **Mutation it catches:** dropping the order guard, which publishes
        ``differs`` with -5 000.00 PLN over 0 rows, ``since=2026-10-09`` and
        ``until=2026-10-08``.
        """
        moved = (
            _snapshot("s1", P8_EARLIER_CASH_PLN, P8_EARLIER_ASOF),
            _snapshot("s2", P8_EARLIER_CASH_PLN - 5000.00, P8_LATER_ASOF),
        )

        check = balance_check.incremental_check(
            _p8_events(), snapshots=moved, sweeps=self._same_window()
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_INTERVAL_RUNS_BACKWARDS)
        self.assertIsNone(check.difference.value)
        self.assertIsNone(check.since)
        self.assertIsNone(check.until)
        self.assertEqual(check.rows, 0)

    def test_a_backfill_of_an_older_window_forms_no_interval_either(self) -> None:
        """The other way in, and it is not a re-run.

        **Mutation it catches:** a guard written only for the equal-window
        case, which a backfill walks straight past.
        """
        check = balance_check.incremental_check(
            _p8_events(), snapshots=self.snapshots, sweeps=self._backfill()
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_INTERVAL_RUNS_BACKWARDS)

    def test_the_shortest_forward_interval_is_still_answerable(self) -> None:
        """The negative control, on the boundary the guard sits on.

        The P8 pair's later window ends exactly ONE day after the earlier
        one's, so the interval is the single day ``[until + 1, until + 1]``. A
        guard written ``last <= first`` refuses it -- and that is the ordinary
        daily cadence, so it would refuse every back-to-back pair of sweeps.
        """
        check = balance_check.incremental_check(
            _p8_events(), snapshots=self.snapshots, sweeps=self.sweeps
        )

        self.assertEqual(check.since, P8_TRADE_DATE)
        self.assertEqual(check.until, P8_TRADE_DATE)
        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertEqual(check.rows, 6)

    def test_a_published_interval_always_runs_forward(self) -> None:
        """The direction itself, over all three shapes, asserted rather than
        inferred from a row count: a check that publishes an interval must
        publish one a row could fall inside, and one that cannot form an
        interval must publish no bounds at all.

        **Mutation it catches:** keeping the backwards pair on the record
        while nulling only the money, which leaves the published basis saying
        something impossible.
        """
        for name, sweeps in (
            ("forward", self.sweeps),
            ("same window", self._same_window()),
            ("backfill", self._backfill()),
        ):
            with self.subTest(sweeps=name):
                check = balance_check.incremental_check(
                    _p8_events(), snapshots=self.snapshots, sweeps=sweeps
                )

                if check.null_reason == balance_check.NULL_INTERVAL_RUNS_BACKWARDS:
                    self.assertIsNone(check.since)
                    self.assertIsNone(check.until)
                else:
                    self.assertIsNotNone(check.since)
                    self.assertIsNotNone(check.until)
                    self.assertLessEqual(check.since or "", check.until or "")


class ACheckThatCannotBeMadeIsNullAndNeverZero(unittest.TestCase):
    def test_one_snapshot_cannot_answer_the_incremental_check(self) -> None:
        """**Mutation it catches:** publishing ``0.0`` as the difference,
        which reads as agreement."""
        snapshots, sweeps = _p8_pair()

        check = balance_check.incremental_check(
            _p8_events(), snapshots=snapshots[:1], sweeps=sweeps[:1]
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_ONE_SNAPSHOT_ONLY)
        self.assertIsNone(check.difference.value)
        self.assertIsNone(check.expected.value)

    def test_no_snapshot_at_all_nulls_both_checks(self) -> None:
        """**Mutation it catches:** ``or 0.0`` on ``cash_balance``."""
        cumulative = balance_check.cumulative_check(BOOKINGS, None)
        incremental = balance_check.incremental_check(BOOKINGS, snapshots=(), sweeps=())

        for check in (cumulative, incremental):
            with self.subTest(kind=check.kind):
                self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
                self.assertEqual(check.null_reason, balance_check.NULL_NO_BALANCE_SNAPSHOT)
                self.assertIsNone(check.difference.value)

    def test_a_snapshot_whose_sweep_window_is_missing_is_not_checkable(self) -> None:
        """The journals are appended in order -- balances BEFORE sweeps -- so
        a crash between the two leaves exactly this state: a snapshot whose
        sweep record is not on disk.

        **Mutation it catches:** defaulting the missing window to the other
        snapshot's, which would sum an interval nobody read.
        """
        snapshots, sweeps = _p8_pair()

        check = balance_check.incremental_check(
            _p8_events(), snapshots=snapshots, sweeps=sweeps[:1]
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_SWEEP_WINDOW_NOT_FOUND)

    def test_an_absent_cash_balance_nulls_the_cumulative_check(self) -> None:
        """A money field the vendor did not report is a named absence, never a
        zero -- the rule ``money.py`` holds for every total."""
        check = balance_check.cumulative_check(
            BOOKINGS, BalanceAt(sweep_id="s1", snapshot=replace(BALANCE, cash_balance=None))
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.observed.null_reason, "account_amount_not_reported")
        self.assertIsNone(check.difference.value)


class AVerdictOfNotCheckableAlwaysNamesItsReason(unittest.TestCase):
    """F6: ``not_checkable`` is reachable two ways, and one of them was mute.

    The three (now four) named absences set ``null_reason`` on the record. The
    OTHER way in is the arithmetic nulling with every input present -- the
    account reported no ``CashBalance``, or two currencies reached one sum --
    and there the verdict said ``not_checkable`` while the record's own
    ``null_reason`` stayed ``None`` and the reason lived on the nested amount.
    A renderer branches on the record's field, so it had a verdict it could
    not explain.
    """

    def _absent_balance(self) -> BalanceAt:
        return BalanceAt(sweep_id="s1", snapshot=replace(BALANCE, cash_balance=None))

    def test_an_absent_cash_balance_names_a_reason_on_the_cumulative_record(self) -> None:
        """**Mutation it catches:** ``null_reason=None`` on this path, which
        publishes a verdict with nothing beside it to branch on."""
        check = balance_check.cumulative_check(BOOKINGS, self._absent_balance())

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_DIFFERENCE_NOT_A_NUMBER)
        self.assertIn(check.null_reason, balance_check.CHECK_NULL_REASONS)

    def test_an_absent_cash_balance_names_a_reason_on_the_incremental_record_too(self) -> None:
        """Both halves, because each builds its own record: the cumulative one
        holding and the incremental one staying mute is exactly the asymmetry
        a machine reader would trip on."""
        snapshots, sweeps = _p8_pair()
        muted = (
            snapshots[0],
            BalanceAt(
                sweep_id=snapshots[1].sweep_id,
                snapshot=replace(snapshots[1].snapshot, cash_balance=None),
            ),
        )

        check = balance_check.incremental_check(_p8_events(), snapshots=muted, sweeps=sweeps)

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_DIFFERENCE_NOT_A_NUMBER)

    def test_two_account_currencies_name_the_same_reason(self) -> None:
        """The other arithmetic route: the cash book's own total refuses with
        ``currency_mixed``, so there is no difference to compare. The WHY
        stays on the amount, in the money vocabulary; the record says that its
        verdict rests on it."""
        mixed = (replace(BOOKINGS[0], account_currency="EUR"), *BOOKINGS[1:])

        check = balance_check.cumulative_check(mixed, BalanceAt(sweep_id="s1", snapshot=BALANCE))

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_DIFFERENCE_NOT_A_NUMBER)
        self.assertEqual(check.expected.null_reason, "currency_mixed")

    def test_a_check_that_was_made_names_no_reason(self) -> None:
        """The negative control. Without it, hard-coding the reason on every
        record satisfies every assertion above."""
        check = balance_check.cumulative_check(BOOKINGS, BalanceAt(sweep_id="s1", snapshot=BALANCE))

        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertIsNone(check.null_reason)

    def _every_shape(self) -> dict[str, balance_check.BalanceCheck]:
        """Every record shape either check can publish, keyed by what it is.

        Shared by the two properties below rather than built twice: a shape
        added to one copy and not the other is a shape one property stops
        covering, silently.
        """
        snapshots, sweeps = _p8_pair()
        latest = BalanceAt(sweep_id="s1", snapshot=BALANCE)
        shapes = {
            "reconciles": balance_check.cumulative_check(BOOKINGS, latest),
            "differs": balance_check.cumulative_check(
                (replace(BOOKINGS[0], amount_account_currency=2000.50), *BOOKINGS[1:]), latest
            ),
            "no snapshot": balance_check.cumulative_check(BOOKINGS, None),
            "absent balance": balance_check.cumulative_check(BOOKINGS, self._absent_balance()),
            "one snapshot": balance_check.incremental_check(
                _p8_events(), snapshots=snapshots[:1], sweeps=sweeps[:1]
            ),
            "no sweep window": balance_check.incremental_check(
                _p8_events(), snapshots=snapshots, sweeps=sweeps[:1]
            ),
            "backwards interval": balance_check.incremental_check(
                _p8_events(),
                snapshots=snapshots,
                sweeps=(
                    _sweep("s1", "2026-10-01", "2026-10-08"),
                    _sweep("s2", "2026-10-01", "2026-10-08"),
                ),
            ),
            "incremental reconciles": balance_check.incremental_check(
                _p8_events(), snapshots=snapshots, sweeps=sweeps
            ),
        }
        return shapes

    def test_the_verdict_and_the_reason_agree_on_every_shape(self) -> None:
        """One property over all of them: a record says ``not_checkable``
        exactly when it names a reason. That is the invariant a renderer may
        rely on, and neither half of it held before."""
        for name, check in sorted(self._every_shape().items()):
            with self.subTest(shape=name):
                self.assertEqual(
                    check.verdict == balance_check.VERDICT_NOT_CHECKABLE,
                    check.null_reason is not None,
                    f"{check.verdict} beside null_reason={check.null_reason!r}",
                )
                if check.null_reason is not None:
                    self.assertIn(check.null_reason, balance_check.CHECK_NULL_REASONS)

    def test_no_published_difference_states_a_count_of_its_own(self) -> None:
        """The second property over the same shapes: the difference rests on
        the rows the check itself summed, never on both sides' counts added.

        One record must not carry two counts of one basis. A renderer prints
        ``difference <amount> over <rows> rows`` beside ``rows <the record's
        own>``, so two numbers there are two answers to one question -- and
        the added one answers 105 over a 104-row cash book.

        **Mutation it catches:** restating only the check that was measured.
        The eight shapes include both kinds and every ``not_checkable``
        reason, where the count is 0 on both sides and must stay there.
        """
        for name, check in sorted(self._every_shape().items()):
            with self.subTest(shape=name):
                self.assertEqual(check.difference.rows, check.rows)
                self.assertEqual(check.expected.rows, check.rows)


#: Every reason a check can answer ``not_checkable`` with, written out rather
#: than read back off ``CHECK_NULL_REASONS``: ``_money_null_for`` validates
#: against that very tuple, so comparing the published set against it is
#: production against production.
CHECK_NULL_VOCABULARY = (
    "no_balance_snapshot",
    "one_snapshot_only",
    "sweep_window_not_found",
    "interval_runs_backwards",
    "difference_not_a_number",
)


class TheVocabularyIsClosed(unittest.TestCase):
    def test_the_check_null_reasons_are_the_published_vocabulary(self) -> None:
        """**Mutation it catches:** a reason registered in the tuple and never
        published, or published and never registered -- which is what a
        renderer branching on the field would meet as an unknown string."""
        self.assertEqual(balance_check.CHECK_NULL_REASONS, CHECK_NULL_VOCABULARY)
        self.assertEqual(balance_check.NULL_INTERVAL_RUNS_BACKWARDS, "interval_runs_backwards")

    def test_every_verdict_and_null_reason_is_registered_and_distinct(self) -> None:
        self.assertEqual(len(set(balance_check.VERDICTS)), len(balance_check.VERDICTS))
        self.assertEqual(
            len(set(balance_check.CHECK_NULL_REASONS)), len(balance_check.CHECK_NULL_REASONS)
        )
        self.assertEqual(
            sorted(balance_check.CHECK_KINDS),
            sorted({balance_check.CHECK_CUMULATIVE, balance_check.CHECK_INCREMENTAL}),
        )

    def test_the_wire_form_names_the_basis_beside_every_number(self) -> None:
        body = balance_check.cumulative_check(
            BOOKINGS, BalanceAt(sweep_id="s1", snapshot=BALANCE)
        ).to_jsonable()

        self.assertEqual(
            sorted(body),
            [
                "asof",
                "detects_restatement",
                "difference",
                "expected",
                "kind",
                "null_reason",
                "observed",
                "rows",
                "since",
                "until",
                "verdict",
            ],
        )
        self.assertEqual(body["expected"]["value_rendered"], "20738.68 PLN")


if __name__ == "__main__":
    unittest.main()

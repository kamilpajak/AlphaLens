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
from alphalens_pipeline.account_cash.money import BALANCE_TOLERANCE_ACCT
from alphalens_pipeline.account_cash.store import BalanceAt
from alphalens_pipeline.brokers.account_cash import BalanceSnapshot, CashEvent

from tests.account_cash.cashbook_fixture import BALANCE, BOOKINGS

_LIVE = "live"

ALL_ROWS = 104
ALL_ROWS_TOTAL_PLN = 20738.68

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
        self.check = balance_check.cumulative_check(
            BOOKINGS, self.latest, since="2026-08-01", until="2026-10-03"
        )

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
        self.assertEqual(self.check.since, "2026-08-01")
        self.assertEqual(self.check.until, "2026-10-03")
        self.assertEqual(self.check.asof, BALANCE.asof.isoformat())


class TheToleranceIsAGroszAndIsTwoClaims(unittest.TestCase):
    """Amendment W7: the residual between the ``Decimal`` accumulation and its
    ``float`` publication is exactly 0.0 on these rows, so a test resting on
    it passes for a naive float sum too and says nothing about the tolerance.
    Both directions are constructed instead."""

    def _with_first_row_shifted(self, to: float) -> balance_check.BalanceCheck:
        events = (replace(BOOKINGS[0], amount_account_currency=to), *BOOKINGS[1:])
        return balance_check.cumulative_check(
            events,
            BalanceAt(sweep_id="s1", snapshot=BALANCE),
            since="2026-08-01",
            until="2026-10-03",
        )

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
        instants. Measured 2026-10-26: a row's date read as midnight
        2026-10-08 falls before the earlier snapshot's 23:46:52, so such a
        check sums 0 of the 6 rows and publishes ``differs`` with +4 319.07 on
        a pair that reconciles exactly.
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
        cumulative = balance_check.cumulative_check(
            BOOKINGS, None, since="2026-08-01", until="2026-10-03"
        )
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
            BOOKINGS,
            BalanceAt(sweep_id="s1", snapshot=replace(BALANCE, cash_balance=None)),
            since="2026-08-01",
            until="2026-10-03",
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.observed.null_reason, "account_amount_not_reported")
        self.assertIsNone(check.difference.value)


class TheVocabularyIsClosed(unittest.TestCase):
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
            BOOKINGS, BalanceAt(sweep_id="s1", snapshot=BALANCE), since="a", until="b"
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

"""The cash book's answer over one window (#1689, memo sections 4, 5 and 6).

Every number asserted here was re-derived from the capture
``tests/brokers/automanager/fixtures/trades/live_2026_10_03/venue.json`` and is
written as a literal; none is computed by the code under test.

**The seeded sweep always covers the WHOLE capture (2026-08-01..2026-10-03)
while the queried window is narrower.** That is not incidental. The full span
cannot tell a window-scoped total from a store-wide one, cannot tell a full
open cost basis from a window-clipped one (both are -3594.86 there), and its
residual is exactly 0.00 -- so a test at the full span alone passes for an
implementation that has none of the three (amendment W1, W2). The narrow
windows are 2026-09-01 and 2026-09-12, both measured.

Isolated home on every class: these tests APPEND to a cash store through the
sweep, so an unisolated run would write into the operator's own book (#1696).
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import unittest
from dataclasses import replace
from typing import Any
from unittest import mock

from alphalens_pipeline.account_cash import (
    balance_check,
    buckets,
    episodes,
    money,
    paths,
    report,
    store,
    sweep,
)
from alphalens_pipeline.account_cash.codec import SweepRecord
from alphalens_pipeline.account_cash.money import BALANCE_TOLERANCE_ACCT
from alphalens_pipeline.brokers.account_cash import AccountCashRead, BalanceSnapshot
from alphalens_pipeline.brokers.fill_history import ReadWindow

from tests.account_cash.cashbook_fixture import (
    BALANCE,
    BOOKINGS,
    TRADES,
    synthetic_clipped_partial,
    synthetic_clipped_sell_only,
)
from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

_LIVE = "live"
_SIM = "sim"

#: The window the one seeded sweep ASKED for -- the whole capture.
_SWEPT_SINCE = dt.date(2026, 8, 1)
_SWEPT_UNTIL = dt.date(2026, 10, 3)
_SWEPT_AT = dt.datetime(2026, 10, 4, 0, 31, tzinfo=dt.UTC)

# --- Measured over the whole capture (104 bookings rows, 41 trades rows) ------

ALL_ROWS = 104
ALL_ROWS_TOTAL_PLN = 20738.68
SHARE_LEGS_PLN, SHARE_LEG_ROWS = -3110.06, 41
FEES_PLN, FEE_ROWS = -155.31, 58
INCOME_PLN, INCOME_ROWS = 4.05, 2
DEPOSITS_PLN, DEPOSIT_ROWS = 24000.00, 3
EPISODES = 19
CLOSED_EPISODES, NET_CLOSED_PLN = 16, 329.49
OPEN_EPISODES, COST_BASIS_OPEN_PLN = 3, -3594.86
FILLS = 41
#: The subtotal at the full span: 329.49 - 3594.86 + 4.05 + 0.00 + 0.00.
SUBTOTAL_PLN = -3261.32

# --- Measured over 2026-08-01..2026-09-12, the narrow window -----------------

CUT = dt.date(2026, 9, 12)
CUT_ROWS, CUT_ROWS_TOTAL_PLN = 64, 16172.84
CUT_SUMMED, CUT_NET_CLOSED_PLN = 9, 101.80
CUT_CLOSED_OUTSIDE = 7
CUT_RESIDUAL_PLN = -4334.10
CUT_SUBTOTAL_PLN = -7827.16
#: What a window-CLIPPED open cost basis would answer at the cut: only RHI,
#: opened 2026-09-02. The other two open positions were opened after it.
CUT_CLIPPED_COST_BASIS_PLN = -466.98

# --- The two closed vocabularies, written out ---------------------------------

#: Every reason an episode can be left out of the summed figure, in render
#: order. Written here rather than read back off ``REPORT_EXCLUSION_ORDER``:
#: ``_exclusions_of`` seeds its counts from that tuple, so asserting the
#: published keys against it is production against production.
EXCLUSION_VOCABULARY = ("window_clipped", "imbalanced", "open", "closed_outside_window")
#: Every reason a fill can go unassigned, for the same reason:
#: ``_unassigned_by_reason`` seeds its dict from ``UNASSIGNED_REASONS``.
UNASSIGNED_VOCABULARY = (
    "no_open_in_store",
    "quantity_not_read",
    "closes_nothing",
    "short_open_not_modelled",
)

# --- Measured over 2026-09-01..2026-10-03, the other narrow window -----------

LATE_SINCE = dt.date(2026, 9, 1)
LATE_ROWS, LATE_ROWS_TOTAL_PLN = 72, 11755.34
LATE_DEPOSITS_PLN, LATE_DEPOSIT_ROWS = 15000.00, 1
LATE_SUMMED, LATE_NET_CLOSED_PLN = 10, 346.15

# --- A second sweep whose balance body moved, for the cumulative check -------

#: How far the account's own cash moves between the two snapshots, with the
#: booking rows unchanged. Large enough that the two readings of "the latest
#: snapshot" answer different VERDICTS and not merely different digits.
BALANCE_MOVED_BY_PLN = 5000.00
MOVED_CASH_PLN = 25738.68
MOVED_ASOF = dt.datetime(2026, 10, 4, 6, 12, tzinfo=dt.UTC)

#: A second sweep's window, strictly AFTER the first one's and adjacent to it,
#: so the two leave no gap and the derived incremental interval runs forward.
#: It holds no booking row, which is what makes the interval's own direction --
#: and not its contents -- the subject.
SECOND_SINCE = dt.date(2026, 10, 4)
SECOND_UNTIL = dt.date(2026, 10, 6)

#: The span the 104 booking rows THEMSELVES cover. Not the window the sweep
#: asked for (2026-08-01..2026-10-03): the first booking row is five days
#: after the asked start and the last is two days before the asked end.
ROWS_FIRST_DATE = "2026-08-06"
ROWS_LAST_DATE = "2026-10-01"

# --- Two sweeps with a hole between them, the memo's own coverage example ----

#: 08-01..08-10 and 09-01..09-10 leave a 21-day hole, so ``swept_through`` is
#: the end of the FIRST run and ``gaps`` holds the hole. Every booking row is
#: stored under the first sweep, so the cumulative check sums all 104 of them
#: -- including the September money that is on the far side of the hole.
GAP_FIRST_UNTIL = dt.date(2026, 8, 10)
GAP_SECOND_SINCE = dt.date(2026, 9, 1)
GAP_SECOND_UNTIL = dt.date(2026, 9, 10)
GAP_HOLE = ("2026-08-11", "2026-08-31")


_BASE_READ = AccountCashRead(
    events=tuple(BOOKINGS),
    trades=tuple(TRADES),
    balance=BALANCE,
    events_window=ReadWindow(_SWEPT_SINCE.isoformat(), _SWEPT_UNTIL.isoformat(), ALL_ROWS),
    trades_window=ReadWindow(_SWEPT_SINCE.isoformat(), _SWEPT_UNTIL.isoformat(), FILLS),
    skipped_reason=None,
)


def _read(**overrides: Any) -> AccountCashRead:
    """What the adapter answered, with named fields changed."""
    return replace(_BASE_READ, **overrides)


def _seed(
    *,
    read: AccountCashRead | None = None,
    since: dt.date = _SWEPT_SINCE,
    until: dt.date = _SWEPT_UNTIL,
    now: dt.datetime = _SWEPT_AT,
) -> SweepRecord:
    """Store the whole capture under one sweep, through the real sweep path."""
    plan = sweep.plan_sweep(read or _read(), env=_LIVE, since=since, until=until)
    return sweep.apply_sweep(plan, now=now)


def _bucket(totals: report.CashBookTotals, name: str) -> buckets.Bucket:
    return next(bucket for bucket in totals.buckets if bucket.name == name)


class TheGoldenLiveWindow(IsolatedHomeTestCase):
    """Every published line at the full span, against hand-written literals.

    **Mutation it catches:** any wiring error between the two pure derivations
    and the published lines. It is NOT sufficient on its own, which is the
    point of every class below it: at this span the window scoping, the open
    cost basis's as-of and the residual are all indistinguishable from their
    absent versions.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed()
        self.totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

    def test_the_money_lines_are_the_measured_ones(self) -> None:
        self.assertEqual(self.totals.net_closed_acct.value, NET_CLOSED_PLN)
        self.assertEqual(self.totals.net_closed_acct.rows, CLOSED_EPISODES)
        self.assertEqual(self.totals.cost_basis_open_acct.value, COST_BASIS_OPEN_PLN)
        self.assertEqual(self.totals.cost_basis_open_acct.rows, OPEN_EPISODES)
        self.assertEqual(self.totals.income_acct.value, INCOME_PLN)
        self.assertEqual(self.totals.income_acct.rows, INCOME_ROWS)
        self.assertEqual(self.totals.unclassified_acct.value, 0.0)
        self.assertEqual(self.totals.unclassified_acct.rows, 0)
        self.assertEqual(self.totals.deposits_acct.value, DEPOSITS_PLN)
        self.assertEqual(self.totals.deposits_acct.rows, DEPOSIT_ROWS)
        self.assertEqual(self.totals.cash_book_sum_acct.value, ALL_ROWS_TOTAL_PLN)
        self.assertEqual(self.totals.cash_book_sum_acct.rows, ALL_ROWS)

    def test_every_published_line_carries_the_account_currency_on_the_number(self) -> None:
        """Memo section 5(d): the currency goes ON the number."""
        for name, line in sorted(self.totals.lines().items()):
            with self.subTest(line=name):
                self.assertEqual(line.currency, "PLN")
                self.assertTrue(line.rendered.endswith(" PLN"), line.rendered)

    def test_the_subtotal_excludes_the_deposits(self) -> None:
        """Memo section 6's first load-bearing property.

        **Mutation it catches:** folding the deposits into the subtotal, which
        turns 24 000.00 PLN of funding into something that reads as profit.
        Both sides are literals, so the identity below cannot absorb it.
        """
        self.assertEqual(self.totals.subtotal_acct.value, SUBTOTAL_PLN)
        self.assertEqual(
            self.totals.cash_book_sum_acct.value,
            round(SUBTOTAL_PLN + DEPOSITS_PLN, 2),
        )
        self.assertEqual(self.totals.subtotal_acct.rows, ALL_ROWS - DEPOSIT_ROWS)

    def test_the_buckets_are_published_in_order_with_the_empty_one_at_zero(self) -> None:
        """Memo section 5(a): ``unclassified`` prints even at zero, so a type
        nobody has seen reads as a CHANGED number rather than as a new row."""
        self.assertEqual(
            [bucket.name for bucket in self.totals.buckets], list(buckets.BUCKET_ORDER)
        )
        measured = {
            buckets.BUCKET_SHARE_LEGS: (SHARE_LEGS_PLN, SHARE_LEG_ROWS),
            buckets.BUCKET_FEES: (FEES_PLN, FEE_ROWS),
            buckets.BUCKET_INCOME: (INCOME_PLN, INCOME_ROWS),
            buckets.BUCKET_DEPOSITS: (DEPOSITS_PLN, DEPOSIT_ROWS),
            buckets.BUCKET_UNCLASSIFIED: (0.0, 0),
        }
        for name, (value, rows) in sorted(measured.items()):
            with self.subTest(bucket=name):
                self.assertEqual(_bucket(self.totals, name).total.value, value)
                self.assertEqual(_bucket(self.totals, name).rows, rows)

    def test_the_basis_states_the_window_it_was_asked_for(self) -> None:
        self.assertEqual(self.totals.basis.window_since, _SWEPT_SINCE.isoformat())
        self.assertEqual(self.totals.basis.window_until, _SWEPT_UNTIL.isoformat())
        self.assertEqual(self.totals.basis.currency, "PLN")
        self.assertEqual(self.totals.env, _LIVE)

    def test_the_coverage_watermarks_reach_the_published_total(self) -> None:
        self.assertEqual(self.totals.coverage.history_begins, _SWEPT_SINCE.isoformat())
        self.assertEqual(self.totals.coverage.swept_through, _SWEPT_UNTIL.isoformat())
        self.assertEqual(self.totals.coverage.gaps, ())
        self.assertEqual(self.totals.coverage.cannot_detect, (report.UNDETECTED_PARTIAL_ENTRY,))

    def test_the_whole_record_renders_as_one_strict_json_value(self) -> None:
        """``allow_nan=False`` is the CLI's rule; a body that cannot satisfy it
        turns a report into a refusal, so it is checked where the body is
        built rather than where it is printed."""
        body = json.dumps(self.totals.to_jsonable(), allow_nan=False, sort_keys=True)

        self.assertEqual(json.loads(body)["env"], _LIVE)
        self.assertEqual(json.loads(body)["cash_book_sum_acct"]["value_rendered"], "20738.68 PLN")


class TheWindowScopesTheCashEventsAndNotTheStore(IsolatedHomeTestCase):
    """Amendment W1: a report that states a window and publishes a store-wide
    total is a number that lies about its own basis.

    The whole capture is swept; the window asked for starts 2026-09-01. The
    store holds 104 rows worth +20 738.68 and three deposits worth
    +24 000.00; the window holds 72 rows worth +11 755.34 and ONE deposit
    worth +15 000.00. Every number below separates the two readings.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed()
        self.totals = report.build_totals(env=_LIVE, since=LATE_SINCE, until=_SWEPT_UNTIL)

    def test_the_cash_book_sum_is_the_windows_rows(self) -> None:
        self.assertEqual(self.totals.cash_book_sum_acct.value, LATE_ROWS_TOTAL_PLN)
        self.assertEqual(self.totals.cash_book_sum_acct.rows, LATE_ROWS)
        self.assertNotEqual(self.totals.cash_book_sum_acct.value, ALL_ROWS_TOTAL_PLN)

    def test_the_deposits_bucket_is_the_windows_deposits(self) -> None:
        self.assertEqual(self.totals.deposits_acct.value, LATE_DEPOSITS_PLN)
        self.assertEqual(self.totals.deposits_acct.rows, LATE_DEPOSIT_ROWS)

    def test_the_row_partition_accounts_for_every_row_in_the_store(self) -> None:
        """The third identity: a row outside the window did not vanish."""
        basis = self.totals.basis
        self.assertEqual(basis.rows_in_store, ALL_ROWS)
        self.assertEqual(basis.rows_read, LATE_ROWS)
        self.assertEqual(basis.rows_outside_window, ALL_ROWS - LATE_ROWS)
        self.assertEqual(basis.rows_undated, 0)
        self.assertEqual(
            basis.rows_read + basis.rows_outside_window + basis.rows_undated,
            basis.rows_in_store,
        )

    def test_the_episodes_summed_are_the_ones_that_closed_in_the_window(self) -> None:
        self.assertEqual(self.totals.basis.episodes_summed, LATE_SUMMED)
        self.assertEqual(self.totals.net_closed_acct.value, LATE_NET_CLOSED_PLN)


class TheNarrowWindowAtTheCut(IsolatedHomeTestCase):
    """2026-08-01..2026-09-12, the window amendment W1 and W2 both name.

    Here the residual is -4 334.10 rather than the 0.00 the full span answers,
    seven closed episodes fall outside the window, and a window-clipped open
    cost basis would answer -466.98 against the -3 594.86 that is actually at
    risk.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed()
        self.totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=CUT)

    def test_the_window_filters_episodes_by_their_closing_date(self) -> None:
        """**Mutation it catches:** filtering the ROWS before
        ``derive_episodes`` runs, which clips every episode straddling the
        window edge and reports a real store incompleteness where there is
        none. That is why the derivation takes no window."""
        basis = self.totals.basis

        self.assertEqual(basis.episodes, EPISODES)
        self.assertEqual(basis.episodes_summed, CUT_SUMMED)
        self.assertEqual(
            basis.exclusions[report.EXCLUSION_CLOSED_OUTSIDE_WINDOW], CUT_CLOSED_OUTSIDE
        )
        self.assertEqual(basis.exclusions[episodes.STATE_OPEN], OPEN_EPISODES)
        self.assertEqual(basis.exclusions[episodes.STATE_WINDOW_CLIPPED], 0)
        self.assertEqual(self.totals.net_closed_acct.value, CUT_NET_CLOSED_PLN)

    def test_the_exclusion_counts_add_up_to_the_excluded_episodes(self) -> None:
        """**Mutation it catches:** an episode stamped with several reasons,
        which makes counts-by-reason over-add. ``EXCLUSION_ORDER``'s first-hit
        rule is what keeps them addable, and the fourth reason exists so the
        window does not break the identity.

        The load-bearing lines are the two literals: ``sum(exclusions) ==
        episodes_excluded`` cannot fail, because ``build_totals`` sets the
        count to that sum.
        """
        basis = self.totals.basis

        self.assertEqual(basis.episodes_summed + basis.episodes_excluded, basis.episodes)
        self.assertEqual(basis.episodes_excluded, CUT_CLOSED_OUTSIDE + OPEN_EPISODES)
        self.assertEqual(sorted(basis.exclusions), sorted(EXCLUSION_VOCABULARY))

    def test_the_four_exclusion_reasons_are_the_published_vocabulary(self) -> None:
        """Written out rather than read back off ``REPORT_EXCLUSION_ORDER``.

        ``_exclusions_of`` seeds its counts from that very tuple, so comparing
        the published keys against it is production against production: the
        mutation that dropped the fourth reason from the tuple passed that
        comparison.
        """
        self.assertEqual(report.REPORT_EXCLUSION_ORDER, EXCLUSION_VOCABULARY)
        self.assertEqual(report.EXCLUSION_CLOSED_OUTSIDE_WINDOW, "closed_outside_window")

    def test_the_open_cost_basis_keeps_the_full_cost_of_every_open_episode(self) -> None:
        """Amendment W1: the cost of a still-open position is as-of NOW, not
        as-of the window's end, so the line is not clipped.

        **Mutation it catches:** filtering the open episodes by their OPENING
        date, which answers -466.98 -- the one position opened before the cut
        -- and reads as the whole exposure.
        """
        self.assertEqual(self.totals.cost_basis_open_acct.value, COST_BASIS_OPEN_PLN)
        self.assertEqual(self.totals.cost_basis_open_acct.rows, OPEN_EPISODES)
        self.assertNotEqual(self.totals.cost_basis_open_acct.value, CUT_CLIPPED_COST_BASIS_PLN)

    def test_the_open_cost_basis_publishes_its_own_as_of_which_is_not_the_window_end(
        self,
    ) -> None:
        """Without the as-of on the line, a full basis inside a narrow window
        reads as a windowed figure. The as-of is the store's own watermark,
        not a clock: ``swept_through``, which here is 2026-10-03 against a
        window ending 2026-09-12."""
        self.assertEqual(self.totals.cost_basis_open_asof, _SWEPT_UNTIL.isoformat())
        self.assertNotEqual(self.totals.cost_basis_open_asof, self.totals.basis.window_until)

    def test_the_residual_is_published_and_is_not_zero_here(self) -> None:
        """Amendment W2. Memo section 4: episodes are summable and their sum
        is NOT the period total; the difference is real money -- here the
        entry legs of the seven episodes that closed after the cut.

        **Mutation it catches:** omitting the line entirely, which the full
        span cannot see because its residual is exactly 0.00.
        """
        self.assertEqual(self.totals.residual_acct.value, CUT_RESIDUAL_PLN)
        self.assertEqual(self.totals.residual_acct.rows, CUT_ROWS)
        self.assertEqual(self.totals.cash_book_sum_acct.value, CUT_ROWS_TOTAL_PLN)
        self.assertEqual(self.totals.cash_book_sum_acct.rows, CUT_ROWS)

    def test_the_subtotal_is_the_five_named_lines(self) -> None:
        self.assertEqual(self.totals.subtotal_acct.value, CUT_SUBTOTAL_PLN)
        self.assertEqual(
            self.totals.cash_book_sum_acct.value,
            round(CUT_SUBTOTAL_PLN + DEPOSITS_PLN, 2),
        )


# --- The window's own two edges, each on a day the capture is not empty on ---

#: The capture's LAST booking date, and the day VST's episode closed. Every
#: window tested above ends on an empty day -- 2026-10-03, 2026-09-12 and
#: 2026-08-05 carry no booking row and no episode close -- so none of them can
#: tell ``stamped <= until`` from ``stamped < until``.
LAST_BOOKED_DAY = dt.date(2026, 10, 1)
#: What that day holds: 7 booking rows worth +1 967.73 and VST's +84.25.
LAST_DAY_ROWS, LAST_DAY_TOTAL_PLN = 7, 1967.73
#: What an EXCLUSIVE right edge would answer at that window instead.
WITHOUT_LAST_DAY_ROWS, WITHOUT_LAST_DAY_TOTAL_PLN = 97, 18770.95
WITHOUT_VST_SUMMED, WITHOUT_VST_NET_CLOSED_PLN = 15, 245.24

#: AMBA's episode opened and closed on 2026-09-04, and the day carries 5
#: booking rows worth +86.47 -- its two share legs, its fees and its income.
#: The late window above starts 2026-09-01, an empty day, so it cannot tell
#: ``since <= stamped`` from ``since < stamped`` either.
FIRST_BOOKED_DAY = dt.date(2026, 9, 4)
FROM_AMBA_ROWS, FROM_AMBA_TOTAL_PLN = 67, -2108.19
FROM_AMBA_SUMMED, FROM_AMBA_NET_CLOSED_PLN = 10, 346.15
#: What an EXCLUSIVE left edge would answer at that window instead.
WITHOUT_AMBA_ROWS, WITHOUT_AMBA_TOTAL_PLN = 62, -2194.66
WITHOUT_AMBA_SUMMED, WITHOUT_AMBA_NET_CLOSED_PLN = 9, 259.68


class TheWindowsLastDayIsInsideIt(IsolatedHomeTestCase):
    """2026-08-01..2026-10-01, a window ending on a day that books rows.

    Both edges of both windows are inclusive, and until now nothing said so:
    every ``until`` any other class asks for falls on an empty day, so an
    exclusive right edge silently dropped nothing and stayed green. Here it
    would drop 7 booking rows worth +1 967.73 and VST's closed episode.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed()
        self.totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=LAST_BOOKED_DAY)

    def test_the_premise_is_that_the_last_day_of_this_window_books_rows(self) -> None:
        """Stated rather than assumed: on an empty day the two tests below
        assert the same thing as the golden window and prove nothing."""
        booked = [event for event in BOOKINGS if event.date == LAST_BOOKED_DAY.isoformat()]

        self.assertEqual(len(booked), LAST_DAY_ROWS)
        self.assertEqual(max(event.date or "" for event in BOOKINGS), LAST_BOOKED_DAY.isoformat())

    def test_a_row_booked_on_the_windows_last_day_is_inside_the_cash_book_sum(self) -> None:
        """**Mutation it catches:** ``since <= stamped < until``. Measured
        2026-10-26: green before this test, and 1 967.73 PLN of the cash book
        sum short."""
        self.assertEqual(self.totals.cash_book_sum_acct.rows, ALL_ROWS)
        self.assertEqual(self.totals.cash_book_sum_acct.value, ALL_ROWS_TOTAL_PLN)
        self.assertEqual(self.totals.basis.rows_outside_window, 0)
        self.assertNotEqual(self.totals.cash_book_sum_acct.rows, WITHOUT_LAST_DAY_ROWS)
        self.assertNotEqual(self.totals.cash_book_sum_acct.value, WITHOUT_LAST_DAY_TOTAL_PLN)

    def test_an_episode_closing_on_the_windows_last_day_is_summed(self) -> None:
        """The episode edge is a second claim and gets its own case: VST
        closed 2026-10-01 for +84.25.

        **Mutation it catches:** ``since <= closed < until`` in the exclusion
        walk, which answers 15 episodes and +245.24.
        """
        self.assertEqual(self.totals.basis.episodes_summed, CLOSED_EPISODES)
        self.assertEqual(self.totals.net_closed_acct.value, NET_CLOSED_PLN)
        self.assertNotEqual(self.totals.basis.episodes_summed, WITHOUT_VST_SUMMED)
        self.assertNotEqual(self.totals.net_closed_acct.value, WITHOUT_VST_NET_CLOSED_PLN)


class TheWindowsFirstDayIsInsideIt(IsolatedHomeTestCase):
    """2026-09-04..2026-10-03, a window starting on a day that books rows.

    The other edge, and the same hole: the narrow windows above all START on
    an empty day, so an exclusive left edge was green too.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed()
        self.totals = report.build_totals(env=_LIVE, since=FIRST_BOOKED_DAY, until=_SWEPT_UNTIL)

    def test_the_premise_is_that_the_first_day_of_this_window_books_rows(self) -> None:
        booked = [event for event in BOOKINGS if event.date == FIRST_BOOKED_DAY.isoformat()]

        self.assertEqual(len(booked), FROM_AMBA_ROWS - WITHOUT_AMBA_ROWS)
        self.assertEqual(len(booked), 5)

    def test_a_row_booked_on_the_windows_first_day_is_inside_the_cash_book_sum(self) -> None:
        """**Mutation it catches:** ``since < stamped <= until``, which drops
        AMBA's five rows and 86.47 PLN with them."""
        self.assertEqual(self.totals.cash_book_sum_acct.rows, FROM_AMBA_ROWS)
        self.assertEqual(self.totals.cash_book_sum_acct.value, FROM_AMBA_TOTAL_PLN)
        self.assertNotEqual(self.totals.cash_book_sum_acct.rows, WITHOUT_AMBA_ROWS)
        self.assertNotEqual(self.totals.cash_book_sum_acct.value, WITHOUT_AMBA_TOTAL_PLN)

    def test_an_episode_closing_on_the_windows_first_day_is_summed(self) -> None:
        """AMBA opened and closed 2026-09-04 for +86.47.

        **Mutation it catches:** ``since < closed <= until`` in the exclusion
        walk, which answers 9 episodes and +259.68. With this case and the one
        above, all three readings of the W1 episode window -- ``[since,
        until]``, ``[since, until)`` and ``(since, until]`` -- are told apart.
        """
        self.assertEqual(self.totals.basis.episodes_summed, FROM_AMBA_SUMMED)
        self.assertEqual(self.totals.net_closed_acct.value, FROM_AMBA_NET_CLOSED_PLN)
        self.assertNotEqual(self.totals.basis.episodes_summed, WITHOUT_AMBA_SUMMED)
        self.assertNotEqual(self.totals.net_closed_acct.value, WITHOUT_AMBA_NET_CLOSED_PLN)


#: 2026-08-01..2026-08-05: inside the swept range and BEFORE the first booking
#: row, which the capture stamps 2026-08-06. Reachable on any account with a
#: quiet stretch, so it is not an edge case.
EMPTY_UNTIL = dt.date(2026, 8, 5)


class AnEmptyWindowInsideTheSweptRange(IsolatedHomeTestCase):
    """A window the store covers and that holds no booking row.

    The buckets can only read the account's currency off the rows they are
    handed (``classify_buckets``' own docstring says naming it on an empty
    window is the CALLER's job), so every bucket total arrives as a unit-less
    zero. Three things then went wrong at once, and all three are measured:
    the bucket identity RAISED, because a pair carrying no currency nulls its
    sum with ``account_amount_not_reported`` while ``all_rows`` is ``0.00``;
    the residual and the subtotal would have been nulls for the same reason;
    and the published lines printed ``0.00`` with no unit against memo section
    5(d)'s rule that the currency goes ON the number.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed()
        self.totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=EMPTY_UNTIL)

    def test_the_premise_is_that_the_window_really_is_empty(self) -> None:
        self.assertEqual(self.totals.basis.rows_read, 0)
        self.assertEqual(self.totals.basis.rows_outside_window, ALL_ROWS)
        self.assertEqual(min(event.date or "" for event in BOOKINGS), "2026-08-06")

    def test_it_answers_rather_than_raising_a_broken_identity(self) -> None:
        """**Mutation it catches:** the ``CashBookIdentityError`` this window
        raised before the fix -- an empty window is answerable, and a
        partition that cannot hold over zero rows is a defect in the
        partition, not an incomplete number."""
        self.assertEqual(self.totals.cash_book_sum_acct.value, 0.0)
        self.assertEqual(self.totals.residual_acct.value, -COST_BASIS_OPEN_PLN)
        self.assertEqual(self.totals.subtotal_acct.value, 0.0)

    def test_every_number_still_carries_the_account_currency(self) -> None:
        """Memo section 5(d). The unit comes from the store's own rows, which
        the report reads even when the window holds none of them."""
        self.assertEqual(self.totals.basis.currency, "PLN")
        for name, line in sorted(self.totals.lines().items()):
            with self.subTest(line=name):
                self.assertEqual(line.currency, "PLN")
                self.assertTrue(line.rendered.endswith(" PLN"), line.rendered)
        for bucket in self.totals.buckets:
            with self.subTest(bucket=bucket.name):
                self.assertEqual(bucket.total.currency, "PLN")
                self.assertEqual(bucket.total.value, 0.0)

    def test_the_open_cost_basis_is_not_emptied_by_an_empty_window(self) -> None:
        """It is never window-scoped, so a quiet stretch does not hide the
        money at risk -- it shows up in the residual instead."""
        self.assertEqual(self.totals.cost_basis_open_acct.value, COST_BASIS_OPEN_PLN)
        self.assertEqual(self.totals.cost_basis_open_acct.rows, OPEN_EPISODES)

    def test_the_cumulative_check_still_reconciles_because_it_is_store_wide(self) -> None:
        check = next(
            one for one in self.totals.balance_checks if one.kind == balance_check.CHECK_CUMULATIVE
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertEqual(check.rows, ALL_ROWS)


class TheIdentitiesAreCheckedAndNotDescribed(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        _seed()

    def test_the_coverage_partition_holds_over_the_real_fills(self) -> None:
        basis = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL).basis

        self.assertEqual(basis.fills_read, FILLS)
        self.assertEqual(basis.fills_assigned, FILLS)
        self.assertEqual(sum(basis.fills_unassigned.values()), 0)
        self.assertEqual(
            basis.fills_assigned + sum(basis.fills_unassigned.values()), basis.fills_read
        )
        self.assertEqual(sorted(basis.fills_unassigned), sorted(UNASSIGNED_VOCABULARY))
        self.assertEqual(episodes.UNASSIGNED_REASONS, UNASSIGNED_VOCABULARY)

    def test_the_coverage_partition_holds_when_something_is_unassigned(self) -> None:
        """The real capture leaves 0 fills unassigned, so every count in
        ``basis.fills_unassigned`` is zero there -- a fixture uniform in the
        column under test cannot tell a read from a constant.

        The two synthetic clip cases add one fill each that no episode can
        hold: a lone sell (nothing to close) and a sell larger than its buy.

        **Mutation it catches:** ``_unassigned_by_reason`` returning its
        all-zero template without counting, which is invisible on the real
        rows and would publish "nothing unaccounted for" over two fills that
        are.
        """
        _seed(
            read=_read(
                trades=(
                    *TRADES,
                    *synthetic_clipped_sell_only(),
                    *synthetic_clipped_partial(),
                )
            ),
            now=_SWEPT_AT + dt.timedelta(days=1),
        )

        basis = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL).basis

        self.assertEqual(basis.fills_read, FILLS + 3)
        self.assertEqual(basis.fills_unassigned[episodes.UNASSIGNED_NO_OPEN], 2)
        self.assertEqual(sum(basis.fills_unassigned.values()), 2)
        self.assertEqual(
            basis.fills_assigned + sum(basis.fills_unassigned.values()), basis.fills_read
        )
        self.assertEqual(basis.exclusions[episodes.STATE_WINDOW_CLIPPED], 1)

    def test_the_bucket_identity_holds_over_the_windows_rows(self) -> None:
        totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=CUT)

        summed = sum(bucket.total.value or 0.0 for bucket in totals.buckets)
        self.assertAlmostEqual(summed, totals.cash_book_sum_acct.value or 0.0, places=6)
        self.assertEqual(len(totals.buckets), len(buckets.BUCKET_ORDER))
        self.assertGreaterEqual(
            sum(1 for bucket in totals.buckets if (bucket.total.value or 0.0) != 0.0), 3
        )

    def test_a_broken_bucket_identity_refuses_rather_than_publishing_a_number(self) -> None:
        """The positive control, and the reason the check is a raise.

        A dropped row would be missing from both sides of a bucket sum
        computed FROM the buckets, so the identity only has power while
        ``all_rows`` is summed over the rows. Here ``all_rows`` is doctored
        away from the buckets it must agree with, which is the observable
        shape of a row the classification lost.
        """
        real = buckets.classify_buckets

        def doctored(rows: object) -> buckets.BucketFold:
            fold = real(rows)  # type: ignore[arg-type]
            return replace(fold, all_rows=replace(fold.all_rows, value=1.0))

        with mock.patch.object(buckets, "classify_buckets", doctored):
            with self.assertRaises(report.CashBookIdentityError) as ctx:
                report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertIn("bucket", str(ctx.exception))

    def _with_all_rows_shifted(self, by: float) -> None:
        """Run the report with ``all_rows`` moved ``by`` away from the buckets.

        The observable shape of a row the classification lost: the buckets
        still add to what they were given, and the total they must agree with
        no longer does.
        """
        real = buckets.classify_buckets

        def doctored(rows: object) -> buckets.BucketFold:
            fold = real(rows)  # type: ignore[arg-type]
            shifted = (fold.all_rows.value or 0.0) + by
            return replace(fold, all_rows=replace(fold.all_rows, value=shifted))

        with mock.patch.object(buckets, "classify_buckets", doctored):
            report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

    def test_half_a_grosz_of_drift_is_already_a_broken_bucket_identity(self) -> None:
        """``_IDENTITY_EPSILON_ACCT``'s own claim, which nothing else pins.

        It is an ARITHMETIC epsilon -- the residual between a ``Decimal``
        accumulation and its ``float`` publication, measured below 1e-9 on the
        real 104 rows -- and NOT the one-grosz width of agreement between two
        independently computed money figures. Its docstring says using the
        grosz here would let a dropped half-grosz row satisfy an identity, so
        both halves of that sentence are asserted: a drift of 0.005 and a
        drift of exactly ``BALANCE_TOLERANCE_ACCT`` must each still raise.

        **Mutation it catches:** the epsilon widened to the grosz, and to 1.0,
        and to 1000.0. Measured 2026-10-26: all three were green before this
        test, because the only case with any power doctored ``all_rows`` to
        1.00 against a real 20 738.68 and so fired only past about 20 737.
        """
        for drift in (0.005, BALANCE_TOLERANCE_ACCT):
            with self.subTest(drift=drift):
                with self.assertRaises(report.CashBookIdentityError):
                    self._with_all_rows_shifted(drift)

    def test_the_float_residual_the_epsilon_exists_for_still_holds(self) -> None:
        """The other direction, which makes the epsilon two claims rather than
        one: a drift below it is the ``Decimal``-to-``float`` residual the
        constant was measured for, and it must NOT raise.

        1e-10 is 28 units in the last place at 20 738.68, so the shifted total
        really is a different float -- a drift of 1e-12 is not representable
        there and would make this test vacuous.

        **Mutation it catches:** the epsilon narrowed to 0.0, which turns
        every report into a refusal the first time a sum lands a hair off.
        """
        self.assertNotEqual(ALL_ROWS_TOTAL_PLN + 1e-10, ALL_ROWS_TOTAL_PLN)

        self._with_all_rows_shifted(1e-10)

    def test_a_null_on_only_one_side_of_the_bucket_identity_still_refuses(self) -> None:
        """The bound on the relaxation that lets two nulls agree.

        A null on BOTH sides is unevaluable and holds, because no total is
        published on that line either. A null on exactly ONE side is a signal
        and must still raise: drop the one row whose amount the vendor did not
        report and the per-bucket side becomes a number while the rows' own
        total stays a named absence.

        **Mutation it catches:** relaxing ``_agree`` all the way to "a null
        anywhere holds", which would let that dropped row through.
        """
        real = buckets.classify_buckets

        def doctored(rows: object) -> buckets.BucketFold:
            fold = real(rows)  # type: ignore[arg-type]
            absent = money.acct_null(
                money.NULL_ACCT_AMOUNT_NOT_REPORTED, "PLN", rows=fold.all_rows.rows
            )
            return replace(fold, all_rows=absent)

        with mock.patch.object(buckets, "classify_buckets", doctored):
            with self.assertRaises(report.CashBookIdentityError) as ctx:
                report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertIn("bucket", str(ctx.exception))

    def test_a_row_the_FOLD_lost_breaks_the_row_partition(self) -> None:
        """F3. The row partition's right-hand side comes from a DIFFERENT path
        than the list its left-hand side splits.

        It used to compare ``inside + outside + undated`` against
        ``len(events_fold.events)`` -- a partition of one list against that
        same list's length. A row lost in the READ is missing from both sides
        at once, so the identity held and the report published a 103-row total
        for a 104-line journal. It is now compared against the journal's own
        arithmetic, ``lines - malformed - duplicate_lines``, which is
        incremented while the file is read.

        Doctored here the way a silent ``continue`` in a fold looks: one
        record gone from the list, every counter untouched.

        **Mutation it catches:** the tautology. Measured 2026-10-26: green
        before this change, with no raise and a published total short one row.
        """
        real = store.read_cash_events

        def doctored(*, env: str) -> store.CashEventFold:
            fold = real(env=env)
            return replace(fold, events=fold.events[1:])

        with mock.patch.object(store, "read_cash_events", doctored):
            with self.assertRaises(report.CashBookIdentityError) as ctx:
                report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertIn("row", str(ctx.exception))
        self.assertIn(str(ALL_ROWS), str(ctx.exception))

    def test_a_row_the_window_split_drops_breaks_it_too(self) -> None:
        """The other half of the same partition, and the half that already
        worked: a row counted in none of the three parts.

        Both halves are asserted because the fix moved only the right-hand
        side, and a right-hand side that no longer tracks the rows at all
        would satisfy the test above while losing this one.
        """
        real = report._split_by_window

        def doctored(events: object, **kwargs: object) -> object:
            window = real(events, **kwargs)  # type: ignore[arg-type]
            return replace(window, outside=window.outside - 1)

        with mock.patch.object(report, "_split_by_window", doctored):
            with self.assertRaises(report.CashBookIdentityError) as ctx:
                report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=CUT)

        self.assertIn("row", str(ctx.exception))

    def test_a_row_the_CLASSIFICATION_lost_breaks_the_bucket_row_count(self) -> None:
        """F4. The fourth partition compares the per-bucket row counts against
        the fold's own ``rows`` counter.

        It used to compare ``bucket_fold.rows`` against the length of the list
        ``classify_buckets`` was handed -- which is what ``classify_buckets``
        sets ``rows`` FROM, so nothing could refute it. That matters beyond
        the count: ``_agree``'s docstring names this partition as what keeps a
        lost row caught while BOTH sides of the bucket identity are nulls, and
        ordinary data reaches that state (one booking row with an unreported
        amount nulls its bucket and the row total together). The compensating
        control was the tautology.

        Doctored here the way a row dropped from the classification looks: one
        bucket a row short, the fold's counter unchanged.

        **Mutation it catches:** the tautology. Measured 2026-10-26: green
        before this change.
        """
        real = buckets.classify_buckets

        def doctored(rows: object) -> buckets.BucketFold:
            fold = real(rows)  # type: ignore[arg-type]
            first, *rest = fold.buckets
            return replace(fold, buckets=(replace(first, rows=first.rows - 1), *rest))

        with mock.patch.object(buckets, "classify_buckets", doctored):
            with self.assertRaises(report.CashBookIdentityError) as ctx:
                report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertIn("bucket row count", str(ctx.exception))

    def test_a_broken_coverage_partition_refuses_too(self) -> None:
        """The second positive control: one identity raising does not show
        that the other one is wired at all."""
        real = episodes.derive_episodes

        def doctored(rows: object) -> episodes.EpisodeFold:
            return replace(real(rows), fills_assigned=0)  # type: ignore[arg-type]

        with mock.patch.object(episodes, "derive_episodes", doctored):
            with self.assertRaises(report.CashBookIdentityError) as ctx:
                report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertIn("fill", str(ctx.exception))


class TheTwoBalanceChecksReachThePublishedTotal(IsolatedHomeTestCase):
    """Both versions ship on every total, and the weaker one says so.

    The arithmetic of each is ``test_balance_check.py``'s subject; what these
    assert is that the report carries both, which half is which, and that the
    cumulative one's basis is its OWN and not the window's (amendment W1).
    """

    def setUp(self) -> None:
        super().setUp()
        _seed()
        self.totals = report.build_totals(env=_LIVE, since=LATE_SINCE, until=_SWEPT_UNTIL)
        self.checks = {check.kind: check for check in self.totals.balance_checks}

    def test_both_kinds_are_published_in_order(self) -> None:
        self.assertEqual(
            [check.kind for check in self.totals.balance_checks],
            [balance_check.CHECK_CUMULATIVE, balance_check.CHECK_INCREMENTAL],
        )

    def test_the_cumulative_check_reconciles_against_the_accounts_own_balance(self) -> None:
        """Both sides real: the 104-row store-wide sum and the committed
        ``CashBalance`` of 20 738.68 (memo section 1.6 P1)."""
        check = self.checks[balance_check.CHECK_CUMULATIVE]

        self.assertEqual(check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertEqual(check.expected.value, ALL_ROWS_TOTAL_PLN)
        self.assertEqual(check.observed.value, ALL_ROWS_TOTAL_PLN)
        self.assertIs(check.detects_restatement, False)

    def test_the_cumulative_check_carries_its_own_basis_and_not_the_windows(self) -> None:
        """Amendment W1. The window asked for starts 2026-09-01 and holds 72
        rows worth +11 755.34; this check is store-wide and says so.

        **Mutation it catches:** handing it the window's rows, which turns a
        store-wide identity into a windowed one that cannot hold -- the
        balance reflects every row, not the window's.
        """
        check = self.checks[balance_check.CHECK_CUMULATIVE]

        self.assertEqual(check.rows, ALL_ROWS)
        # Literals, not the coverage block or the rows they were read from:
        # comparing the check's interval against ``totals.coverage`` is
        # production against production, and both watermark mutations were
        # caught only by the two assertNotEqual lines below it.
        self.assertEqual(check.since, ROWS_FIRST_DATE)
        self.assertEqual(check.until, ROWS_LAST_DATE)
        self.assertNotEqual(check.since, self.totals.basis.window_since)
        self.assertNotEqual(check.rows, self.totals.basis.rows_read)

    def test_the_cumulative_check_reads_the_LATEST_snapshot_and_not_the_first(self) -> None:
        """Which snapshot is "the account's own cash" is a 5 000.00 PLN
        question, and one sweep cannot ask it.

        A second sweep stores a balance body whose ``CashBalance`` has moved
        +5 000.00 while the booking rows are unchanged, so the two readings
        answer different words: against the LATER snapshot the cash book is
        5 000.00 short and the check says ``differs``, against the first one
        it says ``reconciles``.

        **Mutation it catches:** ``balances.snapshots[0]``. Measured
        2026-10-26: green before this test, because every other case that
        asserts a cumulative verdict seeds ONE sweep -- where the first and
        the last snapshot are the same record -- and the one two-sweep case
        stores the same balance body twice and asserts only the incremental
        half.
        """
        _seed(
            read=_read(balance=replace(BALANCE, cash_balance=MOVED_CASH_PLN, asof=MOVED_ASOF)),
            now=_SWEPT_AT + dt.timedelta(days=1),
        )

        totals = report.build_totals(env=_LIVE, since=LATE_SINCE, until=_SWEPT_UNTIL)
        check = next(
            one for one in totals.balance_checks if one.kind == balance_check.CHECK_CUMULATIVE
        )

        self.assertEqual(check.observed.value, MOVED_CASH_PLN)
        self.assertEqual(check.asof, MOVED_ASOF.isoformat())
        self.assertEqual(check.expected.value, ALL_ROWS_TOTAL_PLN)
        self.assertEqual(check.verdict, balance_check.VERDICT_DIFFERS)
        self.assertAlmostEqual(check.difference.value or 0.0, -BALANCE_MOVED_BY_PLN, places=9)
        self.assertNotEqual(check.observed.value, ALL_ROWS_TOTAL_PLN)
        self.assertNotEqual(check.asof, BALANCE.asof.isoformat())

    def test_one_sweep_leaves_the_incremental_check_not_checkable(self) -> None:
        """A store with one sweep has one snapshot, and a change needs two."""
        check = self.checks[balance_check.CHECK_INCREMENTAL]

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_ONE_SNAPSHOT_ONLY)
        self.assertIs(check.detects_restatement, True)

    def test_a_second_sweep_over_a_LATER_window_makes_the_check_answerable(self) -> None:
        """The negative control for the test above: without it, an
        implementation that always answers ``not_checkable`` passes.

        The second sweep asks for a LATER window (2026-10-04..2026-10-06), so
        the derived interval really is one: ``[2026-10-04, 2026-10-06]``. Until
        2026-10-26 this case seeded the second sweep over the SAME window as
        the first and asserted only that ``since`` was not ``None`` -- which an
        interval running BACKWARDS satisfies, and that is exactly what the
        same-window shape publishes (F1). The direction is asserted here, and
        the same-window shape has its own case below.
        """
        _seed(since=SECOND_SINCE, until=SECOND_UNTIL, now=_SWEPT_AT + dt.timedelta(days=3))

        totals = report.build_totals(env=_LIVE, since=LATE_SINCE, until=_SWEPT_UNTIL)
        check = next(
            one for one in totals.balance_checks if one.kind == balance_check.CHECK_INCREMENTAL
        )

        self.assertIsNone(check.null_reason)
        self.assertEqual(check.since, SECOND_SINCE.isoformat())
        self.assertEqual(check.until, SECOND_UNTIL.isoformat())
        self.assertLessEqual(check.since or "", check.until or "")

    def test_a_second_sweep_over_the_SAME_window_forms_no_interval(self) -> None:
        """The operator action the store sees most often after a failed run:
        the same window swept again.

        The interval is ``[until + 1 day, until]``, which runs backwards and
        holds no row, so the check refuses by name instead of comparing
        ``0.00`` against whatever the balance did (F1).

        **Mutation it catches:** dropping the order guard, which publishes a
        verdict over 0 rows with ``since`` one day past ``until``.
        """
        _seed(since=_SWEPT_SINCE, until=_SWEPT_UNTIL, now=_SWEPT_AT + dt.timedelta(days=1))

        totals = report.build_totals(env=_LIVE, since=LATE_SINCE, until=_SWEPT_UNTIL)
        check = next(
            one for one in totals.balance_checks if one.kind == balance_check.CHECK_INCREMENTAL
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)
        self.assertEqual(check.null_reason, balance_check.NULL_INTERVAL_RUNS_BACKWARDS)
        self.assertIsNone(check.since)
        self.assertIsNone(check.until)

    def test_the_checks_survive_the_strict_json_render(self) -> None:
        body = json.loads(json.dumps(self.totals.to_jsonable(), allow_nan=False, sort_keys=True))

        self.assertEqual(
            [check["kind"] for check in body["balance_checks"]],
            [balance_check.CHECK_CUMULATIVE, balance_check.CHECK_INCREMENTAL],
        )
        self.assertIs(body["balance_checks"][0]["detects_restatement"], False)


class TheCoverageWatermarkAndTheCumulativeBasisAreTwoDifferentFacts(IsolatedHomeTestCase):
    """A hole in the coverage, which no test covered until now (F5).

    ``coverage_of`` deliberately stops at the end of the FIRST contiguous run,
    so a hole cannot be claimed as covered. The cumulative check is handed
    EVERY stored row. Those are two different facts, and the check used to
    publish the watermark as its own basis -- a row count covering everything
    under an ``until`` of 2026-08-10, with September money inside the number.

    Clipping the SUM to the first run instead would make the verdict wrong,
    which is worse: the balance snapshot reflects every row the account
    booked, not the ones before the hole.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed(since=_SWEPT_SINCE, until=GAP_FIRST_UNTIL)
        _seed(
            since=GAP_SECOND_SINCE,
            until=GAP_SECOND_UNTIL,
            now=_SWEPT_AT + dt.timedelta(days=1),
        )
        self.totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)
        self.check = next(
            one for one in self.totals.balance_checks if one.kind == balance_check.CHECK_CUMULATIVE
        )

    def test_the_premise_is_that_the_coverage_really_has_a_hole(self) -> None:
        """Stated rather than assumed: with no hole the two readings of the
        basis answer the same thing and the test below proves nothing."""
        self.assertEqual(self.totals.coverage.swept_through, GAP_FIRST_UNTIL.isoformat())
        self.assertEqual(self.totals.coverage.gaps, (GAP_HOLE,))
        self.assertEqual(self.totals.coverage.sweeps, 2)

    def test_the_cumulative_check_states_the_span_of_the_rows_it_summed(self) -> None:
        """**Mutation it catches:** publishing ``coverage.swept_through`` as
        the ``until``, which stops at 2026-08-10 while the number beside it
        rests on rows booked through 2026-10-01.
        """
        self.assertEqual(self.check.rows, ALL_ROWS)
        self.assertEqual(self.check.until, ROWS_LAST_DATE)
        self.assertNotEqual(self.check.until, self.totals.coverage.swept_through)
        self.assertEqual(self.check.since, ROWS_FIRST_DATE)

    def test_the_verdict_still_rests_on_every_stored_row(self) -> None:
        """The other half, and why the remedy is not to clip the sum: the
        balance body reflects all 104 rows, so a sum stopping at the hole
        would answer ``differs`` on an account that reconciles."""
        self.assertEqual(self.check.verdict, balance_check.VERDICT_RECONCILES)
        self.assertEqual(self.check.expected.value, ALL_ROWS_TOTAL_PLN)

    def test_the_coverage_block_still_refuses_to_claim_the_hole(self) -> None:
        """The watermark is NOT relaxed to match: it answers what the store
        can speak for, which is a different question from what the check
        summed."""
        self.assertEqual(self.totals.cost_basis_open_asof, GAP_FIRST_UNTIL.isoformat())


class TheBasisReportsWhatTheReadCouldNotAccountFor(IsolatedHomeTestCase):
    def test_the_basis_carries_a_malformed_counter_per_readable_journal(self) -> None:
        """**Mutation it catches:** publishing only the two keys
        ``SweepRecord.malformed`` holds, which leaves a corrupt balance line
        invisible to the one reader that now depends on it."""
        _seed()
        for name, path in (
            (report.JOURNAL_CASH_EVENTS, paths.cash_events_path(_LIVE)),
            (report.JOURNAL_TRADE_ROWS, paths.trade_rows_path(_LIVE)),
            (report.JOURNAL_BALANCES, paths.balances_path(_LIVE)),
        ):
            with path.open("a", encoding="utf-8") as fh:
                fh.write("{not json\n")
            with self.subTest(journal=name):
                totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)
                self.assertEqual(sorted(totals.basis.malformed), sorted(report.JOURNALS))
                self.assertEqual(totals.basis.malformed[name], 1)

    def test_a_restated_row_is_reported_rather_than_folded_silently(self) -> None:
        """**Mutation it catches:** the restatement counter dropped from the
        basis.

        What this counter CANNOT see, and nothing in this layer can: a
        correction that arrives as a NEW ``BkAmountId`` carrying an offsetting
        amount rather than as a rewrite of the old one. The fold would then
        count both rows and ``restated`` would stay 0 (memo section 9, still
        open -- probe 3 observed no restatement at all over two trade dates).
        """
        _seed()
        corrected = BOOKINGS[0]
        _seed(
            read=_read(
                events=(
                    replace(corrected, amount_account_currency=-99.99),
                    *BOOKINGS[1:],
                )
            ),
            now=_SWEPT_AT + dt.timedelta(days=1),
        )

        totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertEqual(totals.basis.restated[report.JOURNAL_CASH_EVENTS], 1)
        self.assertEqual(totals.basis.rows_in_store, ALL_ROWS)
        self.assertNotEqual(totals.cash_book_sum_acct.value, ALL_ROWS_TOTAL_PLN)

    def test_an_undated_booking_row_is_counted_rather_than_summed(self) -> None:
        """``CashEvent.date`` is optional on the vendor row, so a row with no
        date cannot be placed in any window.

        It is excluded from the window and COUNTED, never dropped: excluding
        it cannot inflate a total, and the row partition is what says it
        happened. The capture has a date on 104 of 104 rows, so only a
        constructed row can prove the branch is read at all.
        """
        _seed(read=_read(events=(replace(BOOKINGS[0], date=None), *BOOKINGS[1:])))

        basis = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL).basis

        self.assertEqual(basis.rows_undated, 1)
        self.assertEqual(basis.rows_date_malformed, 0)
        self.assertEqual(basis.rows_read, ALL_ROWS - 1)
        self.assertEqual(
            basis.rows_read
            + basis.rows_outside_window
            + basis.rows_undated
            + basis.rows_date_malformed,
            basis.rows_in_store,
        )


#: A ``Date`` the vendor could start sending instead of a plain ISO day: the
#: same instant, datetime-shaped. ``dt.date.fromisoformat`` refuses it, the
#: codec stores ``date`` as free text, so it reaches the window split intact.
DATETIME_SHAPED_DATE = "2026-09-04T00:00:00"


class ADateThatWillNotParseIsItsOwnFact(IsolatedHomeTestCase):
    """F7: the parser answers ``None`` for an absent date AND for one it
    refuses, and the two were counted together as ``rows_undated``.

    The codec stores ``date`` as free text, so a vendor change to a
    datetime-shaped string decodes and reaches this layer. Every row then
    falls out of every window -- sum 0.00, every bucket 0.00 -- while the
    published basis said the rows carried no date at all, which points an
    operator at the vendor's nulls instead of at the format.
    """

    def test_the_premise_is_that_the_shape_decodes_and_will_not_parse(self) -> None:
        """Stated rather than assumed: if the string parsed, or if the codec
        refused it, this whole class would be about nothing."""
        with self.assertRaises(ValueError):
            dt.date.fromisoformat(DATETIME_SHAPED_DATE)
        self.assertEqual(replace(BOOKINGS[0], date=DATETIME_SHAPED_DATE).date, DATETIME_SHAPED_DATE)

    def test_one_unparseable_date_is_counted_apart_from_the_undated_rows(self) -> None:
        """**Mutation it catches:** folding it into ``rows_undated``, which
        names the wrong cause on the only line that says what happened."""
        _seed(read=_read(events=(replace(BOOKINGS[0], date=DATETIME_SHAPED_DATE), *BOOKINGS[1:])))

        basis = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL).basis

        self.assertEqual(basis.rows_date_malformed, 1)
        self.assertEqual(basis.rows_undated, 0)
        self.assertEqual(basis.rows_read, ALL_ROWS - 1)

    def test_both_causes_at_once_stay_separately_counted(self) -> None:
        """The discriminator between two counters and one: a fixture holding
        only one of the two cannot tell them apart."""
        _seed(
            read=_read(
                events=(
                    replace(BOOKINGS[0], date=None),
                    replace(BOOKINGS[1], date=DATETIME_SHAPED_DATE),
                    *BOOKINGS[2:],
                )
            )
        )

        basis = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL).basis

        self.assertEqual(basis.rows_undated, 1)
        self.assertEqual(basis.rows_date_malformed, 1)
        self.assertEqual(basis.rows_read, ALL_ROWS - 2)
        self.assertEqual(
            basis.rows_read
            + basis.rows_outside_window
            + basis.rows_undated
            + basis.rows_date_malformed,
            basis.rows_in_store,
        )

    def test_a_vendor_format_change_says_the_FORMAT_and_not_the_absence(self) -> None:
        """The loud case, and the one that matters: every row at once.

        The sum is 0.00 over 0 rows and every bucket is 0.00 -- a report that
        looks like a quiet account. What tells an operator which of the two it
        is, is which counter holds the 104.

        **Mutation it catches:** one counter for both causes, which publishes
        ``rows_undated: 104`` and says the vendor stopped sending dates.
        """
        _seed(
            read=_read(
                events=tuple(replace(event, date=DATETIME_SHAPED_DATE) for event in BOOKINGS)
            )
        )

        totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertEqual(totals.basis.rows_date_malformed, ALL_ROWS)
        self.assertEqual(totals.basis.rows_undated, 0)
        self.assertEqual(totals.basis.rows_read, 0)
        self.assertEqual(totals.cash_book_sum_acct.value, 0.0)
        self.assertEqual(totals.basis.rows_in_store, ALL_ROWS)

    def test_the_counter_reaches_the_wire_form(self) -> None:
        """A counter the record does not publish is one no consumer can read."""
        _seed(read=_read(events=(replace(BOOKINGS[0], date=DATETIME_SHAPED_DATE), *BOOKINGS[1:])))

        body = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL).to_jsonable()

        self.assertEqual(body["basis"]["rows_date_malformed"], 1)
        self.assertEqual(body["basis"]["rows_undated"], 0)


class AWindowTheStoreCannotAnswerIsRefused(IsolatedHomeTestCase):
    """Each refusal raises from its own branch with its own reason, so none of
    these three tests dies only together with another."""

    def test_a_sim_window_refuses_rather_than_summing_an_empty_store(self) -> None:
        """**Mutation it catches:** relying on the store's write guard, which
        the READERS never call. On SIM the root never exists and
        ``_iter_payloads`` returns silently on a missing path, so a naive sum
        answers ``0.0`` -- the one thing memo section 5's table forbids."""
        with self.assertRaises(report.CashBookTotalsRefusedError) as ctx:
            report.build_totals(env=_SIM, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertEqual(ctx.exception.reason, report.REFUSED_SIM_REPORTS_UNUSABLE)
        self.assertEqual(ctx.exception.env, _SIM)
        self.assertFalse(paths.account_cash_root(_SIM).exists())

    def test_an_empty_live_store_refuses_nothing_swept(self) -> None:
        with self.assertRaises(report.CashBookTotalsRefusedError) as ctx:
            report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertEqual(ctx.exception.reason, report.REFUSED_NOTHING_SWEPT)

    def test_a_since_before_the_first_sweep_refuses_rather_than_answering_smaller(self) -> None:
        """Memo section 5's table, and amendment W8's containment.

        **Mutation it catches:** clamping ``since`` to ``history_begins`` and
        answering a number, which reads as a complete answer for a window the
        store never covered -- and which is what would hide an episode whose
        entry is partly before the store.
        """
        _seed()

        with self.assertRaises(report.CashBookTotalsRefusedError) as ctx:
            report.build_totals(env=_LIVE, since=dt.date(2026, 7, 31), until=_SWEPT_UNTIL)

        self.assertEqual(ctx.exception.reason, report.REFUSED_BEFORE_FIRST_SWEEP)
        self.assertEqual(sorted(report.TOTALS_REFUSALS), sorted(set(report.TOTALS_REFUSALS)))
        self.assertIn(report.REFUSED_BEFORE_FIRST_SWEEP, report.TOTALS_REFUSALS)

    def test_the_day_the_history_begins_is_answerable(self) -> None:
        """The negative control: a refusal that refused one day too many would
        satisfy the test above."""
        _seed()

        totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertEqual(totals.basis.window_since, _SWEPT_SINCE.isoformat())

    def test_an_inverted_window_is_a_caller_defect_and_not_a_refusal(self) -> None:
        """``ValueError``, not a published reason: a window that ends before
        it starts is nothing the store can report about."""
        _seed()

        with self.assertRaises(ValueError):
            report.build_totals(env=_LIVE, since=_SWEPT_UNTIL, until=_SWEPT_SINCE)

    def test_an_unknown_environment_is_refused_by_the_path_builder(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            report.build_totals(env="prod", since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertIn("prod", str(ctx.exception))


#: Measured: the capture's trade rows with the three still-open instruments
#: removed. The BOOKING rows are untouched, so their share legs and fees are
#: still in the cash book with no episode to account for them -- which is
#: exactly what the residual line is for.
FLAT_RESIDUAL_PLN = -3594.86
FLAT_SUBTOTAL_PLN = -3261.32


class ThereIsNoOpenEpisodeAtAll(IsolatedHomeTestCase):
    """An account that is flat is the ORDINARY state between positions, not an
    edge case, so every line below it has to stay readable."""

    def setUp(self) -> None:
        super().setUp()
        closed_only = tuple(
            row
            for row in TRADES
            if row.instrument_symbol not in {"RHI:xnys", "TNGX:xnas", "NESR:xnas"}
        )
        _seed(read=_read(trades=closed_only))
        self.totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

    def test_the_cost_basis_line_is_a_zero_with_a_row_count_of_zero(self) -> None:
        """Memo section 5(a)'s own pattern, the one ``unclassified`` follows:
        the line is printed even at zero, and the row count beside the 0.00 is
        what says no episode is open -- so a non-zero one later reads as a
        CHANGED number rather than as a new row appearing.

        **Mutation it catches:** publishing ``money.NULL_NEVER_OPENED`` here.
        A named absence is the more informative WORD, and it nulls the two
        lines below through the all-or-nothing rule, which makes a whole
        report unreadable in the ordinary case where nothing is at risk.
        Measured before the fix: ``residual: never_opened`` and
        ``subtotal: account_amount_not_reported``.
        """
        self.assertEqual(self.totals.cost_basis_open_acct.value, 0.0)
        self.assertIsNone(self.totals.cost_basis_open_acct.null_reason)
        self.assertEqual(self.totals.cost_basis_open_acct.rows, 0)
        self.assertEqual(self.totals.cost_basis_open_acct.currency, "PLN")
        self.assertEqual(self.totals.basis.exclusions[episodes.STATE_OPEN], 0)

    def test_the_lines_below_it_still_carry_numbers(self) -> None:
        """The reason the line above is a zero. Both literals are measured:
        the booking rows still hold the share legs and fees of the three
        positions whose trade rows are gone, so the residual is exactly the
        -3 594.86 no episode accounts for."""
        self.assertEqual(self.totals.residual_acct.value, FLAT_RESIDUAL_PLN)
        self.assertEqual(self.totals.subtotal_acct.value, FLAT_SUBTOTAL_PLN)
        for name, line in sorted(self.totals.lines().items()):
            with self.subTest(line=name):
                self.assertIsNone(line.null_reason)
                self.assertEqual(line.currency, "PLN")


#: A balance body the vendor answered without ``Currency``. Every other field
#: is present, so this is not a skipped read -- it is a body whose unit nobody
#: stated, which is the one input that leaves the report with no currency to
#: name anywhere.
NO_CURRENCY_BALANCE = BalanceSnapshot.from_vendor_body(
    {"CashBalance": 0.0, "CalculationReliability": "Ok"},
    asof=_SWEPT_AT,
)


class AStoreThatNamesNoAccountCurrencyStillAnswers(IsolatedHomeTestCase):
    """Zero booking rows and a balance body with no ``Currency``.

    Reachable on LIVE with ``rows_in_store == 0``: a first sweep over a quiet
    window, or a new account, whose balances body omits the field. Then
    ``_account_currency`` finds no unit anywhere, ``_as_pairs`` substitutes
    ``None``, and the per-bucket sum nulls with
    ``account_amount_not_reported`` while the rows' own total is a plain
    ``0.00``.

    **What it did before:** the two sides of the bucket identity were compared
    across a null and a number, and ``build_totals`` raised
    ``CashBookIdentityError`` -- an ``AssertionError`` on the money path
    instead of a published zero or a named refusal. The same shape was fixed
    for an empty WINDOW in 73cd4f5a; the empty STORE survived it.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed(
            read=_read(
                events=(),
                trades=(),
                balance=NO_CURRENCY_BALANCE,
                events_window=ReadWindow(_SWEPT_SINCE.isoformat(), _SWEPT_UNTIL.isoformat(), 0),
                trades_window=ReadWindow(_SWEPT_SINCE.isoformat(), _SWEPT_UNTIL.isoformat(), 0),
            )
        )
        self.totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

    def test_the_premise_is_that_no_row_and_no_body_states_a_currency(self) -> None:
        self.assertIsNone(NO_CURRENCY_BALANCE.currency)
        self.assertEqual(self.totals.basis.rows_in_store, 0)
        self.assertIsNone(self.totals.basis.currency)

    def test_it_publishes_a_record_rather_than_raising_on_its_own_identity(self) -> None:
        """The headline. A partition that cannot hold over the input it was
        handed is a defect in the partition."""
        self.assertEqual(self.totals.basis.rows_read, 0)
        self.assertEqual(self.totals.basis.episodes, 0)

    def test_no_line_states_a_currency_nobody_reported(self) -> None:
        """The other half, and the reason this is not just "do not crash": a
        figure wearing a unit no row stated is memo section 5(d)'s own named
        defect. Every line is a named absence or a unit-less zero."""
        for name, line in sorted(self.totals.lines().items()):
            with self.subTest(line=name):
                self.assertIsNone(line.currency)
                if line.null_reason is None:
                    self.assertEqual(line.value, 0.0)

    def test_the_whole_record_still_renders_as_one_strict_json_value(self) -> None:
        body = json.loads(json.dumps(self.totals.to_jsonable(), allow_nan=False, sort_keys=True))

        self.assertIsNone(body["basis"]["currency"])
        self.assertEqual(body["env"], _LIVE)


class TheBucketRowCountIsWhatGuardsAStoreWithNoCurrency(IsolatedHomeTestCase):
    """The claim ``_agree`` makes, exercised on the input that reaches it.

    With no account currency stated anywhere, BOTH sides of the bucket
    identity are named absences and that identity holds unconditionally -- by
    design, since no total is published on that line either. ``_agree``'s
    docstring names the fourth partition as what keeps a lost row caught
    there. Until 2026-10-26 that partition compared ``bucket_fold.rows``
    against the length of the list ``classify_buckets`` was handed, which is
    what it sets ``rows`` from, so nothing was left guarding this state at all.
    """

    def setUp(self) -> None:
        super().setUp()
        self.read = _read(
            events=(),
            trades=(),
            balance=NO_CURRENCY_BALANCE,
            events_window=ReadWindow(_SWEPT_SINCE.isoformat(), _SWEPT_UNTIL.isoformat(), 0),
            trades_window=ReadWindow(_SWEPT_SINCE.isoformat(), _SWEPT_UNTIL.isoformat(), 0),
        )

    def test_the_premise_is_that_the_bucket_identity_cannot_speak_here(self) -> None:
        """Stated rather than assumed: if either side carried a number, the
        bucket identity would be the thing that caught the lost row and this
        class would be about nothing."""
        _seed(read=self.read)

        totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertIsNone(totals.basis.currency)
        self.assertEqual(totals.basis.rows_in_store, 0)

    def test_a_row_count_that_does_not_add_up_still_refuses(self) -> None:
        """Doctored the way a row dropped from the classification looks -- one
        bucket a row short, the fold's counter unchanged -- which is the shape
        the old tautology could not see.

        **Mutation it catches:** the fourth partition left as the tautology,
        which leaves this store with no count check at all.
        """
        _seed(read=self.read)
        real = buckets.classify_buckets

        def doctored(rows: object) -> buckets.BucketFold:
            fold = real(rows)  # type: ignore[arg-type]
            first, *rest = fold.buckets
            return replace(fold, buckets=(replace(first, rows=first.rows - 1), *rest))

        with mock.patch.object(buckets, "classify_buckets", doctored):
            with self.assertRaises(report.CashBookIdentityError) as ctx:
                report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

        self.assertIn("bucket row count", str(ctx.exception))


class TwoAccountCurrenciesInOneStoreStillAnswer(IsolatedHomeTestCase):
    """One booking row restated into ``EUR`` -- a vendor anomaly or a bug.

    ``_account_currency``'s docstring says the two disagreeing answer ``None``
    so that every sum refuses on its own rather than picking one. It did not:
    the two sides of the bucket identity nulled with DIFFERENT reasons
    (``account_amount_not_reported`` against ``currency_mixed``) and
    ``build_totals`` raised. This is the documented degradation path for a
    vendor anomaly, so it has to end in a published refusal per line.
    """

    def setUp(self) -> None:
        super().setUp()
        _seed(
            read=_read(
                events=(replace(BOOKINGS[0], account_currency="EUR"), *BOOKINGS[1:]),
            )
        )
        self.totals = report.build_totals(env=_LIVE, since=_SWEPT_SINCE, until=_SWEPT_UNTIL)

    def test_the_premise_is_that_the_store_holds_two_account_currencies(self) -> None:
        self.assertIsNone(self.totals.basis.currency)
        self.assertEqual(self.totals.basis.rows_in_store, ALL_ROWS)
        self.assertEqual(self.totals.basis.rows_read, ALL_ROWS)

    def test_the_lines_that_would_have_added_two_units_refuse_by_name(self) -> None:
        """No total on those lines, and no traceback either. ``BOOKINGS[0]`` is
        a ``Cash Amount`` row of +2 000.00, so the deposits bucket is where
        the two currencies meet, and the three lines computed from it or from
        every row refuse with a registered reason."""
        for name in ("deposits_acct", "cash_book_sum_acct", "residual_acct", "subtotal_acct"):
            with self.subTest(line=name):
                line = self.totals.lines()[name]
                self.assertIsNotNone(line.null_reason)
                self.assertIn(line.null_reason, money.NULL_REASONS)

    def test_the_lines_summed_from_one_currency_keep_answering_in_it(self) -> None:
        """The other half, and why this is not a report-wide refusal: a second
        currency on one booking row says nothing about the trades rows, which
        are PLN on all 41. Those lines state their own unit on the number
        (memo section 5(d)) even though no single ACCOUNT currency could be
        resolved for the basis."""
        for name, value in (
            ("net_closed_acct", NET_CLOSED_PLN),
            ("cost_basis_open_acct", COST_BASIS_OPEN_PLN),
            ("income_acct", INCOME_PLN),
        ):
            with self.subTest(line=name):
                line = self.totals.lines()[name]
                self.assertEqual(line.currency, "PLN")
                self.assertEqual(line.value, value)

    def test_the_cash_book_sum_says_the_two_currencies_are_what_stopped_it(self) -> None:
        """``currency_mixed`` is the informative word on the line summed over
        the rows themselves: a second currency means a vendor change or a bug,
        never a convertible total."""
        self.assertEqual(self.totals.cash_book_sum_acct.null_reason, money.NULL_CURRENCY_MIXED)

    def test_the_balance_check_is_not_checkable_rather_than_wrong(self) -> None:
        check = next(
            one for one in self.totals.balance_checks if one.kind == balance_check.CHECK_CUMULATIVE
        )

        self.assertEqual(check.verdict, balance_check.VERDICT_NOT_CHECKABLE)


class TheToleranceIsAGroszInTheAccountCurrency(IsolatedHomeTestCase):
    def test_the_tolerance_is_declared_once_in_the_money_vocabulary(self) -> None:
        """Imported, not re-declared (amendment W4): ``acct_difference``, the
        only producer of the number it is compared against, lives there."""
        self.assertEqual(BALANCE_TOLERANCE_ACCT, 0.01)
        self.assertIs(report.BALANCE_TOLERANCE_ACCT, BALANCE_TOLERANCE_ACCT)
        self.assertNotIn(
            "BALANCE_TOLERANCE_ACCT =",
            inspect.getsource(report),
            "re-declared rather than imported: two copies of a money-path rule behave "
            "identically on the day they are written and drift on the day the rule changes",
        )


if __name__ == "__main__":
    unittest.main()

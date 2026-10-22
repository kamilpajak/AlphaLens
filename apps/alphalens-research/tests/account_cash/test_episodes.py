"""Episodes: one instrument, flat to flat, at the account (#1689, memo 1.5).

Measured on the capture (``fixtures/trades/live_2026_10_03/venue.json``, 41
trade rows) by walking ``Amount`` per ``Uic`` in ``TradeExecutionTime`` order
and summing ``BookedAmountAccountCurrency``:

* 18 instruments, 15 of them back to flat, 3 still long (RHI +3, TNGX +20,
  NESR +13).
* **16 closed episodes over those 15 instruments** -- ``F:xnys`` goes flat
  TWICE on 2026-08-10, and it is the only row group in the capture that tells
  "count the episodes" apart from "count the instruments that net to zero".
* The realized net over the 16 closed episodes is **+329.49 PLN**; the cost
  basis of the 3 open ones is **-3594.86 PLN**; every fill together is
  **-3265.37 PLN**, which is the vendor's own
  ``sum(BookedAmountAccountCurrency)``.

Those are NOT memo section 1.6 P2's ``+386.90 / -3652.27`` pair, which is
pick-keyed rather than account-keyed. The figures here are the account's own.

Four claims the capture is uniform in, and the synthetic rows that settle them:
every close exactly matches its open (so nothing clips), every open episode is
buy-only (so an entry-side cost basis and a whole-episode one are the same
number), ``ToOpenOrClose`` agrees with ``sign(Amount)`` on 41 of 41 rows, and
``Direction`` is the literal string ``'None'`` on 41 of 41 rows.
"""

from __future__ import annotations

import random
import unittest
from collections import Counter

from alphalens_pipeline.account_cash.episodes import (
    ATTRIBUTION_FIFO_ASSUMED,
    EXCLUSION_ORDER,
    STATE_CLOSED,
    STATE_IMBALANCED,
    STATE_OPEN,
    STATE_WINDOW_CLIPPED,
    UNASSIGNED_NO_OPEN,
    UNASSIGNED_QUANTITY_NOT_READ,
    CashEpisode,
    EpisodeFold,
    derive_episodes,
)
from alphalens_pipeline.account_cash.money import NULL_NOT_CLOSED, acct_sum

from tests.account_cash.cashbook_fixture import (
    SYNTHETIC_PARTLY_CLOSED_COST_BASIS,
    SYNTHETIC_PARTLY_CLOSED_MONEY,
    SYNTHETIC_SHARED_SYMBOL,
    SYNTHETIC_SHARED_SYMBOL_BUY_UIC,
    SYNTHETIC_SHARED_SYMBOL_SELL_UIC,
    TRADES,
    synthetic_clip_then_round_trip,
    synthetic_clipped_partial,
    synthetic_clipped_sell_only,
    synthetic_fractional_round_trip,
    synthetic_over_close_inside_epsilon,
    synthetic_partly_closed,
    synthetic_trade_id_disagrees_with_time,
    synthetic_two_uics_one_symbol,
    synthetic_unread_quantity_first,
    synthetic_unstamped_fill_sorts_last,
    synthetic_word_disagrees_with_sign,
    synthetic_zero_quantity_fill,
    trade_like,
)

FILLS_READ = 41
CLOSED_EPISODES = 16
NET_CLOSED_PLN = 329.49
COST_BASIS_OPEN_PLN = -3594.86
ALL_FILLS_PLN = -3265.37

F_FIRST_EPISODE_TRADES = ("6824247527", "6824262730")
F_SECOND_EPISODE_TRADES = ("6824343414", "6824356464")


def _closed(fold: EpisodeFold) -> tuple[CashEpisode, ...]:
    return tuple(episode for episode in fold.episodes if episode.state == STATE_CLOSED)


def _open(fold: EpisodeFold) -> tuple[CashEpisode, ...]:
    return tuple(episode for episode in fold.episodes if episode.state == STATE_OPEN)


def _by_symbol(episodes: tuple[CashEpisode, ...], symbol: str) -> tuple[CashEpisode, ...]:
    return tuple(episode for episode in episodes if episode.instrument_symbol == symbol)


def _money_pairs(fold: EpisodeFold) -> list[tuple[float | None, str | None]]:
    return [
        (amount.value, amount.currency)
        for amount in (
            *(episode.money_acct for episode in fold.episodes),
            *(fill.money_acct for fill in fold.unassigned),
        )
    ]


def _content(fold: EpisodeFold) -> tuple:
    """The fold's CONTENT, independent of the order the rows arrived in.

    ``derive_episodes`` groups into a dict keyed by ``uic``, so the published
    tuple follows the input's own order of first appearance while the episodes
    themselves do not. Comparing records directly would therefore fail on a
    reordered input for a reason that is not about the walk.
    """
    return (
        fold.fills_read,
        fold.fills_assigned,
        tuple(sorted(fold.episodes, key=lambda episode: (episode.uic or 0, episode.trade_ids))),
        tuple(sorted(fold.unassigned, key=lambda fill: fill.trade_id)),
    )


def _with_amount_nulled(trade_id: str) -> tuple:
    return tuple(
        trade_like(row, amount=None) if row.trade_id == trade_id else row for row in TRADES
    )


class TheRealWindow(unittest.TestCase):
    def setUp(self) -> None:
        self.fold = derive_episodes(TRADES)

    def test_the_real_window_closes_sixteen_episodes_over_fifteen_instruments(self) -> None:
        """``F:xnys`` is the only group that separates the two counts.

        Counting instruments whose net quantity is zero answers 15. Dropping
        ``F`` from the fixture silently loses the discriminator, so the symbol
        and both episodes' trade ids are asserted here by name.
        """
        closed = _closed(self.fold)
        self.assertEqual(len(closed), CLOSED_EPISODES)
        self.assertEqual(len({episode.instrument_symbol for episode in closed}), 15)

        ford = _by_symbol(closed, "F:xnys")
        self.assertEqual(len(ford), 2)
        self.assertEqual(
            sorted(episode.trade_ids for episode in ford),
            sorted([F_FIRST_EPISODE_TRADES, F_SECOND_EPISODE_TRADES]),
        )

    def test_the_three_instruments_still_long_are_open_and_excluded(self) -> None:
        """An open episode reports no net at all, only a cost basis.

        Publishing a partial net as if it were a total is the keeper's own
        stated rule, and an open position's realized figure does not exist.
        """
        still_long = {"RHI:xnys": 3.0, "NESR:xnas": 13.0, "TNGX:xnas": 20.0}
        open_episodes = _open(self.fold)
        self.assertEqual(len(open_episodes), 3)

        for symbol, quantity in still_long.items():
            with self.subTest(symbol=symbol):
                (episode,) = _by_symbol(open_episodes, symbol)
                self.assertEqual(episode.opened_qty - episode.closed_qty, quantity)
                self.assertFalse(episode.eligible)
                self.assertEqual(episode.exclusion_reason, STATE_OPEN)
                self.assertIsNone(episode.net_acct.value)
                self.assertEqual(episode.net_acct.null_reason, NULL_NOT_CLOSED)
                self.assertIsNotNone(episode.cost_basis_acct.value)
                self.assertIsNone(episode.closed_date)

    def test_the_realized_net_on_the_closed_episodes_is_the_accounts_own_figure(self) -> None:
        net = acct_sum(
            [(episode.net_acct.value, episode.net_acct.currency) for episode in _closed(self.fold)],
            rows=CLOSED_EPISODES,
        )
        basis = acct_sum(
            [
                (episode.cost_basis_acct.value, episode.cost_basis_acct.currency)
                for episode in _open(self.fold)
            ],
            rows=3,
        )

        assert net.value is not None and basis.value is not None
        self.assertAlmostEqual(net.value, NET_CLOSED_PLN, places=6)
        self.assertAlmostEqual(basis.value, COST_BASIS_OPEN_PLN, places=6)
        self.assertEqual(net.currency, "PLN")

    def test_every_episode_is_either_eligible_or_carries_one_reason(self) -> None:
        """First-hit-of-``EXCLUSION_ORDER``, so counts by reason ADD UP."""
        for episode in self.fold.episodes:
            with self.subTest(uic=episode.uic, state=episode.state):
                self.assertEqual(episode.eligible, episode.state == STATE_CLOSED)
                if episode.eligible:
                    self.assertIsNone(episode.exclusion_reason)
                else:
                    self.assertIn(episode.exclusion_reason, EXCLUSION_ORDER)
                    self.assertEqual(episode.exclusion_reason, episode.state)

        by_reason = Counter(
            episode.exclusion_reason for episode in self.fold.episodes if not episode.eligible
        )
        self.assertEqual(sum(by_reason.values()), len(self.fold.episodes) - CLOSED_EPISODES)

    def test_the_fifo_attribution_is_stated_on_every_episode(self) -> None:
        """A pairing published as measured would be a claim nobody made.

        The venue states open-or-close per row but pairs nothing: exactly 1 of
        41 rows carries a ``RelatedPositionId``, because the account nets at
        end of day.
        """
        self.assertTrue(self.fold.episodes)
        for episode in self.fold.episodes:
            self.assertEqual(episode.attribution, ATTRIBUTION_FIFO_ASSUMED)


class TheSignComesFromTheAmount(unittest.TestCase):
    def test_the_sign_comes_from_the_amount_and_not_from_the_venues_word(self) -> None:
        """``ToOpenOrClose`` agrees with ``sign(Amount)`` on 41 of 41 real rows.

        So only a row where the two disagree can show which one the walk
        reads. Here both rows say ``ToOpen`` while the second ``Amount`` is
        negative: the quantity walk returns to flat and the episode closes,
        while a walk reading the word would hold a position of 12 open.
        """
        fold = derive_episodes(synthetic_word_disagrees_with_sign())

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.state, STATE_CLOSED)
        self.assertEqual(episode.opened_qty, 6.0)
        self.assertEqual(episode.closed_qty, 6.0)
        assert episode.net_acct.value is not None
        self.assertAlmostEqual(episode.net_acct.value, 10.00, places=6)

    def test_the_two_unread_direction_columns_change_no_answer(self) -> None:
        """``to_open_or_close`` and ``trade_event_type`` are read by nothing.

        On the capture all three of them and ``sign(Amount)`` are collinear
        24/17, so a cross-check between them has no resolving power at all.
        Stated as a test so a later reader does not mistake the omission for
        an oversight and add a branch on either field.
        """
        mutated = tuple(
            trade_like(row, to_open_or_close="Sideways", trade_event_type="Teleported")
            for row in TRADES
        )

        self.assertEqual(derive_episodes(mutated), derive_episodes(TRADES))

    def test_the_direction_column_says_nothing_at_all(self) -> None:
        """A floor on the capture: ``Direction`` is the literal string 'None'."""
        from tests.account_cash.cashbook_fixture import VENUE

        self.assertEqual({row["Direction"] for row in VENUE["trades"]}, {"None"})


class TheWindowClippedDetectorIsTheQuantityWalk(unittest.TestCase):
    def test_a_sell_with_no_open_in_the_store_produces_no_episode(self) -> None:
        """No open quantity at the moment of the close: nothing to clip.

        A walk that forces the close onto nothing reports a ``closed`` episode
        with a positive net out of thin air.
        """
        fold = derive_episodes(synthetic_clipped_sell_only())

        self.assertEqual(len(fold.episodes), 0)
        self.assertEqual(len(fold.unassigned), 1)
        (fill,) = fold.unassigned
        self.assertEqual(fill.reason, UNASSIGNED_NO_OPEN)
        self.assertEqual(fill.quantity, -5.0)
        assert fill.money_acct.value is not None
        self.assertAlmostEqual(fill.money_acct.value, 480.00, places=6)
        self.assertEqual((fold.fills_read, fold.fills_assigned), (1, 0))

    def test_a_sell_larger_than_the_open_position_clips_the_episode(self) -> None:
        """Buy 2 then sell 5: the walk goes 2 and then -3.

        This is the case a "the first row is a close" check passes and the
        sell-only case above does not, so the pair of them is the tie-breaker
        between that detector and the quantity walk.
        """
        fold = derive_episodes(synthetic_clipped_partial())

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.state, STATE_WINDOW_CLIPPED)
        self.assertFalse(episode.eligible)
        self.assertEqual(episode.exclusion_reason, STATE_WINDOW_CLIPPED)
        self.assertIsNone(episode.net_acct.value)
        self.assertEqual(episode.trade_ids, ("9900020001",))

        self.assertEqual(len(fold.unassigned), 1)
        self.assertEqual(fold.unassigned[0].reason, UNASSIGNED_NO_OPEN)
        self.assertEqual((fold.fills_read, fold.fills_assigned), (2, 1))

    def test_a_complete_round_trip_is_not_clipped(self) -> None:
        """Direction 2 of the quiet killer, on the real rows.

        A detector keyed on "the uic re-crosses zero" or on "closes outnumber
        opens" clips ``F:xnys``, which goes flat twice, and makes the total
        silently too SMALL -- the direction memo section 9 calls out.
        """
        fold = derive_episodes(TRADES)

        self.assertEqual(
            [episode for episode in fold.episodes if episode.state == STATE_WINDOW_CLIPPED], []
        )
        self.assertEqual(
            [episode.state for episode in _by_symbol(fold.episodes, "F:xnys")],
            [STATE_CLOSED, STATE_CLOSED],
        )


class TheCoveragePartition(unittest.TestCase):
    def test_the_coverage_partition_holds_on_the_real_rows(self) -> None:
        """``if not lots: continue`` is what the floors make impossible."""
        fold = derive_episodes(TRADES)

        self.assertEqual(fold.fills_assigned + len(fold.unassigned), fold.fills_read)
        self.assertEqual(fold.fills_read, FILLS_READ)
        self.assertEqual(fold.fills_assigned, FILLS_READ)
        # ``fills_read`` is the INPUT's length, not the partition's own total:
        # read back off the two halves it would agree with whatever survived,
        # and the partition above would have nothing left to refute.
        self.assertEqual(fold.fills_read, len(TRADES))

    def test_the_coverage_partition_holds_when_something_is_unassigned(self) -> None:
        """The real rows leave nothing unassigned, so only this case gives the
        identity any power: true and empty is not true."""
        fold = derive_episodes(synthetic_clipped_partial())

        self.assertEqual(fold.fills_assigned + len(fold.unassigned), fold.fills_read)
        self.assertGreaterEqual(len(fold.unassigned), 1)
        self.assertEqual(fold.fills_read, len(synthetic_clipped_partial()))

    def test_every_fills_money_reaches_an_episode_or_a_listed_fill(self) -> None:
        """The partition's MONEY form, and the identity that ties the two
        derivations together: -3265.37 PLN is also the vendor's own
        ``sum(BookedAmountAccountCurrency)`` over the same 41 rows."""
        real = acct_sum(_money_pairs(derive_episodes(TRADES)), rows=FILLS_READ)
        assert real.value is not None
        self.assertAlmostEqual(real.value, ALL_FILLS_PLN, places=6)

        clipped_fold = derive_episodes(synthetic_clipped_partial())
        clipped = acct_sum(_money_pairs(clipped_fold), rows=clipped_fold.fills_read)
        assert clipped.value is not None
        self.assertAlmostEqual(clipped.value, 280.00, places=6)

    def test_a_fill_with_no_quantity_is_listed_rather_than_skipped(self) -> None:
        """``if row.amount is None: continue`` quietly shortens a position and
        makes the NEXT close look like an over-close.

        The fill is listed with its own reason, its money stays in the total,
        and the episode it belonged to becomes ``imbalanced`` -- a first-class
        state, not an ``open`` one, because the quantity will not pair.
        """
        fold = derive_episodes(_with_amount_nulled("6827730144"))

        self.assertEqual(fold.fills_read, FILLS_READ)
        self.assertEqual(fold.fills_assigned, FILLS_READ - 1)
        (fill,) = fold.unassigned
        self.assertEqual(fill.reason, UNASSIGNED_QUANTITY_NOT_READ)
        self.assertIsNone(fill.quantity)
        self.assertEqual(fill.instrument_symbol, "MARA:xnas")

        (episode,) = _by_symbol(fold.episodes, "MARA:xnas")
        self.assertEqual(episode.state, STATE_IMBALANCED)
        self.assertEqual(episode.exclusion_reason, STATE_IMBALANCED)
        self.assertFalse(episode.eligible)

        total = acct_sum(_money_pairs(fold), rows=FILLS_READ)
        assert total.value is not None
        self.assertAlmostEqual(total.value, ALL_FILLS_PLN, places=6)


class AFillContributesAllOfItsMoneyOrNone(unittest.TestCase):
    def test_one_absent_booked_amount_nulls_its_own_episode_and_nothing_else(self) -> None:
        """A ``or 0.0`` written into the episode's own accumulation never
        reaches ``acct_sum``'s guard.

        It would publish ``MARA``'s net as the surviving leg alone -- here
        +32.36 instead of -7.73, a sign flip -- with the quantity walk, the
        state and every row count still right. The other 15 closed episodes
        must be untouched, so this test also pins that the nulling is per
        episode and not per fold.
        """
        rows = tuple(
            trade_like(row, booked_amount_account_currency=None)
            if row.trade_id == "6827730144"
            else row
            for row in TRADES
        )

        fold = derive_episodes(rows)

        (mara,) = _by_symbol(fold.episodes, "MARA:xnas")
        self.assertEqual(mara.state, STATE_CLOSED)
        self.assertIsNone(mara.net_acct.value)
        self.assertIsNone(mara.money_acct.value)
        self.assertEqual(mara.net_acct.null_reason, "account_amount_not_reported")
        self.assertEqual(mara.net_acct.rows, 2)

        (ford, _second) = _by_symbol(fold.episodes, "F:xnys")
        assert ford.net_acct.value is not None
        self.assertAlmostEqual(ford.net_acct.value, -7.72, places=6)

    def test_an_absent_entry_amount_nulls_the_cost_basis_and_not_the_whole_fold(self) -> None:
        rows = tuple(
            trade_like(row, booked_amount_account_currency=None)
            if row.trade_id == "6877387096"
            else row
            for row in TRADES
        )

        fold = derive_episodes(rows)

        (tngx,) = _by_symbol(fold.episodes, "TNGX:xnas")
        self.assertIsNone(tngx.cost_basis_acct.value)
        self.assertEqual(tngx.cost_basis_acct.null_reason, "account_amount_not_reported")
        (rhi,) = _by_symbol(fold.episodes, "RHI:xnys")
        assert rhi.cost_basis_acct.value is not None
        self.assertAlmostEqual(rhi.cost_basis_acct.value, -466.98, places=6)

    def test_one_absent_account_currency_nulls_its_episode_the_same_way(self) -> None:
        """All or nothing covers the CURRENCY of a fill, not only its amount.

        The capture is PLN on 41 of 41 trade rows, so an accumulator that
        writes ``row.account_currency or "PLN"`` is invisible there: measured
        2026-10-22 it publishes MARA's realized net as a complete -7.73 PLN
        for an amount nobody said was in the account's currency. The other 15
        closed episodes must be untouched, so the nulling is per episode here
        too.
        """
        rows = tuple(
            trade_like(row, account_currency=None) if row.trade_id == "6827730144" else row
            for row in TRADES
        )

        fold = derive_episodes(rows)

        (mara,) = _by_symbol(fold.episodes, "MARA:xnas")
        self.assertEqual(mara.state, STATE_CLOSED)
        self.assertIsNone(mara.net_acct.value)
        self.assertEqual(mara.net_acct.null_reason, "account_amount_not_reported")
        self.assertIsNone(mara.money_acct.value)
        (ford, _second) = _by_symbol(fold.episodes, "F:xnys")
        assert ford.net_acct.value is not None
        self.assertAlmostEqual(ford.net_acct.value, -7.72, places=6)

    def test_an_unassigned_fill_with_no_booked_amount_refuses_its_own_money(self) -> None:
        """The rule holds on a fill that reached no episode at all.

        Nothing in the capture ends up unassigned, so the money of an
        unassigned fill is never nulled there: measured 2026-10-22, an ``or
        0.0`` on that path publishes 0.00 with no reason, and the money
        partition then reads a complete total that has quietly lost a fill.
        """
        rows = tuple(
            trade_like(row, booked_amount_account_currency=None)
            for row in synthetic_clipped_sell_only()
        )

        fold = derive_episodes(rows)

        (fill,) = fold.unassigned
        self.assertIsNone(fill.money_acct.value)
        self.assertEqual(fill.money_acct.null_reason, "account_amount_not_reported")
        self.assertEqual(fill.money_acct.currency, "PLN")
        self.assertIsNone(acct_sum(_money_pairs(fold), rows=fold.fills_read).value)


class TheCostBasisIsTheEntrySideOnly(unittest.TestCase):
    def test_the_cost_basis_is_the_entry_side_while_the_money_is_every_fill(self) -> None:
        """Every open episode in the capture is buy-only, so an implementation
        summing EVERY fill into the cost basis passes on the real rows.

        Buy 10, buy 5, sell 4 -- still long 11 -- is the only shape where the
        two are different numbers, and they are asserted as two different
        literals.
        """
        self.assertNotEqual(SYNTHETIC_PARTLY_CLOSED_COST_BASIS, SYNTHETIC_PARTLY_CLOSED_MONEY)

        fold = derive_episodes(synthetic_partly_closed())

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.state, STATE_OPEN)
        self.assertEqual(episode.opened_qty, 15.0)
        self.assertEqual(episode.closed_qty, 4.0)
        assert episode.cost_basis_acct.value is not None
        self.assertAlmostEqual(
            episode.cost_basis_acct.value, SYNTHETIC_PARTLY_CLOSED_COST_BASIS, places=6
        )
        assert episode.money_acct.value is not None
        self.assertAlmostEqual(episode.money_acct.value, SYNTHETIC_PARTLY_CLOSED_MONEY, places=6)
        self.assertEqual(episode.cost_basis_acct.rows, 2)
        self.assertEqual(episode.money_acct.rows, 3)


class TheQuantityEpsilonIsHalfAShare(unittest.TestCase):
    """``QTY_PRECISION`` is 0.5 -- half a share, not a float epsilon.

    Every quantity in the capture is a whole share, so all three places the
    epsilon appears are unexercised there and each mutation measured green on
    2026-10-22: dropping it from the walk's flat check loses the closing DATE,
    dropping it from ``finish()``'s loses the realized NET, and dropping it
    from the clip threshold turns a real net into an exclusion.
    """

    def test_a_fractional_residual_inside_the_epsilon_still_closes_with_its_date(self) -> None:
        """Buy 1.5, sell 1.2: 0.3 shares left, which is not a real quantity.

        A closed episode with no ``closed_date`` is the worst shape of all
        here, because amendment W1 sums an episode when its closed date falls
        in the window -- so it would be dropped from the window or crash it,
        while still counting as closed.
        """
        fold = derive_episodes(synthetic_fractional_round_trip())

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.state, STATE_CLOSED)
        self.assertTrue(episode.eligible)
        self.assertEqual(episode.closed_date, "2026-09-15")
        self.assertEqual((episode.opened_qty, episode.closed_qty), (1.5, 1.2))
        assert episode.net_acct.value is not None
        self.assertAlmostEqual(episode.net_acct.value, -20.00, places=6)

    def test_an_over_close_inside_the_epsilon_is_placed_and_not_clipped(self) -> None:
        """Buy 2, sell 2.4: the 0.4 excess is not a real quantity either.

        The other side of the clip threshold from the buy-2-sell-5 case. A
        threshold without the epsilon refuses this fill, publishes
        ``window_clipped`` and drops +50.00 PLN of realized money out of the
        eligible total -- an exclusion standing in for a real trade.
        """
        fold = derive_episodes(synthetic_over_close_inside_epsilon())

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.state, STATE_CLOSED)
        self.assertEqual(fold.unassigned, ())
        self.assertEqual((fold.fills_read, fold.fills_assigned), (2, 2))
        assert episode.net_acct.value is not None
        self.assertAlmostEqual(episode.net_acct.value, 50.00, places=6)


class AQuantityThatCannotPairMarksTheEpisodeItBelongedTo(unittest.TestCase):
    def test_a_quantity_less_fill_before_any_open_marks_the_next_episode(self) -> None:
        """The capture cannot reach this branch, and it is the costly one.

        ``MARA``'s null-amount fill is not the first on its instrument, so the
        pending flag -- an unpairable quantity remembered until the next
        episode opens -- is never set there. Measured 2026-10-22: with the
        branch removed, this shape publishes a ``closed`` episode, eligible,
        with a realized +40.00 PLN, while the fill whose quantity nobody could
        read sits beside it at -10.00. That is money published as complete
        when it is not a round trip.
        """
        fold = derive_episodes(synthetic_unread_quantity_first())

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.state, STATE_IMBALANCED)
        self.assertFalse(episode.eligible)
        self.assertEqual(episode.exclusion_reason, STATE_IMBALANCED)
        self.assertIsNone(episode.net_acct.value)
        self.assertEqual(episode.net_acct.null_reason, NULL_NOT_CLOSED)
        self.assertIsNone(episode.closed_date)
        assert episode.money_acct.value is not None
        self.assertAlmostEqual(episode.money_acct.value, 40.00, places=6)

        (fill,) = fold.unassigned
        self.assertEqual(fill.reason, UNASSIGNED_QUANTITY_NOT_READ)
        self.assertIsNone(fill.quantity)
        self.assertEqual((fold.fills_read, fold.fills_assigned), (3, 2))

    def test_an_excluded_episode_publishes_no_closed_date_at_all(self) -> None:
        """A date on an excluded episode is a date that places it in a window.

        Every excluded episode in the capture is excluded for being ``open``,
        and an open one never reaches the line that stamps the date, so
        publishing the date unconditionally is invisible there. The shape
        above does reach it: the walk brings the instrument flat and the
        episode is still refused.
        """
        folds = (
            derive_episodes(synthetic_unread_quantity_first()),
            derive_episodes(synthetic_clip_then_round_trip()),
            derive_episodes(TRADES),
        )
        excluded = [episode for fold in folds for episode in fold.episodes if not episode.eligible]

        self.assertGreaterEqual(len(excluded), 5)
        for episode in excluded:
            with self.subTest(uic=episode.uic, state=episode.state):
                self.assertIsNone(episode.closed_date)


class TheWalkRestartsAfterACloseItCouldNotPlace(unittest.TestCase):
    def test_a_clean_round_trip_after_a_clipped_close_keeps_its_money(self) -> None:
        """Buy 2, sell 5, buy 1, sell 1 -- amendment W8's shape.

        The second round trip is real realized money. Measured 2026-10-22:
        without the position reset after an unplaceable close the walk keeps
        a negative position, every later close on that ``uic`` reads as
        another over-close, and the +5.00 PLN disappears -- two
        ``window_clipped`` episodes and no realized net at all.
        """
        fold = derive_episodes(synthetic_clip_then_round_trip())

        self.assertEqual(
            [episode.state for episode in fold.episodes], [STATE_WINDOW_CLIPPED, STATE_CLOSED]
        )
        closed = _closed(fold)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].trade_ids, ("9900080003", "9900080004"))
        assert closed[0].net_acct.value is not None
        self.assertAlmostEqual(closed[0].net_acct.value, 5.00, places=6)

        self.assertEqual(len(fold.unassigned), 1)
        self.assertEqual((fold.fills_read, fold.fills_assigned), (4, 3))


class TheGroupingKeyIsTheUicAndNotTheSymbol(unittest.TestCase):
    def test_two_instruments_sharing_one_symbol_do_not_pair(self) -> None:
        """The capture is 1:1 between ``uic`` and symbol, so nothing there
        separates the two keys.

        A ticker reuse or a dual listing separates them, and the direction is
        the bad one: measured 2026-10-22, grouping by symbol folds a buy on
        one instrument and a sell on another into one ``closed`` episode
        netting +20.00 PLN out of two unrelated positions.
        """
        rows = synthetic_two_uics_one_symbol()
        self.assertEqual({row.instrument_symbol for row in rows}, {SYNTHETIC_SHARED_SYMBOL})
        self.assertEqual(
            {row.uic for row in rows},
            {SYNTHETIC_SHARED_SYMBOL_BUY_UIC, SYNTHETIC_SHARED_SYMBOL_SELL_UIC},
        )

        fold = derive_episodes(rows)

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.uic, SYNTHETIC_SHARED_SYMBOL_BUY_UIC)
        self.assertEqual(episode.state, STATE_OPEN)
        self.assertIsNone(episode.net_acct.value)
        (fill,) = fold.unassigned
        self.assertEqual(fill.reason, UNASSIGNED_NO_OPEN)
        self.assertEqual(fill.uic, SYNTHETIC_SHARED_SYMBOL_SELL_UIC)


class AZeroQuantityFillIsNotAnEntry(unittest.TestCase):
    def test_a_zero_quantity_fill_stays_out_of_the_cost_basis(self) -> None:
        """No zero-quantity fill exists in the capture.

        So a walk that counts one as an entry -- ``quantity >= 0.0`` instead
        of ``> 0.0`` -- puts its money in the cost basis and nothing notices:
        measured 2026-10-22 the basis reads -307.00 against the honest
        -300.00, on an episode whose quantities and state are both right.
        The fill still contributes its money to the episode, which is why the
        two numbers and the two row counts are asserted together.
        """
        fold = derive_episodes(synthetic_zero_quantity_fill())

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.state, STATE_CLOSED)
        assert episode.cost_basis_acct.value is not None
        self.assertAlmostEqual(episode.cost_basis_acct.value, -300.00, places=6)
        self.assertEqual(episode.cost_basis_acct.rows, 1)
        assert episode.money_acct.value is not None
        self.assertAlmostEqual(episode.money_acct.value, 43.00, places=6)
        self.assertEqual(episode.money_acct.rows, 3)


class TheWalkOrderIsTheExecutionStamp(unittest.TestCase):
    """The order is load-bearing and the capture already arrives sorted.

    So every claim here needs rows the capture does not provide: its trades
    array is already in execution-time order, its trade ids are monotone in
    that stamp, and all 41 rows carry one.
    """

    def test_the_answer_does_not_depend_on_the_order_the_rows_arrive_in(self) -> None:
        """Metamorphic, because the real answer is what it is.

        Measured 2026-10-22 with the per-``uic`` sort removed: a shuffled
        capture answers 8 closed episodes, 8 unassigned fills and a realized
        net of -66.49 PLN against the honest +329.49, and a reversed one
        answers 1 closed and 16 unassigned. Compared as CONTENT rather than
        as records: the episodes come out in ``uic`` insertion order, so the
        tuple order follows the input order while the episodes themselves do
        not.
        """
        honest = _content(derive_episodes(TRADES))

        shuffled = list(TRADES)
        random.Random(1689).shuffle(shuffled)

        self.assertNotEqual(tuple(shuffled), TRADES)
        self.assertEqual(_content(derive_episodes(tuple(shuffled))), honest)
        self.assertEqual(_content(derive_episodes(tuple(reversed(TRADES)))), honest)

    def test_the_stamp_orders_the_walk_and_not_the_trade_id(self) -> None:
        """On the capture the two orders agree, so only a row where they
        disagree can say which one the walk reads.

        Here the buy is stamped first and carries the HIGHER id. Ordered by
        id the sell arrives with no position to close, and a real round trip
        is published as an open position plus an unassigned fill.
        """
        fold = derive_episodes(synthetic_trade_id_disagrees_with_time())

        self.assertEqual(len(fold.episodes), 1)
        (episode,) = fold.episodes
        self.assertEqual(episode.state, STATE_CLOSED)
        self.assertEqual(episode.trade_ids, ("9900129999", "9900120001"))
        self.assertEqual(fold.unassigned, ())
        assert episode.net_acct.value is not None
        self.assertAlmostEqual(episode.net_acct.value, 10.00, places=6)

    def test_a_fill_with_no_stamp_sorts_last_rather_than_first(self) -> None:
        """All 41 real rows carry a stamp, so the rule is untested there.

        Sorted first, the unstamped buy joins the round trip and the two
        become one open position of three shares with no realized net.
        """
        fold = derive_episodes(synthetic_unstamped_fill_sorts_last())

        self.assertEqual([episode.state for episode in fold.episodes], [STATE_CLOSED, STATE_OPEN])
        closed, still_open = fold.episodes
        assert closed.net_acct.value is not None
        self.assertAlmostEqual(closed.net_acct.value, 10.00, places=6)
        self.assertEqual(still_open.trade_ids, ("9900130003",))


class TheDatesComeFromTheTradeDate(unittest.TestCase):
    def setUp(self) -> None:
        self.fold = derive_episodes(TRADES)

    def test_the_dates_come_from_trade_date_and_not_value_date(self) -> None:
        """Asserted on episodes whose two dates DIFFER.

        Both ``F:xnys`` episodes open and close on 2026-08-10, so swapping
        ``opened_date`` and ``closed_date`` passes there. ``UBER:xnys``
        (2026-09-08 -> 2026-09-28) and ``QUBT:xnas`` (2026-09-03 ->
        2026-09-17) separate the two fields, and ``TradeDate != ValueDate`` on
        41 of 41 rows separates the two columns.
        """
        spans = {
            "UBER:xnys": ("2026-09-08", "2026-09-28"),
            "QUBT:xnas": ("2026-09-03", "2026-09-17"),
        }
        for symbol, (opened, closed) in spans.items():
            with self.subTest(symbol=symbol):
                (episode,) = _by_symbol(self.fold.episodes, symbol)
                self.assertEqual(episode.opened_date, opened)
                self.assertEqual(episode.closed_date, closed)

    def test_the_value_date_is_read_by_nothing(self) -> None:
        mutated = tuple(trade_like(row, value_date="1999-01-01") for row in TRADES)

        self.assertEqual(derive_episodes(mutated), self.fold)


if __name__ == "__main__":
    unittest.main()

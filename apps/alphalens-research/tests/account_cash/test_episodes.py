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
    TRADES,
    synthetic_clipped_partial,
    synthetic_clipped_sell_only,
    synthetic_partly_closed,
    synthetic_word_disagrees_with_sign,
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

    def test_the_coverage_partition_holds_when_something_is_unassigned(self) -> None:
        """The real rows leave nothing unassigned, so only this case gives the
        identity any power: true and empty is not true."""
        fold = derive_episodes(synthetic_clipped_partial())

        self.assertEqual(fold.fills_assigned + len(fold.unassigned), fold.fills_read)
        self.assertGreaterEqual(len(fold.unassigned), 1)

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

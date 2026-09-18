"""The frozen setups of the recovered names no brief ever stored.

A name the pre-open list carried and a later run dropped is in no brief parquet, so it has
no trade setup. The committed artefact holds one per such name, rebuilt by the production
builder over the frame the pipeline cached that day (#1494 step 2). These tests keep the
artefact inside the recovered record and keep every entry a setup the ladder can replay.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.thematic import pre_open_setup
from alphalens_pipeline.thematic.pre_open_brief import PRE_OPEN_BRIEF_NAMES

# The artefact is frozen: a regeneration that moves either number is a change to the
# record, not a refresh, and has to be argued for.
EXPECTED_DATES = 14
EXPECTED_NAMES = 94


class TheArtefactStaysInsideTheRecoveredRecordTest(unittest.TestCase):
    def test_it_holds_the_expected_number_of_dates_and_names(self) -> None:
        self.assertEqual(len(pre_open_setup.PRE_OPEN_SETUPS), EXPECTED_DATES)
        total = sum(len(v) for v in pre_open_setup.PRE_OPEN_SETUPS.values())
        self.assertEqual(total, EXPECTED_NAMES)

    def test_every_date_is_a_recovered_date(self) -> None:
        for brief_date in pre_open_setup.PRE_OPEN_SETUPS:
            self.assertIn(brief_date, PRE_OPEN_BRIEF_NAMES)

    def test_every_ticker_is_on_that_dates_recovered_list(self) -> None:
        for brief_date, by_ticker in pre_open_setup.PRE_OPEN_SETUPS.items():
            recovered = set(PRE_OPEN_BRIEF_NAMES[brief_date])
            for ticker in by_ticker:
                self.assertIn(ticker, recovered, f"{brief_date} {ticker}")

    def test_no_date_covers_its_whole_recovered_list(self) -> None:
        # A date whose every name needed rebuilding would mean the brief parquet was
        # unreadable, not that the list changed completely.
        for brief_date, by_ticker in pre_open_setup.PRE_OPEN_SETUPS.items():
            self.assertLess(len(by_ticker), len(PRE_OPEN_BRIEF_NAMES[brief_date]) + 1)


class EveryFrozenSetupIsReplayableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.setups = [
            (brief_date, ticker, setup)
            for brief_date, by_ticker in pre_open_setup.PRE_OPEN_SETUPS.items()
            for ticker, setup in by_ticker.items()
        ]

    def test_every_setup_is_ok(self) -> None:
        for brief_date, ticker, setup in self.setups:
            self.assertEqual(setup["status"], "OK", f"{brief_date} {ticker}")

    def test_every_setup_records_the_builder_it_came_from(self) -> None:
        for brief_date, ticker, setup in self.setups:
            self.assertTrue(setup.get("builder_config_version"), f"{brief_date} {ticker}")
            self.assertTrue(setup.get("schema_version"), f"{brief_date} {ticker}")

    def test_one_order_ttl_per_date(self) -> None:
        # The ladder config token is derived from this field, so a date that carried two
        # values would pool its own rows under two tokens.
        for brief_date, by_ticker in pre_open_setup.PRE_OPEN_SETUPS.items():
            values = {s["order_ttl_days"] for s in by_ticker.values()}
            self.assertEqual(len(values), 1, f"{brief_date}: {values}")

    def test_entry_tiers_sit_below_the_close_and_descend(self) -> None:
        for brief_date, ticker, setup in self.setups:
            limits = [t["limit"] for t in setup["entry_tiers"]]
            self.assertTrue(limits, f"{brief_date} {ticker}")
            self.assertLess(max(limits), setup["asof_close"], f"{brief_date} {ticker}")
            self.assertEqual(limits, sorted(limits, reverse=True), f"{brief_date} {ticker}")

    def test_allocations_sum_to_a_whole_position(self) -> None:
        for brief_date, ticker, setup in self.setups:
            total = sum(t["alloc_pct"] for t in setup["entry_tiers"])
            self.assertAlmostEqual(total, 100.0, places=6, msg=f"{brief_date} {ticker}")

    def test_the_stop_sits_below_every_entry(self) -> None:
        for brief_date, ticker, setup in self.setups:
            for tier in setup["entry_tiers"]:
                self.assertLess(setup["disaster_stop"], tier["limit"], f"{brief_date} {ticker}")

    def test_targets_sit_above_the_close_and_ascend(self) -> None:
        for brief_date, ticker, setup in self.setups:
            targets = [t["target"] for t in setup["tp_tranches"]]
            self.assertTrue(targets, f"{brief_date} {ticker}")
            self.assertGreater(min(targets), setup["asof_close"], f"{brief_date} {ticker}")
            self.assertEqual(targets, sorted(targets), f"{brief_date} {ticker}")

    def test_the_atr_is_a_positive_number(self) -> None:
        for brief_date, ticker, setup in self.setups:
            self.assertGreater(setup["atr"], 0.0, f"{brief_date} {ticker}")


class TheLookupAnswersOnlyForFrozenNamesTest(unittest.TestCase):
    def test_it_returns_the_frozen_setup(self) -> None:
        brief_date = sorted(pre_open_setup.PRE_OPEN_SETUPS)[0]
        ticker = sorted(pre_open_setup.PRE_OPEN_SETUPS[brief_date])[0]
        found = pre_open_setup.pre_open_setup(brief_date, ticker)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["status"], "OK")

    def test_it_is_case_insensitive_on_the_ticker(self) -> None:
        brief_date = sorted(pre_open_setup.PRE_OPEN_SETUPS)[0]
        ticker = sorted(pre_open_setup.PRE_OPEN_SETUPS[brief_date])[0]
        self.assertEqual(
            pre_open_setup.pre_open_setup(brief_date, ticker.lower()),
            pre_open_setup.pre_open_setup(brief_date, ticker),
        )

    def test_an_unknown_ticker_answers_none(self) -> None:
        brief_date = sorted(pre_open_setup.PRE_OPEN_SETUPS)[0]
        self.assertIsNone(pre_open_setup.pre_open_setup(brief_date, "NOSUCHTICKER"))

    def test_a_date_outside_the_record_answers_none(self) -> None:
        self.assertIsNone(pre_open_setup.pre_open_setup(dt.date(2026, 9, 15), "AAPL"))

    def test_the_caller_cannot_mutate_the_frozen_record(self) -> None:
        brief_date = sorted(pre_open_setup.PRE_OPEN_SETUPS)[0]
        ticker = sorted(pre_open_setup.PRE_OPEN_SETUPS[brief_date])[0]
        found = pre_open_setup.pre_open_setup(brief_date, ticker)
        assert found is not None
        found["disaster_stop"] = -1.0
        again = pre_open_setup.pre_open_setup(brief_date, ticker)
        assert again is not None
        self.assertNotEqual(again["disaster_stop"], -1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

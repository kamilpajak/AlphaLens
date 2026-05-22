"""Cohen-Malloy-Pomorski 2012 routine/opportunistic insider classifier — TDD.

Faithful to paper p. 1786, Section III.A:

  - Routine = trade in same calendar month for at least 3 CONSECUTIVE prior years.
  - Opportunistic = everyone else WITH sufficient history.
  - Eligibility = >=1 trade in EACH of the 3 preceding calendar years.
  - Lookback = 3 years (NOT 5).

Classification performed at the START of each calendar year Y, locked for that
year, re-evaluated annually using rolling window [Y-3, Y).
"""

from __future__ import annotations

import unittest
from datetime import date

from alphalens_research.screeners.insider_activity.cohen_malloy_classifier import (
    CohenMalloyLabel,
    classify_from_transaction_dates,
)


class TestCohenMalloyClassifier(unittest.TestCase):
    """TDD harness — direct paper-spec tests."""

    def test_three_consecutive_same_month_is_routine(self):
        # Insider trades every January for 3 years prior to year_y=2023.
        # 2020-01, 2021-01, 2022-01 all in same calendar month.
        history = [date(2020, 1, 15), date(2021, 1, 20), date(2022, 1, 10)]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.ROUTINE)

    def test_routine_with_extra_trades_outside_anchor_month(self):
        # Anchor January in 3 consecutive years + one extra March trade — still routine.
        # Paper definition: existence of any month with 3 consecutive years suffices.
        history = [
            date(2020, 1, 15),
            date(2021, 1, 20),
            date(2022, 1, 10),
            date(2021, 3, 5),  # extra opportunistic-looking trade
        ]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.ROUTINE)

    def test_two_consecutive_then_skip_is_opportunistic(self):
        # Trades in 2020-01, 2021-01 but NOT 2022-01 — pattern broken before year_y.
        # Eligibility: must have >=1 trade in EACH of 3 preceding years (2020, 2021, 2022).
        # We add a non-anchor trade in 2022 to keep eligibility but break consecutive pattern.
        history = [
            date(2020, 1, 15),
            date(2021, 1, 20),
            date(2022, 6, 5),  # off-month, breaks the January pattern
        ]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.OPPORTUNISTIC)

    def test_skip_year_breaks_consecutive_pattern(self):
        # 2020-01, MISSING 2021, 2022-01 — not 3 CONSECUTIVE same-month, just same-month
        # in 2/3 years. Eligibility fails (no trade in 2021) → UNCLASSIFIED.
        history = [date(2020, 1, 15), date(2022, 1, 10)]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.UNCLASSIFIED)

    def test_eligibility_requires_trade_in_each_of_three_years(self):
        # Trades in 2020 and 2021 only, none in 2022 → UNCLASSIFIED.
        history = [date(2020, 5, 1), date(2021, 8, 15)]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.UNCLASSIFIED)

    def test_no_history_is_unclassified(self):
        label = classify_from_transaction_dates([], classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.UNCLASSIFIED)

    def test_only_one_year_history_is_unclassified(self):
        # Multiple trades but all in one year — fails 3-year eligibility.
        history = [date(2022, 1, 5), date(2022, 4, 10), date(2022, 8, 20)]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.UNCLASSIFIED)

    def test_eligible_irregular_pattern_is_opportunistic(self):
        # Trades in each of 3 preceding years but in different months each time.
        history = [date(2020, 3, 1), date(2021, 7, 15), date(2022, 11, 20)]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.OPPORTUNISTIC)

    def test_classification_year_window_excludes_year_y(self):
        # A trade IN classification_year=2023 itself must NOT count toward eligibility
        # — paper classifies "at start of year Y based on past history."
        # We give 2 prior-year trades + a 2023 trade. Should be UNCLASSIFIED
        # because only 2 of 3 prior years (2020, 2021, NOT 2022) have trades.
        history = [date(2020, 5, 1), date(2021, 8, 15), date(2023, 2, 1)]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.UNCLASSIFIED)

    def test_pre_window_trades_excluded(self):
        # Trades BEFORE the 3-year window must be ignored. Insider has trades in 2017
        # but none in 2020/2021/2022 → UNCLASSIFIED for year_y=2023.
        history = [date(2017, 1, 1), date(2018, 1, 1), date(2019, 1, 1)]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.UNCLASSIFIED)

    def test_routine_anchor_month_can_be_any_month(self):
        # Paper says "same calendar month" — not specifically January. Verify with December.
        history = [date(2020, 12, 5), date(2021, 12, 20), date(2022, 12, 1)]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.ROUTINE)

    def test_multiple_trades_in_anchor_month_per_year_still_routine(self):
        # Paper: "at least one trade in same month each year" — multiple trades in same
        # month of same year still satisfy.
        history = [
            date(2020, 4, 1),
            date(2020, 4, 15),  # same month, same year, two trades
            date(2021, 4, 10),
            date(2022, 4, 5),
        ]
        label = classify_from_transaction_dates(history, classification_year=2023)
        self.assertEqual(label, CohenMalloyLabel.ROUTINE)


if __name__ == "__main__":
    unittest.main()

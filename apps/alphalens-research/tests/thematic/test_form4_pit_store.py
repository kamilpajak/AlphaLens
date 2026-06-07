"""MemoizedClassifier bad-data hardening — TDD.

Rows written before the F1/F2 parser/writer year guards already sit in
``~/.alphalens/form4_parquet`` on the VPS, so the loader cannot assume clean
data. A NaT ``transaction_date`` or a NULL ``reporting_owner_cik`` must not
crash ``MemoizedClassifier.__init__`` (which would take down
``compute_net_opportunistic_usd`` for the whole ticker) nor create an
unreachable ('nan', year) label group.
"""

from __future__ import annotations

import unittest
from datetime import date

import pandas as pd
from alphalens_pipeline.thematic.sources.form4_store import MemoizedClassifier


def _history(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame.from_records(rows)


class TestMemoizedClassifierBadData(unittest.TestCase):
    def test_nat_transaction_date_does_not_crash(self):
        # A NaT transaction_date makes {d.year for d in ...} yield [nan]
        # (float), then int(year) raises "cannot convert float NaN to integer"
        # at __init__ — killing classification for every sibling row too.
        # The valid insider has trades in 2019/2020/2021 so its 2022
        # classification is deterministically NOT UNCLASSIFIED — proving the
        # loop ran past the NaT row rather than crashing.
        from alphalens_pipeline.scorers.cohen_malloy_classifier import CohenMalloyLabel

        history = _history(
            [
                {"reporting_owner_cik": "0000001000", "transaction_date": pd.NaT},
                {"reporting_owner_cik": "0000001000", "transaction_date": date(2019, 5, 1)},
                {"reporting_owner_cik": "0000001000", "transaction_date": date(2020, 5, 1)},
                {"reporting_owner_cik": "0000001000", "transaction_date": date(2021, 5, 1)},
                {"reporting_owner_cik": "0000001000", "transaction_date": date(2022, 5, 1)},
            ]
        )

        classifier = MemoizedClassifier(history)

        self.assertNotEqual(
            classifier.get("0000001000", 2022),
            CohenMalloyLabel.UNCLASSIFIED,
        )

    def test_null_cik_row_excluded_not_orphaned(self):
        from alphalens_pipeline.scorers.cohen_malloy_classifier import CohenMalloyLabel

        history = _history(
            [
                {"reporting_owner_cik": None, "transaction_date": date(2021, 5, 1)},
                {"reporting_owner_cik": "0000001000", "transaction_date": date(2019, 5, 1)},
                {"reporting_owner_cik": "0000001000", "transaction_date": date(2020, 5, 1)},
                {"reporting_owner_cik": "0000001000", "transaction_date": date(2021, 5, 1)},
                {"reporting_owner_cik": "0000001000", "transaction_date": date(2022, 5, 1)},
            ]
        )

        classifier = MemoizedClassifier(history)

        # No unreachable 'nan'-keyed group should exist.
        self.assertEqual(classifier.get("nan", 2022), CohenMalloyLabel.UNCLASSIFIED)
        # The valid-CIK row is still classified.
        self.assertNotEqual(
            classifier.get("0000001000", 2022),
            CohenMalloyLabel.UNCLASSIFIED,
        )


if __name__ == "__main__":
    unittest.main()

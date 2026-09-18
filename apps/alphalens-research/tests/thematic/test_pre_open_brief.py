"""The recovered pre-open lists, as the pipeline sees them (#1494 follow-up).

The names live in code because the pipeline must not read `docs/`, but the record they
come from is the committed CSV. These tests keep the two equal, and keep the dates the
recovery could NOT answer out of the constant.
"""

from __future__ import annotations

import csv
import datetime as dt
import unittest
from pathlib import Path

from alphalens_pipeline.thematic import pre_open_brief
from alphalens_pipeline.thematic.publication import PUBLISHED_AFTER_OPEN_HISTORY

REPO_ROOT = Path(__file__).resolve().parents[4]
RECOVERED_CSV = REPO_ROOT / "docs/research/pre_open_brief_names_2026_09_18.csv"


def _csv_rows() -> list[dict[str, str]]:
    with RECOVERED_CSV.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class TheConstantIsTheCommittedRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = _csv_rows()

    def test_every_exact_date_is_present_with_its_names_in_order(self) -> None:
        expected: dict[dt.date, list[str]] = {}
        for row in sorted(self.rows, key=lambda r: (r["asof"], int(r["position"]))):
            if row["recovery_status"] == "exact":
                expected.setdefault(dt.date.fromisoformat(row["asof"]), []).append(row["ticker"])
        self.assertEqual(
            pre_open_brief.PRE_OPEN_BRIEF_NAMES,
            {asof: tuple(names) for asof, names in expected.items()},
        )

    def test_the_counts_are_the_ones_the_record_reports(self) -> None:
        self.assertEqual(len(pre_open_brief.PRE_OPEN_BRIEF_NAMES), 14)
        self.assertEqual(sum(len(v) for v in pre_open_brief.PRE_OPEN_BRIEF_NAMES.values()), 155)

    def test_a_date_the_recovery_could_not_answer_is_absent(self) -> None:
        # 2026-05-28 is partial (the printer stopped at 25 rows); the other two had no
        # list before the open at all.
        for asof in (dt.date(2026, 5, 28), dt.date(2026, 6, 9), dt.date(2026, 5, 19)):
            self.assertIsNone(pre_open_brief.pre_open_names(asof))

    def test_every_recovered_date_is_one_the_record_calls_after_open(self) -> None:
        self.assertTrue(set(pre_open_brief.PRE_OPEN_BRIEF_NAMES) <= PUBLISHED_AFTER_OPEN_HISTORY)

    def test_no_date_repeats_a_name(self) -> None:
        for asof, names in pre_open_brief.PRE_OPEN_BRIEF_NAMES.items():
            with self.subTest(asof=asof):
                self.assertEqual(len(set(names)), len(names))


class TheLookupTest(unittest.TestCase):
    def test_a_recovered_date_answers_with_its_list(self) -> None:
        self.assertEqual(
            pre_open_brief.pre_open_names(dt.date(2026, 6, 4)), ("CRL", "FDS", "GME", "IRDM")
        )

    def test_an_ordinary_date_answers_with_nothing(self) -> None:
        self.assertIsNone(pre_open_brief.pre_open_names(dt.date(2026, 9, 15)))


if __name__ == "__main__":
    unittest.main()

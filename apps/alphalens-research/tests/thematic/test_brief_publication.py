"""The brief for a date is decided once, and never changes after publication (#1479).

The owner reads the brief before the NYSE open and does not look again. Whatever
``/edge`` and the ML labels measure must be the list that existed at that read,
so the stages ask one question before they touch a date: is its brief already
published, still open, or past its deadline?
"""

from __future__ import annotations

import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic import publication
from alphalens_pipeline.thematic.publication import PublicationStatus

REPO_ROOT = Path(__file__).resolve().parents[4]
HISTORY_CSV = REPO_ROOT / "docs/research/thematic_brief_publication_history_2026_09_16.csv"

ASOF = dt.date(2026, 9, 10)  # Thursday; arrival Friday 2026-09-11, open 13:30 UTC
DEADLINE = dt.datetime(2026, 9, 11, 13, 30, tzinfo=dt.UTC)


def _utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


class DeadlineTest(unittest.TestCase):
    def test_deadline_is_the_open_of_the_next_session(self) -> None:
        self.assertEqual(publication.deadline_utc(ASOF), DEADLINE)

    def test_friday_brief_is_due_at_monday_open(self) -> None:
        self.assertEqual(publication.deadline_utc(dt.date(2026, 9, 11)), _utc(2026, 9, 14, 13, 30))

    def test_weekend_brief_is_due_at_monday_open(self) -> None:
        self.assertEqual(publication.deadline_utc(dt.date(2026, 9, 12)), _utc(2026, 9, 14, 13, 30))

    def test_holiday_is_skipped_and_winter_open_is_1430_utc(self) -> None:
        # Thursday 2026-11-26 is Thanksgiving; EST, so the open is 14:30 UTC.
        self.assertEqual(
            publication.deadline_utc(dt.date(2026, 11, 25)), _utc(2026, 11, 27, 14, 30)
        )


class PublicationStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.briefs_dir = Path(self._tmp.name)
        self.path = self.briefs_dir / f"{ASOF.isoformat()}.parquet"

    def _status(self, now: dt.datetime) -> PublicationStatus:
        return publication.publication_status(ASOF, self.briefs_dir, now=now)

    def test_no_brief_before_the_deadline_is_open(self) -> None:
        self.assertIs(self._status(_utc(2026, 9, 11, 0, 30)), PublicationStatus.OPEN)

    def test_no_brief_at_the_deadline_is_closed(self) -> None:
        self.assertIs(self._status(DEADLINE), PublicationStatus.CLOSED)

    def test_brief_with_rows_is_published_before_and_after_the_deadline(self) -> None:
        pd.DataFrame({"ticker": ["AAA"]}).to_parquet(self.path, index=False)
        self.assertIs(self._status(_utc(2026, 9, 11, 4, 30)), PublicationStatus.PUBLISHED)
        self.assertIs(self._status(_utc(2026, 9, 12, 0, 30)), PublicationStatus.PUBLISHED)

    def test_empty_brief_is_not_published(self) -> None:
        # A quiet day or a degraded run: replaceable until the open.
        pd.DataFrame({"ticker": pd.Series([], dtype=str)}).to_parquet(self.path, index=False)
        self.assertIs(self._status(_utc(2026, 9, 11, 4, 30)), PublicationStatus.OPEN)
        self.assertIs(self._status(DEADLINE), PublicationStatus.CLOSED)

    def test_unreadable_brief_is_never_rebuilt(self) -> None:
        # A read error must not look like "no brief": that would regenerate a list
        # the owner may already have read.
        self.path.write_bytes(b"not a parquet file")
        with self.assertLogs(publication.logger, level="WARNING"):
            status = self._status(_utc(2026, 9, 11, 4, 30))
        self.assertIs(status, PublicationStatus.UNREADABLE)

    def test_a_read_error_on_an_existing_file_is_unreadable(self) -> None:
        pd.DataFrame({"ticker": ["AAA"]}).to_parquet(self.path, index=False)
        with (
            patch.object(publication.pq, "read_metadata", side_effect=OSError("io")),
            self.assertLogs(publication.logger, level="WARNING"),
        ):
            status = self._status(_utc(2026, 9, 11, 4, 30))
        self.assertIs(status, PublicationStatus.UNREADABLE)


class PublishedBeforeOpenTest(unittest.TestCase):
    def test_stamp_before_the_deadline(self) -> None:
        stamp = pd.Timestamp("2026-09-11T01:10:00Z")
        self.assertIs(publication.published_before_open(ASOF, stamp), True)

    def test_stamp_at_or_after_the_deadline(self) -> None:
        self.assertIs(publication.published_before_open(ASOF, pd.Timestamp(DEADLINE)), False)

    def test_no_stamp_on_a_date_recorded_after_the_open(self) -> None:
        self.assertIs(publication.published_before_open(dt.date(2026, 8, 18), None), False)

    def test_no_stamp_on_a_recorded_date_that_was_set_before_the_open(self) -> None:
        self.assertIs(publication.published_before_open(dt.date(2026, 8, 17), None), True)

    def test_no_stamp_outside_the_journal_record_is_unknown(self) -> None:
        self.assertIsNone(publication.published_before_open(dt.date(2026, 5, 20), None))
        self.assertIsNone(publication.published_before_open(dt.date(2026, 9, 20), None))

    def test_a_null_stamp_counts_as_no_stamp(self) -> None:
        self.assertIsNone(publication.published_before_open(dt.date(2026, 9, 20), pd.NaT))


class HistoryMatchesTheJournalRecordTest(unittest.TestCase):
    """The constants are copied from the committed journal record; keep them equal."""

    def setUp(self) -> None:
        with HISTORY_CSV.open(encoding="utf-8") as fh:
            self.rows = list(csv.DictReader(fh))

    def test_after_open_dates_match_the_record(self) -> None:
        recorded = {
            dt.date.fromisoformat(r["asof"]) for r in self.rows if r["recomputed_after_open"] == "1"
        }
        self.assertEqual(len(recorded), 16)
        self.assertEqual(publication.PUBLISHED_AFTER_OPEN_HISTORY, recorded)

    def test_record_window_matches_the_record(self) -> None:
        dates = [dt.date.fromisoformat(r["asof"]) for r in self.rows]
        self.assertEqual(publication.HISTORY_RECORD_WINDOW, (min(dates), max(dates)))


if __name__ == "__main__":
    unittest.main()

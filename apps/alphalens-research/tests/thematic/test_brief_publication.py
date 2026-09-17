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
PRE_JOURNAL_CSV = (
    REPO_ROOT / "docs/research/thematic_brief_publication_history_pre_journal_2026_09_17.csv"
)

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

    def test_no_stamp_on_a_pre_journal_date_built_after_the_open(self) -> None:
        self.assertIs(publication.published_before_open(dt.date(2026, 5, 19), None), False)

    def test_no_stamp_on_a_pre_journal_date_built_before_the_open(self) -> None:
        self.assertIs(publication.published_before_open(dt.date(2026, 5, 20), None), True)

    def test_no_stamp_outside_the_record_is_unknown(self) -> None:
        self.assertIsNone(publication.published_before_open(dt.date(2026, 5, 18), None))
        self.assertIsNone(publication.published_before_open(dt.date(2026, 9, 20), None))

    def test_a_null_stamp_counts_as_no_stamp(self) -> None:
        self.assertIsNone(publication.published_before_open(dt.date(2026, 9, 20), pd.NaT))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _after_open(rows: list[dict[str, str]], flag: str) -> set[dt.date]:
    return {dt.date.fromisoformat(r["asof"]) for r in rows if r[flag] == "1"}


class HistoryMatchesTheRecordTest(unittest.TestCase):
    """The constants are copied from the two committed records; keep them equal.

    The journal record covers 2026-05-24 onwards. The pre-journal record covers the
    older dates from the brief stamps and file times on the VPS.
    """

    def setUp(self) -> None:
        self.journal = _read_csv(HISTORY_CSV)
        self.pre_journal = _read_csv(PRE_JOURNAL_CSV)

    def test_after_open_dates_match_the_journal_record(self) -> None:
        recorded = _after_open(self.journal, "recomputed_after_open")
        self.assertEqual(len(recorded), 16)
        self.assertTrue(recorded <= publication.PUBLISHED_AFTER_OPEN_HISTORY)

    def test_after_open_dates_match_the_pre_journal_record(self) -> None:
        recorded = _after_open(self.pre_journal, "published_after_open")
        self.assertEqual(recorded, {dt.date(2026, 5, 19)})
        self.assertTrue(recorded <= publication.PUBLISHED_AFTER_OPEN_HISTORY)

    def test_after_open_dates_are_exactly_the_two_records(self) -> None:
        recorded = _after_open(self.journal, "recomputed_after_open") | _after_open(
            self.pre_journal, "published_after_open"
        )
        self.assertEqual(publication.PUBLISHED_AFTER_OPEN_HISTORY, recorded)

    def test_the_records_cover_every_day_of_the_window_exactly_once(self) -> None:
        # A day inside the window without a row would silently read as "before the open".
        dates = [dt.date.fromisoformat(r["asof"]) for r in self.journal + self.pre_journal]
        first, last = publication.HISTORY_RECORD_WINDOW
        every_day = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
        self.assertEqual(sorted(dates), every_day)

    def test_the_pre_journal_record_ends_where_the_journal_record_starts(self) -> None:
        journal = [dt.date.fromisoformat(r["asof"]) for r in self.journal]
        pre_journal = [dt.date.fromisoformat(r["asof"]) for r in self.pre_journal]
        self.assertEqual(max(pre_journal) + dt.timedelta(days=1), min(journal))

    def test_record_window_matches_the_records(self) -> None:
        dates = [dt.date.fromisoformat(r["asof"]) for r in self.journal + self.pre_journal]
        self.assertEqual(publication.HISTORY_RECORD_WINDOW, (min(dates), max(dates)))

    def test_pre_journal_flag_follows_the_last_stamp_and_the_open(self) -> None:
        for r in self.pre_journal:
            with self.subTest(asof=r["asof"]):
                last = dt.datetime.fromisoformat(r["last_brief_generated_utc"])
                open_utc = dt.datetime.fromisoformat(r["arrival_open_utc"])
                self.assertEqual(r["published_after_open"], str(int(last >= open_utc)))
                self.assertEqual(
                    open_utc, publication.deadline_utc(dt.date.fromisoformat(r["asof"]))
                )

    def test_a_pre_journal_date_reads_before_the_open_only_with_consistent_files(self) -> None:
        # The stamp alone is not enough: the meta file of the same run must count the same
        # rows, and the brief must carry exactly the tickers of the candidate file.
        for r in self.pre_journal:
            if r["published_after_open"] != "0":
                continue
            with self.subTest(asof=r["asof"]):
                self.assertEqual(r["meta_rows"], r["brief_rows"])
                self.assertEqual(r["brief_tickers_equal_candidates"], "1")
                self.assertLess(
                    dt.datetime.fromisoformat(r["candidates_parquet_mtime_utc"]),
                    dt.datetime.fromisoformat(r["arrival_open_utc"]),
                )


if __name__ == "__main__":
    unittest.main()

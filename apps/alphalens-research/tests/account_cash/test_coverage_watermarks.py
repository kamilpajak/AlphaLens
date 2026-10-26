"""What the store can speak for, and where it cannot (#1689, memo section 5).

``history_begins`` and ``swept_through`` are published on every total, and they
are the containment for the one incompleteness this layer cannot detect: an
episode whose entry is PARTLY before the store never takes the quantity walk
negative and reads as a complete round trip with half the position (amendment
W8). No test asserts that detection, because it does not exist; these tests
assert the watermarks that bound it instead.

Pure over :class:`SweepRecord` values -- no store, no isolated home. The
refusal a ``--since`` before ``history_begins`` answers is the store-reading
half and lives in ``test_report.py`` beside the rest of ``build_totals``.
"""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.account_cash import report
from alphalens_pipeline.account_cash.codec import SweepRecord

from tests.account_cash.cashbook_fixture import BOOKINGS

_LIVE = "live"


def _sweep(since: str, until: str, *, swept_at: str = "2026-10-03") -> SweepRecord:
    """One stored sweep record, carrying only the fields coverage reads.

    The windows are what the caller ASKED for, which is what
    ``before_first_sweep`` is about: the store can speak for a window it was
    asked to read, whether or not the vendor had a row in every day of it.

    ``swept_at`` is separate from ``until`` so a record can carry a window
    that will not parse while still being a decodable record -- which is the
    case a hand-edited journal line produces.
    """
    stamp = dt.datetime.fromisoformat(f"{swept_at}T00:31:00+00:00")
    return SweepRecord(
        sweep_id=f"{since}..{until}",
        swept_at=stamp,
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


class TheHistoryWatermark(unittest.TestCase):
    def test_history_begins_is_the_earliest_window_asked_not_the_earliest_row(self) -> None:
        """**Mutation it catches:** ``min(event.date)``, which answers
        ``2026-08-06`` -- the account's first deposit (memo section 1.6 P3).

        The two differ by five days on the real capture, and a ``--since`` of
        ``2026-08-03`` is the input that separates them: the store was asked
        to read from 08-01, so it can speak for 08-03 and answer that nothing
        happened, while 2026-07-31 it cannot speak for at all.
        """
        self.assertEqual(min(event.date or "" for event in BOOKINGS), "2026-08-06")

        begins = report.history_begins([_sweep("2026-08-01", "2026-10-03")])

        self.assertEqual(begins, "2026-08-01")

    def test_no_sweep_has_no_history_at_all(self) -> None:
        self.assertIsNone(report.history_begins([]))

    def test_the_earliest_window_wins_whichever_sweep_ran_first(self) -> None:
        """**Mutation it catches:** reading the FIRST record's ``since``.

        The records are published oldest-by-``swept_at``, and a backfill asked
        for an earlier window than the daily sweep that preceded it.
        """
        begins = report.history_begins(
            [_sweep("2026-09-01", "2026-09-30"), _sweep("2026-08-01", "2026-08-31")]
        )

        self.assertEqual(begins, "2026-08-01")


class TheSweptThroughWatermarkIsTheContiguousRunNotTheLatestEnd(unittest.TestCase):
    def test_two_sweeps_with_a_hole_do_not_claim_the_later_end(self) -> None:
        """**Mutation it catches:** ``max(record.until)``, which claims
        coverage through 09-10 over a 21-day hole."""
        coverage = report.coverage_of(
            [_sweep("2026-08-01", "2026-08-10"), _sweep("2026-09-01", "2026-09-10")]
        )

        self.assertEqual(coverage.history_begins, "2026-08-01")
        self.assertEqual(coverage.swept_through, "2026-08-10")
        self.assertEqual(coverage.gaps, (("2026-08-11", "2026-08-31"),))
        self.assertEqual(coverage.sweeps, 2)

    def test_adjacent_sweeps_are_one_run(self) -> None:
        """**Mutation it catches:** a contiguity test using
        ``next.since <= prev.until`` instead of ``<= prev.until + 1 day``,
        which reports a one-day gap between every pair of back-to-back
        sweeps."""
        coverage = report.coverage_of(
            [_sweep("2026-08-01", "2026-08-10"), _sweep("2026-08-11", "2026-08-20")]
        )

        self.assertEqual(coverage.swept_through, "2026-08-20")
        self.assertEqual(coverage.gaps, ())

    def test_an_overlapping_sweep_is_one_run(self) -> None:
        coverage = report.coverage_of(
            [_sweep("2026-08-01", "2026-08-15"), _sweep("2026-08-10", "2026-08-20")]
        )

        self.assertEqual(coverage.swept_through, "2026-08-20")
        self.assertEqual(coverage.gaps, ())

    def test_a_sweep_wholly_inside_another_extends_nothing(self) -> None:
        """A re-sweep of a window already covered. **Mutation:**
        ``cur_end = next.until`` without the ``max``, which would SHRINK the
        run back to 08-05 and invent a gap that is covered."""
        coverage = report.coverage_of(
            [_sweep("2026-08-01", "2026-08-20"), _sweep("2026-08-01", "2026-08-05")]
        )

        self.assertEqual(coverage.swept_through, "2026-08-20")
        self.assertEqual(coverage.gaps, ())

    def test_the_windows_are_compared_as_dates_and_not_as_strings(self) -> None:
        """**Mutation it catches:** string arithmetic on the ISO bounds, which
        cannot add a day across a month boundary."""
        coverage = report.coverage_of(
            [_sweep("2026-08-25", "2026-08-31"), _sweep("2026-09-01", "2026-09-20")]
        )

        self.assertEqual(coverage.swept_through, "2026-09-20")
        self.assertEqual(coverage.gaps, ())

    def test_every_hole_is_published_not_only_the_first(self) -> None:
        """Three runs and two holes. **Mutation:** returning after the first
        gap, which publishes a shorter list that reads as fuller coverage."""
        coverage = report.coverage_of(
            [
                _sweep("2026-08-01", "2026-08-10"),
                _sweep("2026-08-20", "2026-08-25"),
                _sweep("2026-09-10", "2026-09-20"),
            ]
        )

        self.assertEqual(coverage.swept_through, "2026-08-10")
        self.assertEqual(
            coverage.gaps,
            (("2026-08-11", "2026-08-19"), ("2026-08-26", "2026-09-09")),
        )

    def test_no_sweep_is_no_coverage_rather_than_a_zero_length_run(self) -> None:
        coverage = report.coverage_of([])

        self.assertIsNone(coverage.history_begins)
        self.assertIsNone(coverage.swept_through)
        self.assertEqual(coverage.gaps, ())
        self.assertEqual(coverage.sweeps, 0)

    def test_a_record_whose_window_will_not_parse_is_counted_and_not_dropped(self) -> None:
        """A hand-edited journal line is the reachable case: the codec takes
        ``since`` / ``until`` as free text, so a non-ISO window decodes.

        **Mutation it catches:** skipping it silently, which would shrink the
        coverage without saying so -- the store-reading shape of the rule that
        a dropped row must make an identity fail, not vanish.
        """
        coverage = report.coverage_of(
            [_sweep("2026-08-01", "2026-08-10"), _sweep("last tuesday", "whenever")]
        )

        self.assertEqual(coverage.sweeps, 2)
        self.assertEqual(coverage.malformed_windows, 1)
        self.assertEqual(coverage.swept_through, "2026-08-10")

    def test_a_refused_sweep_leaves_no_record_so_coverage_cannot_count_one(self) -> None:
        """``refused_reason`` is ``None`` on every record that reaches disk.

        **Mutation it catches:** treating the field as a log of refused
        sweeps, which reads an empty set as "no sweep was ever refused".
        ``apply_sweep`` raises ``SweepRefusedError`` BEFORE appending, so a
        refused sweep is a window with no record at all -- which coverage
        reports as a gap, not as a refusal.
        """
        covered = report.coverage_of([_sweep("2026-08-01", "2026-08-10")])

        self.assertEqual(covered.gaps, ())
        self.assertIsNone(_sweep("2026-08-01", "2026-08-10").refused_reason)


class TheLimitTheWatermarksContain(unittest.TestCase):
    def test_the_report_names_what_this_source_cannot_detect(self) -> None:
        """Amendment W8: say it on the report, not only in the memo.

        An entry PARTLY before the store -- bought 10 before, bought 10
        inside, sold 10 inside -- never takes the quantity walk negative, so
        ``window_clipped`` cannot see it. The containment is these watermarks
        plus the ``before_first_sweep`` refusal, and a reader of the published
        total has to be able to see that the limit exists.
        """
        coverage = report.coverage_of([_sweep("2026-08-01", "2026-08-10")])

        self.assertEqual(coverage.cannot_detect, (report.UNDETECTED_PARTIAL_ENTRY,))
        self.assertIn("entry", report.UNDETECTED_PARTIAL_ENTRY)


if __name__ == "__main__":
    unittest.main()

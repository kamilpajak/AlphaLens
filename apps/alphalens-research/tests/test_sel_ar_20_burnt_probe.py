"""Unit tests for the two pure helpers of the burnt-panel `sel_ar_20` probe.

Hermetic: no store is read, so no outcome value is touched. These cover the two
pieces of real logic that could be silently wrong and that no store read would
reveal — the rule that picks one publication timestamp when an article has
several recorded, and the construction of contiguous arrival-session blocks.

The positive controls matter more than the happy paths: a tie-break that always
returned the latest timestamp, or a fold builder that dropped the runt instead
of merging it, would pass every straightforward case.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import unittest
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "ml" / "2026_09_sel_ar_20_burnt_probe.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("_burnt_probe", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load()


def _ts(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso).replace(tzinfo=dt.UTC)


class TestPickNewsTimestamp(unittest.TestCase):
    """The brief records only a DATE; the news store keeps the real timestamp.

    Two urls in the burnt window carry two recorded timestamps about 46 hours
    apart, touching 6 panel rows, so "join on url" alone is ambiguous. The
    frozen rule keeps the recovered value consistent with the date the pipeline
    itself stamped.
    """

    def test_a_candidate_on_the_stamped_date_wins_over_a_later_one(self) -> None:
        got = probe.pick_news_timestamp(
            [_ts("2026-06-03T18:00:00"), _ts("2026-06-01T09:00:00")],
            dt.date(2026, 6, 1),
        )
        self.assertEqual(got, _ts("2026-06-01T09:00:00"))

    def test_among_several_on_the_stamped_date_the_latest_wins(self) -> None:
        got = probe.pick_news_timestamp(
            [_ts("2026-06-01T09:00:00"), _ts("2026-06-01T21:30:00"), _ts("2026-06-01T04:00:00")],
            dt.date(2026, 6, 1),
        )
        self.assertEqual(got, _ts("2026-06-01T21:30:00"))

    def test_with_nothing_on_the_stamped_date_the_latest_overall_wins(self) -> None:
        got = probe.pick_news_timestamp(
            [_ts("2026-05-30T08:00:00"), _ts("2026-06-04T12:00:00")],
            dt.date(2026, 6, 1),
        )
        self.assertEqual(got, _ts("2026-06-04T12:00:00"))

    def test_no_candidate_resolves_to_none(self) -> None:
        self.assertIsNone(probe.pick_news_timestamp([], dt.date(2026, 6, 1)))

    def test_the_date_match_is_not_just_the_latest_in_disguise(self) -> None:
        """Positive control. A rule that always returned the latest timestamp
        would pass the three cases above except this one."""
        got = probe.pick_news_timestamp(
            [_ts("2026-06-01T02:00:00"), _ts("2026-06-09T23:00:00")],
            dt.date(2026, 6, 1),
        )
        self.assertEqual(got, _ts("2026-06-01T02:00:00"))
        self.assertNotEqual(got, max([_ts("2026-06-01T02:00:00"), _ts("2026-06-09T23:00:00")]))


class TestTheAnchorIsReadNeverRecomputed(unittest.TestCase):
    """A source gate, because no behavioural test would catch this.

    The label store carries `anchor_session` — the first session AFTER the
    brief date (owner decision D4). `session_on_or_after` returns the brief
    date itself whenever that date is a trading session, so recomputing the
    anchor with it disagrees with the stored value on 296 of 466 burnt rows.
    The first run of this probe did exactly that and measured 65 articles as
    published after their own anchor open; they were not.

    A test that merely checked "the panel has an arrival column" would pass
    either way, so this reads the source instead.
    """

    @staticmethod
    def _called_names(source: str) -> set[str]:
        called = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        return called

    def test_the_module_never_calls_session_on_or_after(self) -> None:
        self.assertNotIn(
            "session_on_or_after",
            self._called_names(_SCRIPT.read_text()),
            "the arrival anchor is READ from the label store's `anchor_session`; "
            "recomputing it is a second definition of the label",
        )

    def test_the_gate_reads_calls_and_not_prose(self) -> None:
        """Positive control, both ways. A substring check would fire on the
        docstring that explains the rule, and would miss a call written as an
        attribute."""
        self.assertIn("session_on_or_after", _SCRIPT.read_text())  # named in prose
        self.assertIn("f", self._called_names("f(1)"))
        self.assertIn("g", self._called_names("mod.g(1)"))
        self.assertNotIn("h", self._called_names('"""h is forbidden"""'))

    def test_the_module_does_read_the_stored_anchor(self) -> None:
        """A file that dropped the anchor entirely would satisfy the gate."""
        self.assertIn("anchor_session", _SCRIPT.read_text())


class TestContiguousBlockFolds(unittest.TestCase):
    """Folds are contiguous blocks of arrival sessions, runt merged into its
    predecessor. Purged folds are infeasible here (a 20-session label over a
    30-session arrival span), so these are the unpurged blocks house rule 9
    prescribes for exactly this situation."""

    @staticmethod
    def _sessions(n: int) -> list[dt.date]:
        return [dt.date(2026, 5, 20) + dt.timedelta(days=i) for i in range(n)]

    def test_every_session_lands_in_exactly_one_block(self) -> None:
        sessions = self._sessions(27)
        blocks = probe.contiguous_block_folds(sessions, block_sessions=5)
        flat = [s for b in blocks for s in b]
        self.assertEqual(sorted(flat), sorted(sessions))
        self.assertEqual(len(flat), len(set(flat)))

    def test_the_runt_is_merged_into_its_predecessor_not_dropped(self) -> None:
        """Positive control. 27 sessions at 5 per block leaves a 2-session runt;
        dropping it would still satisfy 'blocks are contiguous'."""
        blocks = probe.contiguous_block_folds(self._sessions(27), block_sessions=5)
        self.assertEqual(len(blocks), 5)
        self.assertEqual([len(b) for b in blocks], [5, 5, 5, 5, 7])

    def test_an_exact_multiple_leaves_equal_blocks(self) -> None:
        blocks = probe.contiguous_block_folds(self._sessions(25), block_sessions=5)
        self.assertEqual([len(b) for b in blocks], [5, 5, 5, 5, 5])

    def test_blocks_stay_in_calendar_order(self) -> None:
        blocks = probe.contiguous_block_folds(self._sessions(27), block_sessions=5)
        self.assertEqual([b[0] for b in blocks], sorted(b[0] for b in blocks))
        for block in blocks:
            self.assertEqual(block, sorted(block))

    def test_fewer_sessions_than_one_block_is_a_single_fold(self) -> None:
        blocks = probe.contiguous_block_folds(self._sessions(3), block_sessions=5)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]), 3)

    def test_unsorted_input_is_ordered_before_blocking(self) -> None:
        sessions = self._sessions(10)
        blocks = probe.contiguous_block_folds(list(reversed(sessions)), block_sessions=5)
        self.assertEqual([s for b in blocks for s in b], sessions)


if __name__ == "__main__":
    unittest.main()

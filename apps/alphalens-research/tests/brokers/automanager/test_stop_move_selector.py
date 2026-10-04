"""#1669: ``select_stop_move_lines`` is the ONE answer to "which marker moved
this stop", shared by the stop-fill alert, the boot compactor and ``broker
trades``. These pin its window and tie rules on their own, so a reader that
disagrees with another cannot hide behind a fold that happens to agree."""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager import stop_journal as sj

_UIC = 333
_OTHER_UIC = 444


def _trailed(level: Any, ts: float, *, uic: int = _UIC) -> dict[str, Any]:
    return {"kind": "trailed", "uic": uic, "level": level, "ts": ts}


def _reanchored(stop: Any, ts: float, *, uic: int = _UIC) -> dict[str, Any]:
    return {"kind": "reanchored", "uic": uic, "avg_price": 10.0, "stop_price": stop, "ts": ts}


class TestSelectStopMoveLines(unittest.TestCase):
    def test_the_newest_marker_at_or_after_since_wins(self) -> None:
        lines = [_trailed(12.0, 104.0), _reanchored(12.5, 106.0), _trailed(13.0, 105.0)]
        self.assertEqual(sj.select_stop_move_lines(lines, {_UIC: 100.0}), {_UIC: lines[1]})

    def test_a_marker_at_since_is_inside_the_window(self) -> None:
        lines = [_trailed(13.0, 102.0)]
        self.assertEqual(sj.select_stop_move_lines(lines, {_UIC: 102.0}), {_UIC: lines[0]})

    def test_a_marker_before_since_is_outside_the_window(self) -> None:
        lines = [_trailed(13.0, 101.9)]
        self.assertEqual(sj.select_stop_move_lines(lines, {_UIC: 102.0}), {})

    def test_until_bounds_the_window_inclusively(self) -> None:
        lines = [_trailed(12.0, 104.0), _trailed(13.0, 108.0), _trailed(14.0, 108.1)]
        self.assertEqual(
            sj.select_stop_move_lines(lines, {_UIC: 100.0}, {_UIC: 108.0}), {_UIC: lines[1]}
        )

    def test_a_uic_without_an_until_is_open_ended(self) -> None:
        lines = [_trailed(13.0, 108.0)]
        self.assertEqual(
            sj.select_stop_move_lines(lines, {_UIC: 100.0}, {_OTHER_UIC: 101.0}), {_UIC: lines[0]}
        )

    def test_a_timestamp_tie_goes_to_the_later_line(self) -> None:
        lines = [_trailed(13.0, 104.0), _reanchored(12.5, 104.0)]
        self.assertEqual(sj.select_stop_move_lines(lines, {_UIC: 100.0}), {_UIC: lines[1]})
        self.assertEqual(
            sj.select_stop_move_lines(list(reversed(lines)), {_UIC: 100.0}), {_UIC: lines[0]}
        )

    def test_an_unparseable_level_is_skipped(self) -> None:
        good = _trailed(13.0, 104.0)
        for bad in (
            _trailed("junk", 106.0),
            _trailed(None, 106.0),
            _trailed(float("nan"), 106.0),
            _reanchored(-1.0, 106.0),
            {"kind": "reanchored", "uic": _UIC, "avg_price": 10.0, "ts": 106.0},
        ):
            with self.subTest(bad=bad):
                self.assertEqual(
                    sj.select_stop_move_lines([good, bad], {_UIC: 100.0}), {_UIC: good}
                )

    def test_an_unparseable_timestamp_is_skipped(self) -> None:
        good = _trailed(13.0, 104.0)
        bad = {"kind": "trailed", "uic": _UIC, "level": 14.0, "ts": "later"}
        self.assertEqual(sj.select_stop_move_lines([good, bad], {_UIC: 100.0}), {_UIC: good})

    def test_a_non_finite_timestamp_is_skipped(self) -> None:
        good = _trailed(13.0, 104.0)
        for ts in (float("inf"), float("nan")):
            bad = _trailed(14.0, ts)
            for order in ([good, bad], [bad, good]):
                with self.subTest(ts=ts, first=order[0]["level"]):
                    self.assertEqual(sj.select_stop_move_lines(order, {_UIC: 100.0}), {_UIC: good})

    def test_another_uics_marker_is_ignored(self) -> None:
        lines = [_trailed(13.0, 104.0, uic=_OTHER_UIC)]
        self.assertEqual(sj.select_stop_move_lines(lines, {_UIC: 100.0}), {})

    def test_each_uic_gets_its_own_window(self) -> None:
        mine = _trailed(13.0, 104.0)
        theirs = _trailed(20.0, 104.0, uic=_OTHER_UIC)
        self.assertEqual(
            sj.select_stop_move_lines([mine, theirs], {_UIC: 100.0, _OTHER_UIC: 105.0}),
            {_UIC: mine},
        )

    def test_other_kinds_are_not_stop_moves(self) -> None:
        lines = [{"kind": "stop_placed", "uic": _UIC, "stop_price": 9.0, "ts": 104.0}]
        self.assertEqual(sj.select_stop_move_lines(lines, {_UIC: 100.0}), {})


if __name__ == "__main__":
    unittest.main()

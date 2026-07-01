"""Tests for the pure break-even what-if backfill logic.

Fills `breakeven_realized_r_json` onto historical population-ladder rows whose bars
are retained but which the monitor froze before the column existed. NEVER overwrites
an existing (monitor-stamped) value; a row that cannot resolve (no setup / no bars)
is left untouched.
"""

from __future__ import annotations

import unittest

import pandas as pd
from alphalens_research.diagnostics.breakeven_backfill import apply_backfill, rows_needing_backfill

_COL = "breakeven_realized_r_json"


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestRowsNeedingBackfill(unittest.TestCase):
    def test_selects_only_plannable_rows_with_an_empty_column(self):
        df = _df(
            [
                {"ticker": "A", "plannable": True, _COL: ""},  # empty -> needs
                {"ticker": "B", "plannable": True, _COL: '{"be_0p5r": 0.1}'},  # has value -> skip
                {"ticker": "C", "plannable": True, _COL: None},  # None -> needs
                {"ticker": "D", "plannable": False, _COL: ""},  # non-plannable -> skip
            ]
        )
        self.assertEqual(rows_needing_backfill(df), [0, 2])

    def test_absent_column_means_every_plannable_row_needs_it(self):
        df = _df([{"ticker": "A", "plannable": True}, {"ticker": "B", "plannable": False}])
        self.assertEqual(rows_needing_backfill(df), [0])


class TestApplyBackfill(unittest.TestCase):
    def test_fills_only_missing_rows_never_overwrites(self):
        df = _df(
            [
                {"ticker": "A", "plannable": True, _COL: ""},
                {"ticker": "B", "plannable": True, _COL: '{"be_0p5r": 0.9}'},
                {"ticker": "C", "plannable": True, _COL: None},
            ]
        )
        # compute stamps a fixed json for A and C; B already has a value (untouched).
        out, n = apply_backfill(df, lambda row: '{"be_0p5r": 0.5}')
        self.assertEqual(n, 2)
        self.assertEqual(out.loc[0, _COL], '{"be_0p5r": 0.5}')
        self.assertEqual(out.loc[1, _COL], '{"be_0p5r": 0.9}')  # preserved
        self.assertEqual(out.loc[2, _COL], '{"be_0p5r": 0.5}')

    def test_unresolvable_row_left_untouched(self):
        df = _df([{"ticker": "A", "plannable": True, _COL: ""}])
        # compute returns None (no bars / no setup) -> row stays empty, not counted.
        out, n = apply_backfill(df, lambda row: None)
        self.assertEqual(n, 0)
        self.assertEqual(out.loc[0, _COL], "")

    def test_absent_column_is_created_then_filled(self):
        df = _df([{"ticker": "A", "plannable": True}])
        out, n = apply_backfill(df, lambda row: '{"be_0p5r": 0.2}')
        self.assertEqual(n, 1)
        self.assertIn(_COL, out.columns)
        self.assertEqual(out.loc[0, _COL], '{"be_0p5r": 0.2}')

    def test_does_not_mutate_input_frame(self):
        df = _df([{"ticker": "A", "plannable": True, _COL: ""}])
        apply_backfill(df, lambda row: '{"be_0p5r": 0.5}')
        self.assertEqual(df.loc[0, _COL], "")  # original unchanged


if __name__ == "__main__":
    unittest.main()

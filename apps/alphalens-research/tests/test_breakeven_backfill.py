"""Tests for the pure break-even what-if backfill logic.

Fills `breakeven_realized_r_json` onto historical population-ladder rows whose bars
are retained but which the monitor froze before the column existed. NEVER overwrites
an existing (monitor-stamped) value; a row that cannot resolve (no setup / no bars)
is left untouched.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest

import pandas as pd
from alphalens_pipeline.feedback.population_ladder_monitor import _engine_cutoffs
from alphalens_research.diagnostics.breakeven_backfill import (
    UNRESOLVABLE,
    apply_backfill,
    apply_lens_key_backfill,
    entry_ttl_cutoff_ms,
    rows_missing_lens_key,
    rows_needing_backfill,
)

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


# A realistic frozen 5-key map. The atr_bracket_1p5 value stands in for a
# 52w-ceiling-CAPPED monitor stamp that a fresh uncapped recompute would change —
# the merge must preserve it byte-for-value (issue #1232 / the old script's
# full-grid `update()` corruption hazard).
_FROZEN_MAP = json.dumps(
    {
        "be_0p5r": 0.0,
        "fill_anchored_0p5atr": -1.0,
        "be_0p5r_trail0p6": 0.36,
        "atr_bracket_1p5": 0.6802721088435374,
        "atr_bracket_1p5_planned": None,
    }
)
_TTL_ID = "be_0p5r_trail0p6_ttl7"


class TestRowsMissingLensKey(unittest.TestCase):
    def test_selects_nonempty_maps_lacking_or_nulling_the_key(self):
        df = _df(
            [
                {"ticker": "A", "plannable": True, _COL: _FROZEN_MAP},  # lacks key -> needs
                {"ticker": "B", "plannable": True, _COL: f'{{"{_TTL_ID}": null}}'},  # null -> needs
                {"ticker": "C", "plannable": True, _COL: f'{{"{_TTL_ID}": 0.5}}'},  # has -> skip
                {"ticker": "D", "plannable": True, _COL: ""},  # empty cell -> old script's scope
                {"ticker": "E", "plannable": False, _COL: _FROZEN_MAP},  # non-plannable -> skip
                {"ticker": "F", "plannable": True, _COL: None},  # empty cell -> skip
            ]
        )
        self.assertEqual(rows_missing_lens_key(df, _TTL_ID), [0, 1])

    def test_absent_column_yields_no_candidates(self):
        # Nothing to merge INTO — empty-cell seeding stays the old helper's job.
        df = _df([{"ticker": "A", "plannable": True}])
        self.assertEqual(rows_missing_lens_key(df, _TTL_ID), [])


class TestApplyLensKeyBackfill(unittest.TestCase):
    def test_adds_only_the_new_key_and_preserves_every_old_value(self):
        df = _df([{"ticker": "A", "plannable": True, _COL: _FROZEN_MAP}])
        out, n = apply_lens_key_backfill(df, _TTL_ID, lambda row: -1.0)
        self.assertEqual(n, 1)
        merged = json.loads(out.loc[0, _COL])
        original = json.loads(_FROZEN_MAP)
        self.assertEqual(merged[_TTL_ID], -1.0)
        for key, value in original.items():
            self.assertEqual(merged[key], value, f"pre-existing {key} altered by the merge")
        self.assertEqual(set(merged), set(original) | {_TTL_ID})

    def test_computed_null_is_stamped_and_counted(self):
        # A lens that honestly cannot resolve on this row (e.g. no fill within the
        # TTL window... the monitor stamps null forward, the merge must match).
        df = _df([{"ticker": "A", "plannable": True, _COL: _FROZEN_MAP}])
        out, n = apply_lens_key_backfill(df, _TTL_ID, lambda row: None)
        self.assertEqual(n, 1)
        merged = json.loads(out.loc[0, _COL])
        self.assertIn(_TTL_ID, merged)
        self.assertIsNone(merged[_TTL_ID])

    def test_null_key_recovers_but_rerun_is_a_noop(self):
        # An accidental old-script run stamps `"ttl": null` on a resolvable row;
        # the merge recovers it. A second run with the same compute changes
        # nothing and counts zero (idempotent reruns).
        df = _df(
            [{"ticker": "A", "plannable": True, _COL: f'{{"be_0p5r": 0.1, "{_TTL_ID}": null}}'}]
        )
        out, n = apply_lens_key_backfill(df, _TTL_ID, lambda row: 0.25)
        self.assertEqual(n, 1)
        self.assertEqual(json.loads(out.loc[0, _COL])[_TTL_ID], 0.25)
        again, n2 = apply_lens_key_backfill(out, _TTL_ID, lambda row: 0.25)
        self.assertEqual(n2, 0)
        self.assertEqual(again.loc[0, _COL], out.loc[0, _COL])

    def test_existing_nonnull_value_is_never_overwritten(self):
        df = _df([{"ticker": "A", "plannable": True, _COL: f'{{"{_TTL_ID}": 0.5}}'}])
        out, n = apply_lens_key_backfill(df, _TTL_ID, lambda row: -1.0)
        self.assertEqual(n, 0)
        self.assertEqual(json.loads(out.loc[0, _COL])[_TTL_ID], 0.5)

    def test_unresolvable_row_left_untouched(self):
        df = _df([{"ticker": "A", "plannable": True, _COL: _FROZEN_MAP}])
        out, n = apply_lens_key_backfill(df, _TTL_ID, lambda row: UNRESOLVABLE)
        self.assertEqual(n, 0)
        self.assertEqual(out.loc[0, _COL], _FROZEN_MAP)
        self.assertNotIn(_TTL_ID, json.loads(out.loc[0, _COL]))

    def test_does_not_mutate_input_frame(self):
        df = _df([{"ticker": "A", "plannable": True, _COL: _FROZEN_MAP}])
        apply_lens_key_backfill(df, _TTL_ID, lambda row: -1.0)
        self.assertEqual(df.loc[0, _COL], _FROZEN_MAP)


class TestEntryTtlCutoffMs(unittest.TestCase):
    """The offline cutoff derivation must be the EXACT monitor derivation
    (`_engine_cutoffs(...)[5]`), not an approximation — summer AND winter (the
    session-OPEN convention shifts 13:30 <-> 14:30 UTC across DST)."""

    def test_parity_with_engine_cutoffs_summer_and_winter(self):
        for brief_date in (dt.date(2026, 7, 10), dt.date(2026, 1, 15)):
            with self.subTest(brief_date=brief_date):
                expected = _engine_cutoffs(brief_date, {"order_ttl_days": 7}, "XNYS")[5]
                self.assertEqual(entry_ttl_cutoff_ms(brief_date, 7), expected)

    def test_parity_on_a_weekend_brief_date_and_a_nondefault_ttl(self):
        # Saturday brief -> arrival rolls to Monday; ttl 3 exercises the
        # row-stored entry_ttl_days path (never the module default).
        brief_date = dt.date(2026, 7, 11)
        expected = _engine_cutoffs(brief_date, {"order_ttl_days": 3}, "XNYS")[5]
        self.assertEqual(entry_ttl_cutoff_ms(brief_date, 3), expected)


if __name__ == "__main__":
    unittest.main()

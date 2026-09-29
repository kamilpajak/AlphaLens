"""Unit tests for the two pre-run amendments to the cluster-15 last look.

Both amendments answer facts that appeared AFTER the registration froze on
2026-09-01, and both are exercised here on synthetic frames — nothing reads a
store, so no held-out value is touched.

1. Both stores gained shared columns (`source` and `event_overlap` from
   #1307/#1340 on 2026-09-06, `brief_published_at` from #1482 on 2026-09-16).
   The join tripwire has to admit them without losing the power to catch the
   next one.
2. The insider-cluster event lane went live on 2026-09-06 and joins the same
   (brief_date, ticker) key as the thematic lane. The registered estimand is
   the thematic screened population, so the lane is excluded.

The positive controls are the load-bearing half: a tripwire that admits
everything and a lane filter that keeps everything would both pass a test
written only against today's data.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import pandas as pd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ml" / "2026_09_experts_last_look.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_experts_last_look", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


look = _load_script()

# The column sets both stores actually carry, recorded 2026-09-29 on the VPS.
_LADDER_COLUMNS = (
    "brief_date",
    "ticker",
    "theme",
    "scorer_config_version",
    "brief_published_at",
    "event_overlap",
    "source",
    "plannable",
    "market_excess_return",
    "ladder_classification",
)
_BRIEF_COLUMNS = (
    "brief_date",
    "ticker",
    "theme",
    "scorer_config_version",
    "brief_published_at",
    "event_overlap",
    "source",
    "buffett_quality_score",
    "technical_atr_pct",
)


class TestJoinTripwire(unittest.TestCase):
    def test_it_passes_on_the_columns_both_stores_carry_today(self) -> None:
        self.assertEqual(look.unexpected_shared_columns(_LADDER_COLUMNS, _BRIEF_COLUMNS), [])

    def test_it_still_catches_a_genuinely_new_shared_column(self) -> None:
        """Positive control — without this the widened allowlist could rot to
        'admit anything' and the next silently-suffixed column would ship."""
        self.assertEqual(
            look.unexpected_shared_columns(
                (*_LADDER_COLUMNS, "panel_config_version"),
                (*_BRIEF_COLUMNS, "panel_config_version"),
            ),
            ["panel_config_version"],
        )

    def test_a_column_only_one_store_carries_is_not_a_collision(self) -> None:
        self.assertEqual(
            look.unexpected_shared_columns((*_LADDER_COLUMNS, "realized_r"), _BRIEF_COLUMNS),
            [],
        )


def _population(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame([{"brief_date": b, "ticker": t, "source": s} for b, t, s in rows])


class TestThematicLaneOnly(unittest.TestCase):
    def test_the_event_lane_is_excluded_from_the_population(self) -> None:
        frame, dropped = look.thematic_lane_only(
            _population(
                [
                    ("2026-09-05", "AAA", "thematic"),
                    ("2026-09-05", "BWFG", "insider_cluster"),
                    ("2026-09-09", "CCC", "thematic"),
                ]
            )
        )
        self.assertEqual(sorted(frame["ticker"]), ["AAA", "CCC"])
        self.assertEqual(dropped, {"insider_cluster": 1})

    def test_it_counts_every_lane_it_drops_so_the_run_can_print_them(self) -> None:
        _, dropped = look.thematic_lane_only(
            _population(
                [
                    ("2026-09-05", "AAA", "thematic"),
                    ("2026-09-05", "BBB", "insider_cluster"),
                    ("2026-09-06", "CCC", "insider_cluster"),
                    ("2026-09-07", "DDD", "some_future_lane"),
                ]
            )
        )
        self.assertEqual(dropped, {"insider_cluster": 2, "some_future_lane": 1})

    def test_a_population_with_no_thematic_row_becomes_empty(self) -> None:
        """Positive control — a filter that silently kept everything would pass
        every case above that has at least one thematic row."""
        frame, dropped = look.thematic_lane_only(
            _population([("2026-09-05", "BWFG", "insider_cluster")])
        )
        self.assertEqual(len(frame), 0)
        self.assertEqual(dropped, {"insider_cluster": 1})

    def test_a_population_that_is_all_thematic_is_returned_unchanged(self) -> None:
        rows = [("2026-07-06", "AAA", "thematic"), ("2026-07-07", "BBB", "thematic")]
        frame, dropped = look.thematic_lane_only(_population(rows))
        self.assertEqual(len(frame), 2)
        self.assertEqual(dropped, {})

    def test_it_keeps_the_surviving_rows_in_their_original_order(self) -> None:
        """Boolean masking preserves order; a groupby-based rewrite would not,
        and the panel loop reads these rows in sequence."""
        frame, _ = look.thematic_lane_only(
            _population(
                [
                    ("2026-07-06", "AAA", "thematic"),
                    ("2026-07-07", "BBB", "insider_cluster"),
                    ("2026-07-08", "CCC", "thematic"),
                ]
            )
        )
        self.assertEqual(list(frame["ticker"]), ["AAA", "CCC"])

    def test_a_row_with_no_lane_stamp_is_dropped_and_counted(self) -> None:
        """A missing stamp cannot be assumed thematic — the conservative choice
        keeps the estimand narrow, and the count makes the drop visible."""
        frame = _population([("2026-07-06", "AAA", "thematic")])
        frame.loc[1] = {"brief_date": "2026-07-07", "ticker": "BBB", "source": None}
        kept, dropped = look.thematic_lane_only(frame)
        self.assertEqual(list(kept["ticker"]), ["AAA"])
        self.assertEqual(dropped, {"<missing>": 1})


if __name__ == "__main__":
    unittest.main()

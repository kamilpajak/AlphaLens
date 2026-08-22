"""Reading the rollup store as a DIRECTORY silently loses the newest columns.

``pd.read_parquet('~/.alphalens/theme_rollup/')`` builds a pyarrow dataset and
infers its schema from the FIRST fragment. The legacy files on disk predate
``selection_propensity`` / ``tiebreak_seed`` / ``tiebreak_version`` /
``tiebreak_key`` and sort first by filename, so the frame comes back without
those columns — no error, no warning, no missing-value marker. And a directory
read is the natural way to load the store for exactly the off-policy analysis
those columns exist to enable.

So the store gets a reader, and the trap gets a regression test with a legacy
file and a new file side by side.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.extraction import themes

_LEGACY_COLUMNS = (
    "asof",
    "theme",
    "count_window",
    "novelty_score",
    "novelty_rank",
    "selected",
    "novelty_config_version",
)


def _legacy_file(store: Path, asof: str) -> None:
    """A rollup written before the tie-break columns existed."""
    pd.DataFrame(
        [
            {
                "asof": asof,
                "theme": "legacy_theme",
                "count_window": 4,
                "novelty_score": 4.0,
                "novelty_rank": 1,
                "selected": True,
                "novelty_config_version": "cfg-v1",
            }
        ],
        columns=list(_LEGACY_COLUMNS),
    ).to_parquet(store / f"{asof}.parquet", index=False)


def _current_file(store: Path, asof: str) -> None:
    """A rollup written by the current writer, tie-break columns included."""
    frame = pd.DataFrame(
        [
            {
                "asof": asof,
                "theme": "current_theme",
                "count_window": 6,
                "novelty_score": 6.0,
                "novelty_rank": 1,
                "selected": True,
                "novelty_config_version": "cfg-v2",
                "selection_propensity": 0.5,
                "tiebreak_key": "0123456789abcdef",
                "tiebreak_seed": themes.tiebreak_seed(pd.Timestamp(asof).date()),
                "tiebreak_version": themes.TIEBREAK_VERSION,
            }
        ]
    )
    frame.to_parquet(store / f"{asof}.parquet", index=False)


class TestThemeRollupStoreReader(unittest.TestCase):
    def _store(self, tmpdir: str) -> Path:
        store = Path(tmpdir)
        # The legacy date sorts FIRST, which is what makes the trap silent.
        _legacy_file(store, "2026-08-01")
        _current_file(store, "2026-08-20")
        return store

    def test_a_naive_directory_read_silently_drops_the_new_columns(self):
        # Positive control for the reader below: without it, the defect is real
        # and quiet. If a future pyarrow starts unifying fragment schemas (or
        # raising) this test goes red and the reader's docstring needs revisiting
        # — that is the point of pinning the trap rather than only the fix.
        with tempfile.TemporaryDirectory() as tmpdir:
            naive = pd.read_parquet(self._store(tmpdir))

        self.assertNotIn("selection_propensity", naive.columns)

    def test_the_reader_returns_the_new_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = themes.read_theme_rollups(self._store(tmpdir))

        for column in (
            "selection_propensity",
            "tiebreak_key",
            "tiebreak_seed",
            "tiebreak_version",
        ):
            self.assertIn(column, frame.columns)

    def test_every_row_from_every_file_is_returned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = themes.read_theme_rollups(self._store(tmpdir))

        self.assertEqual(set(frame["theme"]), {"legacy_theme", "current_theme"})

    def test_a_legacy_row_reads_as_missing_not_as_impossible(self):
        # NaN, never 0.0. A zero propensity is a CLAIM — "this theme had no
        # chance of being selected" — and a legacy file makes no such claim.
        # Filling it would manufacture data for the estimator to weight on.
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = themes.read_theme_rollups(self._store(tmpdir))

        legacy = frame.loc[frame["theme"] == "legacy_theme", "selection_propensity"]
        self.assertTrue(legacy.isna().all())

    def test_the_declared_columns_lead_the_frame(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = themes.read_theme_rollups(self._store(tmpdir))

        lead = list(frame.columns)[: len(themes.THEME_ROLLUP_COLUMNS)]
        self.assertEqual(lead, list(themes.THEME_ROLLUP_COLUMNS))

    def test_an_empty_store_returns_the_declared_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = themes.read_theme_rollups(Path(tmpdir))

        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), list(themes.THEME_ROLLUP_COLUMNS))

    def test_a_missing_store_returns_the_declared_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = themes.read_theme_rollups(Path(tmpdir) / "never_written")

        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), list(themes.THEME_ROLLUP_COLUMNS))

    def test_the_written_store_round_trips_through_the_reader(self):
        # End-to-end: what the writer produces is what the reader hands back,
        # so the helper cannot drift away from THEME_ROLLUP_COLUMNS unnoticed.
        import datetime as dt

        rollup = pd.DataFrame(
            [
                {
                    "theme": "aa_theme",
                    "count_window": 4,
                    "count_recent": 4,
                    "count_baseline": 0,
                    "novelty_score": 4.0,
                    "rate_surprise": 2.0,
                    "excess_activity": 3.0,
                    "first_seen": pd.Timestamp("2026-08-05", tz="UTC"),
                    "latest_seen": pd.Timestamp("2026-08-05", tz="UTC"),
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Path(tmpdir)
            themes.write_theme_rollup(
                dt.date(2026, 8, 5),
                themes.apply_tiebreak(rollup, asof=dt.date(2026, 8, 5)),
                selected=["aa_theme"],
                out_dir=store,
                novelty_config_version="cfg-token",
                threshold=3.0,
                max_themes=1,
            )
            frame = themes.read_theme_rollups(store)

        self.assertEqual(list(frame.columns), list(themes.THEME_ROLLUP_COLUMNS))
        self.assertEqual(list(frame["theme"]), ["aa_theme"])


if __name__ == "__main__":
    unittest.main()

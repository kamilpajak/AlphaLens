"""P1c immutable news lake log — additive per-run audit trail.

The current-view ``~/.alphalens/thematic_news/{D}.parquet`` keeps its exact
overwrite semantics (the last build of the day, byte-identical to pre-P1c). On
top of that, every ACTUAL build (not a cache hit) ALSO writes the same merged
frame to an immutable per-run file under a SIBLING lake directory:

    <lake_dir>/session_date=<D>/run=<ingested_at_compact>.parquet

so two runs of the same day never collide and the full history is preserved
(append-only). The lake write is best-effort: a failure writing it must NOT
fail the build (the current-view parquet stays load-bearing).
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

_ASOF = dt.date(2026, 5, 29)
_FROZEN_NOW = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)
_FROZEN_NOW_2 = dt.datetime(2026, 5, 29, 18, 30, 15, 123000, tzinfo=dt.UTC)


def _synthetic_polygon(*, date: dt.date) -> pd.DataFrame:
    """A tiny in-window polygon frame WITHOUT an ``ingested_at`` column."""
    ts = pd.Timestamp(date, tz="UTC") + pd.Timedelta(hours=10)
    rows = [
        {
            "id": "poly-1",
            "source": "polygon",
            "timestamp": ts,
            "tickers": ["NVDA"],
            "title": "NVDA news",
            "body": "body text",
            "url": "https://example.com/a",
            "keywords": [],
            "extra": "{}",
        },
        {
            "id": "poly-2",
            "source": "polygon",
            "timestamp": ts + pd.Timedelta(minutes=5),
            "tickers": ["AMD"],
            "title": "AMD news",
            "body": "body text",
            "url": "https://example.com/b",
            "keywords": [],
            "extra": "{}",
        },
    ]
    return pd.DataFrame(rows, columns=NEWS_COLUMNS)


def _empty(*, date: dt.date) -> pd.DataFrame:
    return empty_news_frame()


def _run_ingest(
    cache_dir: Path,
    lake_dir: Path,
    *,
    now: dt.datetime | None,
    force: bool = True,
    polygon=_synthetic_polygon,
) -> pd.DataFrame:
    """Drive ``ingest_daily`` with one synthetic polygon source, no network."""
    with (
        mock.patch.object(news_ingest, "_fetch_edgar_press_release", _empty),
        mock.patch.object(news_ingest, "_fetch_polygon", polygon),
        mock.patch.object(news_ingest, "_fetch_gdelt", _empty),
        mock.patch.object(news_ingest, "_fetch_rss", _empty),
    ):
        return news_ingest.ingest_daily(
            date=_ASOF,
            cache_dir=cache_dir,
            lake_dir=lake_dir,
            max_items=200,
            force=force,
            now=now,
        )


def _session_dir(lake_dir: Path, date: dt.date) -> Path:
    return lake_dir / f"session_date={date.isoformat()}"


def _run_files(lake_dir: Path, date: dt.date) -> list[Path]:
    session = _session_dir(lake_dir, date)
    if not session.exists():
        return []
    return sorted(session.glob("run=*.parquet"))


class TestLakeRunFilename(unittest.TestCase):
    def test_run_filename_is_deterministic_for_frozen_now(self):
        # Contract guarantee 6: the same injected now yields the same run name.
        name = news_ingest._lake_run_filename(_FROZEN_NOW)
        self.assertEqual(name, "run=20260529T120000000Z.parquet")

    def test_run_filename_encodes_milliseconds(self):
        name = news_ingest._lake_run_filename(_FROZEN_NOW_2)
        # 18:30:15.123 → compact filesystem-safe UTC, millisecond precision, Z.
        self.assertEqual(name, "run=20260529T183015123Z.parquet")

    def test_run_filename_normalizes_naive_to_utc(self):
        naive = dt.datetime(2026, 5, 29, 12, 0, 0)
        aware = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)
        self.assertEqual(
            news_ingest._lake_run_filename(naive),
            news_ingest._lake_run_filename(aware),
        )


class TestLakeWriteOnBuild(unittest.TestCase):
    def test_build_writes_exactly_one_run_file_with_news_columns(self):
        # Contract guarantee 2: after a build, exactly one run file under
        # session_date=<D>/, NEWS_COLUMNS schema, ingested_at == run's now,
        # content == current-view frame.
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            view = _run_ingest(cache, lake, now=_FROZEN_NOW)

            runs = _run_files(lake, _ASOF)
            self.assertEqual(len(runs), 1)
            lake_df = pd.read_parquet(runs[0])
            self.assertEqual(list(lake_df.columns), list(NEWS_COLUMNS))
            self.assertFalse(lake_df["ingested_at"].isna().any())
            unique = list(lake_df["ingested_at"].unique())
            self.assertEqual(len(unique), 1)
            self.assertEqual(pd.Timestamp(unique[0]), pd.Timestamp(_FROZEN_NOW))
            # Content equals the returned current-view frame.
            pd.testing.assert_frame_equal(
                lake_df.reset_index(drop=True), view.reset_index(drop=True)
            )

    def test_run_file_lives_under_hive_session_date_partition(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)
            expected = (
                lake / f"session_date={_ASOF.isoformat()}" / "run=20260529T120000000Z.parquet"
            )
            self.assertTrue(expected.exists())

    def test_current_view_content_equals_lake_run_content(self):
        # The lake run == the current-view {D}.parquet for the same build.
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)
            view_df = pd.read_parquet(cache / f"{_ASOF.isoformat()}.parquet")
            lake_df = pd.read_parquet(_run_files(lake, _ASOF)[0])
            pd.testing.assert_frame_equal(
                lake_df.reset_index(drop=True), view_df.reset_index(drop=True)
            )


class TestLakeEmptyDay(unittest.TestCase):
    def test_empty_day_writes_zero_row_run_file_with_schema(self):
        # Empty-day audit signal: "we ran and saw nothing" is recorded as a
        # 0-row run file (parallel to the empty current-view parquet).
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW, polygon=_empty)
            runs = _run_files(lake, _ASOF)
            self.assertEqual(len(runs), 1)
            lake_df = pd.read_parquet(runs[0])
            self.assertEqual(len(lake_df), 0)
            self.assertEqual(list(lake_df.columns), list(NEWS_COLUMNS))


class TestLakeAppendOnly(unittest.TestCase):
    def test_second_build_appends_second_run_file_keeps_first(self):
        # Contract guarantee 3: a SECOND build (force=True) with a DIFFERENT now
        # leaves the first run file intact AND adds a second.
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)
            first_runs = _run_files(lake, _ASOF)
            self.assertEqual(len(first_runs), 1)
            first_bytes = first_runs[0].read_bytes()

            view2 = _run_ingest(cache, lake, now=_FROZEN_NOW_2)
            second_runs = _run_files(lake, _ASOF)
            self.assertEqual(len(second_runs), 2)
            # First run file is byte-identical (never overwritten).
            self.assertEqual(first_runs[0].read_bytes(), first_bytes)

            # Current-view reflects the LATEST run's ingested_at.
            view_df = pd.read_parquet(cache / f"{_ASOF.isoformat()}.parquet")
            self.assertEqual(
                pd.Timestamp(view_df["ingested_at"].iloc[0]),
                pd.Timestamp(_FROZEN_NOW_2),
            )
            pd.testing.assert_frame_equal(
                view_df.reset_index(drop=True), view2.reset_index(drop=True)
            )

    def test_same_now_rerun_does_not_clobber_existing_run_file(self):
        # If the exact run path already exists (same-microsecond rerun), append a
        # numeric suffix rather than overwrite — append-only is inviolable.
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)
            _run_ingest(cache, lake, now=_FROZEN_NOW)
            runs = _run_files(lake, _ASOF)
            self.assertEqual(len(runs), 2)
            # Both retained, distinct paths.
            self.assertNotEqual(runs[0].name, runs[1].name)


class TestLakeCacheHit(unittest.TestCase):
    def test_cache_hit_writes_no_run_file_and_reads_no_clock(self):
        # Contract guarantee 4: a cache hit writes NO new run file and reads NO
        # clock (same gate as the now= resolution).
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)  # build → 1 run file
            self.assertEqual(len(_run_files(lake, _ASOF)), 1)

            # Second call, force=False, cache present → cache hit.
            with mock.patch.object(news_ingest.dt, "datetime", wraps=dt.datetime) as m:
                _run_ingest(cache, lake, now=None, force=False)
                m.now.assert_not_called()
            self.assertEqual(len(_run_files(lake, _ASOF)), 1)


class TestLakeWriteFailureIsSwallowed(unittest.TestCase):
    def test_lake_write_failure_does_not_fail_ingest(self):
        # Contract guarantee 5: a lake-log write failure must NOT fail
        # ingest_daily; the current-view parquet is still written.
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            with mock.patch.object(
                news_ingest, "_write_lake_run", side_effect=OSError("disk full")
            ):
                view = _run_ingest(cache, lake, now=_FROZEN_NOW)
            # Build succeeded, current-view parquet present.
            self.assertGreater(len(view), 0)
            self.assertTrue((cache / f"{_ASOF.isoformat()}.parquet").exists())
            # No run file (the lake write raised).
            self.assertEqual(len(_run_files(lake, _ASOF)), 0)


class TestLakeDefaultDir(unittest.TestCase):
    def test_default_lake_dir_is_sibling_of_cache_dir(self):
        # Sibling subdir, NOT inside thematic_news/ (CRITICAL risk: keep the
        # current-view glob clean).
        self.assertEqual(
            news_ingest.DEFAULT_LAKE_DIR,
            news_ingest.DEFAULT_CACHE_DIR.parent / "thematic_news_lake",
        )
        self.assertNotEqual(news_ingest.DEFAULT_LAKE_DIR, news_ingest.DEFAULT_CACHE_DIR)


if __name__ == "__main__":
    unittest.main()

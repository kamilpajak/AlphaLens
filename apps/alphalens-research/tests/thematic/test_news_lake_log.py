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


def _synthetic_polygon(*, date: dt.date, force: bool = False) -> pd.DataFrame:
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


def _synthetic_polygon_three_in_window(*, date: dt.date, force: bool = False) -> pd.DataFrame:
    """Three in-window polygon rows — used with max_items=1 to prove the lake
    keeps rows the current-view CAP drops."""
    base = pd.Timestamp(date, tz="UTC") + pd.Timedelta(hours=10)
    rows = [
        {
            "id": f"poly-{i}",
            "source": "polygon",
            "timestamp": base + pd.Timedelta(minutes=i),
            "tickers": ["NVDA"],
            "title": f"distinct headline {i}",
            "body": "body text",
            "url": f"https://example.com/{i}",
            "keywords": [],
            "extra": "{}",
        }
        for i in range(3)
    ]
    return pd.DataFrame(rows, columns=NEWS_COLUMNS)


def _synthetic_polygon_with_prior_day(*, date: dt.date, force: bool = False) -> pd.DataFrame:
    """One in-window row + one row in the PRIOR UTC day — used to prove the lake
    keeps rows the current-view strict single-UTC-day filter (P1a) drops. A P2
    session window straddles UTC midnight, so the lake MUST retain the prior-day
    row even though today's brief excludes it."""
    in_day = pd.Timestamp(date, tz="UTC") + pd.Timedelta(hours=10)
    prior_day = pd.Timestamp(date, tz="UTC") - pd.Timedelta(hours=2)
    rows = [
        {
            "id": "poly-in",
            "source": "polygon",
            "timestamp": in_day,
            "tickers": ["NVDA"],
            "title": "in-window headline",
            "body": "body text",
            "url": "https://example.com/in",
            "keywords": [],
            "extra": "{}",
        },
        {
            "id": "poly-prior",
            "source": "polygon",
            "timestamp": prior_day,
            "tickers": ["AMD"],
            "title": "prior-day overnight headline",
            "body": "body text",
            "url": "https://example.com/prior",
            "keywords": [],
            "extra": "{}",
        },
    ]
    return pd.DataFrame(rows, columns=NEWS_COLUMNS)


def _empty(*, date: dt.date, force: bool = False) -> pd.DataFrame:
    return empty_news_frame()


def _run_ingest(
    cache_dir: Path,
    lake_dir: Path,
    *,
    now: dt.datetime | None,
    force: bool = True,
    polygon=_synthetic_polygon,
    max_items: int = 200,
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
            max_items=max_items,
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
        # session_date=<D>/, NEWS_COLUMNS schema, ingested_at == run's now.
        # Content = the RAW per-source union (here the 2 synthetic polygon rows),
        # NOT the recency-sorted current-view (P1c-raw correction).
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)

            runs = _run_files(lake, _ASOF)
            self.assertEqual(len(runs), 1)
            lake_df = pd.read_parquet(runs[0])
            self.assertEqual(list(lake_df.columns), list(NEWS_COLUMNS))
            self.assertFalse(lake_df["ingested_at"].isna().any())
            unique = list(lake_df["ingested_at"].unique())
            self.assertEqual(len(unique), 1)
            self.assertEqual(pd.Timestamp(unique[0]), pd.Timestamp(_FROZEN_NOW))
            # Raw union = exactly the fetched rows.
            self.assertEqual(set(lake_df["id"]), {"poly-1", "poly-2"})

    def test_run_file_lives_under_hive_session_date_partition(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)
            expected = (
                lake / f"session_date={_ASOF.isoformat()}" / "run=20260529T120000000Z.parquet"
            )
            self.assertTrue(expected.exists())

    def test_lake_holds_same_rows_as_view_when_nothing_dropped(self):
        # When the current-view pipeline drops nothing (in-window, under cap, no
        # dupes), the lake holds the SAME row SET as {D}.parquet — but the lake is
        # raw fetch-order, the view is recency-sorted, so they are NOT frame-equal.
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)
            view_df = pd.read_parquet(cache / f"{_ASOF.isoformat()}.parquet")
            lake_df = pd.read_parquet(_run_files(lake, _ASOF)[0])
            self.assertEqual(set(lake_df["id"]), set(view_df["id"]))

    def test_successful_build_leaves_no_temp_file(self):
        # The lake write is atomic (temp file + replace) so a crash mid-write
        # never leaves a partial/corrupt parquet that P2 would later read. After
        # a successful build the session partition holds exactly the run file and
        # NO leftover temp artifact.
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            _run_ingest(cache, lake, now=_FROZEN_NOW)
            session = _session_dir(lake, _ASOF)
            entries = sorted(p.name for p in session.iterdir())
            self.assertEqual(entries, ["run=20260529T120000000Z.parquet"])
            self.assertFalse(any(p.name.endswith(".tmp") for p in session.iterdir()))


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


class TestLakeStoresRawNotServed(unittest.TestCase):
    """P1c-raw: the lake stores the RAW per-source union (pre-dedup/cap/filter),
    NOT the deduped+capped current-view the brief serves. These pin the rows the
    P2 session-window VIEW will need that the current-view drops."""

    def test_lake_keeps_rows_the_cap_drops(self):
        # 3 in-window rows, max_items=1: current-view caps to 1, lake keeps all 3.
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            view = _run_ingest(
                cache,
                lake,
                now=_FROZEN_NOW,
                polygon=_synthetic_polygon_three_in_window,
                max_items=1,
            )
            self.assertEqual(len(view), 1)  # current-view obeys the LLM-budget cap
            lake_df = pd.read_parquet(_run_files(lake, _ASOF)[0])
            self.assertEqual(len(lake_df), 3)  # raw substrate keeps every row
            self.assertEqual(set(lake_df["id"]), {"poly-0", "poly-1", "poly-2"})

    def test_lake_keeps_out_of_strict_day_rows(self):
        # One in-window + one prior-UTC-day row: current-view's strict single-day
        # filter (P1a) drops the prior-day row; the lake keeps it (a P2 session
        # window straddles UTC midnight and needs it).
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "thematic_news"
            lake = Path(td) / "thematic_news_lake"
            view = _run_ingest(
                cache,
                lake,
                now=_FROZEN_NOW,
                polygon=_synthetic_polygon_with_prior_day,
            )
            self.assertEqual(set(view["id"]), {"poly-in"})  # prior-day dropped
            lake_df = pd.read_parquet(_run_files(lake, _ASOF)[0])
            self.assertEqual(set(lake_df["id"]), {"poly-in", "poly-prior"})


if __name__ == "__main__":
    unittest.main()

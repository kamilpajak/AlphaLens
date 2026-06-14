"""L3 golden-master replay of the news-ingest MERGE stage (test-strategy Phase 3b).

Drives the REAL ``news_ingest.ingest_daily`` offline over a frozen 3-source
capture, locking the cross-source dedup / priority-merge / recency-cap pipeline
and each source's parser:

* GDELT  → ``UrlJsonCassette`` over a synthetic 2-bucket response (real
  ``gdelt.transform`` parser; GDELT free tier 429s too hard for a live sweep)
* Polygon→ ``VendorCassette`` (real ``get_news_range`` payload, trimmed to
  universe)
* RSS    → ``FeedCassette`` (real ``rss._parse_feed`` → ``rss.transform``)

EDGAR is excluded from this golden (its nested-fetch press-release path is
high-volume and recency-capped out; covered by unit tests + the 3b-2 tenk gate).
Assert the merge outcome (per-source counts + schema + cluster markers), not
exit codes. A source parser breaking drops it from ``by_source``; a dedup
regression moves ``row_count`` / ``cluster_size``.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.data.alt_data import gdelt_client
from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic.sources import gdelt, polygon_news, rss
from alphalens_pipeline.thematic.sources.schema import empty_news_frame

from tests.golden.projection import ingest_projection
from tests.golden.url_cassette import FeedCassette, UrlJsonCassette
from tests.golden.vendor_cassette import VendorCassette

_ASOF = dt.date(2026, 5, 29)
# Fixed transaction-time (P1b bitemporal): injected so the ``ingested_at`` lake
# column is bit-deterministic under cassette replay (noon UTC on the ingest day).
# The recorder pins the SAME value so the captured golden and the replay match.
_FROZEN_NOW = dt.datetime(2026, 5, 29, 12, 0, 0, tzinfo=dt.UTC)
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ingest_day"
_GOLDEN = _FIXTURES / "golden" / "projection.json"

# Same 2-bucket subset the recorder authored (only these URLs are queried).
_GDELT_BUCKETS = {
    "quantum_ai": '("quantum computing" OR "AI accelerator")',
    "semiconductors": '("semiconductor" OR "chip foundry")',
}

_REAL_POLYGON = polygon_news.fetch_daily_news
_REAL_RSS = rss.fetch_daily_news
_REAL_GDELT = gdelt.fetch_daily_news


def _no_edgar(*, date: dt.date):
    """EDGAR-excluded stand-in matching ``_fetch_edgar_press_release(*, date)``."""
    return empty_news_frame()


def _replay_ingest(unified_cache: Path) -> pd.DataFrame:
    if not _GOLDEN.exists() or not (_FIXTURES / "gdelt.json").exists():
        raise FileNotFoundError(
            f"golden fixtures missing under {_FIXTURES} — run "
            "scripts/record_golden_ingest.py (one-time capture) to record them"
        )
    gdelt_player = UrlJsonCassette(_FIXTURES / "gdelt.json")
    vendor = VendorCassette(_FIXTURES / "cassettes_vendor")
    feed_player = FeedCassette(_FIXTURES / "rss.json")

    with tempfile.TemporaryDirectory(prefix="ingest_replay_") as tmp_root:
        tmp = Path(tmp_root)
        with (
            mock.patch.object(gdelt, "load_theme_buckets", lambda: dict(_GDELT_BUCKETS)),
            mock.patch.object(gdelt_client, "_http_get_json", gdelt_player),
            mock.patch.object(
                news_ingest.gdelt,
                "fetch_daily_news",
                # inter_query_sleep_sec=0: the cassette is instant, so the live
                # rate-limit pause between buckets must not slow the test.
                functools.partial(
                    _REAL_GDELT, cache_dir=tmp / "gdelt", force=True, inter_query_sleep_sec=0.0
                ),
            ),
            mock.patch.object(
                news_ingest.polygon_news,
                "fetch_daily_news",
                functools.partial(_REAL_POLYGON, cache_dir=tmp / "polygon", force=True),
            ),
            mock.patch.object(polygon_news, "get_default_polygon_client", lambda: vendor),
            mock.patch.object(
                news_ingest.rss,
                "fetch_daily_news",
                functools.partial(_REAL_RSS, cache_dir=tmp / "rss", force=True),
            ),
            mock.patch.object(rss, "_parse_feed", feed_player),
            mock.patch.object(news_ingest, "_fetch_edgar_press_release", _no_edgar),
        ):
            return news_ingest.ingest_daily(
                date=_ASOF,
                cache_dir=unified_cache,
                max_items=news_ingest.DEFAULT_MAX_ITEMS,
                now=_FROZEN_NOW,
            )


class TestGoldenIngestReplay(unittest.TestCase):
    def test_replay_matches_golden_projection(self):
        with tempfile.TemporaryDirectory() as td:
            df = _replay_ingest(Path(td))
        got = ingest_projection(df)
        golden = json.loads(_GOLDEN.read_text())
        self.assertEqual(got, golden)

    def test_all_three_sources_present(self):
        # Each parser produced rows: a source vanishing (parser regression
        # against the real vendor shape) would drop it from by_source.
        with tempfile.TemporaryDirectory() as td:
            df = _replay_ingest(Path(td))
        by_source = set(df["source"].unique())
        self.assertEqual(by_source, {"gdelt", "polygon", "rss"})

    def test_schema_is_news_columns(self):
        from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS

        with tempfile.TemporaryDirectory() as td:
            df = _replay_ingest(Path(td))
        self.assertEqual(list(df.columns), list(NEWS_COLUMNS))

    def test_replay_is_deterministic(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            a = ingest_projection(_replay_ingest(Path(td1)))
            b = ingest_projection(_replay_ingest(Path(td2)))
        self.assertEqual(a, b)

    def test_unified_parquet_written(self):
        # Side-effect contract: ingest_daily persists the merged frame to
        # cache_dir/{date}.parquet (the file the extract stage reads next).
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            df = _replay_ingest(cache)
            self.assertGreater(len(df), 0)
            self.assertTrue((cache / f"{_ASOF.isoformat()}.parquet").exists())


if __name__ == "__main__":
    unittest.main()

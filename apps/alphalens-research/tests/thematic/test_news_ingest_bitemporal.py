"""P1b bitemporal lake: ``ingested_at`` transaction-time column on the news lake.

``timestamp`` is the valid-time (when the news was publicly available); the new
``ingested_at`` is the transaction-time (when ``ingest_daily`` recorded the item
into the canonical lake). The stamp is applied at the lake-entry point
(``ingest_daily``), shared by every row of one run, and takes an INJECTABLE clock
so the golden replay stays bit-deterministic (never an un-injectable
``datetime.now()`` into the lake).
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
_PINNED_NOW = dt.datetime(2026, 5, 29, 12, 30, 0, tzinfo=dt.UTC)


def _synthetic_polygon(*, date: dt.date, force: bool = False) -> pd.DataFrame:
    """A tiny in-window polygon frame WITHOUT an ``ingested_at`` column.

    Mirrors a real per-source adapter: the lake-entry stamp is ``ingest_daily``'s
    job, not the source's. ``ingest_daily`` must add + populate the column.
    """
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


def _empty(*, date: dt.date, force: bool = False) -> pd.DataFrame:
    return empty_news_frame()


def _run_ingest(cache_dir: Path, *, now: dt.datetime | None) -> pd.DataFrame:
    """Drive ``ingest_daily`` with one synthetic polygon source, no network."""
    with (
        mock.patch.object(news_ingest, "_fetch_edgar_press_release", _empty),
        mock.patch.object(news_ingest, "_fetch_polygon", _synthetic_polygon),
        mock.patch.object(news_ingest, "_fetch_gdelt", _empty),
        mock.patch.object(news_ingest, "_fetch_rss", _empty),
    ):
        return news_ingest.ingest_daily(
            date=_ASOF, cache_dir=cache_dir, max_items=200, force=True, now=now
        )


class TestBitemporalSchema(unittest.TestCase):
    def test_news_columns_includes_ingested_at_appended(self):
        # Appended at the END so existing column offsets are unchanged.
        self.assertEqual(NEWS_COLUMNS[-1], "ingested_at")
        self.assertEqual(NEWS_COLUMNS.count("ingested_at"), 1)

    def test_empty_frame_carries_ingested_at_utc_datetime(self):
        df = empty_news_frame()
        self.assertIn("ingested_at", df.columns)
        self.assertEqual(len(df), 0)
        # tz-aware UTC datetime dtype, same family as ``timestamp`` (resolution
        # unit not pinned — pandas may pick s/ms/ns; only UTC-awareness matters).
        self.assertTrue(isinstance(df["ingested_at"].dtype, pd.DatetimeTZDtype))
        self.assertEqual(str(df["ingested_at"].dtype.tz), "UTC")
        self.assertEqual(df["ingested_at"].dtype, df["timestamp"].dtype)


class TestIngestDailyStampsIngestedAt(unittest.TestCase):
    def test_injected_now_stamped_on_every_row(self):
        with tempfile.TemporaryDirectory() as td:
            df = _run_ingest(Path(td), now=_PINNED_NOW)
        self.assertGreater(len(df), 0)
        self.assertIn("ingested_at", df.columns)
        # Every row shares the one injected transaction-time; no NaT survivors.
        self.assertFalse(df["ingested_at"].isna().any())
        unique = list(df["ingested_at"].unique())
        self.assertEqual(len(unique), 1)
        self.assertEqual(pd.Timestamp(unique[0]), pd.Timestamp(_PINNED_NOW))

    def test_default_now_is_eager_and_within_call_window(self):
        before = dt.datetime.now(dt.UTC)
        with tempfile.TemporaryDirectory() as td:
            df = _run_ingest(Path(td), now=None)
        after = dt.datetime.now(dt.UTC)
        self.assertGreater(len(df), 0)
        self.assertFalse(df["ingested_at"].isna().any())
        stamped = pd.Timestamp(df["ingested_at"].iloc[0])
        self.assertGreaterEqual(stamped, pd.Timestamp(before))
        self.assertLessEqual(stamped, pd.Timestamp(after))

    def test_stamp_persisted_to_parquet(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            _run_ingest(cache, now=_PINNED_NOW)
            reloaded = pd.read_parquet(cache / f"{_ASOF.isoformat()}.parquet")
        self.assertIn("ingested_at", reloaded.columns)
        self.assertEqual(pd.Timestamp(reloaded["ingested_at"].iloc[0]), pd.Timestamp(_PINNED_NOW))


if __name__ == "__main__":
    unittest.main()

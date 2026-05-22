"""Tests for the Polygon news ingest wrapper (``alphalens_research.thematic.sources.polygon_news``).

After the 2026-05-22 canonical-client consolidation, this module is a thin
domain wrapper around :class:`PolygonClient`. HTTP, pagination, rate-limit,
and Bearer auth are owned by the canonical client and tested independently
in ``tests/test_polygon_client.py``. Tests at this layer mock at the client
boundary (``client.get_news_range``), not at the urllib / requests level.
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from alphalens_research.thematic.sources import polygon_news

SAMPLE_POLYGON_ROWS = [
    {
        "id": "abc123",
        "publisher": {"name": "Motley Fool"},
        "title": "Nvidia announces CUDA-Q for quantum computing",
        "author": "Jane Smith",
        "published_utc": "2026-05-15T14:30:00Z",
        "article_url": "https://fool.com/nvda-cudaq",
        "tickers": ["NVDA", "QUBT"],
        "description": "NVIDIA unveiled CUDA-Q...",
        "keywords": ["quantum", "AI"],
        "insights": [{"ticker": "NVDA", "sentiment": "positive", "sentiment_reasoning": "Bullish"}],
    },
    {
        "id": "def456",
        "publisher": {"name": "Reuters"},
        "title": "Coca-Cola raises dividend",
        "author": "John Doe",
        "published_utc": "2026-05-15T16:00:00Z",
        "article_url": "https://reuters.com/ko",
        "tickers": ["KO"],
        "description": "Quarterly dividend...",
        "keywords": ["dividend"],
        "insights": [],
    },
    {
        "id": "ghi789",
        "publisher": {"name": "Random Blog"},
        "title": "Off-universe penny stock pump",
        "author": "Anon",
        "published_utc": "2026-05-15T18:00:00Z",
        "article_url": "https://example.com/penny",
        "tickers": ["XYZQ"],
        "description": "...",
        "keywords": [],
        "insights": [],
    },
]


def _mock_client(*, rows=None) -> MagicMock:
    """Mock PolygonClient whose ``get_news_range`` returns ``rows``."""
    client = MagicMock()
    client.get_news_range.return_value = rows if rows is not None else SAMPLE_POLYGON_ROWS
    return client


class TestPolygonNewsTransform(unittest.TestCase):
    def test_transforms_response_to_unified_schema(self):
        df = polygon_news.transform(SAMPLE_POLYGON_ROWS, universe={"NVDA", "KO"})
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns)[:3], ["id", "source", "timestamp"])
        self.assertTrue((df["source"] == "polygon").all())
        self.assertTrue(df["timestamp"].dt.tz is not None)

    def test_filters_off_universe_tickers(self):
        df = polygon_news.transform(SAMPLE_POLYGON_ROWS, universe={"NVDA", "KO"})
        # Row 3 had ticker XYZQ — should be dropped (no universe overlap)
        self.assertNotIn("ghi789", df["id"].tolist())

    def test_intersects_tickers_with_universe(self):
        df = polygon_news.transform(SAMPLE_POLYGON_ROWS, universe={"NVDA", "KO"})
        # NVDA+QUBT article: tickers should be filtered to just [NVDA] (QUBT not in universe)
        nvda_row = df[df["id"] == "abc123"].iloc[0]
        self.assertEqual(nvda_row["tickers"], ["NVDA"])

    def test_preserves_insights_in_extra_json(self):
        df = polygon_news.transform(SAMPLE_POLYGON_ROWS, universe={"NVDA"})
        nvda_row = df[df["id"] == "abc123"].iloc[0]
        extra = json.loads(nvda_row["extra"])
        self.assertEqual(extra["publisher"], "Motley Fool")
        self.assertEqual(extra["insights"][0]["sentiment"], "positive")

    def test_empty_results_returns_empty_frame(self):
        df = polygon_news.transform([], universe={"NVDA"})
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), polygon_news.NEWS_COLUMNS)


class TestPolygonNewsFetch(unittest.TestCase):
    def test_fetch_news_range_delegates_to_canonical_client(self):
        client = _mock_client()

        items = polygon_news.fetch_news_range(
            client=client,
            start=dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
        )

        client.get_news_range.assert_called_once()
        kwargs = client.get_news_range.call_args.kwargs
        # tz-aware datetimes are forwarded verbatim; the canonical client
        # formats the ISO strings + handles pagination internally.
        self.assertEqual(kwargs["start"], dt.datetime(2026, 5, 15, tzinfo=dt.UTC))
        self.assertEqual(kwargs["end"], dt.datetime(2026, 5, 16, tzinfo=dt.UTC))
        self.assertEqual(kwargs["order"], "asc")  # ingest defaults to ascending
        self.assertEqual(len(items), 3)

    def test_fetch_news_range_preserves_intra_day_time_component(self):
        client = _mock_client()
        polygon_news.fetch_news_range(
            client=client,
            start=dt.datetime(2026, 5, 15, 14, 30, 0, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 15, 18, 45, 0, tzinfo=dt.UTC),
        )
        kwargs = client.get_news_range.call_args.kwargs
        self.assertEqual(kwargs["start"].hour, 14)
        self.assertEqual(kwargs["start"].minute, 30)
        self.assertEqual(kwargs["end"].hour, 18)
        self.assertEqual(kwargs["end"].minute, 45)

    def test_fetch_news_range_max_items_passed_through(self):
        client = _mock_client()
        polygon_news.fetch_news_range(
            client=client,
            start=dt.datetime(2026, 5, 15, tzinfo=dt.UTC),
            end=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
            max_items=4,
        )
        kwargs = client.get_news_range.call_args.kwargs
        self.assertEqual(kwargs["max_items"], 4)


class TestPolygonNewsCache(unittest.TestCase):
    def test_fetch_daily_news_writes_parquet_and_returns_frame(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            client = _mock_client()
            with patch.object(
                polygon_news, "load_input_universe", return_value=frozenset({"NVDA", "KO"})
            ):
                df = polygon_news.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    client=client,
                    cache_dir=cache_dir,
                )

            cached = cache_dir / "2026-05-15.parquet"
            self.assertTrue(cached.exists())
            self.assertEqual(len(df), 2)
            self.assertEqual(set(df.columns), set(polygon_news.NEWS_COLUMNS))

            # Second call should hit the parquet cache without invoking the
            # canonical client.
            blocking_client = MagicMock()
            blocking_client.get_news_range.side_effect = AssertionError(
                "fetch_daily_news must read parquet cache on second call"
            )
            df2 = polygon_news.fetch_daily_news(
                date=dt.date(2026, 5, 15),
                client=blocking_client,
                cache_dir=cache_dir,
            )
            pd.testing.assert_frame_equal(
                df.reset_index(drop=True), df2.reset_index(drop=True), check_dtype=False
            )


if __name__ == "__main__":
    unittest.main()

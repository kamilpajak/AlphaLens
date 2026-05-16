import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from alphalens.thematic import news_ingest
from alphalens.thematic.sources.schema import NEWS_COLUMNS


def _row(id_, source, ts, tickers, title):
    return {
        "id": id_,
        "source": source,
        "timestamp": pd.Timestamp(ts, tz="UTC"),
        "tickers": tickers,
        "title": title,
        "body": "",
        "url": f"https://example.com/{id_}",
        "keywords": [],
        "extra": "{}",
    }


def _frame(rows):
    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


class TestNewsIngestOrchestration(unittest.TestCase):
    def test_aggregates_from_all_four_sources(self):
        polygon_df = _frame(
            [_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "Polygon piece")]
        )
        gdelt_df = _frame([_row("g1", "gdelt", "2026-05-15T11:00:00Z", [], "GDELT piece")])
        rss_df = _frame([_row("r1", "rss", "2026-05-15T12:00:00Z", [], "RSS piece")])
        edgar_df = _frame([_row("e1", "edgar", "2026-05-15T09:00:00Z", ["AAPL"], "AAPL 8-K")])

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=gdelt_df),
                patch.object(news_ingest, "_fetch_rss", return_value=rss_df),
                patch.object(news_ingest, "_fetch_edgar", return_value=edgar_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(len(df), 4)
            self.assertEqual(set(df["source"]), {"polygon", "gdelt", "rss", "edgar"})

    def test_caps_at_max_items(self):
        many_rows = [
            _row(f"p{i}", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], f"item {i}")
            for i in range(500)
        ]
        polygon_df = _frame(many_rows)
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
                patch.object(news_ingest, "_fetch_edgar", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    max_items=200,
                )

            self.assertEqual(len(df), 200)

    def test_dedupes_across_sources_by_url(self):
        polygon_df = _frame(
            [_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "Same article")]
        )
        # GDELT row with same URL but different id
        gdelt_row = _row("g1", "gdelt", "2026-05-15T10:30:00Z", [], "Same article")
        gdelt_row["url"] = "https://example.com/p1"  # collision on url
        gdelt_df = _frame([gdelt_row])
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=gdelt_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
                patch.object(news_ingest, "_fetch_edgar", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(df["url"].nunique(), 1)
            # Polygon wins (richer schema, ticker-tagged)
            self.assertEqual(df.iloc[0]["source"], "polygon")

    def test_dedupes_across_sources_after_stripping_tracking_params(self):
        # Same article, but Polygon URL has utm_source and RSS doesn't.
        # Raw-URL dedup would let both through; canonical-URL dedup collapses them.
        polygon_row = _row(
            "p1",
            "polygon",
            "2026-05-15T10:00:00Z",
            ["NVDA"],
            "Same article",
        )
        polygon_row["url"] = "https://example.com/article?utm_source=polygon&ref=feed"
        rss_row = _row("r1", "rss", "2026-05-15T10:30:00Z", [], "Same article")
        rss_row["url"] = "https://example.com/article"
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=_frame([polygon_row])),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=_frame([rss_row])),
                patch.object(news_ingest, "_fetch_edgar", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(len(df), 1)
            # Polygon wins (richer schema) — its URL is preserved, tracking params and all
            self.assertEqual(df.iloc[0]["source"], "polygon")

    def test_writes_unified_parquet_and_reuses_cache(self):
        polygon_df = _frame([_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "Piece")])
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
                patch.object(news_ingest, "_fetch_edgar", return_value=empty_df),
            ):
                news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )
            cached = Path(tmpdir) / "2026-05-15.parquet"
            self.assertTrue(cached.exists())

            with patch.object(news_ingest, "_fetch_polygon", side_effect=AssertionError("no call")):
                df2 = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df2), 1)

    def test_source_failure_does_not_abort_ingest(self):
        polygon_df = _frame([_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "OK")])
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", side_effect=RuntimeError("rate limited")),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
                patch.object(news_ingest, "_fetch_edgar", side_effect=RuntimeError("network")),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["source"], "polygon")

    def test_sorts_by_timestamp_descending(self):
        rows = [
            _row("a", "polygon", "2026-05-15T08:00:00Z", ["NVDA"], "old"),
            _row("b", "polygon", "2026-05-15T18:00:00Z", ["NVDA"], "new"),
            _row("c", "polygon", "2026-05-15T13:00:00Z", ["NVDA"], "mid"),
        ]
        polygon_df = _frame(rows)
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
                patch.object(news_ingest, "_fetch_edgar", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(df["id"].tolist(), ["b", "c", "a"])


if __name__ == "__main__":
    unittest.main()

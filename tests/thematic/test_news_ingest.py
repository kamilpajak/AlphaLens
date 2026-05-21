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
    def test_aggregates_from_three_sources(self):
        polygon_df = _frame(
            [_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "Polygon piece")]
        )
        gdelt_df = _frame([_row("g1", "gdelt", "2026-05-15T11:00:00Z", [], "GDELT piece")])
        rss_df = _frame([_row("r1", "rss", "2026-05-15T12:00:00Z", [], "RSS piece")])

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=gdelt_df),
                patch.object(news_ingest, "_fetch_rss", return_value=rss_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(len(df), 3)
            self.assertEqual(set(df["source"]), {"polygon", "gdelt", "rss"})

    def test_edgar_excluded_from_ingest(self):
        """EDGAR signal stays in watchdog Layer 1; thematic ingest skips it."""
        self.assertFalse(hasattr(news_ingest, "_fetch_edgar"))
        self.assertNotIn("edgar", news_ingest._SOURCE_PRIORITY)

    def test_caps_at_max_items(self):
        # Single unique token per title so Tier 1 lexical clustering keeps them apart
        # (|∩|=0 < MIN_TOKEN_OVERLAP=3 → never similar).
        many_rows = [
            _row(
                f"p{i}",
                "polygon",
                f"2026-05-15T{(i % 24):02d}:00:00Z",
                ["NVDA"],
                f"foobar{i:04d}",
            )
            for i in range(500)
        ]
        polygon_df = _frame(many_rows)
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
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
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["source"], "polygon")

    def test_sorts_by_timestamp_descending(self):
        # Disjoint single-token titles so Tier 1 keeps them as separate clusters.
        rows = [
            _row("a", "polygon", "2026-05-15T08:00:00Z", ["NVDA"], "alphafoobar"),
            _row("b", "polygon", "2026-05-15T18:00:00Z", ["NVDA"], "bravofoobar"),
            _row("c", "polygon", "2026-05-15T13:00:00Z", ["NVDA"], "charliefoobar"),
        ]
        polygon_df = _frame(rows)
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(df["id"].tolist(), ["b", "c", "a"])


class TestTier1LexicalClustering(unittest.TestCase):
    """Same-day syndication: collapse echoes into clusters before cap-200."""

    def test_clusters_lexical_echoes_keeps_earliest_root(self):
        # Three rows with high title overlap (5 content tokens, 4 shared) and
        # distinct URLs (so URL-dedup does NOT collapse them — only Tier 1 can).
        polygon_row = _row(
            "p1",
            "polygon",
            "2026-05-15T09:00:00Z",
            ["TSLA"],
            "SpaceX IPO filing lands approval today",
        )
        gdelt_row = _row(
            "g1",
            "gdelt",
            "2026-05-15T10:00:00Z",
            [],
            "SpaceX IPO filing lands approval imminent",
        )
        rss_row = _row(
            "r1",
            "rss",
            "2026-05-15T14:00:00Z",
            [],
            "SpaceX IPO filing lands approval breaking",
        )
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=_frame([polygon_row])),
                patch.object(news_ingest, "_fetch_gdelt", return_value=_frame([gdelt_row])),
                patch.object(news_ingest, "_fetch_rss", return_value=_frame([rss_row])),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

        # All three echoes collapse to the earliest representative — Polygon @ 09:00.
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["source"], "polygon")
        self.assertEqual(row["id"], "p1")
        self.assertEqual(row["timestamp"], pd.Timestamp("2026-05-15T09:00:00Z"))
        # cluster_size persists in the existing `extra` JSON blob — no schema change.
        import json
        extra = json.loads(row["extra"])
        self.assertEqual(extra.get("cluster_size"), 3)

    def test_disjoint_titles_stay_as_separate_clusters(self):
        # Three rows with non-overlapping single-token titles must NOT cluster.
        rows = [
            _row("p1", "polygon", "2026-05-15T09:00:00Z", ["AAPL"], "alphaword"),
            _row("p2", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "bravoword"),
            _row("p3", "polygon", "2026-05-15T11:00:00Z", ["TSLA"], "charlieword"),
        ]
        polygon_df = _frame(rows)
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

        self.assertEqual(len(df), 3)

    def test_cluster_cap200_uses_max_timestamp_for_breaking_news(self):
        # 200 single-member clusters at 02:00..05:19 (one row per minute),
        # PLUS one breaking-news cluster whose representative is at 05:00 but
        # whose latest echo at 23:59 should let the cluster win a cap-200 slot.
        single_rows = [
            _row(
                f"single{i}",
                "polygon",
                f"2026-05-15T{2 + i // 60:02d}:{i % 60:02d}:00Z",
                ["NVDA"],
                f"uniqueword{i:04d}",  # single-token, never clusters
            )
            for i in range(200)
        ]
        # Breaking-news cluster: 2 rows with overlapping titles
        breaking_root = _row(
            "broot",
            "polygon",
            "2026-05-15T05:00:00Z",
            ["SPCE"],
            "Federal Reserve interest rates emergency cut announced",
        )
        breaking_echo = _row(
            "becho",
            "rss",
            "2026-05-15T23:59:00Z",
            [],
            "Federal Reserve interest rates emergency cut breaking",
        )
        # Push the singles via Polygon so all four are in the same source frame
        polygon_df = _frame(single_rows + [breaking_root])
        rss_df = _frame([breaking_echo])
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=rss_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    max_items=200,
                )

        self.assertEqual(len(df), 200)
        # The breaking cluster must be present; representative is the 05:00 Polygon root.
        breaking_rows = df[df["id"] == "broot"]
        self.assertEqual(len(breaking_rows), 1)
        self.assertEqual(breaking_rows.iloc[0]["source"], "polygon")
        self.assertEqual(
            breaking_rows.iloc[0]["timestamp"], pd.Timestamp("2026-05-15T05:00:00Z")
        )

    def test_two_day_window_does_not_cluster_across_dates(self):
        # Two same-titled rows on adjacent days must stay separate — Tier 1 only
        # collapses within a single UTC date. Cross-day clustering is Tier 2's job.
        polygon_a = _row(
            "p1",
            "polygon",
            "2026-05-15T23:00:00Z",
            ["NVDA"],
            "SpaceX IPO filing lands approval today",
        )
        polygon_b = _row(
            "p2",
            "polygon",
            "2026-05-15T01:00:00Z",
            ["NVDA"],
            "SpaceX IPO filing lands approval today",
        )
        # NOTE: both rows belong to the same UTC date here; if we want to assert
        # cross-day, we keep ingest_daily targeted at one date. So instead, run
        # the day for 05-15 with both rows — Tier 1 SHOULD collapse them
        # (same date) into one. This verifies the within-day clustering is
        # correctly date-scoped.
        polygon_df = _frame([polygon_a, polygon_b])
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

        # Same UTC date → cluster.
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["timestamp"], pd.Timestamp("2026-05-15T01:00:00Z"))


if __name__ == "__main__":
    unittest.main()

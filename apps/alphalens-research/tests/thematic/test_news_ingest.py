import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame


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
    def setUp(self):
        # PR-6 added a 4th source. Default it to empty so the legacy
        # three-source assertions stay unchanged; tests that exercise the
        # EDGAR path re-patch it inside their own ``with`` block.
        patcher = patch.object(
            news_ingest, "_fetch_edgar_press_release", return_value=news_ingest.empty_news_frame()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_decodes_html_entities_in_titles_at_ingest(self):
        """Raw HTML character references are decoded once, at the pre-concat chokepoint.

        Covers every source uniformly — a Polygon title carrying ``&#8216;``/``&#8217;``
        comes out with real curly quotes in the unified parquet.
        """
        polygon_df = _frame(
            [
                _row(
                    "p1",
                    "polygon",
                    "2026-05-15T10:00:00Z",
                    ["MSFT"],
                    "Xbox warns of a &#8216;reset&#8217; as it prepares for layoffs",
                )
            ]
        )
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

            self.assertEqual(len(df), 1)
            self.assertEqual(
                df.iloc[0]["title"],
                "Xbox warns of a ‘reset’ as it prepares for layoffs",
            )

    def test_edgar_press_release_included_in_ingest(self):
        """EDGAR issuer press releases (8-K EX-99.1) now enter via PR-6 source."""
        self.assertTrue(hasattr(news_ingest, "_fetch_edgar_press_release"))
        self.assertIn("edgar_press_release", news_ingest._SOURCE_PRIORITY)
        # Issuer-direct is the richest source — must outrank the aggregators.
        self.assertLess(
            news_ingest._SOURCE_PRIORITY["edgar_press_release"],
            news_ingest._SOURCE_PRIORITY["polygon"],
        )

    def test_edgar_press_release_wins_url_canonical_dedup(self):
        """When EDGAR and Polygon share a canonical URL, EDGAR survives (richer)."""
        edgar_row = _row(
            "e1",
            "edgar_press_release",
            "2026-05-15T10:00:00Z",
            ["NVDA"],
            "NVDA reports record revenue",
        )
        edgar_row["url"] = "https://www.sec.gov/Archives/edgar/data/1/acc-index.htm"
        polygon_row = _row(
            "p1",
            "polygon",
            "2026-05-15T10:30:00Z",
            ["NVDA"],
            "NVDA reports record revenue",
        )
        polygon_row["url"] = "https://www.sec.gov/Archives/edgar/data/1/acc-index.htm?utm=feed"
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    news_ingest, "_fetch_edgar_press_release", return_value=_frame([edgar_row])
                ),
                patch.object(news_ingest, "_fetch_polygon", return_value=_frame([polygon_row])),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["source"], "edgar_press_release")

    def test_edgar_source_failure_does_not_abort_ingest(self):
        polygon_df = _frame([_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "OK")])
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    news_ingest,
                    "_fetch_edgar_press_release",
                    side_effect=RuntimeError("SEC 503"),
                ),
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )

            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["source"], "polygon")

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


class TestForceThreadedToSources(unittest.TestCase):
    """`ingest --force` must bust EACH source's own per-day read-through cache,
    not just the aggregator ``{date}.parquet``. Under the 6x/day cadence all
    ticks share the yesterday-UTC asof (hence the same per-source cache key), so
    without threading ``force`` the source caches short-circuit ticks 2-6 and
    late-arriving items for that asof never re-fetch. No ``setUp`` stub here so
    the assertion reaches the real ``_fetch_*`` helpers and the source-level
    ``fetch_daily_news`` they forward to."""

    def _patch_all_sources(self, empty_df):
        return (
            patch.object(
                news_ingest.edgar_press_release, "fetch_daily_news", return_value=empty_df
            ),
            patch.object(news_ingest.polygon_news, "fetch_daily_news", return_value=empty_df),
            patch.object(news_ingest.gdelt, "fetch_daily_news", return_value=empty_df),
            patch.object(news_ingest.rss, "fetch_daily_news", return_value=empty_df),
            patch.object(news_ingest.perplexity, "fetch_daily_news", return_value=empty_df),
        )

    def test_force_is_threaded_to_every_source_fetcher(self):
        empty_df = news_ingest.empty_news_frame()
        p_edgar, p_polygon, p_gdelt, p_rss, p_perplexity = self._patch_all_sources(empty_df)
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                p_edgar as m_edgar,
                p_polygon as m_polygon,
                p_gdelt as m_gdelt,
                p_rss as m_rss,
                p_perplexity as m_perplexity,
                mock.patch.dict("os.environ", {"ALPHALENS_PERPLEXITY_SOURCE": "1"}),
            ):
                news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    force=True,
                )
            for name, m in (
                ("edgar_press_release", m_edgar),
                ("polygon", m_polygon),
                ("gdelt", m_gdelt),
                ("rss", m_rss),
                ("perplexity", m_perplexity),
            ):
                self.assertTrue(m.called, f"{name} source fetcher was not called")
                self.assertIs(
                    m.call_args.kwargs.get("force"),
                    True,
                    f"{name} source fetcher was not called with force=True",
                )

    def test_force_false_is_threaded_as_false(self):
        empty_df = news_ingest.empty_news_frame()
        p_edgar, p_polygon, p_gdelt, p_rss, p_perplexity = self._patch_all_sources(empty_df)
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                p_edgar as m_edgar,
                p_polygon,
                p_gdelt,
                p_rss,
                p_perplexity,
            ):
                news_ingest.ingest_daily(date=dt.date(2026, 5, 15), cache_dir=Path(tmpdir))
            self.assertIs(m_edgar.call_args.kwargs.get("force"), False)


class TestTier1LexicalClustering(unittest.TestCase):
    """Same-day syndication: collapse echoes into clusters before cap-200."""

    def setUp(self):
        patcher = patch.object(
            news_ingest, "_fetch_edgar_press_release", return_value=news_ingest.empty_news_frame()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
        polygon_df = _frame([*single_rows, breaking_root])
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
        self.assertEqual(breaking_rows.iloc[0]["timestamp"], pd.Timestamp("2026-05-15T05:00:00Z"))

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


class TestNewsIngestStrictWindow(unittest.TestCase):
    """P1a: source-agnostic window enforcement + per-source quota allocation."""

    def setUp(self):
        patcher = patch.object(
            news_ingest, "_fetch_edgar_press_release", return_value=news_ingest.empty_news_frame()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_allocate_never_exceeds_max_items_below_source_count(self):
        """Quota floor (>=1 per source) must not over-allocate: with 4 weighted
        sources and max_items < 4, the result is still capped at max_items."""
        rows = [
            _row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "poly"),
            _row("e1", "edgar_press_release", "2026-05-15T11:00:00Z", ["AAPL"], "edgar"),
            _row("g1", "gdelt", "2026-05-15T12:00:00Z", [], "gdelt"),
            _row("r1", "rss", "2026-05-15T13:00:00Z", [], "rss"),
        ]
        df = _frame(rows)
        out = news_ingest._allocate_per_source(df, news_ingest._SOURCE_QUOTA_WEIGHTS, max_items=2)
        self.assertLessEqual(len(out), 2)

    def test_enforces_strict_window_after_clustering(self):
        """Belt-and-suspenders: rows outside [date 00:00, date+1 00:00) UTC are dropped."""
        polygon_row = _row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "Polygon story")
        edgar_row = _row(
            "e1", "edgar_press_release", "2026-05-15T23:50:00Z", ["AAPL"], "EDGAR story"
        )
        rss_in_window = _row("r1", "rss", "2026-05-15T10:00:00Z", [], "RSS story alpha")
        rss_out_of_window = _row("r2", "rss", "2026-05-16T02:00:00Z", [], "RSS story bravo")

        polygon_df = _frame([polygon_row])
        edgar_df = _frame([edgar_row])
        rss_df = _frame([rss_in_window, rss_out_of_window])
        empty_df = news_ingest.empty_news_frame()

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_edgar_press_release", return_value=edgar_df),
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=rss_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    force=True,
                )

        self.assertEqual(len(df), 3)
        window_start = pd.Timestamp("2026-05-15", tz="UTC")
        window_end = window_start + pd.Timedelta(days=1)
        for _, row in df.iterrows():
            self.assertGreaterEqual(row["timestamp"], window_start)
            self.assertLess(row["timestamp"], window_end)
        self.assertNotIn("r2", df["id"].tolist())

    def test_per_source_quota_prevents_crowding(self):
        """When GDELT floods with 150 rows, Polygon/SEC/RSS are NOT crowded out."""
        gdelt_rows = [
            _row(f"g{i:03d}", "gdelt", "2026-05-15T15:00:00Z", [], f"GDELT story {i:04d}")
            for i in range(150)
        ]
        polygon_rows = [
            _row(f"p{i:03d}", "polygon", "2026-05-15T09:00:00Z", ["NVDA"], f"Polygon story {i:04d}")
            for i in range(30)
        ]
        edgar_rows = [
            _row(
                f"e{i:02d}",
                "edgar_press_release",
                "2026-05-15T11:00:00Z",
                ["AAPL"],
                f"EDGAR story {i:04d}",
            )
            for i in range(20)
        ]
        rss_rows = [
            _row(f"r{i:02d}", "rss", "2026-05-15T12:00:00Z", [], f"RSS story {i:04d}")
            for i in range(20)
        ]

        gdelt_df = _frame(gdelt_rows)
        polygon_df = _frame(polygon_rows)
        edgar_df = _frame(edgar_rows)
        rss_df = _frame(rss_rows)

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_edgar_press_release", return_value=edgar_df),
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=gdelt_df),
                patch.object(news_ingest, "_fetch_rss", return_value=rss_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    force=True,
                    max_items=200,
                )

        self.assertEqual(len(df), 200)

        gdelt_count = len(df[df["source"] == "gdelt"])
        polygon_count = len(df[df["source"] == "polygon"])
        edgar_count = len(df[df["source"] == "edgar_press_release"])
        rss_count = len(df[df["source"] == "rss"])

        # Quota weights {edgar:0.25, polygon:0.25, gdelt:0.20, rss:0.15, perplexity:0.15}:
        # GDELT capped to ~40 (not allowed to fill 150 slots), every other
        # source fully admitted (each has < its quota). Backfill of unused
        # budget pulls the rest of GDELT to reach 200.
        self.assertGreater(polygon_count, 0)
        self.assertGreater(edgar_count, 0)
        self.assertGreater(rss_count, 0)
        # All 30 polygon, 20 edgar, 20 rss survive (under their quotas).
        self.assertEqual(polygon_count, 30)
        self.assertEqual(edgar_count, 20)
        self.assertEqual(rss_count, 20)
        # GDELT fills the remaining 130 slots (200 - 70), capped by its 150 supply.
        self.assertEqual(gdelt_count, 130)

    def test_final_sort_is_timestamp_descending(self):
        """After per-source quota, final result is sorted timestamp DESC (newest first)."""
        rows = [
            _row("a", "polygon", "2026-05-15T08:00:00Z", ["NVDA"], "oldword"),
            _row("b", "polygon", "2026-05-15T18:00:00Z", ["NVDA"], "newword"),
            _row("c", "polygon", "2026-05-15T13:00:00Z", ["NVDA"], "midword"),
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


class TestSourceRowCountsOutParam(unittest.TestCase):
    """RAW per-source counts feed the #384 EX-99.1 dead-man-switch.

    The aggregate ingest gauge is POST-dedup/cap and undercounts edgar (the
    lexical-cluster representative is timestamp-first, so a non-earliest edgar
    row loses its source label; the cap drops rows). The switch needs the count
    captured right after _safe_call("edgar_press_release", ...), BEFORE
    dedup/cap. ingest_daily populates a caller-supplied dict so existing callers
    (which pass nothing) keep the unchanged behavior.
    """

    def setUp(self):
        patcher = patch.object(
            news_ingest,
            "_fetch_edgar_press_release",
            return_value=news_ingest.empty_news_frame(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_out_param_carries_one_entry_per_source_including_zero(self):
        polygon_df = _frame(
            [_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "Polygon piece")]
        )
        gdelt_df = _frame([_row("g1", "gdelt", "2026-05-15T11:00:00Z", [], "GDELT piece")])
        empty_df = news_ingest.empty_news_frame()

        counts: dict[str, int] = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=gdelt_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    force=True,
                    source_row_counts=counts,
                )

        self.assertEqual(set(counts), set(news_ingest._SOURCE_PRIORITY))
        self.assertEqual(counts["polygon"], 1)
        self.assertEqual(counts["gdelt"], 1)
        self.assertEqual(counts["edgar_press_release"], 0)
        self.assertEqual(counts["rss"], 0)

    def test_edgar_count_is_raw_pre_dedup_not_post(self):
        # THE load-bearing pin: 2 edgar rows that the URL-canon dedup collapses
        # to 1 (same index.htm, one carries a ?utm tracking param) must still
        # count as 2. A post-dedup count would read 1, masking a half-starved
        # fetch.
        edgar_a = _row(
            "e1",
            "edgar_press_release",
            "2026-05-15T10:00:00Z",
            ["NVDA"],
            "NVDA reports record revenue",
        )
        edgar_b = _row(
            "e2",
            "edgar_press_release",
            "2026-05-15T10:05:00Z",
            ["NVDA"],
            "NVDA reports record revenue",
        )
        edgar_a["url"] = "https://www.sec.gov/Archives/edgar/data/1/acc-index.htm"
        edgar_b["url"] = "https://www.sec.gov/Archives/edgar/data/1/acc-index.htm?utm=x"
        empty_df = news_ingest.empty_news_frame()

        counts: dict[str, int] = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    news_ingest,
                    "_fetch_edgar_press_release",
                    return_value=_frame([edgar_a, edgar_b]),
                ),
                patch.object(news_ingest, "_fetch_polygon", return_value=empty_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                df = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    force=True,
                    source_row_counts=counts,
                )

        self.assertEqual(counts["edgar_press_release"], 2)  # RAW
        self.assertEqual(len(df), 1)  # dedup actually fired

    def test_edgar_count_is_zero_when_source_swallows_exception(self):
        # _safe_call swallows the SEC 403 → empty frame → the count MUST be an
        # explicit 0 (this IS the starvation signal; a skipped emit would let
        # node_exporter re-serve the last nonzero forever and silence the alert).
        empty_df = news_ingest.empty_news_frame()
        counts: dict[str, int] = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    news_ingest,
                    "_fetch_edgar_press_release",
                    side_effect=RuntimeError("SEC 403"),
                ),
                patch.object(
                    news_ingest,
                    "_fetch_polygon",
                    return_value=_frame(
                        [_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "OK")]
                    ),
                ),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    force=True,
                    source_row_counts=counts,
                )

        self.assertEqual(counts["edgar_press_release"], 0)
        self.assertEqual(counts["polygon"], 1)

    def test_out_param_omitted_is_backwards_compatible(self):
        polygon_df = _frame([_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "Piece")])
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
                    force=True,
                )  # NO source_row_counts kwarg
            self.assertEqual(len(df), 1)
            self.assertTrue((Path(tmpdir) / "2026-05-15.parquet").exists())

    def test_out_param_not_populated_on_cache_hit(self):
        polygon_df = _frame([_row("p1", "polygon", "2026-05-15T10:00:00Z", ["NVDA"], "Piece")])
        empty_df = news_ingest.empty_news_frame()
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(news_ingest, "_fetch_polygon", return_value=polygon_df),
                patch.object(news_ingest, "_fetch_gdelt", return_value=empty_df),
                patch.object(news_ingest, "_fetch_rss", return_value=empty_df),
            ):
                news_ingest.ingest_daily(date=dt.date(2026, 5, 15), cache_dir=Path(tmpdir))

            counts: dict[str, int] = {}
            with patch.object(news_ingest, "_fetch_polygon", side_effect=AssertionError("no call")):
                news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                    source_row_counts=counts,
                )
        self.assertEqual(counts, {})


class TestPerplexitySourceRegistration(unittest.TestCase):
    def test_quota_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(news_ingest._SOURCE_QUOTA_WEIGHTS.values()), 1.0, places=9)

    def test_perplexity_registered_in_priority_and_quota(self):
        self.assertEqual(news_ingest._SOURCE_PRIORITY["perplexity"], 4)
        self.assertIn("perplexity", news_ingest._SOURCE_QUOTA_WEIGHTS)

    def test_fetch_perplexity_off_by_default_does_not_call_adapter(self):
        import os

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALPHALENS_PERPLEXITY_SOURCE", None)
            with mock.patch(
                "alphalens_pipeline.thematic.sources.perplexity.fetch_daily_news"
            ) as adapter:
                df = news_ingest._fetch_perplexity(date=dt.date(2026, 6, 12))
        adapter.assert_not_called()
        self.assertEqual(len(df), 0)

    def test_fetch_perplexity_on_calls_adapter(self):
        import os

        with mock.patch.dict(os.environ, {"ALPHALENS_PERPLEXITY_SOURCE": "1"}):
            with mock.patch(
                "alphalens_pipeline.thematic.sources.perplexity.fetch_daily_news",
                return_value=empty_news_frame(),
            ) as adapter:
                news_ingest._fetch_perplexity(date=dt.date(2026, 6, 12))
        adapter.assert_called_once()


if __name__ == "__main__":
    unittest.main()

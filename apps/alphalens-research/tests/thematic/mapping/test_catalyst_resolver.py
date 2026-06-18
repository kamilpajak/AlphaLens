import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver


def _seed_news(news_dir: Path, date: dt.date, rows: list[dict]) -> None:
    news_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(news_dir / f"{date.isoformat()}.parquet", index=False)


def _seed_events(events_dir: Path, date: dt.date, rows: list[dict]) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(events_dir / f"{date.isoformat()}.parquet", index=False)


class TestFindTriggerEvent(unittest.TestCase):
    def test_returns_most_recent_theme_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 4, 14),
                [
                    {
                        "id": "n1",
                        "title": "NVIDIA Launches Ising",
                        "url": "https://nvidianews.example/ising",
                        "published_at": pd.Timestamp("2026-04-14T13:00:00Z"),
                    },
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 4, 14),
                [
                    {
                        "news_id": "n1",
                        "themes": ["quantum_computing", "AI_models"],
                        "confidence": 0.95,
                    },
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="quantum_computing",
                asof=dt.date(2026, 4, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://nvidianews.example/ising")
        self.assertIn("NVIDIA", cat.title)
        self.assertEqual(cat.published_at, "2026-04-14")

    def test_returns_none_when_theme_not_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 4, 14),
                [
                    {
                        "id": "n1",
                        "title": "Other news",
                        "url": "https://other.example",
                        "published_at": pd.Timestamp("2026-04-14T13:00:00Z"),
                    },
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 4, 14),
                [
                    {"news_id": "n1", "themes": ["biotech", "AI_models"], "confidence": 0.9},
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="quantum_computing",
                asof=dt.date(2026, 4, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)

    def test_picks_newest_when_multiple_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 4, 10),
                [
                    {
                        "id": "old",
                        "title": "Older quantum news",
                        "url": "https://x/old",
                        "published_at": pd.Timestamp("2026-04-10T10:00:00Z"),
                    },
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 4, 10),
                [
                    {"news_id": "old", "themes": ["quantum_computing"], "confidence": 0.9},
                ],
            )
            _seed_news(
                news,
                dt.date(2026, 4, 14),
                [
                    {
                        "id": "new",
                        "title": "NEWER quantum news",
                        "url": "https://x/new",
                        "published_at": pd.Timestamp("2026-04-14T13:00:00Z"),
                    },
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 4, 14),
                [
                    {"news_id": "new", "themes": ["quantum_computing"], "confidence": 0.95},
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="quantum_computing",
                asof=dt.date(2026, 4, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertEqual(cat.url, "https://x/new")

    def test_returns_none_when_dirs_missing(self):
        cat = catalyst_resolver.find_trigger_event(
            theme="quantum_computing",
            asof=dt.date(2026, 4, 14),
            events_dir=Path("/nonexistent/events"),
            news_dir=Path("/nonexistent/news"),
            lookback_days=30,
        )
        self.assertIsNone(cat)

    def test_title_truncated_to_max_len(self):
        long_title = "A" * 500
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 4, 14),
                [
                    {
                        "id": "n1",
                        "title": long_title,
                        "url": "https://x",
                        "published_at": pd.Timestamp("2026-04-14T13:00:00Z"),
                    },
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 4, 14),
                [
                    {"news_id": "n1", "themes": ["quantum_computing"], "confidence": 0.9},
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="quantum_computing",
                asof=dt.date(2026, 4, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertLessEqual(len(cat.title), 200)

    def test_skips_files_with_invalid_filename_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events"
            news = Path(tmp) / "news"
            events.mkdir()
            news.mkdir()
            # Filename with non-ISO date stem — _load_window must skip silently.
            (events / "not-a-date.parquet").write_bytes(b"junk")
            cat = catalyst_resolver.find_trigger_event(
                theme="quantum_computing",
                asof=dt.date(2026, 4, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)

    def test_skips_files_with_unreadable_parquet(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events"
            news = Path(tmp) / "news"
            events.mkdir()
            news.mkdir()
            (events / "2026-04-14.parquet").write_bytes(b"not parquet bytes")
            cat = catalyst_resolver.find_trigger_event(
                theme="quantum_computing",
                asof=dt.date(2026, 4, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)

    def test_returns_none_when_news_window_empty(self):
        # Events match theme but no news in the same window → graceful None.
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events"
            news = Path(tmp) / "news"
            _seed_events(
                events,
                dt.date(2026, 4, 14),
                [{"news_id": "n1", "themes": ["quantum_computing"], "confidence": 0.9}],
            )
            news.mkdir()  # exists but empty
            cat = catalyst_resolver.find_trigger_event(
                theme="quantum_computing",
                asof=dt.date(2026, 4, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)

    def test_themes_field_with_non_iterable_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events"
            news = Path(tmp) / "news"
            _seed_news(
                news,
                dt.date(2026, 4, 14),
                [
                    {
                        "id": "n1",
                        "title": "T",
                        "url": "u",
                        "published_at": pd.Timestamp("2026-04-14T13:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 4, 14),
                [{"news_id": "n1", "themes": 42, "confidence": 0.9}],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="quantum_computing",
                asof=dt.date(2026, 4, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)


class TestFindTriggerEventTimestampSchema(unittest.TestCase):
    """As of the 2026-05 ingest refactor, all news adapters (rss/polygon/
    gdelt/edgar) write the column ``timestamp`` (per ``sources/schema.py``)
    instead of the legacy ``published_at``. The catalyst resolver must
    handle the canonical column; otherwise every real-pipeline lookup
    raises KeyError, gets swallowed by the orchestrator's _safe wrapper,
    and silently drops the source_event_url from every brief.
    """

    def test_returns_event_when_news_uses_timestamp_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 18),
                [
                    {
                        "id": "n1",
                        "title": "Cybersecurity vendor M&A wave",
                        "url": "https://example.com/cyber",
                        "timestamp": pd.Timestamp("2026-05-18T13:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 18),
                [{"news_id": "n1", "themes": ["cybersecurity"], "confidence": 0.9}],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="cybersecurity",
                asof=dt.date(2026, 5, 18),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://example.com/cyber")
        self.assertEqual(cat.published_at, "2026-05-18")

    def test_returns_none_when_neither_time_column_present(self):
        # Defensive: this is the silent-failure mode the 2026-05-18 bug
        # originally exhibited (KeyError swallowed by orchestrator _safe →
        # source_event_url=None across all candidates). With the dispatch,
        # the return-None path is now explicit; this test pins it so a
        # future schema rename cannot recreate the symptom undetected.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 18),
                [
                    {
                        "id": "n1",
                        "title": "No time column",
                        "url": "https://example.com/notime",
                        # No timestamp, no published_at — simulates a future
                        # schema where the time column is renamed without
                        # the consumer being updated.
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 18),
                [{"news_id": "n1", "themes": ["AI"], "confidence": 0.9}],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="AI",
                asof=dt.date(2026, 5, 18),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)

    def test_handles_tz_naive_timestamp_input(self):
        # The fix added pd.to_datetime(..., utc=True) to normalize tz handling.
        # Verify that a tz-naive timestamp coerces cleanly (treated as UTC)
        # and produces the expected .date() output. Defensive against a
        # future ingest adapter that emits tz-naive instead of tz-aware.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 18),
                [
                    {
                        "id": "n1",
                        "title": "Tz-naive event",
                        "url": "https://example.com/naive",
                        "timestamp": pd.Timestamp("2026-05-18T13:00:00"),  # naive
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 18),
                [{"news_id": "n1", "themes": ["AI"], "confidence": 0.9}],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="AI",
                asof=dt.date(2026, 5, 18),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.published_at, "2026-05-18")

    def test_prefers_timestamp_when_both_columns_present(self):
        # If a news file happens to carry both legacy and new column,
        # prefer timestamp (canonical, tz-aware). Defensive.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 18),
                [
                    {
                        "id": "n1",
                        "title": "Mixed-schema row",
                        "url": "https://example.com/mixed",
                        "timestamp": pd.Timestamp("2026-05-18T13:00:00Z"),
                        "published_at": pd.Timestamp("2020-01-01T00:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 18),
                [{"news_id": "n1", "themes": ["AI"], "confidence": 0.9}],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="AI",
                asof=dt.date(2026, 5, 18),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertEqual(cat.published_at, "2026-05-18")


class TestFindTriggerEventNoiseFilter(unittest.TestCase):
    """L2 + L1 noise filtering per 2026-05-18 design (option C):
    - L2: drop events whose event_type is in schema.NOISE_EVENT_TYPES
    - L1: drop events whose URL matches a regex in the noise YAML config
    The resolver picks the latest REMAINING event after both filters.
    """

    def test_drops_noise_event_type(self):
        # 'promo' is in NOISE_EVENT_TYPES. The legit M&A event is older but
        # should win over the newer promo.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 18),
                [
                    {
                        "id": "ma",
                        "title": "Real M&A deal announced",
                        "url": "https://reuters.com/article/abc",
                        "timestamp": pd.Timestamp("2026-05-15T10:00:00Z"),
                    },
                    {
                        "id": "promo",
                        "title": "VPN promo codes",
                        "url": "https://wired.com/story/vpn-promo",
                        "timestamp": pd.Timestamp("2026-05-18T10:00:00Z"),
                    },
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 18),
                [
                    {"news_id": "ma", "themes": ["AI"], "event_type": "m_and_a", "confidence": 0.9},
                    {
                        "news_id": "promo",
                        "themes": ["AI"],
                        "event_type": "promo",
                        "confidence": 0.9,
                    },
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="AI",
                asof=dt.date(2026, 5, 18),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        # Promo (newer) dropped → M&A wins.
        self.assertEqual(cat.url, "https://reuters.com/article/abc")

    def test_drops_url_pattern_blocklist(self):
        # Even if event_type isn't flagged (Flash mis-classified the promo
        # page as 'product_launch'), the URL pattern blocklist catches it.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 18),
                [
                    {
                        "id": "ma",
                        "title": "Real M&A deal",
                        "url": "https://reuters.com/article/legit-ma",
                        "timestamp": pd.Timestamp("2026-05-15T10:00:00Z"),
                    },
                    {
                        "id": "p",
                        "title": "Surfshark Promo Codes May 2026",
                        "url": "https://www.wired.com/story/surfshark-coupon/",
                        "timestamp": pd.Timestamp("2026-05-18T10:00:00Z"),
                    },
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 18),
                [
                    {"news_id": "ma", "themes": ["AI"], "event_type": "m_and_a", "confidence": 0.9},
                    # NOTE: event_type='product_launch' (Flash mis-classified)
                    # would slip past the L2 noise filter, but the URL slug
                    # 'surfshark-coupon' matches the blocklist regex.
                    {
                        "news_id": "p",
                        "themes": ["AI"],
                        "event_type": "product_launch",
                        "confidence": 0.9,
                    },
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="AI",
                asof=dt.date(2026, 5, 18),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertEqual(cat.url, "https://reuters.com/article/legit-ma")

    def test_returns_none_when_all_events_are_noise(self):
        # If every candidate event is noise, return None (gates_unknown
        # downstream — no catalyst surfaced rather than a bad one).
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 18),
                [
                    {
                        "id": "p",
                        "title": "Promo only",
                        "url": "https://wired.com/story/some-deal",
                        "timestamp": pd.Timestamp("2026-05-18T10:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 18),
                [{"news_id": "p", "themes": ["AI"], "event_type": "promo", "confidence": 0.9}],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="AI",
                asof=dt.date(2026, 5, 18),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)

    def test_legit_event_with_no_event_type_column_still_works(self):
        # Backward compat: older events parquets (or future schema drift)
        # may not have event_type. Don't crash; treat as "unknown" event_type
        # so the legit event still surfaces.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 18),
                [
                    {
                        "id": "n1",
                        "title": "Some news",
                        "url": "https://reuters.com/article/legit",
                        "timestamp": pd.Timestamp("2026-05-18T10:00:00Z"),
                    }
                ],
            )
            # event row WITHOUT event_type column.
            _seed_events(
                events,
                dt.date(2026, 5, 18),
                [{"news_id": "n1", "themes": ["AI"], "confidence": 0.9}],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="AI",
                asof=dt.date(2026, 5, 18),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://reuters.com/article/legit")


class TestTier2StoryArc(unittest.TestCase):
    """Resolver picks the EARLIEST member of an entity-overlap story arc."""

    def test_returns_earliest_in_story_arc_via_entity_jaccard(self):
        # Three events on three days, all theme="space_exploration", sharing
        # primary_entities {SPACEX, MUSK}. The LATEST event (14:00 RSS echo)
        # activates the brief, but the catalyst URL must point back to the
        # EARLIEST root (09:00 Polygon source two days earlier).
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 12),
                [
                    {
                        "id": "n_root",
                        "title": "SpaceX files IPO with SEC",
                        "url": "https://reuters.example/spacex-ipo",
                        "timestamp": pd.Timestamp("2026-05-12T09:00:00Z"),
                    }
                ],
            )
            _seed_news(
                news,
                dt.date(2026, 5, 13),
                [
                    {
                        "id": "n_mid",
                        "title": "SpaceX IPO valuation jumps",
                        "url": "https://bloomberg.example/spacex-ipo-valuation",
                        "timestamp": pd.Timestamp("2026-05-13T11:00:00Z"),
                    }
                ],
            )
            _seed_news(
                news,
                dt.date(2026, 5, 14),
                [
                    {
                        "id": "n_echo",
                        "title": "Asteroid mining and Musk's plans",
                        "url": "https://ft.example/spacex-echo",
                        "timestamp": pd.Timestamp("2026-05-14T14:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 12),
                [
                    {
                        "news_id": "n_root",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.92,
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 13),
                [
                    {
                        "news_id": "n_mid",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.89,
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 14),
                [
                    {
                        "news_id": "n_echo",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.85,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="space_exploration",
                asof=dt.date(2026, 5, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        # Catalyst URL traces back to the root, not the latest echo
        self.assertEqual(cat.url, "https://reuters.example/spacex-ipo")
        self.assertEqual(cat.published_at, "2026-05-12")
        # Trigger URL is the LATEST event (the freshness that activated the brief)
        self.assertEqual(cat.trigger_url, "https://ft.example/spacex-echo")
        self.assertEqual(cat.trigger_published_at, "2026-05-14")
        self.assertEqual(cat.echo_count, 3)
        self.assertTrue(cat.is_amplified)

    def test_returns_trigger_when_entities_sparse(self):
        # Trigger has only 1 primary_entity (< MIN_TRIGGER_ENTITIES=2). Anchor
        # gate fires; resolver degrades to the legacy "latest-as-catalyst"
        # behaviour to avoid pulling cross-story noise via a single-entity arc.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 14),
                [
                    {
                        "id": "n_lone",
                        "title": "Apple Q4 earnings beat",
                        "url": "https://wsj.example/apple-q4",
                        "timestamp": pd.Timestamp("2026-05-14T14:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 14),
                [
                    {
                        "news_id": "n_lone",
                        "themes": ["earnings"],
                        "primary_entities": ["AAPL"],  # only 1 entity
                        "confidence": 0.88,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="earnings",
                asof=dt.date(2026, 5, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://wsj.example/apple-q4")
        self.assertEqual(cat.echo_count, 1)
        self.assertFalse(cat.is_amplified)
        # When degraded, trigger fields equal catalyst fields
        self.assertEqual(cat.trigger_url, cat.url)

    def test_arc_excludes_unrelated_theme_entity_overlap(self):
        # Two events share entity {SPACEX} but belong to DIFFERENT themes.
        # The theme filter (which runs first) is still load-bearing — the
        # arc resolver must NOT cross theme boundaries even when entities match.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 12),
                [
                    {
                        "id": "n_geo",
                        "title": "Geopolitics piece naming SpaceX exports",
                        "url": "https://geo.example/old",
                        "timestamp": pd.Timestamp("2026-05-12T09:00:00Z"),
                    }
                ],
            )
            _seed_news(
                news,
                dt.date(2026, 5, 14),
                [
                    {
                        "id": "n_space",
                        "title": "SpaceX IPO breaking",
                        "url": "https://space.example/new",
                        "timestamp": pd.Timestamp("2026-05-14T14:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 12),
                [
                    {
                        "news_id": "n_geo",
                        "themes": ["geopolitics"],  # different theme
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.91,
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 14),
                [
                    {
                        "news_id": "n_space",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.93,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="space_exploration",
                asof=dt.date(2026, 5, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        # Geopolitics event SHARES entities but is theme-isolated → NOT in arc
        self.assertEqual(cat.url, "https://space.example/new")
        self.assertEqual(cat.echo_count, 1)
        self.assertFalse(cat.is_amplified)

    def test_arc_excludes_low_entity_jaccard(self):
        # Trigger entities {SPACEX, MUSK}; candidate entities {SPACEX, NASA,
        # BOEING, LOCKHEED} share 1 entity → jaccard = 1/5 = 0.2 < 0.3 → out.
        # Catalyst stays as the trigger (no arc to walk back through).
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 12),
                [
                    {
                        "id": "n_old_diluted",
                        "title": "Aerospace consortium update",
                        "url": "https://defense.example/coalition",
                        "timestamp": pd.Timestamp("2026-05-12T09:00:00Z"),
                    }
                ],
            )
            _seed_news(
                news,
                dt.date(2026, 5, 14),
                [
                    {
                        "id": "n_trigger",
                        "title": "SpaceX files IPO",
                        "url": "https://reuters.example/spacex-trigger",
                        "timestamp": pd.Timestamp("2026-05-14T14:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 12),
                [
                    {
                        "news_id": "n_old_diluted",
                        "themes": ["space_exploration"],
                        # 4 entities; shares only SPACEX with trigger → 1/5 = 0.2
                        "primary_entities": ["SPACEX", "NASA", "BOEING", "LOCKHEED"],
                        "confidence": 0.85,
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 14),
                [
                    {
                        "news_id": "n_trigger",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.93,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="space_exploration",
                asof=dt.date(2026, 5, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        # Diluted event excluded; catalyst == trigger
        self.assertEqual(cat.url, "https://reuters.example/spacex-trigger")
        self.assertEqual(cat.echo_count, 1)

    def test_bare_string_primary_entities_not_shredded_into_chars(self):
        """If Gemini emits "SPACEX" (str) instead of ["SPACEX"] (list), the
        resolver must treat it as a single entity — NOT iterate it
        character-by-character into {"S","P","A","C","E","X"} which would
        otherwise create spurious entity-overlap with any unrelated event
        whose primary_entities happens to share a letter.
        """
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 14),
                [
                    {
                        "id": "n_str",
                        "title": "SpaceX IPO breaking",
                        "url": "https://reuters.example/spacex-str",
                        "timestamp": pd.Timestamp("2026-05-14T14:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 14),
                [
                    {
                        "news_id": "n_str",
                        "themes": ["space_exploration"],
                        # BARE STRING, not list — Gemini-emitted malformed schema
                        "primary_entities": "SPACEX",
                        "confidence": 0.9,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="space_exploration",
                asof=dt.date(2026, 5, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        # Single-entity (post-coercion) → sparse-entity gate fires → legacy mode.
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://reuters.example/spacex-str")
        self.assertEqual(cat.echo_count, 1)
        self.assertFalse(cat.is_amplified)

    def test_arc_high_jaccard_includes_partial_entity_overlap(self):
        # Trigger {SPACEX, MUSK}; candidate {SPACEX, MUSK, BOEING} →
        # jaccard = 2/3 = 0.67 > 0.3 → IN arc. Catalyst = earliest.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 12),
                [
                    {
                        "id": "n_root_partial",
                        "title": "Boeing-SpaceX partnership announced by Musk",
                        "url": "https://reuters.example/spacex-boeing",
                        "timestamp": pd.Timestamp("2026-05-12T09:00:00Z"),
                    }
                ],
            )
            _seed_news(
                news,
                dt.date(2026, 5, 14),
                [
                    {
                        "id": "n_trigger_partial",
                        "title": "SpaceX IPO with Musk addresses board",
                        "url": "https://ft.example/spacex-trigger",
                        "timestamp": pd.Timestamp("2026-05-14T14:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 12),
                [
                    {
                        "news_id": "n_root_partial",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK", "BOEING"],
                        "confidence": 0.88,
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 14),
                [
                    {
                        "news_id": "n_trigger_partial",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.93,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="space_exploration",
                asof=dt.date(2026, 5, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://reuters.example/spacex-boeing")
        self.assertEqual(cat.echo_count, 2)
        self.assertTrue(cat.is_amplified)
        self.assertEqual(cat.trigger_url, "https://ft.example/spacex-trigger")


class TestCatalystEntityAnchor(unittest.TestCase):
    """Eligibility gate: a catalyst event must name >=1 company entity.

    Generic articles that name NO company (``primary_entities=[]``) get
    attached as the catalyst to tickers that share only the THEME — a
    Chinese state-media "build a tech power" piece with no entity was the
    source_event for BAH/PSN/AVAV in production. The fix drops zero-entity
    rows before the trigger is selected. The discriminator is entity
    presence, NOT language — legit foreign-language news resolves entities
    (NVDA, MSFT, TSM, SKHYNIX) and stays eligible.
    """

    def test_theme_with_only_entityless_event_returns_none(self):
        # The theme's ONLY event names no company → no catalyst surfaced.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 6, 12),
                [
                    {
                        "id": "n_generic",
                        "title": "Build a tech power, state media urges",
                        "url": "https://state.example/tech-power",
                        "timestamp": pd.Timestamp("2026-06-12T08:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 6, 12),
                [
                    {
                        "news_id": "n_generic",
                        "themes": ["national_strategy"],
                        "primary_entities": [],  # names NO company
                        "confidence": 0.9,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="national_strategy",
                asof=dt.date(2026, 6, 12),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)

    def test_entityless_latest_ignored_entity_bearing_chosen(self):
        # Latest event is entity-less; an older entity-bearing event exists.
        # The entity-bearing event is chosen despite NOT being the latest.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 6, 10),
                [
                    {
                        "id": "n_nvda",
                        "title": "NVIDIA unveils next-gen accelerator",
                        "url": "https://reuters.example/nvda-launch",
                        "timestamp": pd.Timestamp("2026-06-10T09:00:00Z"),
                    }
                ],
            )
            _seed_news(
                news,
                dt.date(2026, 6, 12),
                [
                    {
                        "id": "n_generic",
                        "title": "Build a tech power, state media urges",
                        "url": "https://state.example/tech-power",
                        "timestamp": pd.Timestamp("2026-06-12T08:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 6, 10),
                [
                    {
                        "news_id": "n_nvda",
                        "themes": ["national_strategy"],
                        "primary_entities": ["NVDA"],
                        "confidence": 0.92,
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 6, 12),
                [
                    {
                        "news_id": "n_generic",
                        "themes": ["national_strategy"],
                        "primary_entities": [],  # latest, but entity-less
                        "confidence": 0.9,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="national_strategy",
                asof=dt.date(2026, 6, 12),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        # Entity-bearing event wins; the latest entity-less row is dropped.
        self.assertEqual(cat.url, "https://reuters.example/nvda-launch")
        self.assertEqual(cat.published_at, "2026-06-10")

    def test_single_entity_event_still_eligible(self):
        # A real single-company catalyst (exactly 1 entity) stays eligible —
        # the 1-entity degraded single-event path is preserved.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 6, 12),
                [
                    {
                        "id": "n_ford",
                        "title": "Ford guides Q3 above consensus",
                        "url": "https://wsj.example/ford-guidance",
                        "timestamp": pd.Timestamp("2026-06-12T13:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 6, 12),
                [
                    {
                        "news_id": "n_ford",
                        "themes": ["autos"],
                        "primary_entities": ["F"],  # exactly 1 entity
                        "confidence": 0.88,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="autos",
                asof=dt.date(2026, 6, 12),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://wsj.example/ford-guidance")
        self.assertEqual(cat.echo_count, 1)
        self.assertFalse(cat.is_amplified)

    def test_macro_geopolitical_entityless_event_excluded(self):
        # No event_type exception — a macro/geopolitical event with no entity
        # is gated exactly like any other entity-less event.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 6, 12),
                [
                    {
                        "id": "n_macro",
                        "title": "Trade tensions reshape supply chains",
                        "url": "https://macro.example/trade-tensions",
                        "timestamp": pd.Timestamp("2026-06-12T07:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 6, 12),
                [
                    {
                        "news_id": "n_macro",
                        "themes": ["geopolitics"],
                        "event_type": "geopolitical",
                        "primary_entities": [],
                        "confidence": 0.9,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="geopolitics",
                asof=dt.date(2026, 6, 12),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNone(cat)

    def test_two_entity_story_arc_unchanged(self):
        # Regression: the >=2-entity story-arc behaviour is unchanged. Both
        # events carry {SPACEX, MUSK}; catalyst traces to the earliest root.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 5, 12),
                [
                    {
                        "id": "n_root",
                        "title": "SpaceX files IPO with SEC",
                        "url": "https://reuters.example/spacex-ipo",
                        "timestamp": pd.Timestamp("2026-05-12T09:00:00Z"),
                    }
                ],
            )
            _seed_news(
                news,
                dt.date(2026, 5, 14),
                [
                    {
                        "id": "n_echo",
                        "title": "SpaceX IPO valuation jumps as Musk speaks",
                        "url": "https://ft.example/spacex-echo",
                        "timestamp": pd.Timestamp("2026-05-14T14:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 12),
                [
                    {
                        "news_id": "n_root",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.92,
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 5, 14),
                [
                    {
                        "news_id": "n_echo",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.89,
                    }
                ],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="space_exploration",
                asof=dt.date(2026, 5, 14),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://reuters.example/spacex-ipo")
        self.assertEqual(cat.echo_count, 2)
        self.assertTrue(cat.is_amplified)
        self.assertEqual(cat.trigger_url, "https://ft.example/spacex-echo")

    def test_missing_primary_entities_column_not_gated(self):
        # Legacy/forward-compat: an events parquet without a primary_entities
        # column at all is NOT gated (the gate fires only on a present column
        # with too few entities), mirroring the event_type-column guard in
        # _apply_noise_and_blocklist_filters. The legit event still surfaces.
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news"
            events = Path(tmp) / "events"
            _seed_news(
                news,
                dt.date(2026, 6, 12),
                [
                    {
                        "id": "n1",
                        "title": "Some legit news",
                        "url": "https://reuters.example/legit",
                        "timestamp": pd.Timestamp("2026-06-12T10:00:00Z"),
                    }
                ],
            )
            _seed_events(
                events,
                dt.date(2026, 6, 12),
                [{"news_id": "n1", "themes": ["AI"], "confidence": 0.9}],
            )
            cat = catalyst_resolver.find_trigger_event(
                theme="AI",
                asof=dt.date(2026, 6, 12),
                events_dir=events,
                news_dir=news,
                lookback_days=30,
            )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.url, "https://reuters.example/legit")


if __name__ == "__main__":
    unittest.main()

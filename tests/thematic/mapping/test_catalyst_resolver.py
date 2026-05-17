import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from alphalens.thematic.mapping import catalyst_resolver


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
        self.assertEqual(cat["url"], "https://nvidianews.example/ising")
        self.assertIn("NVIDIA", cat["title"])
        self.assertEqual(cat["published_at"], "2026-04-14")

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
        self.assertEqual(cat["url"], "https://x/new")

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
        self.assertLessEqual(len(cat["title"]), 200)

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


if __name__ == "__main__":
    unittest.main()

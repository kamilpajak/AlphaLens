"""Load-bearing regression test for the two-tier clustering refactor.

The 2026-05-20 thematic brief disaster — 7 out of 7 ticker positions citing
the same FT.com article — required BOTH halves of the fix to hold
simultaneously:

1. Tier 1 lexical clustering at ingest must collapse same-day RSS echoes so
   the late-publishing RSS feed does not push the original Polygon source out
   of the cap-200 window.
2. Tier 2 entity-overlap arc at the resolver must walk the story back to its
   earliest root rather than picking the latest echo.

This test wires both stages end-to-end with deterministic fixtures (no LLM
mocks needed — both stages run on plain pandas given a seeded events parquet)
and asserts the catalyst URL points to the ROOT, not the LATEST ECHO.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic.mapping import catalyst_resolver
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS


def _news_row(id_, source, ts, url, title):
    return {
        "id": id_,
        "source": source,
        "timestamp": pd.Timestamp(ts, tz="UTC"),
        "tickers": [],
        "title": title,
        "body": "",
        "url": url,
        "keywords": [],
        "extra": "{}",
    }


def _news_frame(rows):
    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def _seed_events_parquet(events_dir: Path, date: dt.date, rows: list[dict]) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(events_dir / f"{date.isoformat()}.parquet", index=False)


class TestRootSourceWinsEndToEnd(unittest.TestCase):
    def test_root_event_wins_over_rss_echo_same_day(self):
        """Polygon @ 09:00 must win over an FT RSS echo @ 14:00 of the same story."""
        polygon_row = _news_row(
            "polygon_root",
            "polygon",
            "2026-05-21T09:00:00Z",
            "https://reuters.example/spacex-ipo-filing",
            "SpaceX IPO filing lands approval announcement",
        )
        rss_echo = _news_row(
            "rss_echo",
            "rss",
            "2026-05-21T14:00:00Z",
            "https://ft.example/spacex-china-less-ipo",
            "SpaceX IPO filing lands approval coverage",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir) / "news"
            events_dir = Path(tmpdir) / "events"

            # --- Stage 1: Tier 1 lexical clustering at ingest ---
            with (
                patch.object(
                    news_ingest, "_fetch_polygon", return_value=_news_frame([polygon_row])
                ),
                patch.object(
                    news_ingest, "_fetch_gdelt", return_value=news_ingest.empty_news_frame()
                ),
                patch.object(news_ingest, "_fetch_rss", return_value=_news_frame([rss_echo])),
            ):
                ingested = news_ingest.ingest_daily(
                    date=dt.date(2026, 5, 21),
                    cache_dir=news_dir,
                )

            # The two echoes collapse to a single surviving row — the earlier
            # Polygon source row, not the FT-RSS echo. cluster_size persists
            # so downstream consumers can show "+1 echo".
            self.assertEqual(len(ingested), 1)
            survivor = ingested.iloc[0]
            self.assertEqual(survivor["source"], "polygon")
            self.assertEqual(survivor["url"], polygon_row["url"])
            self.assertEqual(survivor["id"], "polygon_root")

            # --- Stage 2: Tier 2 story-arc resolver ---
            # In the real pipeline, event_extractor extracts events from each
            # surviving news row. Here we seed the events parquet directly.
            _seed_events_parquet(
                events_dir,
                dt.date(2026, 5, 21),
                [
                    {
                        "news_id": "polygon_root",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.92,
                    }
                ],
            )

            cat = catalyst_resolver.find_trigger_event(
                theme="space_exploration",
                asof=dt.date(2026, 5, 21),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=30,
            )

        self.assertIsNotNone(cat)
        # The catalyst URL points to the ROOT, not the FT echo.
        self.assertEqual(cat["url"], polygon_row["url"])
        self.assertNotEqual(cat["url"], rss_echo["url"])

    def test_root_event_wins_via_tier2_when_tier1_misses_across_days(self):
        """Tier 2 catches cross-day arcs that Tier 1 (day-scoped) cannot.

        Tier 1 only collapses same-day echoes. A story that breaks on Day 1
        via Polygon and gets re-cited on Day 3 via FT-RSS must still trace
        back to the Polygon root — that's Tier 2 entity-overlap's job.
        """
        polygon_day1 = _news_row(
            "polygon_day1",
            "polygon",
            "2026-05-19T09:00:00Z",
            "https://reuters.example/spacex-day1",
            "SpaceX IPO filing approval anchor",
        )
        # Different headline on Day 3 — Tier 1 cannot cluster cross-day.
        ft_day3 = _news_row(
            "ft_day3",
            "rss",
            "2026-05-21T14:00:00Z",
            "https://ft.example/spacex-china-less",
            "Asteroid mining valuation Musk vision moonshot",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir) / "news"
            events_dir = Path(tmpdir) / "events"

            # Day 1 ingest: only Polygon publishes
            with (
                patch.object(
                    news_ingest, "_fetch_polygon", return_value=_news_frame([polygon_day1])
                ),
                patch.object(
                    news_ingest, "_fetch_gdelt", return_value=news_ingest.empty_news_frame()
                ),
                patch.object(
                    news_ingest, "_fetch_rss", return_value=news_ingest.empty_news_frame()
                ),
            ):
                news_ingest.ingest_daily(date=dt.date(2026, 5, 19), cache_dir=news_dir)

            # Day 3 ingest: only FT publishes (different headline; Tier 1 untouched)
            with (
                patch.object(
                    news_ingest, "_fetch_polygon", return_value=news_ingest.empty_news_frame()
                ),
                patch.object(
                    news_ingest, "_fetch_gdelt", return_value=news_ingest.empty_news_frame()
                ),
                patch.object(news_ingest, "_fetch_rss", return_value=_news_frame([ft_day3])),
            ):
                news_ingest.ingest_daily(date=dt.date(2026, 5, 21), cache_dir=news_dir)

            # Seed events sharing primary_entities so Tier 2 catches the arc.
            _seed_events_parquet(
                events_dir,
                dt.date(2026, 5, 19),
                [
                    {
                        "news_id": "polygon_day1",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.91,
                    }
                ],
            )
            _seed_events_parquet(
                events_dir,
                dt.date(2026, 5, 21),
                [
                    {
                        "news_id": "ft_day3",
                        "themes": ["space_exploration"],
                        "primary_entities": ["SPACEX", "MUSK"],
                        "confidence": 0.85,
                    }
                ],
            )

            cat = catalyst_resolver.find_trigger_event(
                theme="space_exploration",
                asof=dt.date(2026, 5, 21),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=30,
            )

        self.assertIsNotNone(cat)
        # Catalyst is the Day-1 Polygon root, not the Day-3 FT echo.
        self.assertEqual(cat["url"], polygon_day1["url"])
        self.assertEqual(cat["echo_count"], 2)
        self.assertTrue(cat["is_amplified"])
        # Trigger reflects "what activated today's brief" = Day-3 FT echo.
        self.assertEqual(cat["trigger_url"], ft_day3["url"])


if __name__ == "__main__":
    unittest.main()

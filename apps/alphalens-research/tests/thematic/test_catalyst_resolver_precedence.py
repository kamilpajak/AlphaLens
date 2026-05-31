"""Catalyst-resolver precedence rule (PR-2): template > flash for same (ticker, event_type, 24h).

Per design memo §1.1, the hybrid pipeline can legitimately produce both a
template event AND a Flash event for the same (primary_entity_ticker,
event_type) within a 24-hour window when two different articles report
the same underlying corporate action. The resolver dedup pass enforces:

- template event wins
- Flash event is dropped to holdout (telemetry only — not deleted from
  the source parquet, just filtered from the resolver's working set)
- ``HOLDOUT_SUPERSEDED_BY_TEMPLATE`` counter ticks per dropped Flash row
- after dedup, the existing theme-match + entity-arc logic runs unchanged
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.extraction.templates.holdout import (
    HOLDOUT_SUPERSEDED_BY_TEMPLATE,
    TemplateMetrics,
)
from alphalens_pipeline.thematic.mapping import catalyst_resolver


def _events_row(
    news_id: str,
    event_type: str,
    primary_entities: list[str],
    themes: list[str],
    extraction_method: str,
    template_id: str | None,
    confidence: float = 0.9,
) -> dict:
    return {
        "news_id": news_id,
        "event_type": event_type,
        "primary_entities": primary_entities,
        "themes": themes,
        "sentiment": "positive",
        "second_order_implications": [],
        "confidence": confidence,
        "model": "deepseek/deepseek-v4-flash",
        "extracted_at": pd.Timestamp("2026-05-30T10:00:00Z"),
        "extraction_method": extraction_method,
        "template_id": template_id,
    }


def _news_row(
    news_id: str,
    timestamp: str,
    title: str,
    url: str,
    tickers: list[str],
) -> dict:
    return {
        "id": news_id,
        "source": "businesswire" if "businesswire" in url else "polygon",
        "timestamp": pd.Timestamp(timestamp),
        "tickers": tickers,
        "title": title,
        "body": "",
        "url": url,
        "keywords": [],
        "extra": "{}",
    }


class TestPrecedenceRule(unittest.TestCase):
    def setUp(self):
        # Reset the cache so each test sees only its own parquet windows.
        catalyst_resolver._load_window.cache_clear()
        catalyst_resolver._load_url_blocklist_patterns.cache_clear()

    def test_template_wins_over_flash_in_24h_window(self):
        # Same (NVDA, m_and_a) reported by two outlets within 4 hours:
        # one resolved by template, the other by flash. Template wins.
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semiconductors"],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                ),
                _events_row(
                    "p:2",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semiconductors"],
                    extraction_method="flash",
                    template_id=None,
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1",
                    "2026-05-30T08:00:00Z",
                    "NVDA acquires XYZ",
                    "https://www.businesswire.com/x",
                    ["NVDA"],
                ),
                _news_row(
                    "p:2",
                    "2026-05-30T12:00:00Z",
                    "NVIDIA snaps up XYZ",
                    "https://polygon.io/x",
                    ["NVDA"],
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            news_dir = Path(tmp) / "news"
            events_dir.mkdir()
            news_dir.mkdir()
            events.to_parquet(events_dir / "2026-05-30.parquet", index=False)
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            metrics = TemplateMetrics()
            result = catalyst_resolver.find_trigger_event(
                theme="semiconductors",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
                metrics=metrics,
            )

        self.assertIsNotNone(result)
        assert result is not None
        # The catalyst URL is the businesswire one — that row was the
        # template-extracted winner. The polygon row was superseded.
        self.assertIn("businesswire.com", result["url"])
        # Holdout counter ticked once for the superseded flash event.
        snap = metrics.snapshot()
        self.assertEqual(snap["holdout"].get(HOLDOUT_SUPERSEDED_BY_TEMPLATE), 1)

    def test_different_event_types_do_not_collide(self):
        # Same ticker, two different event_types — both events kept.
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semis"],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                ),
                _events_row(
                    "p:2",
                    event_type="earnings",
                    primary_entities=["NVDA"],
                    themes=["semis"],
                    extraction_method="flash",
                    template_id=None,
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1",
                    "2026-05-30T08:00:00Z",
                    "NVDA acquires X",
                    "https://www.businesswire.com/x",
                    ["NVDA"],
                ),
                _news_row(
                    "p:2",
                    "2026-05-30T12:00:00Z",
                    "NVDA earnings beat",
                    "https://polygon.io/x",
                    ["NVDA"],
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            news_dir = Path(tmp) / "news"
            events_dir.mkdir()
            news_dir.mkdir()
            events.to_parquet(events_dir / "2026-05-30.parquet", index=False)
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            metrics = TemplateMetrics()
            result = catalyst_resolver.find_trigger_event(
                theme="semis",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
                metrics=metrics,
            )

        self.assertIsNotNone(result)
        # No supersession because (NVDA, m_and_a) and (NVDA, earnings) are
        # different keys.
        snap = metrics.snapshot()
        self.assertEqual(snap["holdout"].get(HOLDOUT_SUPERSEDED_BY_TEMPLATE, 0), 0)

    def test_supersession_window_does_not_extend_beyond_24h(self):
        # Two events same (ticker, event_type) but the flash row is 30h
        # AFTER the template row — outside the 24h supersession window.
        # Both events are kept.
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semis"],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                ),
                _events_row(
                    "p:2",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semis"],
                    extraction_method="flash",
                    template_id=None,
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1",
                    "2026-05-29T08:00:00Z",
                    "NVDA acquires X",
                    "https://www.businesswire.com/x",
                    ["NVDA"],
                ),
                _news_row(
                    "p:2",
                    "2026-05-30T14:00:00Z",  # 30h later
                    "Different NVDA acquisition",
                    "https://polygon.io/y",
                    ["NVDA"],
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            news_dir = Path(tmp) / "news"
            events_dir.mkdir()
            news_dir.mkdir()
            events.to_parquet(events_dir / "2026-05-30.parquet", index=False)
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            metrics = TemplateMetrics()
            catalyst_resolver.find_trigger_event(
                theme="semis",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=2,
                metrics=metrics,
            )

        snap = metrics.snapshot()
        # No supersession because the two events are 30h apart — they
        # represent separate corporate actions, not duplicate reporting.
        self.assertEqual(snap["holdout"].get(HOLDOUT_SUPERSEDED_BY_TEMPLATE, 0), 0)

    def test_two_flash_events_same_window_no_supersession(self):
        # Two flash events same (ticker, event_type, 24h) — neither is
        # a template, so the precedence rule has nothing to do. Existing
        # latest-event behaviour wins. No holdout counter tick.
        events = pd.DataFrame(
            [
                _events_row(
                    "p:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semis"],
                    extraction_method="flash",
                    template_id=None,
                ),
                _events_row(
                    "p:2",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semis"],
                    extraction_method="flash",
                    template_id=None,
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "p:1",
                    "2026-05-30T08:00:00Z",
                    "NVDA deal",
                    "https://polygon.io/1",
                    ["NVDA"],
                ),
                _news_row(
                    "p:2",
                    "2026-05-30T12:00:00Z",
                    "NVDA snaps up rival",
                    "https://polygon.io/2",
                    ["NVDA"],
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            news_dir = Path(tmp) / "news"
            events_dir.mkdir()
            news_dir.mkdir()
            events.to_parquet(events_dir / "2026-05-30.parquet", index=False)
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            metrics = TemplateMetrics()
            catalyst_resolver.find_trigger_event(
                theme="semis",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
                metrics=metrics,
            )

        snap = metrics.snapshot()
        self.assertEqual(snap["holdout"].get(HOLDOUT_SUPERSEDED_BY_TEMPLATE, 0), 0)

    def test_legacy_events_without_columns_default_to_flash(self):
        # Pre-PR-2 parquets without extraction_method / template_id columns
        # must be treated as flash so the resolver doesn't crash on
        # backfill. The dedup pass becomes a no-op for these rows.
        events = pd.DataFrame(
            [
                {
                    "news_id": "legacy:1",
                    "event_type": "m_and_a",
                    "primary_entities": ["NVDA"],
                    "themes": ["semis"],
                    "sentiment": "positive",
                    "second_order_implications": [],
                    "confidence": 0.8,
                    "model": "deepseek/deepseek-v4-flash",
                    "extracted_at": pd.Timestamp("2026-05-30T10:00:00Z"),
                }
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "legacy:1",
                    "2026-05-30T08:00:00Z",
                    "NVDA deal",
                    "https://polygon.io/1",
                    ["NVDA"],
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            news_dir = Path(tmp) / "news"
            events_dir.mkdir()
            news_dir.mkdir()
            events.to_parquet(events_dir / "2026-05-30.parquet", index=False)
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            metrics = TemplateMetrics()
            result = catalyst_resolver.find_trigger_event(
                theme="semis",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
                metrics=metrics,
            )

        self.assertIsNotNone(result)
        snap = metrics.snapshot()
        self.assertEqual(snap["holdout"].get(HOLDOUT_SUPERSEDED_BY_TEMPLATE, 0), 0)


class TestMetricsOptional(unittest.TestCase):
    """Resolver must work without a metrics argument (backward compat)."""

    def setUp(self):
        catalyst_resolver._load_window.cache_clear()
        catalyst_resolver._load_url_blocklist_patterns.cache_clear()

    def test_no_metrics_argument_does_not_crash(self):
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semis"],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                ),
                _events_row(
                    "p:2",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semis"],
                    extraction_method="flash",
                    template_id=None,
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1",
                    "2026-05-30T08:00:00Z",
                    "NVDA acquires X",
                    "https://www.businesswire.com/x",
                    ["NVDA"],
                ),
                _news_row(
                    "p:2",
                    "2026-05-30T12:00:00Z",
                    "NVIDIA snaps up X",
                    "https://polygon.io/x",
                    ["NVDA"],
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            news_dir = Path(tmp) / "news"
            events_dir.mkdir()
            news_dir.mkdir()
            events.to_parquet(events_dir / "2026-05-30.parquet", index=False)
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            # Note: no metrics= kwarg — existing callsites stay unchanged.
            result = catalyst_resolver.find_trigger_event(
                theme="semis",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
            )

        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()

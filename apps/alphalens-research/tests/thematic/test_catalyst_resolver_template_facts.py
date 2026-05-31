"""PR-3 — catalyst_resolver surfaces ``template_id`` + ``template_facts``.

When the resolved catalyst event is a template-extracted row, the
returned payload carries:

- ``template_id``: the source template (``m_and_a_press_release``,
  ``earnings_surprise``, …)
- ``template_facts``: the deserialised typed-fields dict
  (PR-3 ``template_fields_json`` column in the events parquet)

Flash-extracted catalysts surface ``template_id = None`` + ``template_facts
= None`` so the orchestrator's ``_row_to_facts`` projection has a
predictable shape on both paths.

Pre-PR-3 parquets (missing column) are handled by event_extractor's
backfill — the resolver simply forwards whatever it reads, defaulting to
None on absence.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver


def _events_row(
    news_id: str,
    event_type: str,
    primary_entities: list[str],
    themes: list[str],
    extraction_method: str,
    template_id: str | None,
    template_fields_json: str | None = None,
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
        "template_fields_json": template_fields_json,
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


class TestTemplateFactsForwarding(unittest.TestCase):
    def setUp(self):
        catalyst_resolver._load_window.cache_clear()
        catalyst_resolver._load_url_blocklist_patterns.cache_clear()

    def test_template_catalyst_payload_carries_typed_facts(self):
        # A single template-extracted M&A event in the window; the
        # resolved payload must surface template_id + template_facts.
        fields = {
            "acquirer_ticker": "NVDA",
            "target_ticker": "XYZ",
            "consideration_usd": 5_000_000_000,
            "announcement_date": "2026-05-30",
        }
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semiconductors"],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                    template_fields_json=json.dumps(fields),
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
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            news_dir = Path(tmp) / "news"
            events_dir.mkdir()
            news_dir.mkdir()
            events.to_parquet(events_dir / "2026-05-30.parquet", index=False)
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            result = catalyst_resolver.find_trigger_event(
                theme="semiconductors",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["template_id"], "m_and_a_press_release")
        self.assertEqual(result["template_facts"], fields)

    def test_flash_catalyst_payload_template_fields_are_none(self):
        events = pd.DataFrame(
            [
                _events_row(
                    "p:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semiconductors"],
                    extraction_method="flash",
                    template_id=None,
                    template_fields_json=None,
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "p:1",
                    "2026-05-30T08:00:00Z",
                    "NVDA snaps up XYZ",
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

            result = catalyst_resolver.find_trigger_event(
                theme="semiconductors",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNone(result["template_id"])
        self.assertIsNone(result["template_facts"])

    def test_template_fields_malformed_json_degrades_to_none(self):
        # A corrupt template_fields_json must not crash the resolver —
        # template_id still surfaces (the bare id is intact), but
        # template_facts collapses to None so the brief generator's
        # absent-block branch fires.
        events = pd.DataFrame(
            [
                _events_row(
                    "bw:1",
                    event_type="m_and_a",
                    primary_entities=["NVDA"],
                    themes=["semiconductors"],
                    extraction_method="template",
                    template_id="m_and_a_press_release",
                    template_fields_json="{not valid json",
                ),
            ]
        )
        news = pd.DataFrame(
            [
                _news_row(
                    "bw:1",
                    "2026-05-30T08:00:00Z",
                    "x",
                    "https://www.businesswire.com/x",
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

            result = catalyst_resolver.find_trigger_event(
                theme="semiconductors",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["template_id"], "m_and_a_press_release")
        self.assertIsNone(result["template_facts"])

    def test_pre_pr3_events_parquet_missing_template_fields_column(self):
        # Backwards-compat: legacy parquets lack template_fields_json
        # entirely. The resolver must surface template_facts=None rather
        # than crashing on KeyError / column lookup.
        legacy_row = {
            "news_id": "p:1",
            "event_type": "m_and_a",
            "primary_entities": ["NVDA"],
            "themes": ["semiconductors"],
            "sentiment": "positive",
            "second_order_implications": [],
            "confidence": 0.9,
            "model": "deepseek/deepseek-v4-flash",
            "extracted_at": pd.Timestamp("2026-05-30T10:00:00Z"),
            "extraction_method": "template",
            "template_id": "m_and_a_press_release",
            # NO template_fields_json column.
        }
        events = pd.DataFrame([legacy_row])
        news = pd.DataFrame(
            [
                _news_row(
                    "p:1",
                    "2026-05-30T08:00:00Z",
                    "NVDA acquires XYZ",
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

            result = catalyst_resolver.find_trigger_event(
                theme="semiconductors",
                asof=dt.date(2026, 5, 30),
                events_dir=events_dir,
                news_dir=news_dir,
                lookback_days=1,
            )

        self.assertIsNotNone(result)
        assert result is not None
        # template_id still surfaces from the row that had it.
        self.assertEqual(result["template_id"], "m_and_a_press_release")
        self.assertIsNone(result["template_facts"])


if __name__ == "__main__":
    unittest.main()

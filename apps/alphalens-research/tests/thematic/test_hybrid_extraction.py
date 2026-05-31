"""Hybrid extraction path (PR-2): TemplateEngine first, Flash fallback on no-match.

Pins the contract that ``event_extractor.extract_one`` / ``.extract_daily``:
- consults the template library FIRST (deterministic, free, replayable)
- only invokes the DeepSeek Flash LLM when no template matches
- writes ``extraction_method ∈ {template, flash}`` + ``template_id`` (nullable)
  to the events parquet so PR-3 brief generation + PR-4 dedup can branch on
  the source without re-classifying
- gracefully handles legacy cached events (pre-PR-2 parquets without the
  new columns) by backfilling defaults
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.extraction import event_extractor
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS


def _news_row(
    news_id: str,
    source: str,
    title: str,
    body: str = "",
    tickers: list[str] | None = None,
    url: str | None = None,
):
    return {
        "id": news_id,
        "source": source,
        "timestamp": pd.Timestamp("2026-05-30T10:00:00Z"),
        "tickers": tickers or [],
        "title": title,
        "body": body,
        "url": url or f"https://example.com/{news_id}",
        "keywords": [],
        "extra": "{}",
    }


def _news_frame(rows):
    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


SAMPLE_FLASH_EXTRACTION = {
    "event_type": "product_launch",
    "primary_entities": ["NVDA"],
    "themes": ["quantum_computing"],
    "sentiment": "positive",
    "second_order_implications": ["QUBT may benefit"],
    "confidence": 0.85,
}


class TestExtractOneTemplatePath(unittest.TestCase):
    def test_template_match_skips_llm(self):
        # A press-release-style M&A article should match the
        # m_and_a_press_release ship template without calling Flash.
        row = _news_row(
            "bw:1",
            "businesswire",
            "NVDA announces $5 billion acquisition of XYZ",
            body="NVIDIA today announced a $5 billion all-cash acquisition of XYZ Corp.",
            tickers=["NVDA", "XYZ"],
            url="https://www.businesswire.com/news/home/x",
        )

        # _call_llm patched to raise — proves the Flash path is NOT taken.
        with patch.object(
            event_extractor, "_call_llm", side_effect=AssertionError("Flash was called")
        ):
            event = event_extractor.extract_one(row, api_key="testkey")

        self.assertIsNotNone(event)
        assert event is not None  # type narrow
        self.assertEqual(event["extraction_method"], "template")
        self.assertEqual(event["template_id"], "m_and_a_press_release")
        self.assertEqual(event["event_type"], "m_and_a")
        # Confidence on template path is 1.0 — match is deterministic.
        self.assertEqual(event["confidence"], 1.0)
        # Primary entities surface from the resolved entity set (feed-tagged).
        self.assertIn("NVDA", event["primary_entities"])
        self.assertIn("XYZ", event["primary_entities"])


class TestExtractOneFlashFallback(unittest.TestCase):
    def test_no_template_match_falls_back_to_flash(self):
        # A neutral feature article on AI tooling — no template matches
        # (lacks press_release source, no amount, doesn't fit the 5 ship
        # event_types). Should fall through to the LLM path.
        row = _news_row(
            "p:2",
            "polygon",
            "How AI is changing data engineering teams",
            body="Long-form feature article on team workflows.",
            tickers=["NVDA"],
        )
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_FLASH_EXTRACTION))

        with patch.object(event_extractor, "_call_llm", return_value=fake_response):
            event = event_extractor.extract_one(row, api_key="testkey")

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["extraction_method"], "flash")
        self.assertIsNone(event["template_id"])
        # Flash path preserves LLM-extracted enrichment.
        self.assertEqual(event["event_type"], "product_launch")
        self.assertEqual(event["themes"], ["quantum_computing"])
        self.assertEqual(event["sentiment"], "positive")

    def test_flash_error_still_returns_none(self):
        row = _news_row("p:3", "polygon", "Some opaque headline", tickers=["NVDA"])
        with patch.object(event_extractor, "_call_llm", side_effect=RuntimeError("503")):
            event = event_extractor.extract_one(row, api_key="testkey")
        self.assertIsNone(event)


class TestExtractDailySchema(unittest.TestCase):
    def test_parquet_carries_extraction_method_and_template_id(self):
        news = _news_frame(
            [
                _news_row(
                    "bw:1",
                    "businesswire",
                    "NVDA announces $5 billion acquisition of XYZ",
                    body="A $5 billion all-cash acquisition.",
                    tickers=["NVDA", "XYZ"],
                    url="https://www.businesswire.com/news/home/x",
                ),
                _news_row(
                    "p:2",
                    "polygon",
                    "Tech sector overview",
                    body="Feature article",
                    tickers=["NVDA"],
                ),
            ]
        )
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_FLASH_EXTRACTION))

        with tempfile.TemporaryDirectory() as tmp:
            news_dir = Path(tmp) / "news"
            events_dir = Path(tmp) / "events"
            news_dir.mkdir()
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            with patch.object(event_extractor, "_call_llm", return_value=fake_response):
                df = event_extractor.extract_daily(
                    date=dt.date(2026, 5, 30),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )

        self.assertEqual(len(df), 2)
        self.assertIn("extraction_method", df.columns)
        self.assertIn("template_id", df.columns)
        methods = dict(zip(df["news_id"], df["extraction_method"], strict=False))
        self.assertEqual(methods["bw:1"], "template")
        self.assertEqual(methods["p:2"], "flash")
        templates = dict(zip(df["news_id"], df["template_id"], strict=False))
        self.assertEqual(templates["bw:1"], "m_and_a_press_release")
        # Flash path leaves template_id as None / NaN (pandas serializes both).
        self.assertTrue(
            templates["p:2"] is None or pd.isna(templates["p:2"]),
            f"flash row should have null template_id, got {templates['p:2']!r}",
        )


class TestExtractDailyLegacyCacheBackfill(unittest.TestCase):
    def test_legacy_parquet_missing_new_columns_is_backfilled(self):
        # A parquet written by a pre-PR-2 extract_daily has no
        # extraction_method / template_id columns. Reading it must NOT
        # crash + the resulting frame must carry the new columns with
        # safe defaults so downstream consumers (catalyst_resolver,
        # PR-3 brief generator) can always rely on them.
        news = _news_frame([_news_row("p:1", "polygon", "Some news", tickers=["NVDA"])])
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_FLASH_EXTRACTION))

        with tempfile.TemporaryDirectory() as tmp:
            news_dir = Path(tmp) / "news"
            events_dir = Path(tmp) / "events"
            news_dir.mkdir()
            events_dir.mkdir()
            news.to_parquet(news_dir / "2026-05-30.parquet", index=False)

            # Pre-seed a legacy events parquet (pre-PR-2 shape).
            legacy = pd.DataFrame(
                [
                    {
                        "news_id": "legacy:99",
                        "event_type": "earnings",
                        "primary_entities": ["AAPL"],
                        "themes": [],
                        "sentiment": "positive",
                        "second_order_implications": [],
                        "confidence": 0.8,
                        "model": "deepseek/deepseek-v4-flash",
                        "extracted_at": pd.Timestamp("2026-05-29T10:00:00Z"),
                    }
                ]
            )
            legacy.to_parquet(events_dir / "2026-05-30.parquet", index=False)

            with patch.object(event_extractor, "_call_llm", return_value=fake_response):
                df = event_extractor.extract_daily(
                    date=dt.date(2026, 5, 30),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )

        # 2 rows: legacy (backfilled) + new flash extract.
        self.assertEqual(len(df), 2)
        self.assertIn("extraction_method", df.columns)
        self.assertIn("template_id", df.columns)
        legacy_row = df[df["news_id"] == "legacy:99"].iloc[0]
        # Legacy rows default to flash (the only path that existed pre-PR-2).
        self.assertEqual(legacy_row["extraction_method"], "flash")
        self.assertTrue(
            legacy_row["template_id"] is None or pd.isna(legacy_row["template_id"]),
        )


if __name__ == "__main__":
    unittest.main()

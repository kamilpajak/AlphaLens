"""PR-3 — event_extractor persists ``template_fields_json`` on the parquet.

PR-2 added ``extraction_method`` + ``template_id`` columns but DROPPED the
typed ``TemplateEvent.fields`` payload (they were never projected into
the dict path). PR-3 needs the fields to flow to the brief generator, so
the event extractor now stamps a JSON-serialised ``template_fields_json``
column on every template-extracted row (None on the Flash fallback path).

Legacy parquets (no column) are backfilled to ``None`` on read so the
catalyst-resolver + brief-generator can rely on the column without a
one-shot migration script.
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
        "timestamp": pd.Timestamp("2026-05-31T10:00:00Z"),
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


class TestExtractOneTemplateFieldsInDict(unittest.TestCase):
    def test_template_match_emits_template_fields_json(self):
        # Same business-wire M&A row PR-2 used; we now assert that the
        # returned dict carries the typed fields as a JSON string under
        # ``template_fields_json``. Flash path is patched to assert it
        # is NOT taken (preserves PR-2's contract).
        row = _news_row(
            "bw:1",
            "businesswire",
            "NVDA announces $5 billion acquisition of XYZ",
            body="NVIDIA today announced a $5 billion all-cash acquisition of XYZ Corp.",
            tickers=["NVDA", "XYZ"],
            url="https://www.businesswire.com/news/home/x",
        )
        with patch.object(
            event_extractor, "_call_llm", side_effect=AssertionError("Flash was called")
        ):
            event = event_extractor.extract_one(row, api_key="testkey")

        self.assertIsNotNone(event)
        assert event is not None  # type narrow
        # Column is a JSON STRING (pandas list/dict columns are fragile
        # across parquet round-trips and Django serializer paths; JSON is
        # the canonical interop boundary, mirrors brief_trade_setup).
        self.assertIn("template_fields_json", event)
        raw = event["template_fields_json"]
        self.assertIsInstance(raw, str)
        fields = json.loads(raw)
        # The ship m_and_a_press_release template extracts at minimum
        # the acquirer + target tickers from the resolved-entity set; the
        # exact extraction shape is the engine's contract (not asserted
        # here) but the JSON must be a non-empty dict.
        self.assertIsInstance(fields, dict)
        self.assertGreater(len(fields), 0)

    def test_flash_path_leaves_template_fields_json_none(self):
        row = _news_row(
            "p:2",
            "polygon",
            "How AI is changing data engineering teams",
            body="Long-form feature article.",
            tickers=["NVDA"],
        )
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_FLASH_EXTRACTION))
        with patch.object(event_extractor, "_call_llm", return_value=fake_response):
            event = event_extractor.extract_one(row, api_key="testkey")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIsNone(event["template_fields_json"])


class TestExtractDailyPersistsTemplateFieldsColumn(unittest.TestCase):
    def test_parquet_carries_template_fields_json(self):
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
            news.to_parquet(news_dir / "2026-05-31.parquet", index=False)
            with patch.object(event_extractor, "_call_llm", return_value=fake_response):
                df = event_extractor.extract_daily(
                    date=dt.date(2026, 5, 31),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )
        self.assertIn("template_fields_json", df.columns)
        per_id = dict(zip(df["news_id"], df["template_fields_json"], strict=False))
        # Template path → non-null JSON string
        self.assertIsInstance(per_id["bw:1"], str)
        self.assertGreater(len(json.loads(per_id["bw:1"])), 0)
        # Flash path → null (None or pd.NA)
        self.assertTrue(per_id["p:2"] is None or pd.isna(per_id["p:2"]))

    def test_legacy_parquet_missing_template_fields_is_backfilled(self):
        # Pre-PR-3 cache lacks the new column; reading it must surface
        # template_fields_json=None for every row so downstream is safe.
        news = _news_frame([_news_row("p:1", "polygon", "Some news", tickers=["NVDA"])])
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_FLASH_EXTRACTION))
        with tempfile.TemporaryDirectory() as tmp:
            news_dir = Path(tmp) / "news"
            events_dir = Path(tmp) / "events"
            news_dir.mkdir()
            events_dir.mkdir()
            news.to_parquet(news_dir / "2026-05-31.parquet", index=False)
            # Legacy events parquet — PR-2 shape, no template_fields_json.
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
                        "extraction_method": "flash",
                        "template_id": None,
                        "model": "deepseek/deepseek-v4-flash",
                        "extracted_at": pd.Timestamp("2026-05-30T10:00:00Z"),
                    }
                ]
            )
            legacy.to_parquet(events_dir / "2026-05-31.parquet", index=False)
            with patch.object(event_extractor, "_call_llm", return_value=fake_response):
                df = event_extractor.extract_daily(
                    date=dt.date(2026, 5, 31),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )
        self.assertIn("template_fields_json", df.columns)
        legacy_row = df[df["news_id"] == "legacy:99"].iloc[0]
        self.assertTrue(
            legacy_row["template_fields_json"] is None
            or pd.isna(legacy_row["template_fields_json"]),
        )


if __name__ == "__main__":
    unittest.main()

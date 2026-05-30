import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.extraction import event_extractor as gemini_flash
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS


def _news_row(news_id: str, source: str, title: str, body: str = "", tickers=None):
    return {
        "id": news_id,
        "source": source,
        "timestamp": pd.Timestamp("2026-05-15T10:00:00Z"),
        "tickers": tickers or [],
        "title": title,
        "body": body,
        "url": f"https://example.com/{news_id}",
        "keywords": [],
        "extra": "{}",
    }


def _news_frame(rows):
    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


SAMPLE_EXTRACTION = {
    "event_type": "product_launch",
    "primary_entities": ["NVDA"],
    "themes": ["quantum_computing", "AI_quantum_hybrid"],
    "sentiment": "positive",
    "second_order_implications": ["QUBT may benefit"],
    "confidence": 0.85,
}


class TestExtractionPromptBuilding(unittest.TestCase):
    def test_prompt_includes_title_body_tickers_source(self):
        row = _news_row("p1", "polygon", "NVDA CUDA-Q launch", "Body text...", ["NVDA"])
        prompt = gemini_flash.build_prompt(row)
        self.assertIn("NVDA CUDA-Q launch", prompt)
        self.assertIn("Body text", prompt)
        self.assertIn("NVDA", prompt)
        self.assertIn("polygon", prompt)


class TestExtractOne(unittest.TestCase):
    def test_extract_one_returns_normalized_dict(self):
        row = _news_row("p1", "polygon", "NVDA CUDA-Q launch", tickers=["NVDA"])
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_EXTRACTION))
        with patch.object(gemini_flash, "_call_llm", return_value=fake_response):
            extracted = gemini_flash.extract_one(row, api_key="testkey")
        self.assertEqual(extracted["event_type"], "product_launch")
        self.assertEqual(extracted["confidence"], 0.85)
        self.assertEqual(extracted["sentiment"], "positive")

    def test_extract_one_returns_none_on_api_error(self):
        row = _news_row("p1", "polygon", "anything")
        with patch.object(gemini_flash, "_call_llm", side_effect=RuntimeError("boom")):
            extracted = gemini_flash.extract_one(row, api_key="testkey")
        self.assertIsNone(extracted)

    def test_extract_one_returns_none_on_unparseable_response(self):
        row = _news_row("p1", "polygon", "anything")
        fake_response = SimpleNamespace(text="not json")
        with patch.object(gemini_flash, "_call_llm", return_value=fake_response):
            extracted = gemini_flash.extract_one(row, api_key="testkey")
        self.assertIsNone(extracted)


class TestExtractDaily(unittest.TestCase):
    def test_extract_daily_writes_parquet_with_news_id_join_key(self):
        news = _news_frame(
            [
                _news_row("a1", "polygon", "NVDA CUDA-Q", tickers=["NVDA"]),
                _news_row("a2", "rss", "Apple earnings beat"),
            ]
        )
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_EXTRACTION))

        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir) / "news"
            events_dir = Path(tmpdir) / "events"
            news_dir.mkdir()
            news.to_parquet(news_dir / "2026-05-15.parquet", index=False)

            with patch.object(gemini_flash, "_call_llm", return_value=fake_response):
                df = gemini_flash.extract_daily(
                    date=dt.date(2026, 5, 15),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )

            self.assertEqual(len(df), 2)
            self.assertIn("news_id", df.columns)
            self.assertEqual(set(df["news_id"]), {"a1", "a2"})
            self.assertTrue((events_dir / "2026-05-15.parquet").exists())

    def test_extract_daily_is_idempotent_per_news_id(self):
        news = _news_frame([_news_row("a1", "polygon", "First"), _news_row("a2", "rss", "Second")])
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_EXTRACTION))

        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir) / "news"
            events_dir = Path(tmpdir) / "events"
            news_dir.mkdir()
            news.to_parquet(news_dir / "2026-05-15.parquet", index=False)

            calls = {"n": 0}

            def counting_call(*args, **kwargs):
                calls["n"] += 1
                return fake_response

            with patch.object(gemini_flash, "_call_llm", side_effect=counting_call):
                gemini_flash.extract_daily(
                    date=dt.date(2026, 5, 15),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )
                first_calls = calls["n"]
                self.assertEqual(first_calls, 2)

                # Second invocation: cache hit, no new Gemini calls
                gemini_flash.extract_daily(
                    date=dt.date(2026, 5, 15),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )
                self.assertEqual(calls["n"], first_calls)

    def test_extract_daily_skips_already_cached_news_ids_on_incremental(self):
        news = _news_frame(
            [_news_row("a1", "polygon", "Already extracted"), _news_row("a2", "rss", "New item")]
        )
        fake_response = SimpleNamespace(text=json.dumps(SAMPLE_EXTRACTION))

        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir) / "news"
            events_dir = Path(tmpdir) / "events"
            news_dir.mkdir()
            events_dir.mkdir()
            news.to_parquet(news_dir / "2026-05-15.parquet", index=False)

            # Pre-seed cache with a1 already extracted
            pre_cached = pd.DataFrame(
                [
                    {
                        "news_id": "a1",
                        "event_type": "earnings",
                        "primary_entities": ["AAPL"],
                        "themes": ["semiconductors"],
                        "sentiment": "neutral",
                        "second_order_implications": [],
                        "confidence": 0.5,
                        "model": "gemini-2.5-flash",
                        "extracted_at": pd.Timestamp.now(tz="UTC"),
                    }
                ]
            )
            pre_cached.to_parquet(events_dir / "2026-05-15.parquet", index=False)

            calls = {"n": 0}

            def counting_call(*args, **kwargs):
                calls["n"] += 1
                return fake_response

            with patch.object(gemini_flash, "_call_llm", side_effect=counting_call):
                df = gemini_flash.extract_daily(
                    date=dt.date(2026, 5, 15),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )

            self.assertEqual(calls["n"], 1)  # only a2 extracted
            self.assertEqual(len(df), 2)  # both a1 (cached) and a2 (new)
            self.assertEqual(set(df["news_id"]), {"a1", "a2"})

    def test_extract_daily_logs_and_skips_per_item_failure(self):
        news = _news_frame(
            [_news_row("a1", "polygon", "Good"), _news_row("a2", "rss", "Will fail")]
        )

        def selective_fail(*args, **kwargs):
            prompt = args[1] if len(args) > 1 else kwargs.get("prompt", "")
            if "Will fail" in prompt:
                raise RuntimeError("rate limit")
            return SimpleNamespace(text=json.dumps(SAMPLE_EXTRACTION))

        with tempfile.TemporaryDirectory() as tmpdir:
            news_dir = Path(tmpdir) / "news"
            events_dir = Path(tmpdir) / "events"
            news_dir.mkdir()
            news.to_parquet(news_dir / "2026-05-15.parquet", index=False)

            with patch.object(gemini_flash, "_call_llm", side_effect=selective_fail):
                df = gemini_flash.extract_daily(
                    date=dt.date(2026, 5, 15),
                    news_dir=news_dir,
                    events_dir=events_dir,
                    api_key="testkey",
                )

            self.assertEqual(len(df), 1)  # a1 succeeded, a2 dropped
            self.assertEqual(df.iloc[0]["news_id"], "a1")


if __name__ == "__main__":
    unittest.main()

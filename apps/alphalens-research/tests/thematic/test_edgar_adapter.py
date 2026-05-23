import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphalens_pipeline.thematic.sources import edgar_adapter
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS
from alphalens_pipeline.watchdog.types import Event, FormType


def _make_event(ticker: str, accession: str, filed_at: dt.datetime, items: list[str] | None = None):
    raw = {"items": ", ".join(items)} if items else {}
    return Event(
        ticker=ticker,
        form_type=FormType.FORM_8K,
        accession_number=accession,
        filed_at=filed_at,
        url=f"https://www.sec.gov/Archives/edgar/data/0/{accession}.htm",
        raw_data=raw,
    )


SAMPLE_EVENTS = [
    _make_event(
        "NVDA",
        "0001045810-26-000001",
        dt.datetime(2026, 5, 15, 14, 0, tzinfo=dt.UTC),
        items=["2.02", "9.01"],
    ),
    _make_event(
        "AAPL",
        "0000320193-26-000005",
        dt.datetime(2026, 5, 15, 16, 30, tzinfo=dt.UTC),
        items=["8.01"],
    ),
    _make_event(
        "OLD",
        "0000000099-26-000099",
        dt.datetime(2026, 5, 10, 8, 0, tzinfo=dt.UTC),  # outside date window
    ),
]


class TestEdgarAdapterTransform(unittest.TestCase):
    def test_transforms_events_to_unified_schema(self):
        df = edgar_adapter.transform(SAMPLE_EVENTS)
        self.assertEqual(len(df), 3)
        self.assertEqual(list(df.columns), NEWS_COLUMNS)
        self.assertTrue((df["source"] == "edgar").all())

    def test_ticker_in_tickers_list(self):
        df = edgar_adapter.transform(SAMPLE_EVENTS)
        self.assertEqual(df.iloc[0]["tickers"], ["NVDA"])
        self.assertEqual(df.iloc[1]["tickers"], ["AAPL"])

    def test_8k_items_land_in_keywords(self):
        df = edgar_adapter.transform(SAMPLE_EVENTS)
        nvda_row = df.iloc[0]
        self.assertIn("2.02", nvda_row["keywords"])
        self.assertIn("9.01", nvda_row["keywords"])

    def test_accession_number_is_id(self):
        df = edgar_adapter.transform(SAMPLE_EVENTS)
        self.assertEqual(df.iloc[0]["id"], "0001045810-26-000001")

    def test_form_type_in_extra(self):
        df = edgar_adapter.transform(SAMPLE_EVENTS)
        extra = json.loads(df.iloc[0]["extra"])
        self.assertEqual(extra["form_type"], "8-K")

    def test_empty_returns_empty_frame(self):
        df = edgar_adapter.transform([])
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), NEWS_COLUMNS)


class TestEdgarAdapterFetch(unittest.TestCase):
    def test_fetch_daily_news_filters_to_date_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(edgar_adapter, "_detect_events", return_value=SAMPLE_EVENTS):
                df = edgar_adapter.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    universe=["NVDA", "AAPL"],
                    cache_dir=Path(tmpdir),
                    lookback_days=2,
                )

            # 'OLD' filing on 2026-05-10 should be dropped (outside 2-day window)
            self.assertEqual(len(df), 2)
            self.assertEqual(set(df["tickers"].apply(lambda x: x[0])), {"NVDA", "AAPL"})

    def test_fetch_daily_news_caches_to_parquet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(edgar_adapter, "_detect_events", return_value=SAMPLE_EVENTS):
                edgar_adapter.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    universe=["NVDA", "AAPL"],
                    cache_dir=Path(tmpdir),
                )
            cached = Path(tmpdir) / "2026-05-15.parquet"
            self.assertTrue(cached.exists())

    def test_fetch_daily_news_returns_cache_on_second_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(edgar_adapter, "_detect_events", return_value=SAMPLE_EVENTS):
                edgar_adapter.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    universe=["NVDA"],
                    cache_dir=Path(tmpdir),
                )
            with patch.object(
                edgar_adapter, "_detect_events", side_effect=AssertionError("no call")
            ):
                df2 = edgar_adapter.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    universe=["NVDA"],
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df2), 1)

    def test_uses_universe_loader_when_none_provided(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(edgar_adapter, "_detect_events", return_value=SAMPLE_EVENTS),
                patch.object(
                    edgar_adapter, "load_input_universe", return_value=frozenset({"NVDA"})
                ),
            ):
                df = edgar_adapter.fetch_daily_news(
                    date=dt.date(2026, 5, 15),
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["tickers"], ["NVDA"])


if __name__ == "__main__":
    unittest.main()

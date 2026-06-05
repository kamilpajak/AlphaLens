"""L2 contract: the thematic parquet-hop schemas (test-strategy Phase 2).

Each thematic stage writes a parquet the next stage reads. The seams are
``~/.alphalens/thematic_*`` files, so a column rename or dtype drift on the
writer is invisible to the reader's hermetic unit tests (they mock the frame).
This pins the columns + dtypes each CONSUMER depends on at every hop, with a
deliberately-broken positive control per schema so the assertion cannot rot to
a no-op. Legacy-column tolerance is documented by exercising the real
``event_extractor._backfill_legacy_columns`` against the EVENTS schema.

Failure classes pinned (memo §2): seam-contract (column rename / dtype drift
across a stage hop) + the JSON-string-not-dict shape the Django coercer relies
on (``briefs.ingest.coerce.coerce_json_obj``).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_pipeline.data.schemas import (
    NEWS_FRAME_SCHEMA,
    THEMATIC_CANDIDATES_SCHEMA,
    validate_thematic_events,
    validate_thematic_scored,
)
from alphalens_pipeline.thematic.extraction.event_extractor import _backfill_legacy_columns
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS
from pandera.errors import SchemaError


def _valid_news_frame() -> pd.DataFrame:
    """A NEWS frame as ``news_ingest`` writes it (NEWS_COLUMNS order, UTC ts)."""
    df = pd.DataFrame(
        {
            "id": ["news1", "news2"],
            "source": ["polygon", "gdelt"],
            "timestamp": pd.to_datetime(["2026-05-31 10:00", "2026-05-31 11:00"], utc=True),
            "tickers": [["NVDA", "TSLA"], ["AAPL"]],
            "title": ["NVDA acquires XYZ", "AAPL guidance"],
            "body": ["body 1", "body 2"],
            "url": ["https://ex1.test", "https://ex2.test"],
            "keywords": [["acq"], ["guidance"]],
            "extra": ["{}", "{}"],
            # P1b bitemporal transaction-time stamped by ingest_daily at lake entry.
            "ingested_at": pd.to_datetime(["2026-05-31 12:00", "2026-05-31 12:00"], utc=True),
        }
    )
    return df[NEWS_COLUMNS]


def _valid_events_frame() -> pd.DataFrame:
    """An events frame as ``event_extractor.extract_daily`` writes it."""
    return pd.DataFrame(
        {
            "news_id": ["news1", "news2"],
            "event_type": ["m_and_a", "guidance_change"],
            "primary_entities": [["NVDA"], ["AAPL"]],
            "themes": [["consolidation"], ["ai"]],
            "sentiment": ["positive", "neutral"],
            "second_order_implications": [[], []],
            "confidence": [0.95, 0.80],
            "extraction_method": ["template", "flash"],
            "template_id": ["m_and_a_press_release", None],
            "template_fields_json": [json.dumps({"acquirer": "NVDA"}, sort_keys=True), None],
            "model": ["template", "deepseek-flash"],
            "extracted_at": pd.to_datetime(["2026-05-31", "2026-05-31"], utc=True),
        }
    )


def _valid_candidates_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["NVDA", "AVGO"],
            "theme": ["ai-infra", "ai-infra"],
            "verified": [True, False],
            "llm_confidence": [0.7, 0.4],
            "market_cap": [3.0e12, 8.0e11],
        }
    )


def _valid_scored_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["NVDA", "AVGO"],
            "theme": ["ai-infra", "ai-infra"],
            "verified": [True, True],
            "layer4_weighted_score": [18.0, 9.5],
            "catalyst_template_id": ["m_and_a_press_release", None],
            "catalyst_template_facts_json": [json.dumps({"acquirer": "NVDA"}), None],
        }
    )


class TestNewsFrameSchema(unittest.TestCase):
    def test_valid_frame_passes(self):
        NEWS_FRAME_SCHEMA.validate(_valid_news_frame())

    def test_survives_parquet_roundtrip(self):
        # parquet deserialises list[str] columns to numpy arrays; the listlike
        # check must accept them (the real on-disk read shape).
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "news.parquet"
            _valid_news_frame().to_parquet(path, index=False)
            read = pd.read_parquet(path)
            self.assertIsInstance(read.loc[0, "tickers"], np.ndarray)
            NEWS_FRAME_SCHEMA.validate(read)

    def test_missing_required_column_fails(self):
        # positive control: drop the tickers column → fail.
        bad = _valid_news_frame().drop(columns=["tickers"])
        with self.assertRaises(SchemaError):
            NEWS_FRAME_SCHEMA.validate(bad)

    def test_tz_naive_timestamp_fails(self):
        # positive control: the #2-class real-data-shape guard — a tz-naive
        # timestamp (datetime64[ns]) must not pass the UTC-aware contract.
        bad = _valid_news_frame()
        bad["timestamp"] = pd.to_datetime(["2026-05-31 10:00", "2026-05-31 11:00"])
        with self.assertRaises(SchemaError):
            NEWS_FRAME_SCHEMA.validate(bad)

    def test_tickers_column_with_scalar_string_cells_fails(self):
        # positive control: each tickers cell is a scalar string, not a list → fail.
        bad = _valid_news_frame()
        bad["tickers"] = ["NVDA", "AAPL"]
        with self.assertRaises(SchemaError):
            NEWS_FRAME_SCHEMA.validate(bad)

    def test_non_string_id_fails(self):
        # positive control: a numeric id where the consumer expects a string →
        # fail (the dtype=None content guard, not a storage-dtype pin).
        bad = _valid_news_frame()
        bad["id"] = [1, 2]
        with self.assertRaises(SchemaError):
            NEWS_FRAME_SCHEMA.validate(bad)


class TestThematicEventsSchema(unittest.TestCase):
    def test_valid_frame_passes(self):
        validate_thematic_events(_valid_events_frame())

    def test_survives_parquet_roundtrip(self):
        # Proves the string + list element-wise checks accept the REAL on-disk
        # read shape (pandas 3.0 yields str cells + ndarray list cells), so the
        # _STR_CHECK guard cannot false-red on a genuine events parquet.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.parquet"
            _valid_events_frame().to_parquet(path, index=False)
            read = pd.read_parquet(path)
            self.assertIsInstance(read.loc[0, "primary_entities"], np.ndarray)
            validate_thematic_events(read)

    def test_non_string_news_id_fails(self):
        # positive control: numeric news_id where the join key must be a string.
        bad = _valid_events_frame()
        bad["news_id"] = [1, 2]
        with self.assertRaises(SchemaError):
            validate_thematic_events(bad)

    def test_missing_join_key_fails(self):
        # positive control: drop the news_id join key → fail (catalyst_resolver
        # merges events onto candidates on news_id).
        bad = _valid_events_frame().drop(columns=["news_id"])
        with self.assertRaises(SchemaError):
            validate_thematic_events(bad)

    def test_confidence_as_string_fails(self):
        # positive control: confidence as object string, not float → fail
        # (catalyst payload does float(confidence) downstream).
        bad = _valid_events_frame()
        bad["confidence"] = ["0.95", "0.80"]
        with self.assertRaises(SchemaError):
            validate_thematic_events(bad)

    def test_confidence_out_of_range_fails(self):
        bad = _valid_events_frame()
        bad["confidence"] = [1.5, 0.8]
        with self.assertRaises(SchemaError):
            validate_thematic_events(bad)

    def test_scalar_primary_entities_fails(self):
        # positive control: primary_entities as a scalar string → fail.
        bad = _valid_events_frame()
        bad["primary_entities"] = ["NVDA", "AAPL"]
        with self.assertRaises(SchemaError):
            validate_thematic_events(bad)

    def test_native_dict_in_template_fields_json_fails(self):
        # positive control: template_fields_json must be a serialised STRING,
        # never a native dict (would break the parquet + Django coercer).
        bad = _valid_events_frame()
        bad["template_fields_json"] = [{"acquirer": "NVDA"}, None]
        with self.assertRaises(SchemaError):
            validate_thematic_events(bad)

    def test_bad_extraction_method_fails(self):
        bad = _valid_events_frame()
        bad["extraction_method"] = ["template", "gemini"]  # 'gemini' not allowed
        with self.assertRaises(SchemaError):
            validate_thematic_events(bad)

    def test_legacy_frame_tolerated_after_backfill(self):
        # legacy-column tolerance: an old events parquet predates the PR-2/PR-3
        # audit columns. The real _backfill_legacy_columns fills them, and the
        # backfilled frame must satisfy the schema (extraction_method='flash',
        # template_id/template_fields_json=None).
        legacy = _valid_events_frame().drop(
            columns=["extraction_method", "template_id", "template_fields_json"]
        )
        # The required core already validates (audit cols are required=False).
        validate_thematic_events(legacy)
        backfilled = _backfill_legacy_columns(legacy)
        self.assertEqual(backfilled["extraction_method"].iloc[0], "flash")
        self.assertIsNone(backfilled["template_id"].iloc[0])
        validate_thematic_events(backfilled)


class TestThematicCandidatesSchema(unittest.TestCase):
    def test_valid_frame_passes(self):
        THEMATIC_CANDIDATES_SCHEMA.validate(_valid_candidates_frame())

    def test_verified_as_int_fails(self):
        # positive control: verified as int 0/1, not bool → fail (the gate
        # filter scored[verified == True] would silently behave differently).
        bad = _valid_candidates_frame()
        bad["verified"] = [1, 0]
        with self.assertRaises(SchemaError):
            THEMATIC_CANDIDATES_SCHEMA.validate(bad)

    def test_missing_theme_fails(self):
        bad = _valid_candidates_frame().drop(columns=["theme"])
        with self.assertRaises(SchemaError):
            THEMATIC_CANDIDATES_SCHEMA.validate(bad)


class TestThematicScoredSchema(unittest.TestCase):
    def test_valid_frame_passes(self):
        validate_thematic_scored(_valid_scored_frame())

    def test_thin_cohort_null_score_tolerated(self):
        # nullable score: thin-cohort candidates carry None, not 0/NaN-as-error.
        frame = _valid_scored_frame()
        frame["layer4_weighted_score"] = [18.0, None]
        validate_thematic_scored(frame)

    def test_native_dict_in_catalyst_facts_fails(self):
        # positive control: catalyst_template_facts_json must be a JSON string,
        # never a native dict — the contract the Django coerce_json_obj relies on.
        bad = _valid_scored_frame()
        bad["catalyst_template_facts_json"] = [{"acquirer": "NVDA"}, None]
        with self.assertRaises(SchemaError):
            validate_thematic_scored(bad)

    def test_missing_ticker_fails(self):
        bad = _valid_scored_frame().drop(columns=["ticker"])
        with self.assertRaises(SchemaError):
            validate_thematic_scored(bad)


if __name__ == "__main__":
    unittest.main()

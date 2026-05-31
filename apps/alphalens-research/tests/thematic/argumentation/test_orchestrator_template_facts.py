"""PR-3 orchestrator wiring + dedup-at-injection guard.

Pins the contract that the brief generation orchestrator:

1. Propagates ``catalyst_template_id`` + ``catalyst_template_facts_json``
   columns from a Phase D-scored row into the facts dict passed to
   :func:`generator.generate_brief_with_retry` (as ``template_id`` /
   ``template_facts``).

2. Persists ``brief_template_id`` + ``brief_template_facts_json`` to the
   brief parquet so Django + the SPA can render typed-fact citations.

3. Applies the same-window dedup-at-injection guard from design memo §3:
   when the verified frame contains multiple rows for the same
   ``(ticker, template_id)`` within a 24h window, the survivor is the
   row with the MOST non-null template_facts keys (not necessarily the
   one the layer4_weighted_score sort would have kept). This is a
   minimum-viable PR-4 dependency-breaker — full multi-source dedup
   lands in PR-4.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.argumentation import generator, orchestrator


def _scored_row(
    *,
    ticker: str,
    theme: str,
    weighted_score: int = 4,
    template_id: str | None = None,
    template_facts: dict | None = None,
    catalyst_published_at: str | None = None,
) -> dict:
    """Build one Phase D-shaped row carrying the new template_* columns."""
    return {
        "theme": theme,
        "ticker": ticker,
        "company_name": f"{ticker} Inc",
        "rationale": "x",
        "gemini_confidence": 0.9,
        "market_cap": 1.0e9,
        "gates_passed_str": "tenk,press",
        "verified": True,
        "industry_id": 101001,
        "industry_name": "Computer Hardware",
        "sector_name": "Technology",
        "insider_score_usd": 0.0,
        "insider_score_sector_percentile": 50.0,
        "fcff_yield_pct": None,
        "fcff_yield_sector_percentile": None,
        "valuation_ps": 20.0,
        "valuation_ev_rev": 22.0,
        "valuation_fcf_margin": -0.1,
        "valuation_composite_sector_percentile": 50.0,
        "technical_rsi": 55.0,
        "technical_ma50_distance_pct": 1.0,
        "technical_atr_pct": 4.0,
        "technical_volume_zscore": 1.0,
        "technicals_summary_str": "RSI 55",
        "layer4_weighted_score": weighted_score,
        "catalyst_template_id": template_id,
        "catalyst_template_facts_json": (
            json.dumps(template_facts) if template_facts is not None else None
        ),
        "source_event_published_at": catalyst_published_at,
    }


_FAKE_BRIEF = {
    "tldr": "tldr",
    "supply_chain_reasoning": "reasoning",
    "bear_summary": "bear",
    "catalyst_failure_exit": "exit",
    "model_used": generator.PRO_MODEL,
}


class TestOrchestratorTemplateFactsProjection(unittest.TestCase):
    def test_row_to_facts_projects_template_columns(self):
        row = pd.Series(
            _scored_row(
                ticker="NVDA",
                theme="ai",
                template_id="m_and_a_press_release",
                template_facts={"acquirer_ticker": "NVDA", "consideration_usd": 5_000_000_000},
            )
        )
        facts = orchestrator._row_to_facts(row)
        self.assertEqual(facts.get("template_id"), "m_and_a_press_release")
        self.assertEqual(
            facts.get("template_facts"),
            {"acquirer_ticker": "NVDA", "consideration_usd": 5_000_000_000},
        )

    def test_row_to_facts_handles_missing_template_columns(self):
        # Legacy frames (pre-PR-3) lack the new columns entirely — must
        # not crash + must surface explicit None so the prompt's
        # absent-block branch fires.
        row = pd.Series(
            {
                "theme": "x",
                "ticker": "X",
                "company_name": "X",
                "rationale": "",
                "gates_passed_str": "",
                "industry_name": None,
                "sector_name": None,
                "technicals_summary_str": "",
                "layer4_weighted_score": 1,
            }
        )
        facts = orchestrator._row_to_facts(row)
        self.assertIsNone(facts.get("template_id"))
        self.assertIsNone(facts.get("template_facts"))

    def test_row_to_facts_handles_malformed_template_facts_json(self):
        # Pandas / parquet roundtrip should hand back a clean JSON string,
        # but a manually-edited row could carry garbage — degrade to None
        # rather than crashing the entire brief loop.
        row = pd.Series(
            _scored_row(
                ticker="X",
                theme="x",
                template_id="m_and_a_press_release",
            )
            | {"catalyst_template_facts_json": "{not json"}
        )
        facts = orchestrator._row_to_facts(row)
        self.assertIsNone(facts.get("template_facts"))
        # template_id still surfaces — it's the bare id, not the facts.
        self.assertEqual(facts.get("template_id"), "m_and_a_press_release")


class TestOrchestratorBriefParquetCarriesTemplateFacts(unittest.TestCase):
    def test_brief_parquet_emits_template_columns(self):
        scored = pd.DataFrame(
            [
                _scored_row(
                    ticker="NVDA",
                    theme="m_and_a",
                    template_id="m_and_a_press_release",
                    template_facts={"acquirer_ticker": "NVDA", "target_ticker": "XYZ"},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF, None)):
                out = orchestrator.generate_briefs(
                    scored,
                    asof=dt.date(2026, 5, 31),
                    output_dir=Path(tmp),
                    api_key="testkey",
                )
        self.assertIn("brief_template_id", out.columns)
        self.assertIn("brief_template_facts_json", out.columns)
        row = out.iloc[0]
        self.assertEqual(row["brief_template_id"], "m_and_a_press_release")
        self.assertEqual(
            json.loads(row["brief_template_facts_json"]),
            {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"},
        )

    def test_brief_parquet_template_columns_null_when_flash_path(self):
        scored = pd.DataFrame(
            [_scored_row(ticker="ABC", theme="ai", template_id=None, template_facts=None)]
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF, None)):
                out = orchestrator.generate_briefs(
                    scored,
                    asof=dt.date(2026, 5, 31),
                    output_dir=Path(tmp),
                    api_key="testkey",
                )
        row = out.iloc[0]
        self.assertTrue(row["brief_template_id"] is None or pd.isna(row["brief_template_id"]))
        self.assertTrue(
            row["brief_template_facts_json"] is None or pd.isna(row["brief_template_facts_json"])
        )


class TestDedupAtInjectionGuard(unittest.TestCase):
    """Same-window dedup-at-injection guard (design memo §3, 10-line guard).

    When the verified frame has multiple rows for the same
    ``(ticker, template_id)`` within 24h, the surviving row must carry
    the RICHEST template_facts payload — measured as the count of
    non-null keys — even if the sort-by-weighted-score path would have
    kept a sparser row.
    """

    def test_richest_template_facts_survives_same_ticker_24h_collision(self):
        # Two rows for NVDA / m_and_a_press_release published 6h apart.
        # Both share weighted_score=4 so the sort cannot break the tie
        # on that key. The dedup guard MUST pick the row with the most
        # extracted fields.
        sparse = _scored_row(
            ticker="NVDA",
            theme="ai_supply_chain",
            weighted_score=4,
            template_id="m_and_a_press_release",
            template_facts={"acquirer_ticker": "NVDA"},  # 1 field
            catalyst_published_at="2026-05-31T08:00:00Z",
        )
        rich = _scored_row(
            ticker="NVDA",
            theme="quantum_computing",
            weighted_score=4,
            template_id="m_and_a_press_release",
            template_facts={
                "acquirer_ticker": "NVDA",
                "target_ticker": "XYZ",
                "consideration_usd": 5_000_000_000,
                "announcement_date": "2026-05-31",
            },  # 4 fields
            catalyst_published_at="2026-05-31T14:00:00Z",
        )
        scored = pd.DataFrame([sparse, rich])

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF, None)):
                out = orchestrator.generate_briefs(
                    scored,
                    asof=dt.date(2026, 5, 31),
                    output_dir=Path(tmp),
                    api_key="testkey",
                )
        # Collapsed to one row (same ticker).
        self.assertEqual(len(out), 1)
        # Survivor is the richer one — assert by the unique target_ticker
        # that only the rich row carries.
        facts = json.loads(out.iloc[0]["brief_template_facts_json"])
        self.assertEqual(facts["target_ticker"], "XYZ")
        self.assertEqual(facts["consideration_usd"], 5_000_000_000)

    def test_different_template_ids_same_ticker_do_not_collapse_to_one_facts_payload(self):
        # NVDA hits both m_and_a_press_release AND earnings_surprise on
        # the same day — these are different events and the brief should
        # surface the highest-weighted row's facts. The dedup guard only
        # operates on same (ticker, template_id); cross-template-id same-
        # ticker still collapses (existing _sort_and_dedup_for_brief
        # already handles it) but the surviving template_facts are the
        # higher-weighted_score row's, not a merged superset.
        row_a = _scored_row(
            ticker="NVDA",
            theme="ai",
            weighted_score=3,
            template_id="m_and_a_press_release",
            template_facts={"acquirer_ticker": "NVDA"},
            catalyst_published_at="2026-05-31T08:00:00Z",
        )
        row_b = _scored_row(
            ticker="NVDA",
            theme="earnings",
            weighted_score=5,
            template_id="earnings_surprise",
            template_facts={"reporting_ticker": "NVDA", "eps_surprise_pct": 12.5},
            catalyst_published_at="2026-05-31T14:00:00Z",
        )
        scored = pd.DataFrame([row_a, row_b])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(orchestrator, "_brief_for_row", return_value=(_FAKE_BRIEF, None)):
                out = orchestrator.generate_briefs(
                    scored,
                    asof=dt.date(2026, 5, 31),
                    output_dir=Path(tmp),
                    api_key="testkey",
                )
        self.assertEqual(len(out), 1)
        # Higher-weighted row wins (row_b, score 5 vs row_a score 3).
        self.assertEqual(out.iloc[0]["brief_template_id"], "earnings_surprise")
        facts = json.loads(out.iloc[0]["brief_template_facts_json"])
        self.assertIn("eps_surprise_pct", facts)


if __name__ == "__main__":
    unittest.main()

"""An event-lane row flows through the brief stage with NO brief-side change (#1296).

The cluster facts ride in ``source_event_url/title/published_at`` (the catalyst
block of the prompt renders only when the URL is set), the channel block stays
absent (no ``channel_*`` columns), and the trade-setup ladder is built from the
OHLCV cache exactly as for a thematic row.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from alphalens_pipeline.thematic.argumentation import (
    generator,
    orchestrator,
    prompts,
    support_guard,
)

from tests.events.test_merge import event_frame, thematic_frame
from tests.thematic.argumentation.test_orchestrator import _scored_df

EDGAR_URL = "https://www.sec.gov/Archives/edgar/data/99/x-index.htm"


def _scored_event_row() -> pd.Series:
    row = event_frame(("AAA",)).drop(columns=["eligible", "exclusion_reason"]).iloc[0].copy()
    row["event_overlap"] = False
    # scorer enrichment a real `score` stage would have added
    row["layer4_weighted_score"] = 2
    row["selection_score"] = 1.0
    row["technical_atr_pct"] = 3.0
    row["technicals_summary_str"] = "RSI 50"
    return row


def _ohlcv(ticker: str, asof: dt.date) -> pd.DataFrame:
    idx = pd.bdate_range(end=asof, periods=300)
    close = np.linspace(20.0, 25.0, len(idx))
    return pd.DataFrame(
        {"open": close, "high": close * 1.02, "low": close * 0.98, "close": close, "volume": 1e6},
        index=idx,
    )


class TestEventRowFacts(unittest.TestCase):
    def test_event_row_facts_render_catalyst_block_from_cluster_facts(self):
        facts = orchestrator._row_to_facts(_scored_event_row())
        self.assertEqual(facts["source_event_url"], EDGAR_URL)
        self.assertIn("Insider purchase cluster", facts["source_event_title"])
        self.assertEqual(facts["causal_support"], support_guard.NO_RECORD)
        self.assertIsNone(facts["template_facts"])
        prompt = prompts.build_flash_prompt(facts)
        self.assertIn("catalyst (triggering event)", prompt)
        self.assertIn("Insider purchase cluster", prompt)
        self.assertIn(EDGAR_URL, prompt)
        # no channel assessment ran for the row: the record renders as no_record,
        # exactly like a thematic row the assessor skipped
        self.assertIn("causal_support: no_record", prompt)

    def test_thematic_row_facts_unchanged_by_nan_event_columns(self):
        base = _scored_df().iloc[1]
        with_cols = base.copy()
        for col in ("source", "event_overlap", "event_n_insiders", "event_cluster_usd"):
            with_cols[col] = np.nan
        self.assertEqual(orchestrator._row_to_facts(base), orchestrator._row_to_facts(with_cols))


class TestGenerateBriefsWithEventRow(unittest.TestCase):
    def test_event_row_gets_a_brief_row_and_a_trade_setup(self):
        asof = dt.date(2026, 3, 4)
        scored = pd.concat(
            [
                thematic_frame(("QUBT",)).assign(source="thematic", event_overlap=False),
                _scored_event_row().to_frame().T,
            ],
            ignore_index=True,
        )
        scored["verified"] = scored["verified"].astype(bool)
        scored["layer4_weighted_score"] = 2
        scored["selection_score"] = 1.0
        with (
            patch.object(
                orchestrator,
                "_brief_for_row",
                side_effect=lambda row, **kw: (
                    None,
                    None,
                    generator.BriefErrorKind.TRANSPORT,
                    {},
                    [],
                ),
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            out = orchestrator.generate_briefs(
                scored, asof=asof, output_dir=Path(tmp), ohlcv_loader=_ohlcv
            )
        self.assertEqual(set(out.ticker), {"QUBT", "AAA"})
        event = out[out.ticker == "AAA"].iloc[0]
        self.assertEqual(event.source, "insider_cluster")
        self.assertIsNotNone(event.brief_trade_setup)
        self.assertEqual(event.source_event_url, EDGAR_URL)
        self.assertEqual(set(out.rank_in_day), {1, 2})


if __name__ == "__main__":
    unittest.main()

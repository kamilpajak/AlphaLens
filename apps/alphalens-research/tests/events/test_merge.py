"""Tests for the event-lane merge into the day's thematic candidates (#1296)."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.events import EVENT_LANE_ENV, merge
from alphalens_pipeline.events.insider_cluster import SOURCE_INSIDER_CLUSTER
from alphalens_pipeline.events.insider_cluster_detect import EVENT_CANDIDATE_COLUMNS

D = dt.date


def thematic_frame(tickers=("QUBT", "IONQ")) -> pd.DataFrame:
    rows = []
    for t in tickers:
        rows.append(
            {
                "theme": "quantum_computing",
                "ticker": t,
                "company_name": f"{t} Inc",
                "rationale": "x",
                "llm_confidence": 0.8,
                "market_cap": 2e9,
                "gates_passed": ["tenk"],
                "gates_passed_str": "tenk",
                "n_gates_passed": 1,
                "gates_failed": [],
                "gates_failed_str": "",
                "n_gates_failed": 0,
                "gates_unknown": [],
                "gates_unknown_str": "",
                "n_gates_unknown": 0,
                "verified": True,
                "gate_verdict_json": "{}",
                "source_event_url": f"https://pub.test/{t}",
                "source_event_title": f"{t} news",
                "source_event_published_at": "2026-03-03T12:00:00Z",
                "theme_search_keywords": ["quantum"],
                "mapper_config_version": "mapper-freeze-v4",
            }
        )
    return pd.DataFrame(rows)


def event_frame(tickers=("AAA",), *, eligible=True, reason="") -> pd.DataFrame:
    rows = []
    for t in tickers:
        row = dict.fromkeys(EVENT_CANDIDATE_COLUMNS)
        row.update(
            {
                "theme": SOURCE_INSIDER_CLUSTER,
                "ticker": t,
                "company_name": f"{t} Corp",
                "rationale": "Insider purchase cluster: 2 officers/directors bought $120k",
                "market_cap": 1.5e9,
                "gates_passed": [],
                "gates_passed_str": "",
                "n_gates_passed": 0,
                "gates_failed": [],
                "gates_failed_str": "",
                "n_gates_failed": 0,
                "gates_unknown": [],
                "gates_unknown_str": "",
                "n_gates_unknown": 0,
                "verified": True,
                "gate_verdict_json": "{}",
                "source_event_url": "https://www.sec.gov/Archives/edgar/data/99/x-index.htm",
                "source_event_title": "Insider purchase cluster: 2 officers/directors bought $120k",
                "source_event_published_at": "2026-03-03T21:00:00Z",
                "theme_search_keywords": [],
                "source": SOURCE_INSIDER_CLUSTER,
                "event_n_insiders": 2,
                "event_cluster_usd": 120_000.0,
                "event_buyers_json": "[]",
                "event_first_leg_date": D(2026, 3, 2),
                "event_completing_accession": "2-2026-03-03",
                "event_acceptance_utc": "2026-03-03T21:00:00Z",
                "event_arrival_session": D(2026, 3, 4),
                "event_filing_lag_bdays": 1,
                "event_gate_version": "insider_cluster_gate_v1",
                "eligible": eligible,
                "exclusion_reason": reason,
            }
        )
        rows.append(row)
    return pd.DataFrame(rows, columns=list(EVENT_CANDIDATE_COLUMNS))


class TestFlag(unittest.TestCase):
    def test_event_lane_enabled_only_for_literal_one(self):
        self.assertTrue(merge.event_lane_enabled({EVENT_LANE_ENV: "1"}))
        for v in ("0", "true", "yes", "", " 1"):
            self.assertFalse(merge.event_lane_enabled({EVENT_LANE_ENV: v}), v)
        self.assertFalse(merge.event_lane_enabled({}))


class TestLoad(unittest.TestCase):
    def test_load_missing_file_returns_typed_empty_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = merge.load_event_candidates(Path(tmp) / "2026-03-04.parquet")
        self.assertTrue(out.empty)
        self.assertEqual(tuple(out.columns), EVENT_CANDIDATE_COLUMNS)

    def test_load_reads_existing_parquet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-03-04.parquet"
            event_frame().to_parquet(path, index=False)
            out = merge.load_event_candidates(path)
        self.assertEqual(list(out.ticker), ["AAA"])


class TestMerge(unittest.TestCase):
    def test_thematic_rows_get_source_thematic_and_are_otherwise_unchanged(self):
        cands = thematic_frame()
        out = merge.merge_event_candidates(cands, event_frame())
        thematic = out[out["source"] == merge.SOURCE_THEMATIC].reset_index(drop=True)
        pd.testing.assert_frame_equal(thematic[cands.columns], cands)
        self.assertFalse(thematic["event_overlap"].any())

    def test_eligible_event_rows_appended_with_source_insider_cluster(self):
        out = merge.merge_event_candidates(thematic_frame(), event_frame(("AAA", "BBB")))
        self.assertEqual(list(out.ticker), ["QUBT", "IONQ", "AAA", "BBB"])
        self.assertEqual(list(out.source), ["thematic", "thematic"] + [SOURCE_INSIDER_CLUSTER] * 2)
        self.assertNotIn("eligible", out.columns)
        self.assertNotIn("exclusion_reason", out.columns)
        self.assertEqual(out.loc[2, "theme"], SOURCE_INSIDER_CLUSTER)
        self.assertEqual(out.loc[2, "event_n_insiders"], 2)

    def test_nan_eligible_never_reads_as_eligible(self):
        events = event_frame(("AAA",))
        events["eligible"] = events["eligible"].astype(object)
        events.loc[0, "eligible"] = None
        out = merge.merge_event_candidates(thematic_frame(), events)
        self.assertEqual(list(out.ticker), ["QUBT", "IONQ"])

    def test_ineligible_event_rows_are_not_appended(self):
        out = merge.merge_event_candidates(
            thematic_frame(), event_frame(("AAA",), eligible=False, reason="mcap_unknown")
        )
        self.assertEqual(list(out.ticker), ["QUBT", "IONQ"])

    def test_same_ticker_overlap_keeps_thematic_row_and_copies_event_facts(self):
        out = merge.merge_event_candidates(thematic_frame(), event_frame(("QUBT", "AAA")))
        self.assertEqual(list(out.ticker), ["QUBT", "IONQ", "AAA"])
        q = out.iloc[0]
        self.assertEqual(q.source, "thematic")
        self.assertTrue(q.event_overlap)
        self.assertEqual(q.source_event_url, "https://pub.test/QUBT")  # thematic catalyst primary
        self.assertEqual(q.theme, "quantum_computing")
        self.assertEqual(q.event_n_insiders, 2)
        self.assertEqual(q.event_completing_accession, "2-2026-03-03")
        self.assertFalse(out.iloc[1].event_overlap)
        self.assertFalse(out.iloc[2].event_overlap)

    def test_ticker_match_is_case_insensitive(self):
        out = merge.merge_event_candidates(thematic_frame(("qubt",)), event_frame(("QUBT",)))
        self.assertEqual(len(out), 1)
        self.assertTrue(out.iloc[0].event_overlap)

    def test_verified_dtype_stays_bool_after_concat(self):
        out = merge.merge_event_candidates(thematic_frame(), event_frame())
        self.assertEqual(out["verified"].dtype, bool)
        self.assertEqual(out["event_overlap"].dtype, bool)
        self.assertTrue(out["verified"].all())

    def test_empty_candidates_with_events_yields_event_only_frame(self):
        out = merge.merge_event_candidates(thematic_frame(()), event_frame())
        self.assertEqual(list(out.ticker), ["AAA"])
        self.assertEqual(list(out.source), [SOURCE_INSIDER_CLUSTER])

    def test_empty_events_still_stamps_source_and_fact_columns(self):
        out = merge.merge_event_candidates(thematic_frame(), event_frame(()))
        self.assertEqual(list(out.source), ["thematic", "thematic"])
        for col in merge.EVENT_FACT_COLUMNS:
            self.assertIn(col, out.columns)
            self.assertTrue(out[col].isna().all(), col)

    def test_inputs_are_not_mutated(self):
        cands, events = thematic_frame(), event_frame(("QUBT",))
        c0, e0 = cands.copy(), events.copy()
        merge.merge_event_candidates(cands, events)
        pd.testing.assert_frame_equal(cands, c0)
        pd.testing.assert_frame_equal(events, e0)

    def test_event_rows_carry_no_scorer_enrichment_names(self):
        forbidden = {
            "selection_score",
            "scorer_config_version",
            "catalyst_template_id",
            "technical_rsi",
        }
        self.assertFalse(forbidden & set(EVENT_CANDIDATE_COLUMNS))


if __name__ == "__main__":
    unittest.main()

"""Stage-1 retro Phase-1 labeling script — blinding guard + pure-core tests.

Pins, per the pre-registration
(`docs/research/stage1_retro_gate_increment_prereg_2026_08_19.md` §5, §11.1):

* the labeling module contains NO reference to the outcome store
  (`population_ladders`) or outcome columns — labels are generated blind;
* the k=5 majority-vote pair labeling and the row-level label derivation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pandas as pd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage1_retro_label_pairs.py"
_spec = importlib.util.spec_from_file_location("stage1_retro_label_pairs", _SCRIPT)
assert _spec and _spec.loader
label_pairs = importlib.util.module_from_spec(_spec)
sys.modules["stage1_retro_label_pairs"] = label_pairs
_spec.loader.exec_module(label_pairs)


class TestBlindingGuard(unittest.TestCase):
    """The labeling code must be unable to see outcomes (pre-reg §5)."""

    # Assembled from parts so this test file itself stays out of any
    # source-wide sweep for the forbidden strings.
    FORBIDDEN = ("population_" + "ladders", "market_excess" + "_return", "realized" + "_r")

    def _breaches(self, source: str) -> list[str]:
        """The one scan used by both the guard and its positive control."""
        return [needle for needle in self.FORBIDDEN if needle in source]

    def test_script_source_never_references_outcome_stores(self):
        source = _SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(self._breaches(source), [], "blinding breach in labeling script source")

    def test_positive_control_detects_a_seeded_breach(self):
        # The check cannot rot to a tautology: the SAME scan that clears the
        # real script must flag a source with a seeded forbidden reference.
        seeded = "df = pd.read_parquet(root / '" + self.FORBIDDEN[0] + "/2026-06-01.parquet')"
        self.assertEqual(self._breaches(seeded), [self.FORBIDDEN[0]])


class TestPairKey(unittest.TestCase):
    def test_pair_key_is_theme_pipe_url(self):
        self.assertEqual(
            label_pairs.pair_key("quantum_computing", "https://x.test/a"),
            "quantum_computing|https://x.test/a",
        )


class TestMajorityPairLabel(unittest.TestCase):
    R = label_pairs.LABEL_THEME_REFUSED
    K_LBL = label_pairs.LABEL_KEPT

    def test_three_of_five_refusals_is_theme_refused(self):
        labels = [self.R, self.R, self.R, self.K_LBL, self.K_LBL]
        self.assertEqual(label_pairs.majority_pair_label(labels), self.R)

    def test_two_of_five_refusals_is_kept(self):
        labels = [self.R, self.R, self.K_LBL, self.K_LBL, self.K_LBL]
        self.assertEqual(label_pairs.majority_pair_label(labels), self.K_LBL)

    def test_fewer_than_k_valid_calls_is_instrument_failure(self):
        labels = [self.R, self.R, self.R, self.R]  # only 4 valid calls
        self.assertEqual(
            label_pairs.majority_pair_label(labels), label_pairs.LABEL_INSTRUMENT_FAILURE
        )

    def test_unanimous_kept(self):
        self.assertEqual(label_pairs.majority_pair_label([self.K_LBL] * 5), self.K_LBL)


class TestMajorityProposalSet(unittest.TestCase):
    def test_ticker_in_three_of_five_calls_is_in_the_set(self):
        proposals = [["NVDA", "AMD"], ["NVDA"], ["NVDA", "TSM"], [], ["AMD"]]
        self.assertEqual(label_pairs.majority_proposal_set(proposals), {"NVDA"})

    def test_duplicate_mentions_within_one_call_count_once(self):
        proposals = [["NVDA", "NVDA", "NVDA"], ["NVDA"], [], [], []]
        self.assertEqual(label_pairs.majority_proposal_set(proposals), set())

    def test_declined_calls_contribute_empty_lists(self):
        proposals = [["IONQ"], ["IONQ"], ["IONQ"], [], []]
        self.assertEqual(label_pairs.majority_proposal_set(proposals), {"IONQ"})


class TestDeriveRowLabel(unittest.TestCase):
    def test_refused_pair_propagates_to_every_row(self):
        self.assertEqual(
            label_pairs.derive_row_label(label_pairs.LABEL_THEME_REFUSED, {"NVDA"}, "NVDA"),
            label_pairs.LABEL_THEME_REFUSED,
        )

    def test_kept_pair_with_row_ticker_in_majority_set(self):
        self.assertEqual(
            label_pairs.derive_row_label(label_pairs.LABEL_KEPT, {"NVDA", "TSM"}, "TSM"),
            label_pairs.LABEL_KEPT_PROPOSED,
        )

    def test_kept_pair_with_row_ticker_absent(self):
        self.assertEqual(
            label_pairs.derive_row_label(label_pairs.LABEL_KEPT, {"NVDA"}, "QUBT"),
            label_pairs.LABEL_KEPT_ABSENT,
        )

    def test_instrument_failure_propagates(self):
        self.assertEqual(
            label_pairs.derive_row_label(label_pairs.LABEL_INSTRUMENT_FAILURE, set(), "QUBT"),
            label_pairs.LABEL_INSTRUMENT_FAILURE,
        )


class TestAggregatePair(unittest.TestCase):
    def _rec(self, label: str, proposals: list[str] | None = None) -> dict:
        return {"call_label": label, "proposed_tickers": proposals or []}

    def test_kept_pair_aggregate(self):
        recs = [
            self._rec(label_pairs.LABEL_KEPT, ["NVDA"]),
            self._rec(label_pairs.LABEL_KEPT, ["NVDA", "AMD"]),
            self._rec(label_pairs.LABEL_KEPT, ["NVDA"]),
            self._rec(label_pairs.LABEL_THEME_REFUSED),
            self._rec(label_pairs.LABEL_THEME_REFUSED),
        ]
        agg = label_pairs.aggregate_pair(recs)
        self.assertEqual(agg["pair_label"], label_pairs.LABEL_KEPT)
        self.assertEqual(agg["majority_proposal_set"], ["NVDA"])
        self.assertEqual((agg["n_refused_votes"], agg["n_kept_votes"]), (2, 3))
        self.assertFalse(agg["unanimous"])

    def test_refused_pair_has_empty_proposal_set(self):
        recs = [self._rec(label_pairs.LABEL_THEME_REFUSED, ["NVDA"])] * 5
        agg = label_pairs.aggregate_pair(recs)
        self.assertEqual(agg["pair_label"], label_pairs.LABEL_THEME_REFUSED)
        self.assertEqual(agg["majority_proposal_set"], [])
        self.assertTrue(agg["unanimous"])

    def test_short_pair_is_instrument_failure(self):
        recs = [self._rec(label_pairs.LABEL_KEPT, ["NVDA"])] * 3
        agg = label_pairs.aggregate_pair(recs)
        self.assertEqual(agg["pair_label"], label_pairs.LABEL_INSTRUMENT_FAILURE)


class TestPayloadFromRow(unittest.TestCase):
    def _row(self, **overrides) -> pd.Series:
        base = {
            "source_event_url": "https://news.test/a",
            "source_event_title": "Title A",
            "source_event_published_at": "2026-06-01T12:00:00Z",
            "catalyst_event_type": "litigation",
            "event_primary_entities": ["EBAY"],
            "catalyst_confidence": 0.9,
            "event_second_order_implications": ["security vendors gain"],
            "catalyst_template_id": None,
            "catalyst_template_facts_json": None,
        }
        base.update(overrides)
        return pd.Series(base)

    def test_stamped_fields_carry_through(self):
        payload = label_pairs.payload_from_row(self._row())
        self.assertEqual(payload.url, "https://news.test/a")
        self.assertEqual(payload.event_type, "litigation")
        self.assertEqual(payload.primary_entities, ["EBAY"])
        self.assertEqual(payload.confidence, 0.9)
        self.assertEqual(payload.second_order_implications, ["security vendors gain"])
        self.assertIsNone(payload.template_facts)

    def test_nan_fields_become_neutral_defaults(self):
        payload = label_pairs.payload_from_row(
            self._row(
                catalyst_event_type=float("nan"),
                event_primary_entities=None,
                catalyst_confidence=float("nan"),
                catalyst_template_facts_json='{"amount": "6200 USD"}',
            )
        )
        self.assertIsNone(payload.event_type)
        self.assertEqual(payload.primary_entities, [])
        self.assertIsNone(payload.confidence)
        self.assertEqual(payload.template_facts, {"amount": "6200 USD"})


class _FakeResp:
    def __init__(self, provider: str):
        self.provider = provider
        self.served_model = "deepseek/deepseek-v4-pro"
        self.generation_id = "gen-test"
        self.text = "{}"


class _FakeClient:
    def __init__(self, provider: str = label_pairs.PINNED_PROVIDER):
        self._provider_routing = dict(label_pairs.PINNED_ROUTING)
        self._provider = provider

    def generate_content(self, **kwargs):
        return _FakeResp(self._provider)


def _mapper_result(outcome, tickers=(), reason=None) -> dict:
    return {
        "outcome": outcome,
        "candidates": [{"ticker": t} for t in tickers],
        "no_candidates_reason": reason,
    }


class TestWrapProvenance(unittest.TestCase):
    def test_on_pin_call_is_logged_with_raw_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            prov_log = Path(tmp) / "prov.jsonl"
            raw_dir = Path(tmp) / "raw"
            raw_dir.mkdir()
            client = _FakeClient()
            label_pairs._wrap_provenance(client, prov_log, raw_dir)
            resp = client.generate_content(prompt="x")
            self.assertEqual(resp.provider, label_pairs.PINNED_PROVIDER)
            rec = json.loads(prov_log.read_text().splitlines()[0])
            self.assertEqual(rec["provider"], label_pairs.PINNED_PROVIDER)
            self.assertEqual(len(list(raw_dir.glob("call_*.json"))), 1)

    def test_off_pin_call_raises_after_logging(self):
        with tempfile.TemporaryDirectory() as tmp:
            prov_log = Path(tmp) / "prov.jsonl"
            raw_dir = Path(tmp) / "raw"
            raw_dir.mkdir()
            client = _FakeClient(provider="DeepInfra")
            label_pairs._wrap_provenance(client, prov_log, raw_dir)
            with self.assertRaises(RuntimeError):
                client.generate_content(prompt="x")
            self.assertIn("DeepInfra", prov_log.read_text())


class TestOneSlot(unittest.TestCase):
    def _run(self, side_effects: list[dict]) -> dict:
        calls = iter(side_effects)
        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch.object(
                label_pairs.stage1_frozen_v2,
                "propose_candidates_frozen",
                lambda **kw: next(calls),
            ),
            unittest.mock.patch.object(label_pairs.time, "sleep", lambda s: None),
        ):
            return label_pairs.one_slot(
                _FakeClient(),
                Path(tmp) / "calls.jsonl",
                pair_id="theme|https://news.test/a",
                slot=0,
                theme="theme",
                catalyst=None,
            )

    def test_success_records_kept_with_tickers(self):
        rec = self._run([_mapper_result(label_pairs.MapperOutcome.SUCCESS, tickers=("NVDA",))])
        self.assertEqual(rec["call_label"], label_pairs.LABEL_KEPT)
        self.assertEqual(rec["proposed_tickers"], ["NVDA"])

    def test_declined_records_theme_refused_with_reason(self):
        rec = self._run([_mapper_result(label_pairs.MapperOutcome.DECLINED, reason="no channel")])
        self.assertEqual(rec["call_label"], label_pairs.LABEL_THEME_REFUSED)
        self.assertEqual(rec["no_candidates_reason"], "no channel")

    def test_failure_is_retried_then_succeeds(self):
        rec = self._run(
            [
                _mapper_result(label_pairs.MapperOutcome.CALL_FAILED),
                _mapper_result(label_pairs.MapperOutcome.SUCCESS, tickers=("AMD",)),
            ]
        )
        self.assertEqual(rec["call_label"], label_pairs.LABEL_KEPT)
        self.assertEqual(rec["attempt"], 2)

    def test_exhausted_retries_record_unresolved_failure_not_a_label(self):
        fails = [_mapper_result(label_pairs.MapperOutcome.EMPTY_PAYLOAD)] * (
            label_pairs.MAX_ATTEMPTS_PER_SLOT
        )
        rec = self._run(fails)
        self.assertEqual(rec["outcome"], "UNRESOLVED_FAILURE")
        self.assertIsNone(rec["call_label"])


class TestCompletedSlots(unittest.TestCase):
    def test_only_labeled_records_count_as_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls.jsonl"
            log.write_text(
                json.dumps({"pair_id": "p1", "slot": 0, "call_label": "KEPT"})
                + "\n\n"
                + json.dumps({"pair_id": "p1", "slot": 1, "call_label": None})
                + "\n"
            )
            done = label_pairs._completed_slots(log)
            self.assertEqual(set(done), {("p1", 0)})

    def test_missing_log_is_empty(self):
        self.assertEqual(label_pairs._completed_slots(Path("/nonexistent/calls.jsonl")), {})


class TestBuildLabelTable(unittest.TestCase):
    def test_rows_get_pair_aggregates_and_nan_pair_is_no_source_event(self):
        inputs = pd.DataFrame(
            [
                {
                    "brief_date": "2026-06-01",
                    "ticker": "TENB",
                    "theme": "t",
                    "pair_id": "p1",
                    "window": "CLEAN",
                    "source_event_url": "https://news.test/a",
                },
                {
                    "brief_date": "2026-06-01",
                    "ticker": "QUBT",
                    "theme": "t",
                    "pair_id": "p1",
                    "window": "CLEAN",
                    "source_event_url": "https://news.test/a",
                },
                {
                    "brief_date": "2026-06-02",
                    "ticker": "XYZ",
                    "theme": "u",
                    "pair_id": float("nan"),
                    "window": "CLEAN",
                    "source_event_url": None,
                },
            ]
        )
        aggs = {
            "p1": {
                "pair_label": label_pairs.LABEL_KEPT,
                "majority_proposal_set": ["TENB"],
                "n_valid_calls": 5,
                "n_refused_votes": 1,
                "n_kept_votes": 4,
                "unanimous": False,
            }
        }
        table = label_pairs.build_label_table(inputs, aggs)
        self.assertEqual(
            list(table["row_label"]),
            [
                label_pairs.LABEL_KEPT_PROPOSED,
                label_pairs.LABEL_KEPT_ABSENT,
                label_pairs.LABEL_NO_SOURCE_EVENT,
            ],
        )
        self.assertTrue(pd.isna(table["pair_id"].iloc[2]))


class TestMainSmoke(unittest.TestCase):
    def test_end_to_end_with_stubbed_mapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = pd.DataFrame(
                [
                    {
                        "brief_date": "2026-06-01",
                        "ticker": "TENB",
                        "theme": "breach",
                        "pair_id": "breach|https://news.test/a",
                        "window": "CLEAN",
                        "rank_in_day": 1,
                        "source_event_url": "https://news.test/a",
                        "source_event_title": "T",
                        "source_event_published_at": "2026-06-01",
                        "catalyst_event_type": "breach",
                        "event_primary_entities": ["X"],
                        "catalyst_confidence": 0.8,
                        "event_second_order_implications": [],
                        "catalyst_template_id": None,
                        "catalyst_template_facts_json": None,
                    }
                ]
            )
            inputs.to_parquet(root / "inputs.parquet", index=False)
            argv = [
                "stage1_retro_label_pairs.py",
                "--input",
                str(root / "inputs.parquet"),
                "--out",
                str(root / "labels.parquet"),
                "--raw-dir",
                str(root / "raw"),
                "--calls-log",
                str(root / "calls.jsonl"),
                "--provenance-log",
                str(root / "prov.jsonl"),
                "--summary",
                str(root / "summary.json"),
            ]
            declined = _mapper_result(label_pairs.MapperOutcome.DECLINED, reason="no channel")
            with (
                unittest.mock.patch.object(sys, "argv", argv),
                unittest.mock.patch.object(
                    label_pairs.stage1_frozen_v2,
                    "frozen_mapper_config_version",
                    lambda **kw: label_pairs.FROZEN_MCV,
                ),
                unittest.mock.patch.object(
                    label_pairs.stage1_frozen_v2,
                    "propose_candidates_frozen",
                    lambda **kw: dict(declined),
                ),
                unittest.mock.patch.object(
                    label_pairs.OpenRouterClient, "from_env", staticmethod(_FakeClient)
                ),
            ):
                self.assertEqual(label_pairs.main(), 0)
            labels = pd.read_parquet(root / "labels.parquet")
            self.assertEqual(list(labels["pair_label"]), [label_pairs.LABEL_THEME_REFUSED])
            summary = json.loads((root / "summary.json").read_text())
            self.assertEqual(summary["pair_label_distribution"], {"THEME_REFUSED": 1})
            # k=5 slots all journaled
            self.assertEqual(len((root / "calls.jsonl").read_text().splitlines()), 5)


if __name__ == "__main__":
    unittest.main()

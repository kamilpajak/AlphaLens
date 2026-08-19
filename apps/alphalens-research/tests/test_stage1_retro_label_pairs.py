"""Stage-1 retro Phase-1 labeling script — blinding guard + pure-core tests.

Pins, per the pre-registration
(`docs/research/stage1_retro_gate_increment_prereg_2026_08_19.md` §5, §11.1):

* the labeling module contains NO reference to the outcome store
  (`population_ladders`) or outcome columns — labels are generated blind;
* the k=5 majority-vote pair labeling and the row-level label derivation.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

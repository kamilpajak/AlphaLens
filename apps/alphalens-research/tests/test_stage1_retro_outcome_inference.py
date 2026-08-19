"""Stage-1 retro Phase-2/3 inference script — pure-stats tests.

Pins, per the pre-registration
(`docs/research/stage1_retro_gate_increment_prereg_2026_08_19.md` §8, §10):

* the Stage-B labels-hash gate constant matches the committed value;
* winsorization, pair-cluster delta, and the two-way cluster bootstrap
  behave correctly on synthetic data with a known effect;
* the refusal-reason classifier buckets canonical decline phrasings;
* the power helpers are monotone and self-consistent.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage1_retro_outcome_inference.py"
_spec = importlib.util.spec_from_file_location("stage1_retro_outcome_inference", _SCRIPT)
assert _spec and _spec.loader
inference = importlib.util.module_from_spec(_spec)
sys.modules["stage1_retro_outcome_inference"] = inference
_spec.loader.exec_module(inference)

STAGE_B_SHA256 = "c8440d40a0c2751717b62f3a83ef2d245957c2a43cd55fbe3405ccdb25bfb95f"


def _synthetic_rows(effect: float, seed: int, n_pairs_per_leg: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    days = [f"2026-05-{d:02d}" for d in range(19, 29)]
    for leg, label in ((0, inference.REFUSED), (1, inference.KEPT)):
        for p in range(n_pairs_per_leg):
            pair_id = f"{label}_{p}"
            pair_shift = rng.normal(0.0, 0.02)
            for _ in range(rng.integers(1, 4)):
                rows.append(
                    {
                        "pair_id": pair_id,
                        "pair_label": label,
                        "brief_date": days[rng.integers(0, len(days))],
                        "y": leg * effect + pair_shift + rng.normal(0.0, 0.05),
                    }
                )
    return pd.DataFrame(rows)


class TestStageBHashGate(unittest.TestCase):
    def test_labels_sha_constant_matches_stage_b_commit(self):
        self.assertEqual(inference.LABELS_SHA256, STAGE_B_SHA256)


class TestWinsorize(unittest.TestCase):
    def test_clips_extremes_to_quantiles(self):
        values = np.concatenate([np.zeros(98), [10.0, -10.0]])
        out = inference.winsorize(values, pct=0.01)
        self.assertLess(out.max(), 10.0)
        self.assertGreater(out.min(), -10.0)

    def test_interior_values_unchanged(self):
        values = np.linspace(-1.0, 1.0, 101)
        out = inference.winsorize(values, pct=0.01)
        np.testing.assert_allclose(out[5:-5], values[5:-5])

    def test_empty_input_passes_through(self):
        self.assertEqual(inference.winsorize(np.array([])).size, 0)


class TestPairClusterDelta(unittest.TestCase):
    def test_delta_is_difference_of_unweighted_pair_mean_averages(self):
        rows = pd.DataFrame(
            {
                "pair_id": ["a", "a", "b", "c"],
                "pair_label": [
                    inference.KEPT,
                    inference.KEPT,
                    inference.KEPT,
                    inference.REFUSED,
                ],
                "brief_date": ["d1", "d2", "d1", "d1"],
                "y": [0.10, 0.30, 0.40, 0.10],
            }
        )
        delta, pair_means = inference.pair_cluster_delta(rows)
        # KEPT pair means: a=0.20, b=0.40 -> 0.30; REFUSED: c=0.10.
        self.assertAlmostEqual(delta, 0.20)
        self.assertEqual(len(pair_means), 3)


class TestTwoWayClusterBootstrap(unittest.TestCase):
    def test_seed_reproducibility(self):
        rows = _synthetic_rows(effect=0.0, seed=7)
        a = inference.two_way_cluster_bootstrap(rows, n_boot=200, seed=1)
        b = inference.two_way_cluster_bootstrap(rows, n_boot=200, seed=1)
        self.assertEqual(a["p_one_sided_gt0"], b["p_one_sided_gt0"])
        self.assertEqual(a["ci95"], b["ci95"])

    def test_large_positive_effect_gives_small_one_sided_p(self):
        rows = _synthetic_rows(effect=0.10, seed=11)
        boot = inference.two_way_cluster_bootstrap(rows, n_boot=500, seed=2)
        self.assertLess(boot["p_one_sided_gt0"], 0.01)

    def test_null_effect_gives_moderate_p(self):
        rows = _synthetic_rows(effect=0.0, seed=13)
        boot = inference.two_way_cluster_bootstrap(rows, n_boot=500, seed=3)
        self.assertGreater(boot["p_one_sided_gt0"], 0.05)
        self.assertLess(boot["p_one_sided_gt0"], 0.95)

    def test_negative_effect_gives_large_one_sided_p(self):
        rows = _synthetic_rows(effect=-0.10, seed=17)
        boot = inference.two_way_cluster_bootstrap(rows, n_boot=500, seed=4)
        self.assertGreater(boot["p_one_sided_gt0"], 0.99)


class TestRefusalReasonClassifier(unittest.TestCase):
    def test_non_event(self):
        self.assertEqual(
            inference.classify_refusal_reason(
                "The headline is a stock market commentary without any concrete "
                "business development."
            ),
            inference.NON_EVENT,
        )

    def test_direction_filter(self):
        self.assertEqual(
            inference.classify_refusal_reason(
                "The event is adverse for the named company; declining to propose "
                "its victim as a long."
            ),
            inference.DIRECTION,
        )

    def test_no_channel(self):
        self.assertEqual(
            inference.classify_refusal_reason(
                "No transmission channel exists to any U.S.-listed company's revenue or costs."
            ),
            inference.NO_CHANNEL,
        )

    def test_empty_reason_is_other(self):
        self.assertEqual(inference.classify_refusal_reason(None), inference.OTHER)
        self.assertEqual(inference.classify_refusal_reason("  "), inference.OTHER)


class TestMajorityBucket(unittest.TestCase):
    def test_plain_majority_wins(self):
        self.assertEqual(
            inference.majority_bucket(["no_channel", "no_channel", "non_event"]),
            "no_channel",
        )

    def test_tie_breaks_deterministically_alphabetical(self):
        # A 1-1 tie must not depend on Python set iteration order.
        self.assertEqual(inference.majority_bucket(["non_event", "no_channel"]), "no_channel")
        self.assertEqual(inference.majority_bucket(["no_channel", "non_event"]), "no_channel")


class TestPowerHelpers(unittest.TestCase):
    def test_power_monotone_in_n(self):
        p_small = inference.power_one_sided(0.07, 0.15, 10, 15)
        p_large = inference.power_one_sided(0.07, 0.15, 60, 90)
        self.assertLess(p_small, p_large)

    def test_large_effect_power_near_one(self):
        self.assertGreater(inference.power_one_sided(1.0, 0.15, 30, 30), 0.999)

    def test_required_n_reaches_target_power(self):
        n = inference.required_n_per_leg(0.07, 0.15, power=0.80)
        self.assertGreaterEqual(inference.power_one_sided(0.07, 0.15, n, n), 0.80 - 1e-6)
        self.assertLess(inference.power_one_sided(0.07, 0.15, n - 2, n - 2), 0.80)

    def test_required_n_rejects_nonpositive_effect(self):
        with self.assertRaises(ValueError):
            inference.required_n_per_leg(-0.01, 0.15)


def _e2e_fixture(root: Path) -> tuple[Path, Path, Path]:
    """Small but complete study fixture: labels parquet (both windows, both
    legs, every attrition class), matching outcomes CSV, and a calls log."""
    rows = []

    def add(pair, window, label, row_label, ticker, date):
        rows.append(
            {
                "brief_date": date,
                "ticker": ticker,
                "theme": pair.split("|")[0],
                "pair_id": pair,
                "window": window,
                "source_event_url": "https://news.test/" + pair,
                "pair_label": label,
                "row_label": row_label,
            }
        )

    kept, refused = inference.KEPT, inference.REFUSED
    for i, date in enumerate(("2026-05-20", "2026-05-21", "2026-05-22")):
        add(f"k{i}|u", "CLEAN", kept, "KEPT_TICKER_ABSENT", f"KT{i}A", date)
        add(f"k{i}|u", "CLEAN", kept, "KEPT_TICKER_PROPOSED", f"KT{i}B", date)
        add(f"r{i}|u", "CLEAN", refused, refused, f"RT{i}A", date)
        add(f"r{i}|u", "CLEAN", refused, refused, f"RT{i}B", date)
    add("k9|u", "DEV", kept, "KEPT_TICKER_ABSENT", "DKA", "2026-07-01")
    add("k9|u", "DEV", kept, "KEPT_TICKER_ABSENT", "DKB", "2026-07-02")
    add("r9|u", "DEV", refused, refused, "DRA", "2026-07-01")
    add("r9|u", "DEV", refused, refused, "DRB", "2026-07-02")
    add("x1|u", "CLEAN", "INSTRUMENT_FAILURE", "INSTRUMENT_FAILURE", "IFT", "2026-05-20")
    add("k0|u", "CLEAN", kept, "KEPT_TICKER_ABSENT", "ONG", "2026-05-22")  # ongoing
    add("k0|u", "CLEAN", kept, "KEPT_TICKER_ABSENT", "NPL", "2026-05-22")  # nonplannable
    add("e1|u", "EXCLUDED_CLEAN_DAY", refused, refused, "EXC", "2026-05-24")
    labels_path = root / "labels.parquet"
    labels = pd.DataFrame(rows)
    labels.loc[len(labels)] = {
        "brief_date": "2026-05-21",
        "ticker": "NSE",
        "theme": "t",
        "pair_id": None,
        "window": "CLEAN",
        "source_event_url": None,
        "pair_label": "NO_SOURCE_EVENT",
        "row_label": "NO_SOURCE_EVENT",
    }
    labels.to_parquet(labels_path, index=False)

    outcome_rows = []
    for r in labels.to_dict("records"):
        terminal = r["ticker"] not in ("ONG", "NPL")
        excess = None
        if terminal:
            # Vary by pair so each leg has nonzero pair-level spread (the
            # power helpers reject sd == 0); leg means stay 0.06 vs −0.02.
            spread = {"0": -0.01, "1": 0.0, "2": +0.01}.get(str(r["pair_id"])[1], 0.0)
            base = 0.06 if r["pair_label"] == inference.KEPT else -0.02
            excess = base + spread
        outcome_rows.append(
            {
                "brief_date": r["brief_date"],
                "ticker": r["ticker"],
                "terminal": terminal,
                "matured_at": r["brief_date"] if terminal else None,
                "market_excess_return": excess,
                "plannable": r["ticker"] != "NPL",
            }
        )
    outcomes_path = root / "outcomes.csv"
    pd.DataFrame(outcome_rows).to_csv(outcomes_path, index=False)

    calls_path = root / "calls.jsonl"
    reasons = {
        "r0|u": "no transmission channel to any beneficiary",
        "r1|u": "the article is commentary, not an event",
        "r2|u": "adverse for the named company",
    }
    with calls_path.open("w") as fh:
        for pid, reason in reasons.items():
            for _ in range(3):
                fh.write(
                    json.dumps(
                        {
                            "pair_id": pid,
                            "call_label": inference.REFUSED,
                            "no_candidates_reason": reason,
                        }
                    )
                    + "\n"
                )
    return labels_path, outcomes_path, calls_path


class TestMainEndToEnd(unittest.TestCase):
    def _run(self, root: Path, labels: Path, outcomes: Path, calls: Path) -> int:
        out = root / "results.json"
        argv = [
            "--labels",
            str(labels),
            "--outcomes",
            str(outcomes),
            "--calls-log",
            str(calls),
            "--out",
            str(out),
        ]
        return inference.main(argv)

    def test_full_pipeline_writes_consistent_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels, outcomes, calls = _e2e_fixture(root)
            digest = hashlib.sha256(labels.read_bytes()).hexdigest()
            with unittest.mock.patch.object(inference, "LABELS_SHA256", digest):
                self.assertEqual(self._run(root, labels, outcomes, calls), 0)
            results = json.loads((root / "results.json").read_text())
            primary = results["primary_clean"]
            # KEPT leg mean +0.06, REFUSED −0.02 → delta ≈ +0.08 (winsorization
            # of the tiny fixture nudges the extreme rows slightly).
            self.assertAlmostEqual(primary["delta"], 0.08, places=2)
            self.assertAlmostEqual(results["primary_clean_unwinsorized"]["delta"], 0.08, places=9)
            self.assertEqual(primary["legs"][inference.KEPT]["n_pairs"], 3)
            self.assertEqual(primary["legs"][inference.REFUSED]["n_pairs"], 3)
            attrition = results["attrition"]["CLEAN"]
            self.assertEqual(attrition["rows_ongoing_plannable_excluded"], 1)
            self.assertEqual(attrition["rows_nonplannable_never_matured_excluded"], 1)
            self.assertEqual(attrition["rows_instrument_failure"], 1)
            self.assertEqual(attrition["rows_no_source_event"], 1)
            self.assertEqual(results["crowd_out"]["ticker_proposed"], 3)
            self.assertEqual(
                results["refusal_taxonomy"]["pair_majority_counts"],
                {"no_channel": 1, "non_event": 1, "direction_filter": 1},
            )
            self.assertEqual(results["refusal_rate"]["clean_refused"], 3)
            self.assertGreater(results["power_memo"]["required_n_per_leg_full_effect"], 0)

    def test_wrong_labels_hash_refuses_to_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels, outcomes, calls = _e2e_fixture(root)
            # No patch: the real Stage-B constant cannot match this fixture.
            self.assertEqual(self._run(root, labels, outcomes, calls), 1)
            self.assertFalse((root / "results.json").exists())

    def test_non_unique_outcome_join_refuses_to_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels, outcomes, calls = _e2e_fixture(root)
            frame = pd.read_csv(outcomes)
            pd.concat([frame, frame.head(1)]).to_csv(outcomes, index=False)
            digest = hashlib.sha256(labels.read_bytes()).hexdigest()
            with unittest.mock.patch.object(inference, "LABELS_SHA256", digest):
                self.assertEqual(self._run(root, labels, outcomes, calls), 1)


if __name__ == "__main__":
    unittest.main()

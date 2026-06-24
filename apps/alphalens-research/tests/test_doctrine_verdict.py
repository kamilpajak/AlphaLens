"""Tests for the doctrine-verdict gate (wires the pre-registered PASS bars
into executable code, distinct from phase_robust_backtesting.robust_verdict
which only checks offset-phase stability at αt >= 1.5)."""

import json
import tempfile
import unittest
from pathlib import Path

from alphalens_research.backtest.doctrine_verdict import (
    DOCTRINE_G4_NET15_T,
    DOCTRINE_JOINT_T,
    DOCTRINE_PHASE_MEAN_T,
    evaluate_doctrine,
    evaluate_doctrine_from_jsons,
    net_alpha_t_from_audit,
)


def _audit_json(net_t_by_cost: dict[float, float]) -> dict:
    """Minimal run_audit-shaped output with one config per cost arm whose
    aggregated net αt mean is the given value."""
    return {
        "script": "x",
        "rebalance_stride": 2,
        "configs": [
            {
                "config": f"cost={int(c)}bps",
                "n_phases": 2,
                "summary": {"alpha_t_net": {"mean": t, "std": 0.0, "min": t, "max": t, "n": 2}},
                "verdict": "MID",
                "per_phase": [],
            }
            for c, t in net_t_by_cost.items()
        ],
    }


class TestEvaluateDoctrine(unittest.TestCase):
    def test_all_gates_pass_is_PASS(self) -> None:
        v = evaluate_doctrine(
            full_sample_alpha_t=4.0,
            per_phase_alpha_t=[3.0, 2.8, 2.6],
            per_phase_alpha_t_15bps=[2.5, 2.4, 2.3],
            av_pit_passed=True,
        )
        self.assertEqual(v.verdict, "PASS")
        self.assertTrue(all(v.gates.values()))

    def test_joint_below_3_5_but_marginal_band_is_PASS_MARGINAL(self) -> None:
        # full αt in [2.5, 3.5), gates 3/4/5 pass → PASS_MARGINAL per memo §8
        v = evaluate_doctrine(
            full_sample_alpha_t=3.0,
            per_phase_alpha_t=[2.0, 1.5, 1.0],  # mean 1.5 < 2.5 → g2 fails
            per_phase_alpha_t_15bps=[2.5, 2.2, 2.1],
            av_pit_passed=True,
        )
        self.assertEqual(v.verdict, "PASS_MARGINAL")
        self.assertFalse(v.gates["g1_joint_3_5"])

    def test_negative_phase_is_FAIL_even_if_joint_high(self) -> None:
        v = evaluate_doctrine(
            full_sample_alpha_t=4.0,
            per_phase_alpha_t=[5.0, 5.0, -0.1],  # one negative phase → g3 fails
            per_phase_alpha_t_15bps=[3.0, 3.0, 3.0],
            av_pit_passed=True,
        )
        self.assertEqual(v.verdict, "FAIL")
        self.assertFalse(v.gates["g3_per_phase_positive"])

    def test_cost_knockout_15bps_below_2_is_FAIL(self) -> None:
        v = evaluate_doctrine(
            full_sample_alpha_t=4.0,
            per_phase_alpha_t=[3.0, 3.0, 3.0],
            per_phase_alpha_t_15bps=[2.5, 1.9, 2.5],  # one phase < 2.0 → g4 fails
            av_pit_passed=True,
        )
        self.assertEqual(v.verdict, "FAIL")
        self.assertFalse(v.gates["g4_net15_2_0"])

    def test_av_pit_fail_is_FAIL(self) -> None:
        v = evaluate_doctrine(
            full_sample_alpha_t=4.0,
            per_phase_alpha_t=[3.0, 3.0, 3.0],
            per_phase_alpha_t_15bps=[2.5, 2.5, 2.5],
            av_pit_passed=False,
        )
        self.assertEqual(v.verdict, "FAIL")
        self.assertFalse(v.gates["g5_av_pit"])

    def test_empty_per_phase_is_FAIL_not_crash(self) -> None:
        v = evaluate_doctrine(
            full_sample_alpha_t=4.0,
            per_phase_alpha_t=[],
            per_phase_alpha_t_15bps=[],
            av_pit_passed=True,
        )
        self.assertEqual(v.verdict, "FAIL")

    def test_thresholds_are_the_doctrine_bars(self) -> None:
        self.assertEqual(DOCTRINE_JOINT_T, 3.5)
        self.assertEqual(DOCTRINE_PHASE_MEAN_T, 2.5)
        self.assertEqual(DOCTRINE_G4_NET15_T, 2.0)


class TestNetAlphaTFromAudit(unittest.TestCase):
    def test_extracts_mean_net_alpha_t_for_cost_arm(self) -> None:
        audit = _audit_json({5.0: 2.7, 15.0: 2.1})
        self.assertAlmostEqual(net_alpha_t_from_audit(audit, 5.0), 2.7)
        self.assertAlmostEqual(net_alpha_t_from_audit(audit, 15.0), 2.1)

    def test_missing_cost_arm_raises(self) -> None:
        audit = _audit_json({5.0: 2.7})
        with self.assertRaises(KeyError):
            net_alpha_t_from_audit(audit, 15.0)


class TestEvaluateDoctrineFromJsons(unittest.TestCase):
    def test_reads_four_window_jsons_and_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            full = d / "full.json"
            is_ = d / "is.json"
            oos = d / "oos.json"
            fl = d / "fl.json"
            full.write_text(json.dumps(_audit_json({5.0: 4.0, 15.0: 3.0})))
            for p, t5, t15 in ((is_, 3.0, 2.5), (oos, 2.8, 2.4), (fl, 2.6, 2.3)):
                p.write_text(json.dumps(_audit_json({5.0: t5, 15.0: t15})))
            v = evaluate_doctrine_from_jsons(full=full, is_=is_, oos=oos, fl=fl, av_pit_passed=True)
            self.assertEqual(v.verdict, "PASS")
            self.assertAlmostEqual(v.full_sample_alpha_t, 4.0)
            self.assertAlmostEqual(v.phase_mean_alpha_t, (3.0 + 2.8 + 2.6) / 3)


if __name__ == "__main__":
    unittest.main()

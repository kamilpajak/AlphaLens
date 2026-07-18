"""Tests for the doctrine-verdict gate (wires the pre-registered PASS bars
into executable code, distinct from phase_robust_backtesting.robust_verdict
which only checks offset-phase stability at αt >= 1.5)."""

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
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


class TestDoctrineBoundaryHardening(unittest.TestCase):
    """Boundary + logic-inversion tests derived from surviving mutants
    (cosmic-ray sweep). Each pins an exact pre-registered bar or a gate-
    combining branch so a comparison/boolean mutation that would silently flip
    a PASS<->FAIL verdict is caught. evaluate_doctrine IS the capital-deploy-vs-
    kill judgment; a survived mutant here can reclassify a strategy.
    """

    def _passing(
        self,
        *,
        full_sample_alpha_t: float = 4.0,
        per_phase_alpha_t: list[float] | None = None,
        per_phase_alpha_t_15bps: list[float] | None = None,
        av_pit_passed: bool = True,
    ):
        # None sentinels keep the all-pass defaults while allowing an explicit
        # empty list (the empty-per-phase case) to be passed through unchanged.
        return evaluate_doctrine(
            full_sample_alpha_t=full_sample_alpha_t,
            per_phase_alpha_t=[3.0, 3.0, 3.0] if per_phase_alpha_t is None else per_phase_alpha_t,
            per_phase_alpha_t_15bps=(
                [2.5, 2.5, 2.5] if per_phase_alpha_t_15bps is None else per_phase_alpha_t_15bps
            ),
            av_pit_passed=av_pit_passed,
        )

    def test_g1_exactly_at_3_5_bar_passes(self) -> None:
        # >= vs > at the 3.5 joint bar: exactly 3.5 must satisfy g1.
        v = self._passing(full_sample_alpha_t=3.5)
        self.assertTrue(v.gates["g1_joint_3_5"])
        self.assertEqual(v.verdict, "PASS")

    def test_g2_phase_mean_exactly_2_5_bar_passes(self) -> None:
        # >= vs > / != at the 2.5 phase-mean bar: mean exactly 2.5 passes g2.
        v = self._passing(per_phase_alpha_t=[2.5, 2.5, 2.5])
        self.assertTrue(v.gates["g2_phase_mean_2_5"])
        self.assertEqual(v.verdict, "PASS")

    def test_g2_below_2_5_nonempty_fails_not_truthiness(self) -> None:
        # `bool(per_phase) and mean>=2.5`: a non-empty phase list with mean
        # below 2.5 must FAIL g2. Kills `and`->`or` (which would pass g2 on
        # mere non-emptiness) and `>=`->`is not`.
        v = self._passing(per_phase_alpha_t=[2.0, 2.0, 2.0])  # mean 2.0 < 2.5
        self.assertFalse(v.gates["g2_phase_mean_2_5"])
        self.assertEqual(v.verdict, "FAIL")  # full 4.0 not in marginal band

    def test_g3_phase_exactly_zero_fails_strictly_positive(self) -> None:
        # > vs >= at the strictly-positive per-phase bar: a phase of exactly
        # 0.0 must FAIL g3 (0.0 is not > 0.0).
        v = self._passing(per_phase_alpha_t=[3.0, 0.0, 3.0])
        self.assertFalse(v.gates["g3_per_phase_positive"])
        self.assertEqual(v.verdict, "FAIL")

    def test_g4_net15_exactly_2_0_bar_passes(self) -> None:
        # >= vs > at the 2.0 net-15bps bar: a phase of exactly 2.0 passes g4.
        v = self._passing(per_phase_alpha_t_15bps=[2.0, 2.0, 2.0])
        self.assertTrue(v.gates["g4_net15_2_0"])
        self.assertEqual(v.verdict, "PASS")

    def test_empty_per_phase_reports_zero_phase_mean(self) -> None:
        # The `... if per_phase_alpha_t else 0.0` default must be 0.0, not
        # 1.0/-1.0 — phase_mean_alpha_t is reported and feeds the marginal band.
        v = self._passing(per_phase_alpha_t=[])
        self.assertEqual(v.phase_mean_alpha_t, 0.0)

    def test_marginal_band_lower_2_5_inclusive(self) -> None:
        # `2.5 <= full < 3.5`: full exactly 2.5 (g1 fails) with gates 3/4/5
        # passing must be PASS_MARGINAL. Kills `<=`->`<` and `<=`->`!=`.
        v = self._passing(full_sample_alpha_t=2.5)
        self.assertFalse(v.gates["g1_joint_3_5"])
        self.assertEqual(v.verdict, "PASS_MARGINAL")

    def test_marginal_band_upper_3_5_exclusive_not_marginal(self) -> None:
        # full exactly 3.5 with g2 failing must NOT become PASS_MARGINAL: the
        # upper bound is strict (`< 3.5`). Kills `<`->`<=` and the branch
        # `and`->`or` (which would grant MARGINAL on gates 3/4/5 alone).
        v = self._passing(full_sample_alpha_t=3.5, per_phase_alpha_t=[2.0, 2.0, 2.0])
        self.assertFalse(v.gates["g2_phase_mean_2_5"])
        self.assertEqual(v.verdict, "FAIL")

    def test_marginal_band_below_2_5_is_fail_not_marginal(self) -> None:
        # Lower bound is a real comparison, not identity: full below 2.5 with
        # gates 3/4/5 passing must FAIL, never PASS_MARGINAL. Kills `<=`->`is
        # not` (identity is ~always True and would grant MARGINAL below 2.5).
        v = self._passing(full_sample_alpha_t=2.0)
        self.assertEqual(v.verdict, "FAIL")

    def test_fail_reason_names_the_failed_gate_not_the_passing_ones(self) -> None:
        # `[k for k, ok in gates.items() if not ok]`: dropping the `not` would
        # list the PASSING gates. The reason must name the gate that failed.
        v = self._passing(av_pit_passed=False)
        self.assertEqual(v.verdict, "FAIL")
        self.assertIn("g5_av_pit", v.reason)
        self.assertNotIn("g1_joint_3_5", v.reason)  # g1 passed → must not be listed

    def test_verdict_is_frozen(self) -> None:
        v = self._passing()
        with self.assertRaises(FrozenInstanceError):
            v.verdict = "TAMPERED"  # type: ignore[misc]


class TestNetAlphaTFromAuditHardening(unittest.TestCase):
    def test_matches_exact_cost_key_not_lexical_order(self) -> None:
        # `cfg.get("config") == config_key`: an `==`->`<=` mutation would
        # lexically match the wrong arm ("cost=15bps" <= "cost=5bps" is True).
        # 15bps config listed FIRST so a `<=` match would return its value.
        audit = _audit_json({15.0: 9.9, 5.0: 2.7})
        self.assertEqual(audit["configs"][0]["config"], "cost=15bps")  # order guard
        self.assertAlmostEqual(net_alpha_t_from_audit(audit, 5.0), 2.7)

    def test_config_present_but_missing_mean_raises_specific_error(self) -> None:
        # `if not net or "mean" not in net`: an `or`->`and` mutation would skip
        # the guard for a present-but-mean-less net and raise a bare KeyError.
        # Pin the specific message so only the guarded path passes.
        audit = {
            "configs": [
                {"config": "cost=5bps", "summary": {"alpha_t_net": {"std": 0.0}}},
            ]
        }
        with self.assertRaisesRegex(KeyError, "aggregated alpha_t_net.mean"):
            net_alpha_t_from_audit(audit, 5.0)


if __name__ == "__main__":
    unittest.main()

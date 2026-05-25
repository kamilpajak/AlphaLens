"""Unit tests for run_ev_fcff_yield_audit orchestrator (issue #105 H1 follow-up).

Zen review of PR #106 caught a HIGH-severity gap: the experiment-script G4 fix
did not propagate through the orchestrator, which re-derives G4 from stderr
regex. These tests lock the propagation: regex captures new `t-net` tokens,
parser exposes them as `alpha_t_net`, aggregate carries the stat, and
`_evaluate_window_gates` G4 reads the NET mean t-stat (not gross).

Also locks the backwards-compat fallback: legacy logs without the optional
net tokens still parse cleanly (`alpha_t_net` mirrors `alpha_t`) so a
post-merge run against an older experiment-script log is not a hard fail.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.run_ev_fcff_yield_audit as orch  # noqa: E402

_LINE_WITH_NET = (
    "INFO __main__: cost=15bps | n=441 topN=148.3 turn=12.4% | "
    "Sh gross=1.42 net=1.38 | excess gross=4.2% net=3.8% | "
    "α 4F=8.3% t=2.71 | α-net 4F=6.5% t-net=2.10"
)
_LINE_LEGACY = (
    "INFO __main__: cost=5bps | n=441 topN=148.3 turn=12.4% | "
    "Sh gross=1.42 net=1.40 | excess gross=4.2% net=4.0% | "
    "α 4F=8.3% t=2.71"
)


class TestResultLineRegex(unittest.TestCase):
    def test_captures_net_tokens_when_present(self):
        rows = orch._parse_per_cost_rows(_LINE_WITH_NET)
        self.assertIn(15.0, rows)
        r = rows[15.0]
        self.assertAlmostEqual(r["alpha_t"], 2.71)
        self.assertAlmostEqual(r["alpha_t_net"], 2.10)
        self.assertAlmostEqual(r["alpha_ann"], 0.083)
        self.assertAlmostEqual(r["alpha_net_ann"], 0.065)

    def test_falls_back_to_gross_when_net_absent(self):
        """Legacy logs (pre-H1 experiment script) must still parse — alpha_t_net
        mirrors alpha_t so the orchestrator gate logic doesn't KeyError."""
        rows = orch._parse_per_cost_rows(_LINE_LEGACY)
        self.assertIn(5.0, rows)
        r = rows[5.0]
        self.assertAlmostEqual(r["alpha_t"], 2.71)
        self.assertAlmostEqual(r["alpha_t_net"], 2.71)
        self.assertAlmostEqual(r["alpha_ann"], 0.083)
        self.assertAlmostEqual(r["alpha_net_ann"], 0.083)

    def test_captures_nan_t_stat(self):
        """Degenerate regression with t=nan must match the regex so the
        phase is classified as a failure (NaN propagates through mean
        → gate FAIL), not silently dropped (which would shrink the
        denominator and bias the aggregate t-stat upward). Issue #105 L1.
        """
        line = (
            "INFO __main__: cost=5bps | n=10 topN=148.3 turn=12.4% | "
            "Sh gross=1.42 net=1.40 | excess gross=4.2% net=4.0% | "
            "α 4F=8.3% t=nan | α-net 4F=6.5% t-net=nan"
        )
        rows = orch._parse_per_cost_rows(line)
        self.assertIn(5.0, rows)
        self.assertTrue(math.isnan(rows[5.0]["alpha_t"]))
        self.assertTrue(math.isnan(rows[5.0]["alpha_t_net"]))

    def test_captures_inf_t_stat(self):
        """Zero-residual edge: t=inf must also match — should still be
        classified as a failure when aggregated (mean propagates inf and
        threshold comparison would PASS, so detect via separate guard if
        ever observed in practice). Captures the parse path only here."""
        line = (
            "INFO __main__: cost=5bps | n=10 topN=148.3 turn=12.4% | "
            "Sh gross=1.42 net=1.40 | excess gross=4.2% net=4.0% | "
            "α 4F=8.3% t=inf"
        )
        rows = orch._parse_per_cost_rows(line)
        self.assertIn(5.0, rows)
        self.assertTrue(math.isinf(rows[5.0]["alpha_t"]))


class TestAggregateAndGateMatrix(unittest.TestCase):
    def _phase_results(
        self,
        baseline_ts: list[float],
        stress_ts: list[float],
        stress_t_nets: list[float],
    ) -> list[dict]:
        out = []
        for i, (bt, st, stn) in enumerate(zip(baseline_ts, stress_ts, stress_t_nets, strict=True)):
            out.append(
                {
                    "window": "IS",
                    "phase_offset": i,
                    "returncode": 0,
                    "wall_seconds": 1.0,
                    "log_path": "",
                    "out_path": "",
                    "per_cost": {
                        5.0: {
                            "sharpe_gross": 1.4,
                            "sharpe_net": 1.4,
                            "excess_gross_ann": 0.04,
                            "excess_net_ann": 0.04,
                            "alpha_ann": 0.08,
                            "alpha_t": bt,
                            "alpha_net_ann": 0.08,
                            "alpha_t_net": bt,  # at 5bps cost drag is small → ≈ gross
                            "raw_line": "",
                        },
                        15.0: {
                            "sharpe_gross": 1.4,
                            "sharpe_net": 1.3,
                            "excess_gross_ann": 0.04,
                            "excess_net_ann": 0.03,
                            "alpha_ann": 0.08,
                            "alpha_t": st,
                            "alpha_net_ann": 0.06,
                            "alpha_t_net": stn,
                            "raw_line": "",
                        },
                    },
                }
            )
        return out

    def test_aggregate_exposes_alpha_t_net_stats(self):
        phases = self._phase_results([3.6] * 5, [3.5] * 5, [2.1] * 5)
        agg = orch._aggregate_per_phase(phases, cost=15.0)
        self.assertIn("alpha_t_net", agg)
        self.assertAlmostEqual(agg["alpha_t_net"]["mean"], 2.1)
        self.assertAlmostEqual(agg["alpha_t"]["mean"], 3.5)

    def test_g4_reads_net_not_gross(self):
        """Core H1-orchestrator fix: with gross t=3.5 ≥ 2.0 but net t=1.5 < 2.0
        the gate must FAIL. Pre-fix it read gross and PASSED — that was the no-op
        duplicate of G1."""
        phases = self._phase_results(
            baseline_ts=[3.6, 3.7, 3.55, 3.65, 3.5],
            stress_ts=[3.5] * 5,
            stress_t_nets=[1.5] * 5,
        )
        window_block = {
            "baseline_cost_5bps": orch._aggregate_per_phase(phases, cost=5.0),
            "stress_cost_15bps": orch._aggregate_per_phase(phases, cost=15.0),
        }
        verdict = orch._evaluate_window_gates(window_block)
        self.assertFalse(verdict["gates"]["G4_cost_stress_15bps_mean_alpha_t"]["passed"])
        self.assertAlmostEqual(verdict["gates"]["G4_cost_stress_15bps_mean_alpha_t"]["value"], 1.5)

    def test_g1_fails_when_a_phase_is_nan(self):
        """One NaN-t phase among five must propagate through statistics.mean
        and flip G1 to FAIL — pre-L1, the NaN line failed the regex entirely
        and the phase was dropped, inflating the 4-phase mean. Issue #105 L1.
        """
        phases = self._phase_results(
            baseline_ts=[3.6, 3.7, float("nan"), 3.65, 3.5],
            stress_ts=[3.5] * 5,
            stress_t_nets=[2.3] * 5,
        )
        window_block = {
            "baseline_cost_5bps": orch._aggregate_per_phase(phases, cost=5.0),
            "stress_cost_15bps": orch._aggregate_per_phase(phases, cost=15.0),
        }
        verdict = orch._evaluate_window_gates(window_block)
        self.assertFalse(verdict["gates"]["G1_full_sample_alpha_t"]["passed"])

    def test_g4_passes_when_net_exceeds_threshold(self):
        phases = self._phase_results(
            baseline_ts=[3.6] * 5,
            stress_ts=[3.5] * 5,
            stress_t_nets=[2.3] * 5,
        )
        window_block = {
            "baseline_cost_5bps": orch._aggregate_per_phase(phases, cost=5.0),
            "stress_cost_15bps": orch._aggregate_per_phase(phases, cost=15.0),
        }
        verdict = orch._evaluate_window_gates(window_block)
        self.assertTrue(verdict["gates"]["G4_cost_stress_15bps_mean_alpha_t"]["passed"])


class TestOverallVerdict(unittest.TestCase):
    """Joint-PASS rule per memo §8: every window in {IS, OOS, FL} must
    individually clear the gate matrix. Mixed PASS/PASS_MARGINAL collapses
    to PASS_MARGINAL; any FAIL collapses to FAIL. Issue #105 M1.
    """

    @staticmethod
    def _w(name: str, verdict: str) -> dict:
        return {"window_name": name, "verdict": verdict}

    def test_all_pass_collapses_to_pass(self):
        blocks = [self._w(n, "PASS") for n in ("IS", "OOS", "FL")]
        out = orch._overall_verdict(blocks)
        self.assertEqual(out["overall_verdict"], "PASS")
        self.assertEqual(out["per_window_verdicts"], {"IS": "PASS", "OOS": "PASS", "FL": "PASS"})
        self.assertIn("rule", out)

    def test_all_pass_marginal_collapses_to_pass_marginal(self):
        blocks = [self._w(n, "PASS_MARGINAL") for n in ("IS", "OOS", "FL")]
        self.assertEqual(orch._overall_verdict(blocks)["overall_verdict"], "PASS_MARGINAL")

    def test_mixed_pass_and_pass_marginal_collapses_to_pass_marginal(self):
        blocks = [
            self._w("IS", "PASS"),
            self._w("OOS", "PASS_MARGINAL"),
            self._w("FL", "PASS"),
        ]
        self.assertEqual(orch._overall_verdict(blocks)["overall_verdict"], "PASS_MARGINAL")

    def test_any_fail_window_collapses_to_fail(self):
        blocks = [
            self._w("IS", "PASS"),
            self._w("OOS", "PASS"),
            self._w("FL", "FAIL"),
        ]
        self.assertEqual(orch._overall_verdict(blocks)["overall_verdict"], "FAIL")

    def test_all_fail_collapses_to_fail(self):
        blocks = [self._w(n, "FAIL") for n in ("IS", "OOS", "FL")]
        out = orch._overall_verdict(blocks)
        self.assertEqual(out["overall_verdict"], "FAIL")
        self.assertEqual(out["per_window_verdicts"], {"IS": "FAIL", "OOS": "FAIL", "FL": "FAIL"})

    def test_unknown_verdict_token_treated_as_fail(self):
        """A future verdict value outside {PASS, PASS_MARGINAL, FAIL} (e.g.
        an upstream API drift) must NOT silently collapse to PASS. The
        current implementation falls through to FAIL — lock that contract."""
        blocks = [
            self._w("IS", "PASS"),
            self._w("OOS", "UNKNOWN_VERDICT"),
            self._w("FL", "PASS"),
        ]
        self.assertEqual(orch._overall_verdict(blocks)["overall_verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()

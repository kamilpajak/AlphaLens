"""Unit tests for the insider_pc_compound custom audit orchestrator.

Focused on the load-bearing pure functions:
- ``_phase_command`` MUST inject ``--rebalance-stride 21`` (memo §3.1 lock).
- ``_classify_verdict`` MUST apply memo §5.1 verdict matrix exactly.

The orchestrator wraps the experiment script in a 5-phase parallel sweep
with synchronous block-bootstrap (memo §5.4); the bootstrap helper is
covered by the form4 sibling test (it's literal byte-equivalent reuse).
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import run_insider_pc_compound_audit as orch  # noqa: E402


class TestPhaseCommandLocksStride(unittest.TestCase):
    """The CLI passed to the experiment subprocess must HARD-LOCK stride
    at 21 — the bug we're fixing was the audit driver passing 5 instead.
    """

    def test_cmd_includes_rebalance_stride_21(self):
        cmd = orch._phase_command(
            phase_offset=0,
            returns_parquet=Path("/tmp/r.parquet"),
            report_md=Path("/tmp/r.md"),
            is_start=date(2018, 1, 1),
            is_end=date(2023, 12, 31),
            skip_precheck=True,
        )
        self.assertIn("--rebalance-stride", cmd)
        idx = cmd.index("--rebalance-stride")
        self.assertEqual(cmd[idx + 1], "21", "orchestrator must hard-lock stride=21")

    def test_cmd_never_passes_other_stride(self):
        for phase in range(orch.N_PHASES):
            cmd = orch._phase_command(
                phase_offset=phase,
                returns_parquet=Path(f"/tmp/r{phase}.parquet"),
                report_md=Path(f"/tmp/r{phase}.md"),
                is_start=date(2018, 1, 1),
                is_end=date(2023, 12, 31),
                skip_precheck=True,
            )
            # Find every --rebalance-stride occurrence and verify only 21
            indices = [i for i, a in enumerate(cmd) if a == "--rebalance-stride"]
            self.assertEqual(len(indices), 1, f"phase {phase}: stride passed != 1 time")
            self.assertEqual(cmd[indices[0] + 1], "21")

    def test_phase_offset_propagates(self):
        for phase in (0, 1, 2, 3, 4):
            cmd = orch._phase_command(
                phase_offset=phase,
                returns_parquet=Path("/tmp/r.parquet"),
                report_md=Path("/tmp/r.md"),
                is_start=date(2018, 1, 1),
                is_end=date(2023, 12, 31),
                skip_precheck=True,
            )
            idx = cmd.index("--phase-offset")
            self.assertEqual(cmd[idx + 1], str(phase))

    def test_skip_precheck_flag_passthrough(self):
        cmd_skip = orch._phase_command(
            phase_offset=0,
            returns_parquet=Path("/tmp/r.parquet"),
            report_md=Path("/tmp/r.md"),
            is_start=date(2018, 1, 1),
            is_end=date(2023, 12, 31),
            skip_precheck=True,
        )
        self.assertIn("--skip-precheck", cmd_skip)

        cmd_no_skip = orch._phase_command(
            phase_offset=0,
            returns_parquet=Path("/tmp/r.parquet"),
            report_md=Path("/tmp/r.md"),
            is_start=date(2018, 1, 1),
            is_end=date(2023, 12, 31),
            skip_precheck=False,
        )
        self.assertNotIn("--skip-precheck", cmd_no_skip)


class TestPreRegConstants(unittest.TestCase):
    """Lock the orchestrator's own constants against the memo."""

    def test_n_phases_is_5(self):
        # Memo §5.1: 5 phase offsets.
        self.assertEqual(orch.N_PHASES, 5)

    def test_rebalance_stride_days_is_21(self):
        # Memo §3.1 + §4: locked monthly.
        self.assertEqual(orch.REBALANCE_STRIDE_DAYS, 21)

    def test_bonferroni_threshold_is_2_974(self):
        self.assertAlmostEqual(orch.G1_BONFERRONI_T, 2.974, places=3)

    def test_block_size_is_126(self):
        # Memo §5.4: Romano-Wolf bootstrap block_size=126.
        self.assertEqual(orch.BLOCK_SIZE_TRADING_DAYS, 126)

    def test_n_bootstrap_is_1000(self):
        self.assertEqual(orch.N_BOOTSTRAP, 1000)


class TestVerdictMatrix(unittest.TestCase):
    """Apply each row of memo §5.1's matrix to ``_classify_verdict`` and
    assert the right label is returned. These are pure-function tests
    against the verdict logic, no subprocess invocation needed.
    """

    @staticmethod
    def _gates(
        *,
        mean_t: float,
        every_phase_pass_15: bool,
        every_phase_ge_0: bool,
        excess_net_ge_0: bool,
        dispersion_pp: float,
    ) -> dict:
        return {
            "G1_pooled_alpha_t_mean": mean_t,
            "G2_every_phase_pass_15": every_phase_pass_15,
            "G2_every_phase_ge_0": every_phase_ge_0,
            "G3_excess_net_mean_passed": excess_net_ge_0,
            "G4_dispersion_pp": dispersion_pp,
        }

    def test_pass_when_all_gates_clear_bonferroni(self):
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=3.5,
                every_phase_pass_15=True,
                every_phase_ge_0=True,
                excess_net_ge_0=True,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "PASS")

    def test_pass_marginal_in_band_with_nonnegative_phases(self):
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=2.71,
                every_phase_pass_15=False,
                every_phase_ge_0=True,
                excess_net_ge_0=True,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "PASS_MARGINAL")

    def test_inconclusive_in_band_but_one_phase_negative(self):
        # Memo §5.1: "mean αt ∈ [2.50, 2.974) AND ≥1 phase αt < 0 → INCONCLUSIVE"
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=2.71,
                every_phase_pass_15=False,
                every_phase_ge_0=False,
                excess_net_ge_0=True,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "INCONCLUSIVE")

    def test_inconclusive_when_dispersion_blows_up(self):
        # Memo §5.1: "(dispersion > 70pp AND mean ≥ 2.50) → INCONCLUSIVE"
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=3.5,
                every_phase_pass_15=True,
                every_phase_ge_0=True,
                excess_net_ge_0=True,
                dispersion_pp=80.0,
            )
        )
        self.assertEqual(verdict, "INCONCLUSIVE")

    def test_fail_when_mean_below_2_50(self):
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=2.0,
                every_phase_pass_15=False,
                every_phase_ge_0=False,
                excess_net_ge_0=True,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "FAIL")

    def test_fail_when_excess_net_negative_regardless_of_alpha_t(self):
        # Memo §5.1: "mean excess_net_ann < 0 → FAIL"
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=3.5,
                every_phase_pass_15=True,
                every_phase_ge_0=True,
                excess_net_ge_0=False,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "FAIL")

    def test_boundary_at_2_974(self):
        # mean = 2.974 exactly → PASS (>= threshold)
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=2.974,
                every_phase_pass_15=True,
                every_phase_ge_0=True,
                excess_net_ge_0=True,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "PASS")

    def test_boundary_at_2_50_pass_marginal(self):
        # mean = 2.50 exactly → PASS_MARGINAL (>= 2.50 boundary)
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=2.50,
                every_phase_pass_15=False,
                every_phase_ge_0=True,
                excess_net_ge_0=True,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "PASS_MARGINAL")

    def test_just_below_2_50_is_fail(self):
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=2.499,
                every_phase_pass_15=False,
                every_phase_ge_0=True,
                excess_net_ge_0=True,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "FAIL")

    def test_high_mean_weak_phase_yields_pass_marginal_not_inconclusive(self):
        """Zen 2026-05-11 HIGH catch — memo gap.

        (mean ≥ 2.974, every-phase ≥ 0 but NOT every-phase ≥ 1.5,
        dispersion + excess_net OK): memo §5.1 PASS_MARGINAL row
        strictly bounds mean ∈ [2.50, 2.974), so this case is
        unclassified by the literal matrix. Operationally it should
        not be penalised below PASS_MARGINAL — the signal is stronger
        than the PASS_MARGINAL band requires, only the strict per-phase
        floor for PASS is missed. _classify_verdict therefore widens
        PASS_MARGINAL's lower-bound-only check (PASS is checked first,
        so anything reaching this branch has already failed PASS).
        Tracked as memo amendment in postmortem 2026-05-11.
        """
        verdict, _ = orch._classify_verdict(
            self._gates(
                mean_t=3.0,
                every_phase_pass_15=False,  # e.g. one phase at αt=1.0
                every_phase_ge_0=True,
                excess_net_ge_0=True,
                dispersion_pp=20.0,
            )
        )
        self.assertEqual(verdict, "PASS_MARGINAL")


class TestBootstrapEmitsHacLtRatio(unittest.TestCase):
    """Memo §7 risk #7: audit output MUST surface HAC L/T ratio so
    downstream consumers can flag undersized windows. Zen 2026-05-11
    MEDIUM catch — synth-validate via direct construction (no subprocess).
    """

    def test_bootstrap_dict_includes_hac_lt_ratio_and_n_obs(self):
        # Synthesize 5 phases × 200 days of returns, factors aligned.
        import numpy as np
        import pandas as pd

        rng = np.random.default_rng(42)
        idx = pd.date_range("2024-01-01", periods=200, freq="B")
        per_phase_rets = [pd.Series(rng.normal(0, 0.01, size=200), index=idx) for _ in range(5)]
        factors = pd.DataFrame(
            {
                "Mkt-RF": rng.normal(0, 0.01, size=200),
                "SMB": rng.normal(0, 0.005, size=200),
                "HML": rng.normal(0, 0.005, size=200),
                "Mom": rng.normal(0, 0.005, size=200),
                "RF": np.zeros(200),
            },
            index=idx,
        )
        # Tiny rep count to keep test fast; semantics not affected.
        out = orch._synchronous_bootstrap_pooled_alpha_t(
            per_phase_rets,
            factors,
            n_reps=10,
            block_size_trading_days=50,
            hac_maxlags=60,
        )
        self.assertIn("hac_lt_ratio", out)
        self.assertIn("n_obs_used", out)
        self.assertIn("hac_maxlags", out)
        self.assertEqual(out["n_obs_used"], 200)
        self.assertEqual(out["hac_maxlags"], 60)
        self.assertAlmostEqual(out["hac_lt_ratio"], 60 / 200, places=6)


if __name__ == "__main__":
    unittest.main()

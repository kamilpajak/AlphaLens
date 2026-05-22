"""Pre-reg constant lock for insider_pc_compound_2026_05_10.

Single-purpose guard: if any of the pre-registered constants drift from
their memo'd values, the audit's verdict mathematics are invalidated.
A test failure here means a refactor or debug edit silently changed the
pre-reg lock — STOP and reconcile against design memo before running.

Source of truth: `docs/research/insider_pc_compound_design_2026_05_10.md`
- §4 (architecture table): hac_maxlags = 126
- §3.1: rebalance stride = 21d
- §5.2: Bonferroni threshold |t| >= 2.974 (n=34 with Z5 selection penalty)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import experiment_insider_pc_compound as exp  # noqa: E402


class TestPreRegConstantLock(unittest.TestCase):
    """Lock the three constants that mathematically define the verdict.

    If you intend to change one of these values, the design memo must be
    amended first (and a new pre-reg class registered) — never touch the
    constant in isolation.
    """

    def test_hac_maxlags_lock_is_126(self):
        # Memo §4: 6m signal-window lock inherited from insider_form4 v2.
        # Final-lock window (~567 obs) has L/T=22%; mitigation: Romano-Wolf
        # bootstrap is primary inference per §5.4 + risk #7.
        self.assertEqual(exp._HAC_MAXLAGS_LOCK, 126)

    def test_rebalance_stride_lock_is_21d(self):
        # Memo §3.1: 21d monthly. P/C originally 5d, pinned to 21d for
        # parity with insider_form4.
        self.assertEqual(exp._REBALANCE_STRIDE_LOCK, 21)

    def test_bonferroni_threshold_is_2_974(self):
        # Memo §5.2: scipy.stats.norm.ppf(1 - 0.05/34). n=34 from
        # alpha-class +1 + zen Z5 +6 implicit C(4,2) selection penalty.
        self.assertAlmostEqual(exp._BONFERRONI_THRESHOLD, 2.974, places=3)


class TestEffectiveRebalanceStrideHardLock(unittest.TestCase):
    """Locks the EFFECTIVE rebalance stride at audit time, not just the
    constant. Previously the constant was 21 but the CLI accepted any
    override; the generic `alphalens audit` driver passed
    `--rebalance-stride 5` and ran a 5d cadence audit, silently
    deviating from memo §3.1 / §4. This test makes that path fail
    loudly: passing `--rebalance-stride N` where N != 21 must exit
    nonzero before the engine ever sees the overridden value.

    Discovery: 2026-05-11 audit launch postmortem — 2h pod compute
    wasted on stride-5 OOS phase 0 before the mismatch was caught.
    """

    def _run_main_with_argv(
        self, argv: list[str], *, env_overrides: dict[str, str] | None = None
    ) -> tuple[int, str]:
        """Spawn `experiment_insider_pc_compound.py main()` with synthetic argv.

        Returns (exit_code, stderr). Uses subprocess to keep arg-parsing
        clean — the script's main() uses sys.argv and reads .env on
        import; an in-process call would pollute test state.
        """
        import os
        import subprocess
        import sys

        from alphalens_cli.commands.audit import _SCRIPTS

        script = _SCRIPTS["insider_pc_compound"]
        env = os.environ.copy()
        env["ALPHALENS_WORKERS"] = "1"
        if env_overrides:
            env.update(env_overrides)
        proc = subprocess.run(
            [sys.executable, str(script), *argv],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=60,
        )
        return proc.returncode, proc.stderr

    def test_override_to_5_fails_loud_before_any_compute(self):
        rc, stderr = self._run_main_with_argv(
            [
                "--rebalance-stride",
                "5",
                "--phase-offset",
                "0",
                "--is-start",
                "2019-01-01",
                "--is-end",
                "2019-03-31",
                "--skip-precheck",
                "--universe-size-cap",
                "10",
                "--out",
                "/tmp/preaudit_stride_lock_test.md",
            ]
        )
        self.assertNotEqual(rc, 0, "stride override must exit nonzero")
        self.assertIn(
            "PRE-REG VIOLATION",
            stderr,
            f"expected PRE-REG VIOLATION in stderr; got:\n{stderr[-500:]}",
        )
        self.assertIn("rebalance-stride", stderr)

    def test_override_to_42_fails_loud(self):
        rc, stderr = self._run_main_with_argv(
            [
                "--rebalance-stride",
                "42",
                "--phase-offset",
                "0",
                "--is-start",
                "2019-01-01",
                "--is-end",
                "2019-03-31",
                "--skip-precheck",
                "--universe-size-cap",
                "10",
                "--out",
                "/tmp/preaudit_stride_lock_test.md",
            ]
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("PRE-REG VIOLATION", stderr)

    def test_default_stride_21_passes_guard(self):
        # No --rebalance-stride override → default = _REBALANCE_STRIDE_LOCK = 21.
        # The script will proceed past the guard. It may still fail later
        # (data missing on CI, e.g.) — that's not what this test asserts.
        # We only check: the guard does NOT trip on the locked value.
        rc, stderr = self._run_main_with_argv(
            [
                "--phase-offset",
                "0",
                "--is-start",
                "2019-01-01",
                "--is-end",
                "2019-03-31",
                "--skip-precheck",
                "--universe-size-cap",
                "10",
                "--out",
                "/tmp/preaudit_stride_lock_test.md",
            ]
        )
        # Either rc==0 (full pipeline worked) or rc!=0 but for a DIFFERENT
        # reason (e.g. missing data on CI). What MUST NOT happen: the
        # PRE-REG VIOLATION exit.
        self.assertNotIn("PRE-REG VIOLATION", stderr)

    def test_explicit_stride_21_passes_guard(self):
        rc, stderr = self._run_main_with_argv(
            [
                "--rebalance-stride",
                "21",
                "--phase-offset",
                "0",
                "--is-start",
                "2019-01-01",
                "--is-end",
                "2019-03-31",
                "--skip-precheck",
                "--universe-size-cap",
                "10",
                "--out",
                "/tmp/preaudit_stride_lock_test.md",
            ]
        )
        self.assertNotIn("PRE-REG VIOLATION", stderr)


if __name__ == "__main__":
    unittest.main()

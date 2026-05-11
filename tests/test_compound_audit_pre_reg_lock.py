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


if __name__ == "__main__":
    unittest.main()

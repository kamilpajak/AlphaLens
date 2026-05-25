"""Real-data integration smoke for ``alphalens preaudit``.

Skipped when ``~/.alphalens`` is missing or lacks the expected
strategy data — exactly the situation in CI, where iVolatility SMD
and Form-4 parquet are not synced.

When data is present (e.g. local Mac), this test:

1. Runs the real coverage check against the local ``~/.alphalens``.
2. Runs the smoke subprocess for ``insider_pc_compound`` on the locked
   2019-Q1 fixture window (matches the golden master cap=300 setup).
3. Asserts both stages PASS and the smoke completes within the
   default 5-min wall budget.

This is the ONLY test in the preaudit suite that touches the real
filesystem + spawns a real subprocess. Its job is to prove the
framework actually catches what the unit tests claim it catches.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from alphalens_research.preaudit.coverage import check_all_deps
from alphalens_research.preaudit.profiles import SMOKE_PROFILES, SmokeStatus
from alphalens_research.preaudit.runner import run_smoke

_ALPHALENS_ROOT = Path.home() / ".alphalens"
_INSIDER_PROFILE = SMOKE_PROFILES["insider_pc_compound"]


def _has_required_data() -> bool:
    """Same gate pattern as test_compound_audit_equivalence.py."""
    if not _ALPHALENS_ROOT.is_dir():
        return False
    required = [_ALPHALENS_ROOT / d.name for d in _INSIDER_PROFILE.data_deps]
    return all(p.exists() and any(p.iterdir()) for p in required)


@unittest.skipUnless(_has_required_data(), "Required ~/.alphalens data not available")
class TestPreauditIntegrationInsiderPcCompound(unittest.TestCase):
    """End-to-end smoke for insider_pc_compound on the local Mac environment."""

    def test_coverage_passes_on_local_env(self):
        report = check_all_deps(_INSIDER_PROFILE, root=_ALPHALENS_ROOT)
        if not report.passed:
            # Show each failing dep so it's easy to fix locally.
            lines = [f"{c.dep.name:30s} {c.status.value:13s} {c.detail}" for c in report.failures]
            self.fail("local coverage failures:\n" + "\n".join(lines))

    def test_smoke_subprocess_passes_within_budget(self):
        result = run_smoke("insider_pc_compound", timeout_s=300)
        self.assertEqual(
            result.status,
            SmokeStatus.PASS,
            f"smoke failed: exit={result.exit_code} duration={result.duration_s}\n"
            f"detail:\n{result.detail}",
        )
        self.assertLess(result.duration_s or 0, 300)


if __name__ == "__main__":
    unittest.main()

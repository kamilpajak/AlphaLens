"""Component hash guard for insider_pc_compound_2026_05_10 (memo §7 risk #4).

The compound's per-asof score is mathematically defined by two scorer
modules:
  - alphalens/screeners/insider_activity/opportunistic_form4.py
  - alphalens/screeners/options_volume/pc_abnormal_volume.py

If either drifts during the ~30h pod audit (e.g. a mid-run main-branch
update touches the file), the audit is no longer the pre-registered
test and must not be used as a verdict. The driver fires
`_verify_component_hashes()` on every invocation to fail loudly before
consuming compute.

These tests assert:
  1. Current locked-state passes (no audit invalidation today).
  2. Wrong expected hash raises RuntimeError (guard mechanism works).
  3. Missing component file raises RuntimeError.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import experiment_insider_pc_compound as exp  # noqa: E402


class TestComponentHashGuard(unittest.TestCase):
    def test_locked_hashes_match_current_component_files(self):
        """The hashes pinned in the driver script must match the files
        as they exist on disk *right now*. A failure here means either:
          (a) someone edited a component module without updating the
              lock — the audit IS invalid until lock is restored or
              memo is amended; OR
          (b) the lock in the driver was bumped without committing the
              file change to the same PR — fix the script.
        """
        for rel_path, expected_hash in exp._COMPONENT_LOCKED_HASHES.items():
            file_path = exp.REPO_ROOT / rel_path
            self.assertTrue(file_path.is_file(), f"missing component module: {file_path}")
            actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
            self.assertEqual(
                actual_hash,
                expected_hash,
                f"{rel_path} drifted from locked hash — see memo §7 risk #4",
            )

    def test_verify_passes_on_current_state(self):
        """Calling the guard at run time should not raise on the locked tree."""
        try:
            exp._verify_component_hashes()
        except RuntimeError as e:
            self.fail(f"_verify_component_hashes raised on locked state: {e}")

    def test_drift_raises_runtime_error(self):
        """Inject a wrong expected hash; guard must reject."""
        poisoned = dict(exp._COMPONENT_LOCKED_HASHES)
        first_key = next(iter(poisoned))
        poisoned[first_key] = "0" * 64  # impossible match
        with mock.patch.object(exp, "_COMPONENT_LOCKED_HASHES", poisoned):
            with self.assertRaises(RuntimeError) as ctx:
                exp._verify_component_hashes()
        self.assertIn("PRE-REG VIOLATION", str(ctx.exception))
        self.assertIn(first_key, str(ctx.exception))

    def test_missing_component_file_raises(self):
        """If a locked module is missing, fail loud — no silent skip."""
        ghost = dict(exp._COMPONENT_LOCKED_HASHES)
        ghost["alphalens/screeners/__nonexistent_module__.py"] = "0" * 64
        with mock.patch.object(exp, "_COMPONENT_LOCKED_HASHES", ghost):
            with self.assertRaises(RuntimeError) as ctx:
                exp._verify_component_hashes()
        self.assertIn("PRE-REG GUARD", str(ctx.exception))
        self.assertIn("missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

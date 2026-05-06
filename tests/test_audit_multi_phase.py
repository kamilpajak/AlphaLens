"""Tests for the AlphaLens-side `scripts/audit_multi_phase.py` wrapper.

The wrapper only adds name → path resolution before delegating to
:func:`phase_robust_backtesting.audit_multi_phase.run_audit`. The
parsing/grouping/aggregation logic now lives in the external dep and
is tested there. AlphaLens-specific concern: every entry in the
``_SCRIPTS`` dict must point to a real script in ``scripts/``, and
the OSS ``run_audit`` symbol must remain importable.
"""

from __future__ import annotations

import unittest

from scripts.audit_multi_phase import _SCRIPTS


class TestScriptsDictIntegrity(unittest.TestCase):
    def test_every_strategy_resolves_to_existing_file(self):
        missing = [name for name, path in _SCRIPTS.items() if not path.exists()]
        self.assertEqual(
            missing,
            [],
            f"_SCRIPTS entries pointing to non-existent files: {missing}",
        )

    def test_every_path_lives_under_scripts_dir(self):
        # Defensive: an entry pointing outside scripts/ would break the
        # convention and surprise anyone reading the dict.
        offenders = [
            (name, path) for name, path in _SCRIPTS.items() if path.parent.name != "scripts"
        ]
        self.assertEqual(offenders, [], f"_SCRIPTS entries outside scripts/: {offenders}")

    def test_strategy_keys_are_unique_and_snake_case(self):
        # Convention: snake_case names. Catches accidental dupes from copy-paste.
        self.assertEqual(len(_SCRIPTS), len(set(_SCRIPTS)))
        for name in _SCRIPTS:
            self.assertEqual(name, name.lower(), f"non-lowercase strategy name: {name!r}")
            self.assertNotIn("-", name, f"name should be snake_case: {name!r}")


class TestRunAuditDelegation(unittest.TestCase):
    def test_run_audit_importable(self):
        # Smoke test: the wrapper's reason-to-exist is delegating to this
        # symbol. If the OSS package ever renames it the wrapper breaks
        # silently — catch it here at AlphaLens CI time.
        from phase_robust_backtesting.audit_multi_phase import run_audit

        self.assertTrue(callable(run_audit))


if __name__ == "__main__":
    unittest.main()

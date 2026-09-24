"""The pre-open setup verifier only applies under the builder rule it was built with (#1529).

The verifier re-derives the frozen artefact with the LIVE builder. After a builder rule
change that re-derivation differs by design, so the check must say it does not apply
rather than report a reproduction failure.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "apps/alphalens-research/scripts"))

from verify_pre_open_setups import rule_mismatch  # noqa: E402

_OLD = '{"schema":1}'
_NEW = '{"schema":2}'


def _artefact(*tokens: str) -> dict:
    return {"2026-06-01": {f"T{i}": {"builder_config_version": t} for i, t in enumerate(tokens)}}


class TheVerifierNamesARuleChangeTest(unittest.TestCase):
    def test_an_artefact_built_by_the_live_rule_is_checked(self) -> None:
        self.assertIsNone(rule_mismatch(_artefact(_NEW, _NEW), _NEW))

    def test_an_artefact_built_by_an_older_rule_is_not_applicable(self) -> None:
        message = rule_mismatch(_artefact(_OLD), _NEW)
        self.assertIsNotNone(message)
        self.assertIn(_OLD, message)
        self.assertIn(_NEW, message)

    def test_a_mixed_artefact_is_not_applicable(self) -> None:
        self.assertIsNotNone(rule_mismatch(_artefact(_OLD, _NEW), _NEW))


if __name__ == "__main__":
    unittest.main()

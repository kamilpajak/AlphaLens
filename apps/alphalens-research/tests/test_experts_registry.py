"""Unit tests for the expert registry + a stale-import-path guard.

The registry routes an id to the matching :class:`Expert`; the guard pins that the
``buffett -> experts/buffett`` move left no source file referencing the old import
path (the move's whole point — a half-migrated tree would import-break on main).
"""

from __future__ import annotations

import pathlib
import unittest

from alphalens_pipeline.experts.buffett.expert import BuffettExpert
from alphalens_pipeline.experts.registry import all_experts, expert_ids, get_expert

# .../apps/alphalens-research/tests/<this file> -> repo root is parents[3].
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class TestExpertRegistry(unittest.TestCase):
    def test_get_expert_returns_buffett(self) -> None:
        exp = get_expert("buffett")
        self.assertIsInstance(exp, BuffettExpert)
        self.assertIn("buffett", expert_ids())
        self.assertIn(exp, all_experts())

    def test_get_expert_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_expert("nonexistent_expert")


class TestNoStaleBuffettImportPaths(unittest.TestCase):
    """No source file may reference the OLD ``alphalens_pipeline.<buffett>`` import
    path after the move. The needle is built by concatenation so this test file
    itself does not contain the literal substring it searches for. The CLI command
    module ``alphalens_cli.commands.buffett`` is a different prefix and never
    matches; ``alphalens_pipeline.experts.buffett`` has ``.experts.`` between the
    package and ``buffett`` so it never matches either."""

    def test_no_stale_paths(self) -> None:
        needle = "alphalens_pipeline" + "." + "buffett"
        offenders: list[str] = []
        for path in (_REPO_ROOT / "apps").rglob("*.py"):
            if needle in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
        self.assertEqual(offenders, [], f"stale import paths remain: {offenders}")


if __name__ == "__main__":
    unittest.main()

"""Guard thematic cross-stage imports against private-name reach.

The thematic pipeline is staged: ``verification`` (Layer 3 gates) and
``screening`` (Layer 4 signals) are distinct stages. Shared primitives must
live in a NEUTRAL module both stages import — reaching into a sibling stage's
underscore-prefixed internals couples the stages to each other's private
implementation (the exact anti-pattern ``screening/_common.py`` exists to
prevent).

This test walks ``screening/*`` and fails if any module imports a
private (leading-underscore) name from ``verification.*`` or vice versa.
Public names are allowed (a stage may consume another stage's public API),
but private names crossing the boundary are not.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

# tests/thematic/<file> -> thematic/ -> tests/ -> alphalens-research/ -> apps/ -> repo
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
THEMATIC_DIR = WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline" / "thematic"

# Stage pairs that must not reach into each other's private names. Each entry
# is (consumer_subdir, sibling_stage_module_prefix).
_FORBIDDEN_STAGE_PAIRS = (
    ("screening", "alphalens_pipeline.thematic.verification"),
    ("verification", "alphalens_pipeline.thematic.screening"),
)


def _private_cross_stage_imports(path: Path, sibling_prefix: str) -> list[tuple[str, str]]:
    """Return (module, name) for every private name imported from ``sibling_prefix``."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module != sibling_prefix and not node.module.startswith(sibling_prefix + "."):
            continue
        for alias in node.names:
            if alias.name.startswith("_"):
                out.append((node.module, alias.name))
    return out


class TestThematicStageBoundaries(unittest.TestCase):
    def test_no_private_name_reach_across_stages(self):
        violations: list[str] = []
        for subdir, sibling_prefix in _FORBIDDEN_STAGE_PAIRS:
            stage_dir = THEMATIC_DIR / subdir
            for path in sorted(stage_dir.rglob("*.py")):
                rel = str(path.relative_to(WORKSPACE_ROOT))
                for module, name in _private_cross_stage_imports(path, sibling_prefix):
                    violations.append(f"{rel}: imports private {name!r} from {module}")
        self.assertEqual(
            violations,
            [],
            "private cross-stage imports (move the shared primitive to a "
            "neutral module both stages import):\n  " + "\n  ".join(violations),
        )

    def test_detector_catches_a_known_private_import(self):
        """Positive control: the AST walk must flag a synthetic private import."""
        sample = (
            "from alphalens_pipeline.thematic.verification.insider import "
            "_MemoizedClassifier, has_opportunistic_buy\n"
        )
        tree = ast.parse(sample)
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if alias.name.startswith("_"):
                        found.append(alias.name)
        self.assertEqual(found, ["_MemoizedClassifier"])


if __name__ == "__main__":
    unittest.main()

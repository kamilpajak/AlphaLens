"""One share-quantity precision on the rail, read from one place (issue #1125).

`broker_contract.constants.QTY_PRECISION` is the single definition, aliased as
`broker_contract.contract._QTY_EPS`. Every rail module answers "is this
quantity real?" against it, and three of them import it:

    brokers/reconcile.py, brokers/saxo/broker.py,
    brokers/automanager/position_manager.py

`live_exit_engine.py` used to carry its own `_QTY_EPS = 0.5` with a comment
claiming it mirrored the shared constant. Nothing enforced that, and the copy
was load-bearing: it decided whether a filled position keeps its standalone
disaster stop. Change the shared value and that one site would silently keep
the old one.

Same narrow shape as `test_broker_contract_has_no_vendor_economics` (#1122):
parse rather than grep, so a mention in a docstring or a comment is never
mistaken for a definition, and carry a positive control so the scan cannot rot
to empty. An ALIAS (`_QTY_EPS = QTY_PRECISION`) is not a definition — the whole
point is that a name may be re-bound as long as the VALUE comes from one place.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PIPELINE = WORKSPACE_ROOT / "alphalens-pipeline" / "alphalens_pipeline"
CANONICAL = WORKSPACE_ROOT / "alphalens-broker-contract" / "broker_contract" / "constants.py"

# The names that carry the share-quantity precision on this rail.
PRECISION_NAMES = frozenset({"QTY_PRECISION", "_QTY_EPS"})


def _literal_definitions(path: Path) -> set[str]:
    """Module-level bindings of a precision name to a NUMERIC LITERAL.

    A binding to another name (``_QTY_EPS = QTY_PRECISION``) is an alias, not a
    definition, and is deliberately not reported.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, (int, float)):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in PRECISION_NAMES:
                found.add(target.id)
    return found


class TestOneShareQuantityPrecision(unittest.TestCase):
    def test_scanner_finds_the_canonical_definition(self) -> None:
        # POSITIVE CONTROL. Without it, a wrong path or a parser change would
        # let the real assertion below pass over an empty scan forever.
        self.assertTrue(CANONICAL.is_file(), f"missing {CANONICAL}")
        self.assertIn(
            "QTY_PRECISION",
            _literal_definitions(CANONICAL),
            "the canonical share precision should be a numeric literal here",
        )

    def test_no_pipeline_module_defines_its_own_share_precision(self) -> None:
        self.assertTrue(PIPELINE.is_dir(), f"missing {PIPELINE}")
        offenders: list[str] = []
        for path in sorted(PIPELINE.rglob("*.py")):
            for name in sorted(_literal_definitions(path)):
                offenders.append(f"{path.relative_to(WORKSPACE_ROOT)}: {name}")
        self.assertEqual(
            offenders,
            [],
            "a rail module defines its own share-quantity precision instead of "
            "importing the shared one (issue #1125) — nothing keeps the copies equal",
        )


if __name__ == "__main__":
    unittest.main()

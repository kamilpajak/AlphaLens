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
import tempfile
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PIPELINE = WORKSPACE_ROOT / "alphalens-pipeline" / "alphalens_pipeline"
BROKER_CONTRACT = WORKSPACE_ROOT / "alphalens-broker-contract" / "broker_contract"
TESTS = WORKSPACE_ROOT / "alphalens-research" / "tests"
# Both production trees are scanned. The contract layer is not exempt just
# because it OWNS the constant — a second numeric copy there would be the
# same defect one layer up.
#
# THE TEST TREE IS SCANNED TOO, and that is not tidiness. A copy here does not
# mis-size an order; it decides what the suite can OBSERVE. The acceptance
# `FakeBroker` carried its own `0.5` and deleted any position at or below it,
# so measured on this tree: selling 0.6 of a 1.0-share position — leaving 0.4
# shares with no stop — made the position vanish entirely, indistinguishable
# from a clean close. A fractional rail that sold more than it held would have
# gone GREEN. A false instrument is worse than a missing one, because it
# reports success.
SCAN_ROOTS = (PIPELINE, BROKER_CONTRACT, TESTS)
CANONICAL = WORKSPACE_ROOT / "alphalens-broker-contract" / "broker_contract" / "constants.py"
ALIAS_SITE = WORKSPACE_ROOT / "alphalens-broker-contract" / "broker_contract" / "contract.py"

# The names that carry the share-quantity precision on this rail.
PRECISION_NAMES = frozenset({"QTY_PRECISION", "_QTY_EPS"})


def _value_declarations(path: Path) -> set[str]:
    """Module-level bindings of a precision name that DECLARE a value.

    The rule is stated as an exemption rather than a match, because the ways to
    write ``0.5`` are unbounded while the ways to point at an existing value are
    not. A binding whose right-hand side is a plain name (``_QTY_EPS =
    QTY_PRECISION``) or an attribute (``= constants.QTY_PRECISION``) is an
    ALIAS: the value still comes from one place. Anything else declares one,
    including the forms a narrower literal check would miss:

        _QTY_EPS = 0.5            _QTY_EPS = float("0.5")
        _QTY_EPS = 1.0 / 2.0      _QTY_EPS = 0.25 * 2
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
        if node.value is None or isinstance(node.value, (ast.Name, ast.Attribute)):
            continue  # an alias, not a declaration
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
            _value_declarations(CANONICAL),
            "the canonical share precision should be a numeric literal here",
        )

    def test_scanner_ignores_an_alias(self) -> None:
        # NEGATIVE CONTROL, and the subtlest part of the rule. `contract.py`
        # binds `_QTY_EPS = QTY_PRECISION` — a re-binding of the NAME, which is
        # exactly what the rail is allowed to do. Only re-declaring the VALUE
        # is a defect. Without this, a future "simplification" of the scanner
        # into a plain name check would still pass the two tests above while
        # flagging every legitimate alias.
        self.assertTrue(ALIAS_SITE.is_file(), f"missing {ALIAS_SITE}")
        source = ALIAS_SITE.read_text(encoding="utf-8")
        self.assertIn("_QTY_EPS = QTY_PRECISION", source, "the alias under test moved")
        self.assertEqual(
            _value_declarations(ALIAS_SITE),
            set(),
            "an alias is not a definition and must never be reported",
        )

    def test_the_rule_holds_on_every_way_of_writing_it(self) -> None:
        # The detector's contract, pinned on synthetic sources rather than on
        # whatever the tree happens to contain today. A clean tree is only half
        # the evidence — the other half is knowing which shapes it LACKS, kept
        # here as counterexamples that would break a narrower check.
        declarations = (
            "_QTY_EPS = 0.5",
            "_QTY_EPS = float('0.5')",  # a literal check misses this
            "_QTY_EPS = 1.0 / 2.0",  # and this
            "_QTY_EPS = 0.25 * 2",  # and this
            "_QTY_EPS: float = 0.5",
            "QTY_PRECISION = 0.5",
        )
        aliases_and_noise = (
            "_QTY_EPS = QTY_PRECISION",  # the legal re-binding
            "_QTY_EPS = constants.QTY_PRECISION",  # also legal
            "EPS = 0.5",  # not one of the names we police
            '"""prose mentioning _QTY_EPS = 0.5"""',  # parsed, so not a match
        )
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            for source in declarations:
                probe.write_text(source + "\n", encoding="utf-8")
                self.assertTrue(
                    _value_declarations(probe), f"should be reported as a declaration: {source}"
                )
            for source in aliases_and_noise:
                probe.write_text(source + "\n", encoding="utf-8")
                self.assertEqual(
                    _value_declarations(probe), set(), f"should NOT be reported: {source}"
                )

    def test_the_scan_actually_visits_the_module_that_regressed(self) -> None:
        # The positive control proves the PARSER works; this proves the WALK
        # does. A path recompute that left a root existing but empty would let
        # the assertion below pass over nothing at all and report success
        # forever — the same rot the positive control exists to prevent, one
        # level up.
        scanned = {p.relative_to(WORKSPACE_ROOT) for root in SCAN_ROOTS for p in root.rglob("*.py")}
        for expected in (
            Path("alphalens-pipeline/alphalens_pipeline/brokers/automanager/live_exit_engine.py"),
            Path("alphalens-broker-contract/broker_contract/constants.py"),
            Path("alphalens-research/tests/brokers/automanager/acceptance/fake_broker.py"),
        ):
            self.assertIn(expected, scanned, f"{expected} is not being scanned")

    def test_no_module_declares_its_own_share_precision(self) -> None:
        for root in SCAN_ROOTS:
            self.assertTrue(root.is_dir(), f"missing {root}")
        offenders: list[str] = []
        for root in SCAN_ROOTS:
            for path in sorted(root.rglob("*.py")):
                if path == CANONICAL:
                    continue  # the one place the value is allowed to be declared
                for name in sorted(_value_declarations(path)):
                    offenders.append(f"{path.relative_to(WORKSPACE_ROOT)}: {name}")
        self.assertEqual(
            offenders,
            [],
            "a module declares its own share-quantity precision instead of "
            "importing the shared one (issue #1125) — nothing keeps the copies equal",
        )


if __name__ == "__main__":
    unittest.main()

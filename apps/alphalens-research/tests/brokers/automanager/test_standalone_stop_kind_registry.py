"""A new record kind in ``standalone_stops.jsonl`` cannot reach the real-money
daemon without a compaction decision.

THE HAZARD. ``control_loop._compact_standalone_stop_journal_lines`` runs at every
daemon boot and keeps a fold-identical subset per uic. Its election is a
closed list of kinds; anything else is dropped whole. So the failure shape is:
someone adds a record kind, writes a fold that reads it, and at the next restart
every line of that kind is gone with nothing turning red. Memo G4
(``docs/research/entry_trailing_design_2026_08_12.md``) calls that bug class
CRITICAL, and the decision taken in August 2026 was to route AROUND this
compactor with a separate journal rather than fix it; ``entry_trails``' module
docstring records it as "that bug class must not recur here".

WHY NOTHING ELSE CATCHES IT. ``test_compaction_fold_identity`` is the strongest
test on this compactor, and its randomized battery is driven by a hand-written
``_KINDS`` tuple and a hand-written ``_every_fold``. Both are copies of the
writer set with no gate tying them to it: ``_KINDS`` holds 16 of the 17 kinds
written today (``envelope_clamped`` is absent), so a new kind simply never
appears in a generated journal and the battery stays green on 1500 trials.
``test_journal_snapshots`` tests snapshot mechanics only and never looks at a
kind. The per-uic growth bound is an upper bound: losing a kind lowers the
count, so it cannot fail either.

WHAT THIS GATE DOES. It reads the kinds off the SOURCE rather than off a list
someone maintains: an AST walk over ``alphalens_pipeline`` and ``alphalens_cli``
collects the first argument of every
``stop_journal._append_standalone_stop_journal`` call and resolves its
``"kind"``. An argument it cannot resolve is a FAILURE, not a skip -- a writer
in a shape the walk does not understand must be made visible, never silently
excluded, so a kind assembled at runtime (``f"tier_{n}_event"``) turns this gate
red too, asking for a literal. The resolved set must equal the keys of
``control_loop.STANDALONE_STOP_JOURNAL_KINDS``, and each kind's declared
decision must match what the compactor actually does with a line of that kind.

WHAT IT CANNOT SEE: a line written to this journal by something other than that
one seam. There is no such writer today --
``stop_journal._standalone_stop_journal_path`` is referenced only by the seam
itself and by the compactor that rewrites the file.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any

import alphalens_cli
import alphalens_pipeline
from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from broker_contract.sizing import TpTranchePlan

_WRITER = "_append_standalone_stop_journal"
_KIND_KEY = "kind"
# The resolver follows a builder call, then that builder's own local, then one
# more hop. Three is already one more than any writer needs today; a deeper
# chain should be read by a human, not guessed at.
_MAX_RESOLVE_DEPTH = 3


def _scanned_roots() -> tuple[Path, ...]:
    """``alphalens_pipeline`` PLUS ``alphalens_cli``, the same two-root corpus
    ``test_broker_cli_places_nothing`` walks.

    Every writer sits in ``alphalens_pipeline`` today, and ``alphalens_cli`` is
    the sibling package a future one would land in -- a command that journals a
    hand-driven stop would otherwise be invisible to this gate.
    """
    return (
        Path(alphalens_pipeline.__file__).resolve().parent,
        Path(alphalens_cli.__file__).resolve().parent,
    )


def _pipeline_sources() -> dict[Path, ast.Module]:
    return {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root in _scanned_roots()
        for path in sorted(root.rglob("*.py"))
    }


def _module_level_string_constants(trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """Every module-level ``NAME = "literal"`` in the corpus, by bare name.

    Keyed by the BARE name so ``stop_journal._TRANCHE_PLAN_RETRACTED_KIND`` read
    from ``control_loop`` resolves without import tracking. Values are a SET:
    two modules defining one name differently is an ambiguity the resolver
    reports rather than picking a side.
    """
    constants: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants.setdefault(target.id, set()).add(node.value.value)
    return constants


def _functions_by_name(trees: dict[Path, ast.Module]) -> dict[str, list[ast.FunctionDef]]:
    functions: dict[str, list[ast.FunctionDef]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                functions.setdefault(node.name, []).append(node)
    return functions


class _KindResolver:
    """Resolve one ``_append_standalone_stop_journal`` argument to its kinds.

    Every method returns ``None`` for "cannot tell", which the test turns into
    a failure naming the file and line.
    """

    def __init__(self, trees: dict[Path, ast.Module]) -> None:
        self._constants = _module_level_string_constants(trees)
        self._functions = _functions_by_name(trees)

    def _string(self, node: ast.AST) -> set[str] | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(name, str) and name in self._constants:
            return self._constants[name]
        return None

    def _from_dict(self, node: ast.Dict) -> set[str] | None:
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == _KIND_KEY:
                return self._string(value)
        return None

    @staticmethod
    def _dicts_bound_to(function: ast.FunctionDef, name: str) -> list[ast.Dict]:
        found: list[ast.Dict] = []
        for node in ast.walk(function):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            if not isinstance(getattr(node, "value", None), ast.Dict):
                continue
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                found.append(node.value)  # type: ignore[arg-type]
        return found

    def _from_function_result(self, function: ast.FunctionDef) -> set[str] | None:
        """The kinds of the dicts ``function`` returns, directly or via a local."""
        kinds: set[str] = set()
        for node in ast.walk(function):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            returned: list[ast.Dict] = []
            if isinstance(node.value, ast.Dict):
                returned = [node.value]
            elif isinstance(node.value, ast.Name):
                returned = self._dicts_bound_to(function, node.value.id)
            for literal in returned:
                found = self._from_dict(literal)
                if found is None:
                    return None
                kinds |= found
        return kinds or None

    def resolve(
        self, argument: ast.AST, enclosing: ast.FunctionDef | None, depth: int = 0
    ) -> set[str] | None:
        if depth > _MAX_RESOLVE_DEPTH:
            return None
        if isinstance(argument, ast.Dict):
            return self._from_dict(argument)
        if isinstance(argument, ast.Name) and enclosing is not None:
            kinds: set[str] = set()
            for literal in self._dicts_bound_to(enclosing, argument.id):
                found = self._from_dict(literal)
                if found is None:
                    return None
                kinds |= found
            return kinds or None
        if isinstance(argument, ast.Call):
            callee = argument.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", None)
            if name == "dict" and argument.args:
                return self.resolve(argument.args[0], enclosing, depth + 1)
            kinds = set()
            for function in self._functions.get(str(name), ()):
                found = self._from_function_result(function)
                if found is None:
                    return None
                kinds |= found
            return kinds or None
        return None


def _enclosing_function(tree: ast.Module, call: ast.Call) -> ast.FunctionDef | None:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    walker: ast.AST = call
    while walker in parents:
        walker = parents[walker]
        if isinstance(walker, ast.FunctionDef):
            return walker
    return None


def _writer_call_sites(trees: dict[Path, ast.Module]) -> list[tuple[str, ast.Call, ast.Module]]:
    sites: list[tuple[str, ast.Call, ast.Module]] = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", None)
            if name == _WRITER and node.args:
                sites.append((f"{path.name}:{node.lineno}", node, tree))
    return sites


def _written_kinds() -> tuple[dict[str, list[str]], list[str]]:
    """``(kind -> call sites, unresolvable call sites)``."""
    trees = _pipeline_sources()
    resolver = _KindResolver(trees)
    written: dict[str, list[str]] = {}
    unresolvable: list[str] = []
    for location, call, tree in _writer_call_sites(trees):
        kinds = resolver.resolve(call.args[0], _enclosing_function(tree, call))
        if kinds is None:
            unresolvable.append(location)
            continue
        for kind in kinds:
            written.setdefault(kind, []).append(location)
    return written, unresolvable


# --- one minimal line per kind, in the shape its writer emits ------------------

_UIC = 555
_PICK_KEY = "ZZZ:2026-09-09"


def _tranche_plan_line() -> dict[str, Any]:
    return sj._build_tranche_plan_line(
        uic=_UIC,
        tp_tranches=(
            TpTranchePlan(
                tranche_index=0, target_price=20.0, tranche_frac=1.0, r_multiple=1.0, tag="tp1"
            ),
        ),
        reference_qty=5.0,
        stop_price=9.0,
        pick_key=_PICK_KEY,
    )


def _one_line_per_kind() -> dict[str, dict[str, Any]]:
    """One well-formed line per written kind, each the only line of its journal.

    A single-line journal is the discriminator the declaration is checked
    against: it removes every interaction between elections, so a RETAINED kind
    survives on its own and the other two do not.
    """
    return {
        "planned": {
            "kind": "planned",
            "client_request_id": "crid-A",
            "uic": _UIC,
            "side": "SELL",
            "stop_price": 9.0,
            "take_profit": 20.0,
            "tier_index": 0,
            "gen": 0,
        },
        "planned_retracted": {
            "kind": "planned_retracted",
            "client_request_id": "crid-A",
            "uic": _UIC,
        },
        "tranche_plan": _tranche_plan_line(),
        "tranche_plan_retracted": {
            "kind": "tranche_plan_retracted",
            "uic": _UIC,
            "pick_key": _PICK_KEY,
        },
        "tranche_fired": {"kind": "tranche_fired", "uic": _UIC, "tag": "tp1"},
        "stop_placed": {
            "kind": "stop_placed",
            "uic": _UIC,
            "qty": 5.0,
            "order_id": "O1",
            "ref": "bracket-xyz-stop-0",
            "stop_price": 9.0,
            "ts": 100.0,
        },
        "stop_filled": {
            "kind": "stop_filled",
            "uic": _UIC,
            "order_id": "O1",
            "qty": 5.0,
            "avg_price": 9.0,
            "ref": "ZZZ-2026-09-09-entry-t0-stop-0",
            "partial": False,
            "ts": 101.0,
        },
        "trailed": {"kind": "trailed", "uic": _UIC, "level": 12.0, "peak": 14.0, "ts": 110.0},
        "reanchored": {
            "kind": "reanchored",
            "uic": _UIC,
            "avg_price": 10.0,
            "stop_price": 9.2,
            "ts": 111.0,
        },
        "oco_placed": {"kind": "oco_placed", "uic": _UIC, "ts": 112.0},
        "oco_unsupported": {"kind": "oco_unsupported", "uic": _UIC},
        "oco_too_far": {"kind": "oco_too_far", "uic": _UIC, "ts": 113.0},
        "amend_failed": {"kind": "amend_failed", "uic": _UIC, "ts": 114.0},
        "amend_ok": {"kind": "amend_ok", "uic": _UIC, "qty": 5.0, "ts": 115.0},
        "amend_seq": {"kind": "amend_seq", "uic": _UIC, "seq": 3},
        "gen": {"kind": "gen", "uic": _UIC, "gen": 1, "qty": 5.0},
        "envelope_clamped": {
            "kind": "envelope_clamped",
            "uic": _UIC,
            "policy": "never-below-brief-floor",
            "proposed": 8.0,
            "clamped": 9.0,
            "prior_stop": 8.5,
            "avg_price": 10.0,
            "ts": 116.0,
        },
    }


class TheWrittenKindsAreStaticallyEnumerable(unittest.TestCase):
    def test_every_writer_call_site_resolves_to_a_kind(self) -> None:
        written, unresolvable = _written_kinds()
        self.assertEqual(
            unresolvable,
            [],
            "a writer whose kind the gate cannot read: resolve it or extend _KindResolver, "
            "never leave it out -- an invisible writer is the bug this gate exists for",
        )
        self.assertTrue(written, "the walk found no writer at all, so it is measuring nothing")

    def test_the_walk_sees_the_writers_the_project_knows_it_has(self) -> None:
        # Positive control: three kinds whose writers sit in three DIFFERENT
        # modules, so the walk is demonstrably not reading one file.
        written, _ = _written_kinds()
        self.assertIn("stop_journal.py", written["trailed"][0])
        self.assertIn("live_exit_engine.py", written["tranche_fired"][0])
        self.assertIn("placed_geometry.py", written["tranche_plan"][0])


class EveryWrittenKindCarriesACompactionDecision(unittest.TestCase):
    def test_the_declaration_matches_the_writers(self) -> None:
        written, _ = _written_kinds()
        declared = set(cl.STANDALONE_STOP_JOURNAL_KINDS)
        undeclared = {kind: written[kind] for kind in sorted(set(written) - declared)}
        self.assertEqual(
            undeclared,
            {},
            "a kind is written to standalone_stops.jsonl with no compaction decision: add it to "
            "control_loop.STANDALONE_STOP_JOURNAL_KINDS as RETAINED, CONSUMED or DROPPED. "
            "Leaving it out makes the boot compactor eat it silently (memo G4)",
        )
        self.assertEqual(
            sorted(declared - set(written)),
            [],
            "a declared kind no writer emits any more -- drop the row",
        )

    def test_every_decision_is_one_of_the_three(self) -> None:
        allowed = {cl.KIND_RETAINED, cl.KIND_CONSUMED, cl.KIND_DROPPED}
        self.assertEqual(set(cl.STANDALONE_STOP_JOURNAL_KINDS.values()) - allowed, set())

    def test_every_declared_kind_has_a_line_to_check_it_with(self) -> None:
        self.assertEqual(
            sorted(_one_line_per_kind()),
            sorted(cl.STANDALONE_STOP_JOURNAL_KINDS),
            "a declared kind with no fixture is a decision nothing verifies",
        )


class TheDeclaredDecisionIsWhatTheCompactorDoes(unittest.TestCase):
    """The declaration is checked against behaviour, not trusted. Without this
    class a kind could be declared RETAINED while the election never names it,
    which is the #1324 defect written down instead of fixed."""

    def _kept_alone(self, kind: str) -> bool:
        line = _one_line_per_kind()[kind]
        kept = cl._compact_standalone_stop_journal_lines([line])
        return any(k.get("kind") == kind for k in kept)

    def test_a_retained_kind_survives_a_journal_of_its_own(self) -> None:
        for kind, decision in sorted(cl.STANDALONE_STOP_JOURNAL_KINDS.items()):
            if decision != cl.KIND_RETAINED:
                continue
            with self.subTest(kind=kind):
                self.assertTrue(
                    self._kept_alone(kind),
                    f"{kind} is declared RETAINED but the election drops it",
                )

    def test_a_consumed_or_dropped_kind_does_not(self) -> None:
        for kind, decision in sorted(cl.STANDALONE_STOP_JOURNAL_KINDS.items()):
            if decision == cl.KIND_RETAINED:
                continue
            with self.subTest(kind=kind, decision=decision):
                self.assertFalse(
                    self._kept_alone(kind),
                    f"{kind} is declared {decision} but the election keeps it",
                )


if __name__ == "__main__":
    unittest.main()

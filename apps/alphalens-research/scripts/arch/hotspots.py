"""Per-function structure, measured directly rather than read off a dashboard.

The audit needs file-level and function-level shape for the biggest modules,
and the gates already in place cannot supply it:

* SonarCloud's ``S3776`` (cognitive complexity <= 15) is a PER-FUNCTION rule,
  so a file of 11 000 lines whose functions each comply passes it. The Sonar
  job also runs ``continue-on-error: true``, so it blocks nothing.
* ``ruff`` ignores ``PLR0915`` (statements), ``PLR0912`` (branches) and
  ``PLR0911`` (returns) repo-wide, by project choice. Function size is
  therefore ungated.

Cyclomatic complexity here is McCabe's: one, plus one per decision point. The
arithmetic is pinned on hand-worked fixtures in ``tests/arch/test_hotspots.py``,
because a number nobody can recompute by hand is a number the report cannot
defend.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "CallSite",
    "FunctionMetrics",
    "call_sites",
    "measure_source",
    "module_level_mutable_state",
]

_MUTABLE_BUILTIN_CALLS = frozenset({"dict", "list", "set", "defaultdict", "Counter", "deque"})


@dataclass(frozen=True)
class FunctionMetrics:
    qualname: str
    lineno: int
    lines: int
    cyclomatic: int
    parameters: int
    returns: int


@dataclass(frozen=True)
class CallSite:
    qualname: str
    callee: str
    lineno: int


_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def measure_source(source: str) -> list[FunctionMetrics]:
    """Metrics for every function and method, nested ones measured separately."""
    tree = ast.parse(source)
    out: list[FunctionMetrics] = []
    for node, qualname in _walk_named(tree):
        if not isinstance(node, _FunctionNode):
            continue
        out.append(
            FunctionMetrics(
                qualname=qualname,
                lineno=node.lineno,
                lines=(node.end_lineno or node.lineno) - node.lineno + 1,
                cyclomatic=_cyclomatic(node),
                parameters=_parameter_count(node),
                returns=sum(1 for n in _own_body(node) if isinstance(n, ast.Return)),
            )
        )
    return out


def _walk_named(tree: ast.AST, prefix: str = "") -> Iterable[tuple[ast.AST, str]]:
    """Yield every class and function with its dotted qualified name."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, _FunctionNode | ast.ClassDef):
            qualname = f"{prefix}{node.name}"
            yield node, qualname
            yield from _walk_named(node, f"{qualname}.")
        else:
            yield from _walk_named(node, prefix)


def _own_body(node: _FunctionNode) -> Iterable[ast.AST]:
    """Every node inside ``node`` except the bodies of nested functions.

    A nested function is measured in its own right, so folding its decisions
    into the enclosing one would double-count them.
    """
    for child in ast.iter_child_nodes(node):
        yield from _descend(child)


def _descend(node: ast.AST) -> Iterable[ast.AST]:
    yield node
    if isinstance(node, _FunctionNode):
        return  # a nested function is measured in its own right
    # A nested CLASS is descended into: its body executes when the enclosing
    # function runs, so its decisions belong to that function. Its methods are
    # function nodes and so stop the descent above, which keeps them from being
    # counted twice. The production corpus has no instance of this today; the
    # metric is defined this way so it stays right when one appears.
    for child in ast.iter_child_nodes(node):
        yield from _descend(child)


#: Each of these contributes exactly one decision point. Two deliberate
#: absences:
#:
#: * ``else`` — ``if/else`` is one decision, not two.
#: * ``with`` (``ast.withitem``) — entering a context manager is not a branch,
#:   and standard McCabe implementations do not count it. Counting it inflated
#:   every figure slightly against the definition the report claims to use.
_ONE_DECISION = (
    ast.If,
    ast.IfExp,
    ast.While,
    ast.For,
    ast.AsyncFor,
    ast.ExceptHandler,
    ast.Assert,
    ast.match_case,
)


def _cyclomatic(node: _FunctionNode) -> int:
    score = 1
    for child in _own_body(node):
        if isinstance(child, ast.BoolOp):
            # `a and b and c` is two decisions, not one.
            score += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
        elif isinstance(child, _ONE_DECISION):
            score += 1
    return score


def _parameter_count(node: _FunctionNode) -> int:
    args = node.args
    total = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
    total += 1 if args.vararg else 0
    total += 1 if args.kwarg else 0
    return total


def module_level_mutable_state(source: str) -> list[str]:
    """Module-level names bound to a mutable value.

    A tuple or a frozenset is not state, so a detector that keyed on "is a
    collection" would over-report; the control for that is in the tests.
    """
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        targets: Sequence[ast.expr]
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None or not _is_mutable_literal(value):
            continue
        names.extend(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _is_mutable_literal(value: ast.expr) -> bool:
    if isinstance(value, ast.Dict | ast.List | ast.ListComp | ast.DictComp | ast.SetComp):
        return True
    if isinstance(value, ast.Set):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        return name in _MUTABLE_BUILTIN_CALLS
    return False


def call_sites(source: str, *, names: Iterable[str]) -> list[CallSite]:
    """Every call whose callee attribute or name is in ``names``.

    Reading or passing the attribute is not calling it, which is what keeps
    this from reporting ``f = broker.place_bracket`` as an order placement.
    """
    wanted = set(names)
    tree = ast.parse(source)
    found: list[CallSite] = []
    for node, qualname in _walk_named(tree):
        if not isinstance(node, _FunctionNode):
            continue
        for child in _own_body(node):
            if not isinstance(child, ast.Call):
                continue
            callee = _callee_name(child.func)
            if callee in wanted:
                found.append(CallSite(qualname, callee, child.lineno))
    return found


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None

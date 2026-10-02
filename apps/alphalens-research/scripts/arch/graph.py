"""The import graph the architecture audit measures.

Three distinctions are kept, because collapsing any of them changes a published
number in a known direction:

1. **Runtime versus type-only.** An import inside ``if TYPE_CHECKING:`` costs
   nothing at runtime. Counting it inflates coupling, fan-in and the cycle
   count. ``scripts/deadcode_broker.py`` conflates them on purpose — for its
   question ("does anything import this module at all?") that is correct — so
   this module's all-flags view must agree with it exactly, and
   ``tests/arch/test_graph.py`` pins that.
2. **Top-level versus function-scope.** ``PLC0415`` is ignored repo-wide, so a
   lazy import inside a function body is a sanctioned pattern here. It is a
   logical dependency but not an import-time one, and the documented
   ``pipeline -> research`` exceptions are exactly this shape.
3. **Observed versus implicit parent-package edges.** Importing ``pkg.mod``
   does execute ``pkg/__init__.py``, so an edge ``pkg.mod -> pkg`` is real at
   runtime. But adding it turns every package whose ``__init__`` re-exports a
   submodule into a cycle. It is therefore opt-in via
   :meth:`ImportGraph.with_parent_package_edges`, and the audit publishes cycle
   counts from the default view.

Usage::

    from scripts.arch import graph as arch

    g = arch.build_graph(arch.repo_root(), arch.PRODUCTION_ROOTS)
    g.cycles()  # runtime, observed edges only
    g.fan_in()["broker_contract.contract"]
    g.unreachable_from(["alphalens_cli.main"])
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

# Every root production code lives in. Kept identical to
# ``deadcode_broker.PRODUCTION_ROOTS`` so the two tools measure one corpus;
# the parity test in tests/arch/test_graph.py fails if they fork.
PRODUCTION_ROOTS: tuple[str, ...] = (
    "apps/alphalens-broker-contract/broker_contract",
    "apps/intent-replay/intent_replay",
    "apps/alphalens-pipeline/alphalens_pipeline",
    "apps/alphalens-pipeline/alphalens_cli",
    "apps/alphalens-research/alphalens_research",
    "apps/alphalens-research/scripts",
    "apps/alphalens-feedback/alphalens_feedback",
    "apps/alphalens-django",
)

# The test suites, which live apart from the code they cover: one directory in
# the research app holds the tests for every workspace member.
TEST_ROOTS: tuple[str, ...] = (
    "apps/alphalens-research/tests",
    "apps/alphalens-pipeline/tests",
)

_EXCLUDED_DIR_PARTS = ("tests", "migrations", "__pycache__")

_TYPE_CHECKING = "TYPE_CHECKING"


class UnknownRootError(Exception):
    """A configured root does not exist. Never resolved to an empty scan: a
    path typo would otherwise make every number read as a clean zero."""


class UnknownEntryPointError(Exception):
    """An entry point names no discovered module. Never resolved to "nothing is
    reachable": a typo'd root would otherwise report the whole tree as dead."""


class RelativeImportBeyondRootError(Exception):
    """A relative import climbs past its package root. It cannot be resolved to
    a plausible name, so it is an error rather than a silent empty prefix."""


@dataclass(frozen=True)
class RawTarget:
    """A dotted name an import statement can load, with where it was written."""

    name: str
    function_scope: bool
    type_checking: bool


@dataclass(frozen=True)
class Edge:
    """One resolved dependency between two discovered modules."""

    importer: str
    imported: str
    function_scope: bool
    type_checking: bool


def repo_root() -> Path:
    """The repository root, derived from this file's own location."""
    return Path(__file__).resolve().parents[4]


def module_name(path: Path) -> str:
    """Dotted import name: climb while the parent directory is a package.

    Semantics match ``deadcode_broker._module_name`` exactly, including the
    deliberate loudness: a module under a directory with no ``__init__.py``
    gets a short name that no import matches, so it shows up as unreferenced
    rather than quietly resolving to something plausible.
    """
    parts = [] if path.name == "__init__.py" else [path.stem]
    directory = path.parent
    while (directory / "__init__.py").is_file():
        parts.insert(0, directory.name)
        directory = directory.parent
    return ".".join(parts)


def discover_modules(
    root: Path,
    relroots: Iterable[str],
    *,
    include_tests: bool = False,
) -> dict[Path, str]:
    """Map every ``.py`` file under ``relroots`` to its dotted module name."""
    discovered: dict[Path, str] = {}
    for relroot in relroots:
        base = root / relroot
        if not base.is_dir():
            raise UnknownRootError(f"{relroot} is not a directory under {root}")
        for path in sorted(base.rglob("*.py")):
            if not include_tests and _is_test_or_generated(path, root):
                continue
            discovered[path] = module_name(path)
    return discovered


def _is_test_or_generated(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in _EXCLUDED_DIR_PARTS for part in relative.parts[:-1]):
        return True
    return path.name.startswith("test_")


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == _TYPE_CHECKING
    if isinstance(test, ast.Attribute):
        return test.attr == _TYPE_CHECKING
    return False


def _relative_prefix(node: ast.ImportFrom, package: Sequence[str]) -> list[str]:
    keep = len(package) - (node.level - 1)
    if keep < 0:
        raise RelativeImportBeyondRootError(
            f"relative import of level {node.level} from a package of depth {len(package)}"
        )
    base = list(package[:keep])
    if node.module:
        base.extend(node.module.split("."))
    return base


def imported_targets(tree: ast.AST, module: str, *, is_package: bool) -> list[RawTarget]:
    """Every dotted name the imports in ``tree`` can load, with their context.

    Walks the statement tree rather than ``ast.walk`` so each import keeps the
    two facts ``ast.walk`` discards: whether a function body encloses it, and
    whether a ``TYPE_CHECKING`` guard does.
    """
    package = module.split(".") if is_package else module.split(".")[:-1]
    targets: list[RawTarget] = []

    def visit(node: ast.AST, *, function_scope: bool, type_checking: bool) -> None:
        if isinstance(node, ast.Import):
            targets.extend(
                RawTarget(alias.name, function_scope, type_checking) for alias in node.names
            )
            return
        if isinstance(node, ast.ImportFrom):
            base = _relative_prefix(node, package) if node.level else (node.module or "").split(".")
            prefix = ".".join(part for part in base if part)
            # Only `prefix.alias` is recorded, never the bare `prefix`.
            # `from pkg import thing` depends on `pkg.thing` when that is a
            # module, and resolution climbs to `pkg` when it is a symbol — so
            # the bare prefix adds nothing in the symbol case and, in the
            # submodule case, adds exactly the implicit parent-package edge
            # that `with_parent_package_edges` exists to make opt-in.
            targets.extend(
                RawTarget(f"{prefix}.{alias.name}", function_scope, type_checking)
                for alias in node.names
            )
            return
        if isinstance(node, ast.If):
            # Only the taken branch of a TYPE_CHECKING guard is type-only; the
            # else branch runs at runtime, and `if not TYPE_CHECKING:` inverts.
            guarded, inverted = _type_checking_branches(node.test)
            for child in node.body:
                visit(child, function_scope=function_scope, type_checking=type_checking or guarded)
            for child in node.orelse:
                visit(child, function_scope=function_scope, type_checking=type_checking or inverted)
            return
        enclosed = function_scope or isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for child in ast.iter_child_nodes(node):
            visit(child, function_scope=enclosed, type_checking=type_checking)

    for child in ast.iter_child_nodes(tree):
        visit(child, function_scope=False, type_checking=False)
    return targets


def _type_checking_branches(test: ast.expr) -> tuple[bool, bool]:
    """``(body_is_type_only, orelse_is_type_only)`` for an ``if`` condition."""
    if _is_type_checking_guard(test):
        return True, False
    inverted = (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and _is_type_checking_guard(test.operand)
    )
    return False, inverted


def resolve_target(name: str, known: Iterable[str]) -> str | None:
    """The nearest discovered module a dotted name refers to, or ``None``.

    ``from pkg.mod import SYMBOL`` yields ``pkg.mod.SYMBOL``, which is not a
    module; climbing finds ``pkg.mod``. A third-party or stdlib name climbs all
    the way out and resolves to nothing, which is what keeps the graph
    first-party.
    """
    if not name:
        return None
    known_set = known if isinstance(known, set | frozenset) else set(known)
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in known_set:
            return candidate
        parts.pop()
    return None


@dataclass(frozen=True)
class ImportGraph:
    """Discovered modules and the resolved dependencies between them."""

    modules: frozenset[str]
    edges: tuple[Edge, ...]
    parent_package_edges_included: bool = False

    def select(
        self,
        *,
        include_type_checking: bool = False,
        include_function_scope: bool = True,
    ) -> tuple[Edge, ...]:
        """The edges matching one view. Defaults to the runtime view."""
        return tuple(
            edge
            for edge in self.edges
            if (include_type_checking or not edge.type_checking)
            and (include_function_scope or not edge.function_scope)
        )

    def adjacency(self, **view: bool) -> dict[str, frozenset[str]]:
        out: dict[str, set[str]] = {module: set() for module in self.modules}
        for edge in self.select(**view):
            out[edge.importer].add(edge.imported)
        return {module: frozenset(targets) for module, targets in out.items()}

    def fan_in(self, **view: bool) -> dict[str, int]:
        """Static inbound references per module — the count of distinct
        importers. This is NOT a public-API measure: a high count can be a
        popular helper or an ``__init__`` re-export hub, and an API is a
        contract rather than an edge count.
        """
        importers: dict[str, set[str]] = {module: set() for module in self.modules}
        for edge in self.select(**view):
            importers[edge.imported].add(edge.importer)
        return {module: len(seen) for module, seen in importers.items()}

    def fan_out(self, **view: bool) -> dict[str, int]:
        return {module: len(targets) for module, targets in self.adjacency(**view).items()}

    def cycles(self, **view: bool) -> list[tuple[str, ...]]:
        """Strongly connected components of more than one module."""
        adjacency = self.adjacency(**view)
        return _strongly_connected(adjacency)

    def reachable_from(self, entry_points: Iterable[str], **view: bool) -> frozenset[str]:
        adjacency = self.adjacency(**view)
        roots = list(entry_points)
        unknown = [root for root in roots if root not in self.modules]
        if unknown:
            raise UnknownEntryPointError(f"not discovered modules: {sorted(unknown)}")
        seen: set[str] = set()
        pending = list(roots)
        while pending:
            module = pending.pop()
            if module in seen:
                continue
            seen.add(module)
            pending.extend(adjacency.get(module, ()))
        return frozenset(seen)

    def unreachable_from(self, entry_points: Iterable[str], **view: bool) -> frozenset[str]:
        return frozenset(self.modules - self.reachable_from(entry_points, **view))

    def with_parent_package_edges(self) -> ImportGraph:
        """Add the implicit ``pkg.mod -> pkg`` edges.

        Real at runtime — importing a submodule executes every ancestor
        ``__init__`` — but it turns every re-exporting package into a cycle, so
        it is opt-in and never the default for a published cycle count.
        """
        if self.parent_package_edges_included:
            return self
        extra: set[Edge] = set()
        for module in self.modules:
            parts = module.split(".")
            for depth in range(1, len(parts)):
                parent = ".".join(parts[:depth])
                if parent in self.modules:
                    extra.add(Edge(module, parent, function_scope=False, type_checking=False))
        return replace(
            self,
            edges=tuple(sorted(set(self.edges) | extra, key=_edge_sort_key)),
            parent_package_edges_included=True,
        )


def _edge_sort_key(edge: Edge) -> tuple[str, str, bool, bool]:
    return (edge.importer, edge.imported, edge.function_scope, edge.type_checking)


def build_graph(
    root: Path,
    relroots: Iterable[str],
    *,
    include_tests: bool = False,
    extra_modules: Mapping[Path, str] | None = None,
) -> ImportGraph:
    """Discover modules under ``relroots`` and resolve their imports.

    ``extra_modules`` widens the set of names an import may resolve TO without
    adding those files as importers — used to resolve test-file imports against
    production modules.
    """
    discovered = discover_modules(root, relroots, include_tests=include_tests)
    known = set(discovered.values()) | set((extra_modules or {}).values())
    edges: set[Edge] = set()
    for path, module in discovered.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target in imported_targets(tree, module, is_package=path.name == "__init__.py"):
            imported = resolve_target(target.name, known)
            if imported is None or imported == module:
                continue
            edges.add(Edge(module, imported, target.function_scope, target.type_checking))
    return ImportGraph(
        modules=frozenset(discovered.values()),
        edges=tuple(sorted(edges, key=_edge_sort_key)),
    )


def _strongly_connected(adjacency: Mapping[str, frozenset[str]]) -> list[tuple[str, ...]]:
    """Tarjan's algorithm, iterative so a deep graph cannot blow the stack."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[tuple[str, ...]] = []
    counter = 0

    for start in sorted(adjacency):
        if start in index:
            continue
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            node, child_index = work[-1]
            if child_index == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            successors = sorted(adjacency.get(node, ()))
            descended = False
            for position in range(child_index, len(successors)):
                successor = successors[position]
                if successor not in index:
                    work[-1] = (node, position + 1)
                    work.append((successor, 0))
                    descended = True
                    break
                if successor in on_stack:
                    low[node] = min(low[node], index[successor])
            if descended:
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(tuple(sorted(component)))
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return sorted(components)

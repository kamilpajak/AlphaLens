"""The architecture-audit import graph (``scripts/arch/graph.py``).

These tests pin the three distinctions the audit's numbers depend on, because
each one, if collapsed, silently changes a published figure:

* An import inside ``if TYPE_CHECKING:`` is not a runtime dependency. Counting
  it inflates coupling, fan-in and the cycle count.
* An import inside a function body is a sanctioned pattern here (``PLC0415``
  is ignored repo-wide), so it is a logical dependency but not an import-time
  one. Those are different questions and get different answers.
* An implicit edge from ``pkg.mod`` to its parent package ``pkg`` is a
  MODELLING CHOICE, not an observation. With it on, every package whose
  ``__init__`` re-exports a submodule becomes a cycle. It is off by default and
  the test below pins that, because the planning run for this audit counted
  five cycles with it on and three of them were this artifact.

Each distinction carries a positive control: a test that fails if the walker
stops making the distinction, so the suite cannot go quiet by degrading.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from collections.abc import Mapping
from pathlib import Path

from scripts.arch import graph as arch


def _tree(root: Path, files: Mapping[str, str]) -> None:
    """Write a synthetic package tree. Keys are POSIX paths under ``root``."""
    for relpath, source in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


class ModuleNamingTest(unittest.TestCase):
    def test_it_climbs_while_the_parent_directory_is_a_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/sub/__init__.py": "",
                    "pkg/sub/leaf.py": "",
                },
            )
            self.assertEqual(arch.module_name(root / "pkg/sub/leaf.py"), "pkg.sub.leaf")

    def test_a_package_initialiser_names_the_package_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, {"pkg/__init__.py": "", "pkg/sub/__init__.py": ""})
            self.assertEqual(arch.module_name(root / "pkg/sub/__init__.py"), "pkg.sub")

    def test_it_stops_at_a_directory_that_is_not_a_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, {"scripts/tool.py": ""})
            self.assertEqual(arch.module_name(root / "scripts/tool.py"), "tool")


class EdgeClassificationTest(unittest.TestCase):
    """The three distinctions, each with the control that proves it is made."""

    def _graph(self, source: str) -> arch.ImportGraph:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _tree(
            root,
            {
                "pkg/__init__.py": "",
                "pkg/target.py": "",
                "pkg/importer.py": source,
            },
        )
        return arch.build_graph(root, ["pkg"])

    def _edge(self, g: arch.ImportGraph) -> arch.Edge:
        edges = [e for e in g.edges if e.importer == "pkg.importer"]
        self.assertEqual(len(edges), 1, f"expected exactly one edge, got {edges}")
        return edges[0]

    def test_a_module_level_import_is_runtime_and_top_level(self) -> None:
        edge = self._edge(self._graph("from pkg import target\n"))
        self.assertEqual(edge.imported, "pkg.target")
        self.assertFalse(edge.type_checking)
        self.assertFalse(edge.function_scope)

    def test_an_import_under_if_type_checking_is_type_only(self) -> None:
        edge = self._edge(
            self._graph(
                """
                from typing import TYPE_CHECKING

                if TYPE_CHECKING:
                    from pkg import target
                """
            )
        )
        self.assertTrue(edge.type_checking)

    def test_the_dotted_spelling_of_type_checking_is_recognised_too(self) -> None:
        edge = self._edge(
            self._graph(
                """
                import typing

                if typing.TYPE_CHECKING:
                    from pkg import target
                """
            )
        )
        self.assertTrue(edge.type_checking, "typing.TYPE_CHECKING must count as the guard")

    def test_the_else_branch_of_a_type_checking_guard_is_runtime(self) -> None:
        """Positive control: a classifier that keys on the `if` statement rather
        than on the branch would wrongly call this type-only."""
        edge = self._edge(
            self._graph(
                """
                from typing import TYPE_CHECKING

                if TYPE_CHECKING:
                    pass
                else:
                    from pkg import target
                """
            )
        )
        self.assertFalse(edge.type_checking, "the else branch runs at runtime")

    def test_an_import_inside_a_function_is_function_scope_but_still_runtime(self) -> None:
        edge = self._edge(
            self._graph(
                """
                def build():
                    from pkg import target

                    return target
                """
            )
        )
        self.assertTrue(edge.function_scope)
        self.assertFalse(edge.type_checking)

    def test_an_import_nested_deep_inside_a_function_is_still_function_scope(self) -> None:
        """Positive control: a walker that only looks one level down would miss this."""
        edge = self._edge(
            self._graph(
                """
                def outer():
                    if True:
                        with open("x"):
                            from pkg import target

                            return target
                """
            )
        )
        self.assertTrue(edge.function_scope)

    def test_an_import_in_a_method_is_function_scope(self) -> None:
        edge = self._edge(
            self._graph(
                """
                class Builder:
                    def build(self):
                        from pkg import target

                        return target
                """
            )
        )
        self.assertTrue(edge.function_scope)

    def test_a_class_body_import_is_not_function_scope(self) -> None:
        """A class body executes at import time, so it is a top-level cost."""
        edge = self._edge(
            self._graph(
                """
                class Builder:
                    from pkg import target
                """
            )
        )
        self.assertFalse(edge.function_scope)

    def test_an_import_both_lazy_and_type_only_carries_both_flags(self) -> None:
        edge = self._edge(
            self._graph(
                """
                from typing import TYPE_CHECKING

                def build():
                    if TYPE_CHECKING:
                        from pkg import target

                        return target
                """
            )
        )
        self.assertTrue(edge.function_scope)
        self.assertTrue(edge.type_checking)


class ResolutionTest(unittest.TestCase):
    def test_a_target_resolves_to_the_nearest_known_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/target.py": "VALUE = 1",
                    "pkg/importer.py": "from pkg.target import VALUE\n",
                },
            )
            g = arch.build_graph(root, ["pkg"])
            self.assertIn(
                ("pkg.importer", "pkg.target"),
                [(e.importer, e.imported) for e in g.edges],
                "VALUE is a symbol, so the edge must stop at the module that holds it",
            )

    def test_an_unknown_target_creates_no_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, {"pkg/__init__.py": "", "pkg/importer.py": "import json\nimport numpy\n"})
            g = arch.build_graph(root, ["pkg"])
            self.assertEqual([e.imported for e in g.edges], [])

    def test_each_relative_import_form_resolves_to_an_absolute_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/sibling.py": "",
                    "pkg/sub/__init__.py": "",
                    "pkg/sub/leaf.py": "",
                    "pkg/sub/importer.py": """
                        from . import leaf
                        from .leaf import thing
                        from .. import sibling
                        from ..sibling import other
                    """,
                },
            )
            g = arch.build_graph(root, ["pkg"])
            resolved = {e.imported for e in g.edges if e.importer == "pkg.sub.importer"}
            self.assertEqual(resolved, {"pkg.sub.leaf", "pkg.sibling"})

    def test_a_relative_import_past_the_root_is_not_silently_dropped(self) -> None:
        """It must raise, not resolve to something plausible. The pinned gate in
        test_module_dependencies.py made the same choice for the same reason."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, {"pkg/__init__.py": "", "pkg/importer.py": "from ..... import nope\n"})
            with self.assertRaises(arch.RelativeImportBeyondRootError):
                arch.build_graph(root, ["pkg"])


class CycleTest(unittest.TestCase):
    def test_it_finds_a_known_three_module_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/a.py": "from pkg import b\n",
                    "pkg/b.py": "from pkg import c\n",
                    "pkg/c.py": "from pkg import a\n",
                },
            )
            g = arch.build_graph(root, ["pkg"])
            cycles = g.cycles()
            self.assertEqual(len(cycles), 1)
            self.assertEqual(set(cycles[0]), {"pkg.a", "pkg.b", "pkg.c"})

    def test_an_acyclic_tree_reports_no_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/a.py": "from pkg import b\n",
                    "pkg/b.py": "",
                },
            )
            self.assertEqual(arch.build_graph(root, ["pkg"]).cycles(), [])

    def test_a_type_only_edge_does_not_make_a_runtime_cycle(self) -> None:
        """The classic shape: two modules that reference each other's types."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/a.py": "from pkg import b\n",
                    "pkg/b.py": """
                        from typing import TYPE_CHECKING

                        if TYPE_CHECKING:
                            from pkg import a
                    """,
                },
            )
            g = arch.build_graph(root, ["pkg"])
            self.assertEqual(g.cycles(), [], "runtime view must be acyclic")
            self.assertEqual(
                len(g.cycles(include_type_checking=True)),
                1,
                "the control: counting type-only edges DOES manufacture this cycle",
            )

    def test_parent_package_edges_are_off_by_default(self) -> None:
        """A package whose __init__ re-exports a submodule is not a cycle, and
        turning the implicit parent edge on is what makes it look like one. The
        audit's planning run counted five cycles with this on; this test is why
        the published number will not."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "from pkg.leaf import Thing\n",
                    "pkg/leaf.py": "class Thing: ...\n",
                },
            )
            g = arch.build_graph(root, ["pkg"])
            self.assertEqual(g.cycles(), [], "a re-export is not a cycle")
            self.assertEqual(
                len(g.with_parent_package_edges().cycles()),
                1,
                "the control: the implicit parent edge is what manufactures it",
            )


class ReachabilityTest(unittest.TestCase):
    def test_it_reports_a_module_no_entry_point_reaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/main.py": "from pkg import used\n",
                    "pkg/used.py": "",
                    "pkg/orphan.py": "",
                },
            )
            g = arch.build_graph(root, ["pkg"])
            reached = g.reachable_from(["pkg.main"])
            self.assertEqual(set(reached), {"pkg.main", "pkg.used"})
            self.assertEqual(set(g.unreachable_from(["pkg.main"])), {"pkg", "pkg.orphan"})

    def test_reachability_follows_function_scope_imports(self) -> None:
        """A lazily imported module is reached. `PLC0415` is ignored repo-wide,
        so treating a lazy import as no dependency would call live code dead."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/main.py": """
                        def run():
                            from pkg import lazy

                            return lazy
                    """,
                    "pkg/lazy.py": "",
                },
            )
            g = arch.build_graph(root, ["pkg"])
            self.assertIn("pkg.lazy", g.reachable_from(["pkg.main"]))

    def test_a_root_that_names_no_module_is_an_error(self) -> None:
        """Vacuous-pass guard: a typo'd entry point must not quietly report the
        whole tree as unreachable."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, {"pkg/__init__.py": "", "pkg/main.py": ""})
            g = arch.build_graph(root, ["pkg"])
            with self.assertRaises(arch.UnknownEntryPointError):
                g.reachable_from(["pkg.doesnotexist"])


class FanInTest(unittest.TestCase):
    def test_it_counts_distinct_importers_not_import_statements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/target.py": "A = 1\nB = 2\n",
                    "pkg/one.py": "from pkg.target import A\nfrom pkg.target import B\n",
                    "pkg/two.py": "from pkg import target\n",
                },
            )
            g = arch.build_graph(root, ["pkg"])
            self.assertEqual(g.fan_in()["pkg.target"], 2, "two modules, not three statements")

    def test_type_only_importers_are_excluded_from_the_runtime_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/target.py": "",
                    "pkg/runtime.py": "from pkg import target\n",
                    "pkg/typed.py": """
                        from typing import TYPE_CHECKING

                        if TYPE_CHECKING:
                            from pkg import target
                    """,
                },
            )
            g = arch.build_graph(root, ["pkg"])
            self.assertEqual(g.fan_in()["pkg.target"], 1)
            self.assertEqual(g.fan_in(include_type_checking=True)["pkg.target"], 2)


class DiscoveryTest(unittest.TestCase):
    def test_tests_and_migrations_are_excluded_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root,
                {
                    "pkg/__init__.py": "",
                    "pkg/live.py": "",
                    "pkg/tests/__init__.py": "",
                    "pkg/tests/test_live.py": "",
                    "pkg/migrations/__init__.py": "",
                    "pkg/migrations/0001_initial.py": "",
                    "pkg/test_inline.py": "",
                },
            )
            modules = set(arch.discover_modules(root, ["pkg"]).values())
            self.assertEqual(modules, {"pkg", "pkg.live"})

    def test_the_harness_does_not_measure_itself(self) -> None:
        """The harness lives under a production root. Counting its own modules
        made the module count move three times while it was being written, so
        the exclusion is unconditional and pinned here."""
        for include_tests in (False, True):
            modules = set(
                arch.discover_modules(
                    arch.repo_root(), arch.PRODUCTION_ROOTS, include_tests=include_tests
                ).values()
            )
            self.assertNotIn("graph", modules)
            self.assertNotIn("entrypoints", modules)
            self.assertNotIn("hotspots", modules)

    def test_an_unrelated_directory_named_arch_is_still_measured(self) -> None:
        """Positive control: the exclusion is by exact path, not by the name
        `arch`, so a future `alphalens_pipeline/arch/` would be measured."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(
                root, {"pkg/arch/__init__.py": "", "pkg/arch/thing.py": "", "pkg/__init__.py": ""}
            )
            modules = set(arch.discover_modules(root, ["pkg"]).values())
            self.assertIn("pkg.arch.thing", modules)

    def test_discovery_over_the_real_repo_is_not_empty(self) -> None:
        """Anti-rot: a path typo that discovers nothing would make every other
        number on the real tree read as a clean zero."""
        modules = arch.discover_modules(arch.repo_root(), arch.PRODUCTION_ROOTS)
        self.assertGreater(len(modules), 300, "the real tree has hundreds of modules")

    def test_an_unreadable_root_is_an_error_not_an_empty_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(arch.UnknownRootError):
                arch.discover_modules(Path(tmp), ["no/such/dir"])


class ParityWithThePinnedToolTest(unittest.TestCase):
    """``scripts/deadcode_broker.py`` already walks this tree to find modules
    nothing imports, and it is pinned by its own 390-line test. The audit must
    not drift from it: where the two ask the same question they must give the
    same answer, on the real repository, not on a fixture.
    """

    def test_module_naming_agrees_on_every_real_production_file(self) -> None:
        from scripts import deadcode_broker as pinned

        root = arch.repo_root()
        disagreements = []
        for path in arch.discover_modules(root, arch.PRODUCTION_ROOTS):
            mine, theirs = arch.module_name(path), pinned._module_name(path)
            if mine != theirs:
                disagreements.append(f"{path.relative_to(root)}: {mine!r} != {theirs!r}")
        self.assertEqual(disagreements, [], "module naming must not fork")

    def _pinned_importers(self) -> dict[str, set[str]]:
        """``deadcode_broker``'s own importer map, over the same corpus."""
        import ast

        from scripts import deadcode_broker as pinned

        root = arch.repo_root()
        importers: dict[str, set[str]] = {}
        for path, module in arch.discover_modules(root, arch.PRODUCTION_ROOTS).items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name in pinned._imported_names(tree, module, is_package=path.name == "__init__.py"):
                importers.setdefault(name, set()).add(module)
        return importers

    def test_the_unreferenced_verdict_agrees_with_the_pinned_tool(self) -> None:
        """Where the two tools ask the same question they must answer alike.

        The raw target sets differ on purpose: the pinned tool also records the
        bare ``from pkg import ...`` prefix, which is the implicit
        parent-package edge this graph keeps opt-in. That difference can only
        add an importer for a PACKAGE, and the pinned tool excludes packages
        from its verdict — so the verdict itself must match exactly, for every
        production module, not just the broker scope it prints.
        """
        root = arch.repo_root()
        discovered = arch.discover_modules(root, arch.PRODUCTION_ROOTS)
        packages = {module for path, module in discovered.items() if path.name == "__init__.py"}
        graph = arch.build_graph(root, arch.PRODUCTION_ROOTS)
        fan_in = graph.fan_in(include_type_checking=True, include_function_scope=True)
        pinned_importers = self._pinned_importers()

        mine = {m for m in graph.modules if m not in packages and fan_in[m] == 0}
        theirs = {
            m
            for m in graph.modules
            if m not in packages and not pinned_importers.get(m, set()) - {m}
        }
        self.assertEqual(
            sorted(mine ^ theirs), [], f"verdict forked on {len(mine ^ theirs)} modules"
        )

    def test_the_verdict_comparison_can_actually_fail(self) -> None:
        """Positive control for the test above.

        An equality assertion between two sets built from the same tree passes
        trivially if both are empty or if the comparison is vacuous. This pins
        that the corpus really contains unreferenced modules, so the agreement
        above is an agreement about something.
        """
        root = arch.repo_root()
        discovered = arch.discover_modules(root, arch.PRODUCTION_ROOTS)
        packages = {module for path, module in discovered.items() if path.name == "__init__.py"}
        graph = arch.build_graph(root, arch.PRODUCTION_ROOTS)
        fan_in = graph.fan_in(include_type_checking=True, include_function_scope=True)
        unreferenced = {m for m in graph.modules if m not in packages and fan_in[m] == 0}
        self.assertGreater(
            len(unreferenced), 10, "a corpus with no unreferenced module cannot test agreement"
        )


if __name__ == "__main__":
    unittest.main()

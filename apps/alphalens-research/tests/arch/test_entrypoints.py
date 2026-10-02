"""Where production code is actually entered from (``scripts/arch/entrypoints.py``).

Reachability only means something if the root set is complete. An entry point
left out turns live code into a dead-code candidate, and the planning run for
this audit made exactly that mistake: it walked only the CLI and reported 39
unreachable pipeline modules, several of which systemd runs every night
through a research script.

So the root set is derived from the deployment, not from memory, and the
completeness check below is the gate: every ``ExecStart`` in every unit file
either resolves to a Python module or matches a named non-Python shape. A new
unit with a shape nobody taught this module about is reported, not ignored.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts.arch import entrypoints as ep


class SystemdParsingTest(unittest.TestCase):
    def _unit(self, body: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        units = root / "deploy" / "systemd"
        units.mkdir(parents=True)
        (units / "probe.service").write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        return root

    def test_it_joins_a_continuation_line(self) -> None:
        root = self._unit(
            """
            [Service]
            ExecStart=%h/AlphaLens/.venv/bin/python \\
                apps/alphalens-research/scripts/thing.py \\
                --flag 3
            """
        )
        commands = ep.exec_commands(root)
        self.assertEqual(len(commands), 1)
        self.assertIn("scripts/thing.py", commands[0].command)
        self.assertEqual(commands[0].unit, "probe.service")

    def test_it_reads_the_pre_and_post_hooks_too(self) -> None:
        """A unit's ExecStartPost is a second thing it runs. Reading only
        ExecStart would miss the Postgres mirror the edge dashboard depends on."""
        root = self._unit(
            """
            [Service]
            ExecStart=/usr/bin/true
            ExecStartPre=-/usr/bin/docker rm -f box
            ExecStartPost=/usr/bin/docker compose run --rm rebuild-ladder-outcomes
            """
        )
        directives = {c.directive for c in ep.exec_commands(root)}
        self.assertEqual(directives, {"ExecStart", "ExecStartPre", "ExecStartPost"})

    def test_a_python_script_under_apps_resolves_to_its_module(self) -> None:
        root = self._unit(
            """
            [Service]
            ExecStart=%h/AlphaLens/.venv/bin/python apps/alphalens-research/scripts/thing.py
            """
        )
        (root / "apps/alphalens-research/scripts").mkdir(parents=True)
        (root / "apps/alphalens-research/scripts/thing.py").write_text("", encoding="utf-8")
        resolved = ep.resolve(ep.exec_commands(root)[0], root)
        self.assertEqual([e.module for e in resolved], ["thing"])

    def test_an_alphalens_invocation_resolves_to_the_cli_root(self) -> None:
        """``alphalens_cli.main`` imports every command module at top level, so
        one root covers the whole CLI surface however it is invoked."""
        root = self._unit(
            """
            [Service]
            ExecStart=%h/AlphaLens/.venv/bin/alphalens broker manage --poll-seconds 45
            """
        )
        resolved = ep.resolve(ep.exec_commands(root)[0], root)
        self.assertEqual([e.module for e in resolved], [ep.CLI_ROOT])

    def test_a_named_non_python_shape_resolves_to_nothing_without_complaint(self) -> None:
        root = self._unit(
            """
            [Service]
            ExecStart=-/usr/bin/docker rm -f alphalens-thematic-build
            """
        )
        command = ep.exec_commands(root)[0]
        self.assertEqual(ep.resolve(command, root), [])
        self.assertTrue(ep.is_known_non_python(command.command))

    def test_an_unknown_shape_is_reported_not_swallowed(self) -> None:
        """Positive control for the completeness gate."""
        root = self._unit(
            """
            [Service]
            ExecStart=/usr/bin/perl /opt/mystery.pl
            """
        )
        command = ep.exec_commands(root)[0]
        self.assertEqual(ep.resolve(command, root), [])
        self.assertFalse(ep.is_known_non_python(command.command))
        self.assertEqual(len(ep.unresolved(root)), 1)

    def test_a_shell_command_must_resolve_on_its_own_merits(self) -> None:
        """`/bin/sh -c` is not a named non-Python shape.

        A shell command can run anything, so treating it as plumbing would let
        the gate MASK a script this module failed to resolve — the one failure
        the gate exists to prevent. One real unit runs a research script this
        way, so the shape must be resolved, never excused.
        """
        root = self._unit(
            """
            [Service]
            ExecStart=/bin/sh -c 'exec /opt/venv/bin/python /opt/elsewhere/thing.py'
            """
        )
        command = ep.exec_commands(root)[0]
        self.assertFalse(
            ep.is_known_non_python(command.command),
            "a shell command must not be excused by a prefix match",
        )
        self.assertEqual(len(ep.unresolved(root)), 1)

    def test_a_shell_command_naming_a_real_script_resolves(self) -> None:
        root = self._unit(
            """
            [Service]
            ExecStart=/bin/sh -c 'PY=python; S=apps/alphalens-research/scripts/thing.py; "$PY" "$S"'
            """
        )
        (root / "apps/alphalens-research/scripts").mkdir(parents=True)
        (root / "apps/alphalens-research/scripts/thing.py").write_text("", encoding="utf-8")
        self.assertEqual([e.module for e in ep.resolve(ep.exec_commands(root)[0], root)], ["thing"])
        self.assertEqual(ep.unresolved(root), [])

    def test_a_python_script_that_does_not_exist_is_unresolved(self) -> None:
        """A unit naming a deleted script must be loud. It means the deployment
        and the tree disagree."""
        root = self._unit(
            """
            [Service]
            ExecStart=%h/AlphaLens/.venv/bin/python apps/alphalens-research/scripts/gone.py
            """
        )
        self.assertEqual(len(ep.unresolved(root)), 1)


class RealDeploymentTest(unittest.TestCase):
    """The audit's verification gate, run against the real unit files."""

    def setUp(self) -> None:
        self.root = ep.repo_root()

    def test_there_are_unit_files_to_read(self) -> None:
        """Anti-rot: a path change that finds no units would make the
        completeness gate below pass by measuring nothing."""
        self.assertGreater(len(ep.unit_files(self.root)), 15)

    def test_every_exec_start_resolves_or_is_a_named_non_python_shape(self) -> None:
        unresolved = ep.unresolved(self.root)
        self.assertEqual(
            [f"{c.unit}: {c.command}" for c in unresolved],
            [],
            "an unexplained ExecStart means the entry-point set is incomplete, "
            "which turns live code into a dead-code candidate",
        )

    def test_the_entry_point_set_covers_the_cli_the_scripts_and_django(self) -> None:
        modules = {e.module for e in ep.all_entry_points(self.root)}
        self.assertIn(ep.CLI_ROOT, modules)
        self.assertTrue(
            any(m.startswith("config") for m in modules), "Django must contribute a root"
        )
        self.assertTrue(
            any("form4" in m for m in modules),
            "the systemd-run Form-4 scripts must be roots; the planning run missed them",
        )

    def test_a_dunder_main_module_counts_as_an_entry_point(self) -> None:
        """``python -m pkg`` runs ``__main__.py`` and nothing imports it, so
        omitting it reported a live entry point as referenced by nothing."""
        origins = {e.module: e.origin for e in ep.all_entry_points(self.root)}
        self.assertEqual(origins.get("intent_replay.__main__"), "python -m")

    def test_each_entry_point_names_a_discovered_module(self) -> None:
        """A root that names nothing would silently shrink the reachable set."""
        from scripts.arch import graph as arch

        known = set(arch.discover_modules(self.root, arch.PRODUCTION_ROOTS).values())
        stray = sorted(
            {e.module for e in ep.all_entry_points(self.root)} - known,
        )
        self.assertEqual(stray, [], "entry points must be modules the graph discovered")


if __name__ == "__main__":
    unittest.main()

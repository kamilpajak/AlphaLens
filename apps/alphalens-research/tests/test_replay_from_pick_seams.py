"""``scripts/replay_from_pick`` must reach production through its own seams.

Every behavioural test of the fetch path injects a fake, and the no-live-network
guard makes the production default untestable by running it. So a ``main()``
whose ``bar_fetch`` default was left ``None``, or pointed at a private local
copy of the fetcher, would pass every one of those tests and still make no
vendor call -- or the wrong one -- in the operator's hands.

This repository's remedy for that shape is a source gate, the one
``tests/brokers/test_broker_cli_places_nothing.py`` established, in its own
words: a rule a caller declares is a rule a caller can opt out of, while "an
AST walk over every CLI module cannot be opted out of". This gate reads the
module's source; it runs nothing and fakes nothing.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import scripts.replay_from_pick

# The three keyword-only seams a test injects and production defaults.
_SEAMS = ("actions_lookup", "bar_fetch", "now")

_BAR_WINDOW_MODULE = "alphalens_pipeline.feedback.bar_window"
_BAR_FETCH_DEFAULT = "default_bar_fetch"


def _module_ast() -> ast.Module:
    source = pathlib.Path(scripts.replay_from_pick.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def _main_defaults(tree: ast.Module) -> dict[str, ast.expr]:
    """``main``'s keyword-only arguments that carry a default, by name.

    An argument declared without a default is absent from the mapping, so a
    seam that became required reads as missing rather than as wired.
    """
    main = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    pairs = zip(main.args.kwonlyargs, main.args.kw_defaults, strict=True)
    return {arg.arg: default for arg, default in pairs if default is not None}


def _imported_names(tree: ast.Module, module: str) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def _called_name(call: ast.Call) -> str | None:
    """The bare name of what ``call`` calls, attribute access included."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


class TheFetchSeamIsWiredToProductionTest(unittest.TestCase):
    """A seam defaulting to ``None`` is a production path that silently does
    nothing while every injected-fake test stays green."""

    def test_the_bar_fetch_default_is_the_canonical_polygon_seam(self) -> None:
        tree = _module_ast()

        default = _main_defaults(tree)["bar_fetch"]

        self.assertIsInstance(default, ast.Name)
        self.assertEqual(default.id, _BAR_FETCH_DEFAULT)
        self.assertIn(_BAR_FETCH_DEFAULT, _imported_names(tree, _BAR_WINDOW_MODULE))

    def test_the_actions_lookup_default_builds_the_cached_polygon_lookup(self) -> None:
        default = _main_defaults(_module_ast())["actions_lookup"]

        # The cache path is a run value, so the default is a factory; what the
        # gate is about is the composition inside it.
        built = default.body if isinstance(default, ast.Lambda) else default

        self.assertIsInstance(built, ast.Call)
        self.assertEqual(_called_name(built), "CachedCorporateActionsLookup")
        self.assertTrue(built.args, "the disk cache wraps nothing")
        self.assertIsInstance(built.args[0], ast.Call)
        self.assertEqual(_called_name(built.args[0]), "PolygonCorporateActionsLookup")

    def test_no_injected_seam_defaults_to_none(self) -> None:
        """The shape this gate exists to catch: a seam resolved inside the body
        from a ``None``, which every injected-fake test passes straight over."""
        defaults = _main_defaults(_module_ast())

        self.assertEqual(sorted(defaults), sorted(_SEAMS))
        for name, default in sorted(defaults.items()):
            with self.subTest(seam=name):
                self.assertFalse(
                    isinstance(default, ast.Constant) and default.value is None,
                    f"{name} defaults to None, so production would run without it",
                )


if __name__ == "__main__":
    unittest.main()

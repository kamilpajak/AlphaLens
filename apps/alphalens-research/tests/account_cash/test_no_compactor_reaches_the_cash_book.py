"""No compactor can reach the cash book (#1689, memo section 3).

"This journal is never compacted" is a STRUCTURAL claim, so it gets a
structural gate rather than a sentence in a docstring. The reason it matters:
the cash book's whole purpose is that a restated row stays visible as having
changed, and compaction is precisely what destroyed ``trailed``,
``reanchored``, ``stop_filled`` and ``tranche_fired`` lines elsewhere in this
repo before the first snapshot-taking boot. A restatement history that gets
compacted is not a restatement history.

The claim is established by four properties, each one a different way a
compactor could arrive at a path:

1. both compactors take ZERO arguments, so nothing can hand one a directory;
2. each one resolves its journal through the keeper's own ``state_paths``
   builders, whose roots are ``broker_orders/<env>/`` and never
   ``account_cash/<env>/``;
3. ``journal_snapshots.replace_compacted`` -- the one primitive that DOES take
   a path and rewrites a file -- is called from exactly those two compactors
   and nowhere else, so the zero-argument property above is what bounds it;
4. no production module globs for ``*.jsonl`` anywhere, so no compactor can
   discover the cash book's journals by walking the home root.

Stated limit: this is a gate on the SHAPE of the code, not a proof that a
future compactor cannot be written. What it makes impossible is adding one
quietly -- each of the four properties fails loudly on the commit that breaks
it.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from alphalens_pipeline import account_cash
from alphalens_pipeline.account_cash import paths
from alphalens_pipeline.brokers.automanager import control_loop, entry_trails, state_paths

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

#: Every compactor in the tree, as ``(module, function name)``. Property 3
#: below is what keeps this list complete: a third compactor would have to call
#: ``replace_compacted``, and that call site is asserted against this list.
_COMPACTORS = (
    (control_loop, "_compact_standalone_stop_journal"),
    (entry_trails, "compact_entry_trail_journal"),
)

_REPLACE_PRIMITIVE = "replace_compacted"

_PRODUCTION_ROOTS = (
    "apps/alphalens-pipeline/alphalens_pipeline",
    "apps/alphalens-pipeline/alphalens_cli",
    "apps/alphalens-broker-contract/broker_contract",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _production_sources() -> list[Path]:
    found: list[Path] = []
    for relroot in _PRODUCTION_ROOTS:
        found.extend(sorted((_repo_root() / relroot).rglob("*.py")))
    return found


class NoCompactorCanBeHandedAPath(unittest.TestCase):
    def test_both_compactors_were_actually_found(self) -> None:
        """The vacuity control: a scan that found no compactor has tested
        nothing, and the properties below are all about the ones that exist."""
        self.assertEqual(len(_COMPACTORS), 2)
        for module, name in _COMPACTORS:
            with self.subTest(compactor=name):
                self.assertTrue(callable(getattr(module, name)))

    def test_no_compactor_takes_an_argument(self) -> None:
        import inspect

        for module, name in _COMPACTORS:
            with self.subTest(compactor=name):
                signature = inspect.signature(getattr(module, name))
                self.assertEqual(
                    list(signature.parameters),
                    [],
                    f"{name} takes an argument, so a caller can point it at a journal "
                    f"it was never written for -- including the cash book's",
                )

    def test_the_replace_primitive_is_called_only_by_those_compactors(self) -> None:
        """Property 3. ``replace_compacted(journal, ...)`` DOES take a path and
        DOES rewrite the file, so the zero-argument property above only bounds
        it while these are its only callers."""
        callers: set[tuple[str, str]] = set()
        for source in _production_sources():
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for call in ast.walk(node):
                    if (
                        isinstance(call, ast.Call)
                        and getattr(call.func, "attr", getattr(call.func, "id", ""))
                        == _REPLACE_PRIMITIVE
                    ):
                        callers.add((source.stem, node.name))

        self.assertEqual(
            callers,
            {(module.__name__.rsplit(".", 1)[-1], name) for module, name in _COMPACTORS},
            f"{_REPLACE_PRIMITIVE} gained a caller outside the two compactors; it takes a "
            f"journal path and rewrites it, so a new caller can reach any journal",
        )

    def test_no_production_module_globs_for_jsonl(self) -> None:
        """Property 4. A glob over the home root would find the cash book's four
        journals without naming any of them."""
        offenders: list[str] = []
        for source in _production_sources():
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in ("glob", "rglob"):
                    continue
                patterns = [
                    arg.value
                    for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                ]
                if any("jsonl" in pattern for pattern in patterns):
                    offenders.append(f"{source.name}:{node.lineno}")

        self.assertEqual(offenders, [], "a production glob can discover the cash book's journals")

    def test_the_glob_probe_can_actually_fire(self) -> None:
        """Positive control for the scan above."""
        tree = ast.parse('directory.rglob("*.jsonl")')
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rglob"
            and any(
                isinstance(arg, ast.Constant) and "jsonl" in str(arg.value) for arg in node.args
            )
        ]
        self.assertEqual(len(matches), 1)


class TheCashBookIsOutsideEveryCompactedRoot(IsolatedHomeTestCase):
    """Property 2, asserted on the resolved paths rather than on prose. Both
    compactors name a journal under ``broker_orders/<env>/``; the cash book
    lives under ``account_cash/<env>/`` and is not reachable from there."""

    def test_no_cash_book_journal_sits_under_the_keepers_state_root(self) -> None:
        keeper_root = state_paths.broker_orders_root("live")
        for env in paths.ENVIRONMENTS:
            for builder in (
                paths.cash_events_path,
                paths.trade_rows_path,
                paths.balances_path,
                paths.sweeps_path,
            ):
                with self.subTest(env=env, journal=builder.__name__):
                    self.assertFalse(builder(env).is_relative_to(keeper_root))

    def test_the_two_compacted_journals_are_not_cash_book_journals(self) -> None:
        compacted = {
            state_paths.standalone_stops_path("live"),
            state_paths.entry_trails_path("live"),
            state_paths.standalone_stops_path("sim"),
            state_paths.entry_trails_path("sim"),
        }
        cash_book = {
            builder(env)
            for env in paths.ENVIRONMENTS
            for builder in (
                paths.cash_events_path,
                paths.trade_rows_path,
                paths.balances_path,
                paths.sweeps_path,
            )
        }
        self.assertEqual(compacted & cash_book, set())
        self.assertEqual(len(cash_book), 8, sorted(str(path) for path in cash_book))


class TheCashBookPackageCompactsNothingItself(unittest.TestCase):
    def test_no_module_in_the_package_rewrites_or_truncates_a_file(self) -> None:
        """The other direction: the cash book must not grow its own compactor.
        An ``os.replace`` or a write-mode open is how one would arrive."""
        package_dir = Path(account_cash.__file__).resolve().parent
        sources = sorted(package_dir.rglob("*.py"))
        self.assertTrue(sources, "no modules found; the gate would pass vacuously")
        offenders: list[str] = []
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr in ("replace", "unlink", "rmtree", "write_text", "write_bytes"):
                    offenders.append(f"{source.name}:{node.lineno}:{node.func.attr}")
                if node.func.attr == "open":
                    modes = [
                        arg.value
                        for arg in node.args
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    ]
                    if any(set(mode) & {"w", "x", "+"} for mode in modes):
                        offenders.append(f"{source.name}:{node.lineno}:open({modes})")

        self.assertEqual(
            offenders,
            [],
            "the cash book package rewrites or removes a file; it is append-only and "
            "never compacted (memo section 3)",
        )


if __name__ == "__main__":
    unittest.main()

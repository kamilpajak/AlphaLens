"""The broker dead-code report (#1471): ``just deadcode``.

The report is not a CI gate. These tests pin what makes it trustworthy: it can
never read "clean" while checking nothing. Vulture exits 3 whenever it finds
anything, and a file it cannot parse only prints to stderr, so the exit status
alone says little. The scope filter must match the paths vulture really prints.
A whitelist entry whose symbol is gone must show up, and so must a broker module
that nothing in production imports.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from collections.abc import Sequence
from pathlib import Path

from scripts import deadcode_broker as deadcode

_SCOPED = (
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py:297: "
    "unused method 'get_exchanges' (60% confidence)"
)
_OUT_OF_SCOPE = (
    "apps/alphalens-pipeline/alphalens_pipeline/thematic/brief.py:12: "
    "unused function 'helper' (60% confidence)"
)
_CONTRACT = (
    "apps/alphalens-broker-contract/broker_contract/contract.py:643: "
    "unused method 'place_stop_limit' (60% confidence)"
)


class _FakeRunner:
    """Answers the with-whitelist run and the no-whitelist run separately."""

    def __init__(
        self,
        with_whitelist: deadcode.VultureRun,
        without_whitelist: deadcode.VultureRun | None = None,
    ) -> None:
        self._with = with_whitelist
        self._without = without_whitelist or with_whitelist
        self.calls: list[Sequence[str]] = []

    def __call__(self, argv: Sequence[str]) -> deadcode.VultureRun:
        self.calls.append(tuple(argv))
        uses_whitelist = deadcode.WHITELIST_RELPATH in argv
        return self._with if uses_whitelist else self._without


def _run(stdout: str = "", returncode: int = 0, stderr: str = "") -> deadcode.VultureRun:
    return deadcode.VultureRun(returncode=returncode, stdout=stdout, stderr=stderr)


class _TreeCase(unittest.TestCase):
    """A tiny repo tree: one scoped package and an empty whitelist."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.write("apps/alphalens-pipeline/alphalens_pipeline/__init__.py", "")
        self.write("apps/alphalens-pipeline/alphalens_pipeline/brokers/__init__.py", "")
        self.whitelist("")

    def write(self, relpath: str, text: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def whitelist(self, entries: str) -> None:
        header = "from vulture.whitelists.whitelist_utils import Whitelist\n\n_ = Whitelist()\n\n"
        self.write(deadcode.WHITELIST_RELPATH, header + textwrap.dedent(entries))

    def report(self, runner: _FakeRunner, **kwargs: object) -> tuple[int, str]:
        lines: list[str] = []
        # The committed allow-list names real repo modules this tree does not have.
        kwargs.setdefault("unwired_allowed", {})
        status = deadcode.run_report(self.root, runner, emit=lines.append, **kwargs)
        return status, "\n".join(lines)


class TheExitStatusNeverReadsCleanWhileCheckingNothing(_TreeCase):
    def test_no_finding_is_clean(self) -> None:
        status, _ = self.report(_FakeRunner(_run()))
        self.assertEqual(status, deadcode.EXIT_CLEAN)

    def test_findings_outside_the_scope_are_clean(self) -> None:
        status, text = self.report(_FakeRunner(_run(_OUT_OF_SCOPE, returncode=3)))
        self.assertEqual(status, deadcode.EXIT_CLEAN)
        self.assertNotIn("helper", text)

    def test_a_scoped_finding_is_reported(self) -> None:
        status, text = self.report(_FakeRunner(_run(f"{_OUT_OF_SCOPE}\n{_SCOPED}", returncode=3)))
        self.assertEqual(status, deadcode.EXIT_FINDINGS)
        self.assertIn("get_exchanges", text)

    def test_the_contract_package_is_in_the_scope(self) -> None:
        status, _ = self.report(_FakeRunner(_run(_CONTRACT, returncode=3)))
        self.assertEqual(status, deadcode.EXIT_FINDINGS)

    def test_a_file_vulture_cannot_parse_is_a_tool_failure(self) -> None:
        # Vulture still exits 3 here; the unparsed file only shows on stderr.
        run = _run(_OUT_OF_SCOPE, returncode=3, stderr='x.py:1: invalid syntax at "def f(:"')
        status, text = self.report(_FakeRunner(run))
        self.assertEqual(status, deadcode.EXIT_TOOL_FAILED)
        self.assertIn("invalid syntax", text)

    def test_a_missing_path_or_bad_arguments_is_a_tool_failure(self) -> None:
        for returncode in (1, 2):
            with self.subTest(returncode=returncode):
                status, _ = self.report(_FakeRunner(_run(returncode=returncode)))
                self.assertEqual(status, deadcode.EXIT_TOOL_FAILED)

    def test_the_scan_covers_all_production_code_and_the_whitelist(self) -> None:
        runner = _FakeRunner(_run())
        self.report(runner)
        argv = runner.calls[0]
        for root in deadcode.PRODUCTION_ROOTS:
            self.assertIn(root, argv)
        self.assertTrue(deadcode.WHITELIST_RELPATH in argv)


class AStaleWhitelistEntryIsReported(_TreeCase):
    def test_an_entry_no_finding_needs_is_stale(self) -> None:
        self.whitelist("_.gone_symbol  # it was removed\n")
        status, text = self.report(_FakeRunner(_run(), without_whitelist=_run()))
        self.assertEqual(status, deadcode.EXIT_FINDINGS)
        self.assertIn("gone_symbol", text)

    def test_an_entry_that_hides_a_scoped_finding_is_not_stale(self) -> None:
        self.whitelist("_.get_exchanges  # kept on purpose\n")
        status, _ = self.report(_FakeRunner(_run(), without_whitelist=_run(_SCOPED, returncode=3)))
        self.assertEqual(status, deadcode.EXIT_CLEAN)

    def test_an_entry_that_hides_only_an_out_of_scope_finding_is_stale(self) -> None:
        self.whitelist("_.helper  # not a broker symbol\n")
        status, text = self.report(
            _FakeRunner(_run(), without_whitelist=_run(_OUT_OF_SCOPE, returncode=3))
        )
        self.assertEqual(status, deadcode.EXIT_FINDINGS)
        self.assertIn("helper", text)

    def test_an_entry_without_a_reason_is_reported(self) -> None:
        self.whitelist("_.get_exchanges\n")
        status, text = self.report(
            _FakeRunner(_run(), without_whitelist=_run(_SCOPED, returncode=3))
        )
        self.assertEqual(status, deadcode.EXIT_FINDINGS)
        self.assertIn("reason", text)


class AnUnwiredBrokerModuleIsReported(_TreeCase):
    _PKG = "apps/alphalens-pipeline/alphalens_pipeline/brokers"

    def test_a_module_nothing_imports_is_reported(self) -> None:
        self.write(f"{self._PKG}/orphan.py", "def f():\n    return 1\n")
        status, text = self.report(_FakeRunner(_run()))
        self.assertEqual(status, deadcode.EXIT_FINDINGS)
        self.assertIn("brokers/orphan.py", text)

    def test_an_absolute_import_wires_a_module(self) -> None:
        self.write(f"{self._PKG}/used.py", "X = 1\n")
        self.write(f"{self._PKG}/user.py", "from alphalens_pipeline.brokers import used\n")
        self.write(
            "apps/alphalens-pipeline/alphalens_pipeline/app.py",
            "import alphalens_pipeline.brokers.user\n",
        )
        status, text = self.report(_FakeRunner(_run()))
        self.assertEqual(status, deadcode.EXIT_CLEAN, text)

    def test_a_relative_import_inside_a_function_wires_a_module(self) -> None:
        self.write(f"{self._PKG}/used.py", "X = 1\n")
        self.write(
            f"{self._PKG}/user.py",
            "def load():\n    from .used import X\n    return X\n",
        )
        self.write(
            "apps/alphalens-pipeline/alphalens_pipeline/app.py",
            "from alphalens_pipeline.brokers.user import load\n",
        )
        status, text = self.report(_FakeRunner(_run()))
        self.assertEqual(status, deadcode.EXIT_CLEAN, text)

    def test_a_module_imported_only_by_itself_is_still_unwired(self) -> None:
        self.write(f"{self._PKG}/selfish.py", "import alphalens_pipeline.brokers.selfish\n")
        status, text = self.report(_FakeRunner(_run()))
        self.assertEqual(status, deadcode.EXIT_FINDINGS)
        self.assertIn("brokers/selfish.py", text)

    def test_an_allowed_module_is_not_reported(self) -> None:
        self.write(f"{self._PKG}/boundary.py", "X = 1\n")
        status, text = self.report(
            _FakeRunner(_run()),
            unwired_allowed={f"{self._PKG}/boundary.py": "driven by the acceptance suite"},
        )
        self.assertEqual(status, deadcode.EXIT_CLEAN, text)

    def test_an_allowed_module_that_is_now_imported_is_stale(self) -> None:
        self.write(f"{self._PKG}/used.py", "X = 1\n")
        self.write(
            "apps/alphalens-pipeline/alphalens_pipeline/app.py",
            "from alphalens_pipeline.brokers import used\n",
        )
        status, text = self.report(
            _FakeRunner(_run()),
            unwired_allowed={f"{self._PKG}/used.py": "was unwired"},
        )
        self.assertEqual(status, deadcode.EXIT_FINDINGS)
        self.assertIn("brokers/used.py", text)


@unittest.skipUnless(deadcode.vulture_available(), "vulture is not installed")
class TheRealToolFindsAPlantedFunction(_TreeCase):
    """End to end with the real vulture: a dead function in a scoped path is found."""

    def test_a_planted_unused_function_is_reported(self) -> None:
        self.write(
            "apps/alphalens-pipeline/alphalens_pipeline/brokers/planted.py",
            "def never_called():\n    return 1\n",
        )
        self.write(
            "apps/alphalens-pipeline/alphalens_pipeline/app.py",
            "from alphalens_pipeline.brokers import planted\n",
        )
        status, text = self.report(
            deadcode.subprocess_runner(self.root),
            production_roots=("apps/alphalens-pipeline/alphalens_pipeline",),
        )
        self.assertEqual(status, deadcode.EXIT_FINDINGS, text)
        self.assertIn("never_called", text)

    def test_a_whitelisted_function_is_hidden_and_its_entry_is_not_stale(self) -> None:
        self.write(
            "apps/alphalens-pipeline/alphalens_pipeline/brokers/planted.py",
            "def kept_on_purpose():\n    return 1\n",
        )
        self.write(
            "apps/alphalens-pipeline/alphalens_pipeline/app.py",
            "from alphalens_pipeline.brokers import planted\n",
        )
        self.whitelist("_.kept_on_purpose  # planted for the test\n")
        status, text = self.report(
            deadcode.subprocess_runner(self.root),
            production_roots=(
                "apps/alphalens-pipeline/alphalens_pipeline",
                "apps/alphalens-research/scripts",
            ),
        )
        self.assertEqual(status, deadcode.EXIT_CLEAN, text)

    def test_a_whitelist_entry_for_a_removed_function_is_stale(self) -> None:
        self.write(
            "apps/alphalens-pipeline/alphalens_pipeline/app.py",
            "def main():\n    return 1\n\nmain()\n",
        )
        self.whitelist("_.already_removed  # it was deleted\n")
        status, text = self.report(
            deadcode.subprocess_runner(self.root),
            production_roots=(
                "apps/alphalens-pipeline/alphalens_pipeline",
                "apps/alphalens-research/scripts",
            ),
        )
        self.assertEqual(status, deadcode.EXIT_FINDINGS, text)
        self.assertIn("already_removed", text)


class TheCommittedWhitelistKeepsTheTrailingStopMethod(unittest.TestCase):
    def test_place_stop_limit_is_whitelisted_with_a_reason(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        entries = deadcode.whitelist_entries(repo_root / deadcode.WHITELIST_RELPATH)
        self.assertIn("place_stop_limit", entries)
        self.assertTrue(entries["place_stop_limit"].strip())


if __name__ == "__main__":
    unittest.main()

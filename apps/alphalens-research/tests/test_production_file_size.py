"""Ratchet the size of production files, so the next 11 000-line module cannot grow unnoticed.

Finding 3 of the 2026-10-02 architecture audit: nothing in CI measures file or
function size. ``ruff`` ignores ``PLR0915`` / ``PLR0912`` / ``PLR0911``
repo-wide (see the root ``pyproject.toml``), SonarCloud runs with
``continue-on-error: true`` and excludes ``**/tests/**`` and both
``scripts/**``, and no workflow counts lines.

The audit also measured why re-enabling the ruff rules would NOT have caught
the file that prompted this: ``control_loop.py`` held 301 functions and only
**4** above cyclomatic complexity 15. A file of 11 130 individually compliant
functions passes every per-function rule there is. The gate that was missing
is per FILE.

This is a RATCHET, not a limit, and it is red in BOTH directions:

* a file may not grow past its baseline entry (or past ``CEILING`` when it has
  no entry) — that is what stops a big file from growing back;
* a file that has shrunk more than ``SHRINK_SLACK`` below its entry must have
  the entry lowered — that is what stops the baseline from recording a size the
  file left behind. Without this half the baseline silently stops meaning
  anything, the way a dead allowlist entry does.

Raising an entry is allowed and is meant to be visible: it is one line in the
diff, in the PR that needs the file to grow.

The corpus is the production corpus, held in place on TWO independent axes,
because either one alone leaves a way in:

* against ``sonar.sources`` — a root Sonar scans but this gate does not;
* against ``[tool.uv.workspace] members`` — a member added to the workspace
  but to neither list. The Sonar check alone cannot see this one: both lists
  stay unchanged, both stay equal, and the new member's files are never walked.

What is left out is listed by DIRECTORY (``EXCLUDED_DIRS``), not matched on the
name ``tests`` wherever it appears. Name matching would mean a large file could
hide in any new directory called ``tests``; a declared list makes an
undeclared one red. Django is the only member whose tests and migrations sit
inside its source root — everywhere else the suite is a sibling of the package.
"""

from __future__ import annotations

import unittest
from pathlib import Path

# Workspace root = repo top dir (three levels above this test file:
# tests/foo.py -> tests/ -> apps/alphalens-research/ -> apps/ -> repo)
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

# A file above this many lines must carry a baseline entry below. 1 000 is the
# point at which a module stops fitting in one reading; 16 of the 517
# production files are above it today.
CEILING = 1000

# How far below its entry a file may sit before the entry is stale. A file that
# legitimately lost a few lines should not force a baseline edit; one that lost
# a tenth of itself has been restructured, and the entry must record that.
SHRINK_SLACK = 0.10

# The production roots. Mirrors ``sonar.sources`` (minus the one non-Python
# entry) and is held to it by a test below.
PRODUCTION_ROOTS: tuple[str, ...] = (
    "apps/alphalens-broker-contract/broker_contract",
    "apps/alphalens-feedback/alphalens_feedback",
    "apps/intent-replay/intent_replay",
    "apps/alphalens-pipeline/alphalens_pipeline",
    "apps/alphalens-pipeline/alphalens_cli",
    "apps/alphalens-research/alphalens_research",
    "apps/alphalens-django",
)

# Directories inside a production root that are not production code, each with
# what it holds. Declared one by one rather than matched by name, so a large
# file cannot hide in a new directory called ``tests``: an undeclared one is red
# (``test_no_undeclared_tests_or_migrations_directory_exists``), and a declared
# one that disappears is red too.
EXCLUDED_DIRS: dict[str, str] = {
    "apps/alphalens-django/auth_cf/tests": "django app test suite",
    "apps/alphalens-django/briefs/migrations": "django schema migrations",
    "apps/alphalens-django/briefs/tests": "django app test suite",
    "apps/alphalens-django/config/tests": "django project test suite",
    "apps/alphalens-django/core/tests": "django app test suite",
    "apps/alphalens-django/edge/migrations": "django schema migrations",
    "apps/alphalens-django/edge/tests": "django app test suite",
    "apps/alphalens-django/market/tests": "django app test suite",
}

# Names that must not appear as a directory inside a production root unless
# EXCLUDED_DIRS declares that directory.
MUST_BE_DECLARED_DIR_NAMES = frozenset({"tests", "migrations"})

# path (repo-relative, posix) -> the line count this file may not exceed.
#
# Every entry is a file the audit left too big. Lower one when the file
# shrinks; raise one deliberately, in the PR that needs the room.
BASELINE: dict[str, int] = {
    "apps/alphalens-pipeline/alphalens_cli/commands/broker.py": 3414,
    "apps/alphalens-pipeline/alphalens_cli/commands/thematic.py": 1708,
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py": 8654,
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py": 1406,
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/stop_journal.py": 1383,
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/trades.py": 3058,
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py": 2416,
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py": 1141,
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_price_stream.py": 1395,
    "apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_chart.py": 1329,
    "apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_replay.py": 1910,
    "apps/alphalens-pipeline/alphalens_pipeline/feedback/population_ladder_monitor.py": 3347,
    "apps/alphalens-pipeline/alphalens_pipeline/feedback/selection_label.py": 1354,
    "apps/alphalens-pipeline/alphalens_pipeline/thematic/mapping/channel_assessor.py": 1267,
    "apps/alphalens-pipeline/alphalens_pipeline/thematic/mapping/orchestrator.py": 1626,
    "apps/alphalens-research/alphalens_research/eval/faithfulness.py": 1034,
}


def iter_production_files(root: Path) -> list[str]:
    """Every production ``.py`` file under ``root``, as repo-relative posix paths."""
    found: list[str] = []
    for top in PRODUCTION_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(root)
            posix = rel.as_posix()
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            if any(posix.startswith(excluded + "/") for excluded in EXCLUDED_DIRS):
                continue
            found.append(posix)
    return sorted(found)


def line_count(root: Path, rel: str) -> int:
    """Newline bytes in a file — exactly what ``wc -l`` reports.

    Counted this way on purpose rather than by iterating lines: iteration counts
    a final line with no trailing newline, ``wc -l`` does not, and the baseline
    below was generated with ``wc -l``. Today the two agree on every production
    file (``end-of-file-fixer`` in pre-commit), so this only removes a way for
    them to disagree later.
    """
    with (root / rel).open("rb") as handle:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))


def measure(root: Path) -> dict[str, int]:
    """The production corpus as path -> line count."""
    return {rel: line_count(root, rel) for rel in iter_production_files(root)}


def files_over_their_limit(
    sizes: dict[str, int], baseline: dict[str, int]
) -> list[tuple[str, int, int]]:
    """Files above their effective limit, as (path, lines, limit).

    The effective limit is the baseline entry when there is one, and
    ``CEILING`` otherwise — so a file with no entry may grow up to the ceiling
    and must then be split or given an entry.
    """
    over: list[tuple[str, int, int]] = []
    for rel, lines in sorted(sizes.items()):
        limit = baseline.get(rel, CEILING)
        if lines > limit:
            over.append((rel, lines, limit))
    return over


def stale_baseline_entries(
    sizes: dict[str, int], baseline: dict[str, int]
) -> list[tuple[str, int, int]]:
    """Entries the file has outgrown DOWNWARD, as (path, lines, entry).

    Three shapes, all of them an entry that no longer guards anything: the file
    is gone, the file has fallen back under the ceiling, or it sits more than
    ``SHRINK_SLACK`` below what the entry claims.
    """
    stale: list[tuple[str, int, int]] = []
    for rel, entry in sorted(baseline.items()):
        lines = sizes.get(rel)
        if lines is None:
            stale.append((rel, 0, entry))
        elif lines <= CEILING or lines < entry * (1 - SHRINK_SLACK):
            stale.append((rel, lines, entry))
    return stale


class ProductionFilesStayWithinTheirBaseline(unittest.TestCase):
    """The ratchet itself, measured over the real tree."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sizes = measure(WORKSPACE_ROOT)

    def test_no_production_file_is_over_its_limit(self) -> None:
        over = files_over_their_limit(self.sizes, BASELINE)
        self.assertEqual(
            over,
            [],
            "These production files are over their limit. Split the file, or — if the "
            "change genuinely needs the room — raise its entry in BASELINE in this same "
            f"PR so the growth is visible in review:\n{self._render(over)}",
        )

    def test_no_baseline_entry_records_a_size_the_file_has_left_behind(self) -> None:
        stale = stale_baseline_entries(self.sizes, BASELINE)
        self.assertEqual(
            stale,
            [],
            "These BASELINE entries no longer describe their file. Lower each entry to "
            "the count shown (or drop it, if the file is now at or under the "
            f"{CEILING}-line ceiling), so the baseline keeps ratcheting "
            f"down:\n{self._render(stale)}",
        )

    @staticmethod
    def _render(rows: list[tuple[str, int, int]]) -> str:
        return "\n".join(
            f"  {path}: {lines} lines, entry/limit {limit}" for path, lines, limit in rows
        )


class TheCorpusIsTheProductionCorpus(unittest.TestCase):
    """What the gate measures, pinned — a gate over an empty corpus tests nothing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = iter_production_files(WORKSPACE_ROOT)

    def test_the_corpus_covers_every_python_root_sonar_scans(self) -> None:
        """A new workspace member added to Sonar must also come under this gate."""
        text = (WORKSPACE_ROOT / "sonar-project.properties").read_text(encoding="utf-8")
        declared = [
            line.split("=", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("sonar.sources=")
        ]
        self.assertEqual(len(declared), 1, "expected exactly one sonar.sources line")
        self.assertFalse(
            declared[0].endswith("\\"),
            "sonar.sources continues onto the next line. A .properties continuation would "
            "leave this test comparing a truncated list, which can pass while roots are "
            "missing — keep the value on one line.",
        )
        sonar_roots = [entry.strip() for entry in declared[0].split(",")]
        python_roots = [root for root in sonar_roots if not root.startswith("apps/web/")]
        self.assertEqual(
            sorted(python_roots),
            sorted(PRODUCTION_ROOTS),
            "sonar.sources and PRODUCTION_ROOTS have drifted apart. A Python root Sonar "
            "scans but this gate does not is a member where the next oversized file can "
            "grow unseen.",
        )

    def test_the_corpus_holds_a_file_from_every_root(self) -> None:
        for root in PRODUCTION_ROOTS:
            with self.subTest(root=root):
                self.assertTrue(
                    any(rel.startswith(root + "/") for rel in self.files),
                    f"no production file found under {root} — the walk is broken or the root moved",
                )

    def test_the_corpus_excludes_every_declared_directory(self) -> None:
        leaked = [
            rel
            for rel in self.files
            if any(rel.startswith(excluded + "/") for excluded in EXCLUDED_DIRS)
        ]
        self.assertEqual(leaked, [], f"non-production files leaked into the corpus: {leaked[:10]}")

    def test_every_declared_excluded_directory_still_exists(self) -> None:
        """An entry for a directory that is gone guards nothing — drop it."""
        gone = sorted(d for d in EXCLUDED_DIRS if not (WORKSPACE_ROOT / d).is_dir())
        self.assertEqual(gone, [], f"EXCLUDED_DIRS names directories that no longer exist: {gone}")

    def test_no_undeclared_tests_or_migrations_directory_exists(self) -> None:
        """A new such directory must be declared, so a big file cannot hide in one."""
        undeclared: list[str] = []
        for top in PRODUCTION_ROOTS:
            base = WORKSPACE_ROOT / top
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if not path.is_dir() or path.name not in MUST_BE_DECLARED_DIR_NAMES:
                    continue
                rel = path.relative_to(WORKSPACE_ROOT).as_posix()
                if rel not in EXCLUDED_DIRS:
                    undeclared.append(rel)
        self.assertEqual(
            sorted(undeclared),
            [],
            "These directories sit inside a production root and are named like test or "
            "migration trees, but EXCLUDED_DIRS does not declare them. Either add each "
            "one with what it holds, or — if it does hold production code — rename it, "
            f"because the name is what makes a reader skip it:\n  {sorted(undeclared)}",
        )

    def test_every_workspace_member_contributes_a_production_root(self) -> None:
        """A member added to the workspace but to neither list would escape the gate.

        The Sonar parity test cannot see this: both lists stay unchanged and equal.
        """
        import tomllib

        config = tomllib.loads((WORKSPACE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        members = config["tool"]["uv"]["workspace"]["members"]
        self.assertTrue(members, "no uv workspace members declared")
        uncovered = sorted(
            member
            for member in members
            if not any(root == member or root.startswith(member + "/") for root in PRODUCTION_ROOTS)
        )
        self.assertEqual(
            uncovered,
            [],
            "These uv workspace members have no production root in this gate, so files "
            f"inside them are never measured: {uncovered}",
        )

    def test_every_production_root_lives_under_a_workspace_member(self) -> None:
        import tomllib

        config = tomllib.loads((WORKSPACE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        members = config["tool"]["uv"]["workspace"]["members"]
        orphaned = sorted(
            root
            for root in PRODUCTION_ROOTS
            if not any(root == member or root.startswith(member + "/") for member in members)
        )
        self.assertEqual(
            orphaned, [], f"PRODUCTION_ROOTS entries outside every workspace member: {orphaned}"
        )

    def test_the_corpus_is_the_size_the_audit_measured(self) -> None:
        """A walk that silently narrowed to a handful of files would pass every rule above."""
        self.assertGreater(len(self.files), 400, f"only {len(self.files)} production files found")

    def test_every_baseline_entry_names_a_file_in_the_corpus(self) -> None:
        missing = sorted(set(BASELINE) - set(self.files))
        self.assertEqual(
            missing,
            [],
            f"BASELINE names files that are not in the production corpus: {missing}",
        )


class TheGateCatchesWhatItIsFor(unittest.TestCase):
    """Positive controls — synthetic corpora driven through the REAL predicates.

    Without these the two rules above could rot to "nothing to check" and still
    report green, which is the failure mode the audit kept finding: a check
    with no power to produce the observation that would refute it.
    """

    def test_a_file_growing_past_its_entry_is_caught(self) -> None:
        sizes = {"a/b.py": 2001}
        self.assertEqual(files_over_their_limit(sizes, {"a/b.py": 2000}), [("a/b.py", 2001, 2000)])

    def test_a_file_exactly_at_its_entry_is_allowed(self) -> None:
        self.assertEqual(files_over_their_limit({"a/b.py": 2000}, {"a/b.py": 2000}), [])

    def test_a_new_file_over_the_ceiling_with_no_entry_is_caught(self) -> None:
        sizes = {"a/new.py": CEILING + 1}
        self.assertEqual(
            files_over_their_limit(sizes, {}),
            [("a/new.py", CEILING + 1, CEILING)],
        )

    def test_a_small_file_with_no_entry_is_allowed(self) -> None:
        self.assertEqual(files_over_their_limit({"a/small.py": CEILING}, {}), [])

    def test_a_file_that_shrank_past_the_slack_forces_its_entry_down(self) -> None:
        sizes = {"a/b.py": 1799}  # 2000 * (1 - 0.10) = 1800
        self.assertEqual(stale_baseline_entries(sizes, {"a/b.py": 2000}), [("a/b.py", 1799, 2000)])

    def test_a_file_that_shrank_within_the_slack_keeps_its_entry(self) -> None:
        self.assertEqual(stale_baseline_entries({"a/b.py": 1800}, {"a/b.py": 2000}), [])

    def test_an_entry_for_a_file_that_fell_under_the_ceiling_is_stale(self) -> None:
        sizes = {"a/b.py": CEILING}
        self.assertEqual(
            stale_baseline_entries(sizes, {"a/b.py": 1100}), [("a/b.py", CEILING, 1100)]
        )

    def test_an_entry_for_a_deleted_file_is_stale(self) -> None:
        self.assertEqual(stale_baseline_entries({}, {"a/gone.py": 1200}), [("a/gone.py", 0, 1200)])

    def test_the_walk_finds_a_planted_file_and_skips_a_planted_test(self) -> None:
        """The discovery half, driven over a synthetic tree by the real function."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declared = next(iter(EXCLUDED_DIRS))
            member = root / PRODUCTION_ROOTS[0]
            (member / "sub").mkdir(parents=True)
            (member / "sub" / "kept.py").write_text("x = 1\n", encoding="utf-8")
            (member / "sub" / "__pycache__").mkdir()
            (member / "sub" / "__pycache__" / "cached.py").write_text("x = 1\n", encoding="utf-8")
            (member / "sub" / "notpython.txt").write_text("x\n", encoding="utf-8")
            # An undeclared directory called `tests` is production until declared:
            # that is the point of the declared list, so this file IS measured.
            (member / "tests").mkdir()
            (member / "tests" / "measured.py").write_text("x = 1\n", encoding="utf-8")
            # A declared one is skipped.
            (root / declared).mkdir(parents=True)
            (root / declared / "skipped.py").write_text("x = 1\n", encoding="utf-8")

            found = iter_production_files(root)

            self.assertEqual(
                found,
                [f"{PRODUCTION_ROOTS[0]}/sub/kept.py", f"{PRODUCTION_ROOTS[0]}/tests/measured.py"],
            )
            self.assertEqual(measure(root)[f"{PRODUCTION_ROOTS[0]}/sub/kept.py"], 1)


if __name__ == "__main__":
    unittest.main()

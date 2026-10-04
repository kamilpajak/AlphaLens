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

The corpus is the production corpus — the same roots SonarCloud scans, minus
tests and Django migrations. ``test_the_corpus_covers_every_python_root_sonar_scans``
keeps the two lists from drifting apart, so a new workspace member cannot land
outside this gate without someone noticing.
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

# Directory names that are not production code wherever they appear.
EXCLUDED_DIR_PARTS = frozenset({"tests", "test", "migrations", "__pycache__"})

# path (repo-relative, posix) -> the line count this file may not exceed.
#
# Every entry is a file the audit left too big. Lower one when the file
# shrinks; raise one deliberately, in the PR that needs the room.
BASELINE: dict[str, int] = {
    "apps/alphalens-pipeline/alphalens_cli/commands/broker.py": 3414,
    "apps/alphalens-pipeline/alphalens_cli/commands/thematic.py": 1708,
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py": 9313,
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
            if EXCLUDED_DIR_PARTS & set(rel.parts[:-1]):
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            found.append(rel.as_posix())
    return sorted(found)


def line_count(root: Path, rel: str) -> int:
    """Lines in a file, counted the way ``wc -l`` counts them."""
    with (root / rel).open("rb") as handle:
        return sum(1 for _ in handle)


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
        sonar_roots = declared[0].split(",")
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

    def test_the_corpus_excludes_tests_and_migrations(self) -> None:
        leaked = [rel for rel in self.files if EXCLUDED_DIR_PARTS & set(Path(rel).parts[:-1])]
        self.assertEqual(leaked, [], f"non-production files leaked into the corpus: {leaked[:10]}")

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
            member = root / PRODUCTION_ROOTS[0]
            (member / "sub").mkdir(parents=True)
            (member / "sub" / "kept.py").write_text("x = 1\n", encoding="utf-8")
            (member / "tests").mkdir()
            (member / "tests" / "skipped.py").write_text("x = 1\n", encoding="utf-8")
            (member / "sub" / "notpython.txt").write_text("x\n", encoding="utf-8")

            found = iter_production_files(root)

            self.assertEqual(found, [f"{PRODUCTION_ROOTS[0]}/sub/kept.py"])
            self.assertEqual(measure(root), {f"{PRODUCTION_ROOTS[0]}/sub/kept.py": 1})


if __name__ == "__main__":
    unittest.main()

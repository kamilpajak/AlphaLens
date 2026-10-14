"""Every nested ``CLAUDE.md`` must be TRACKED BY GIT.

A nested ``CLAUDE.md`` is how an area rule reaches Claude lazily: it loads only
when a file in that directory is read, written or edited, which is what keeps it
out of the always-loaded root file. That only works if the file is in the
repository. An untracked or gitignored area rule exists on ONE machine, is
invisible in every PR, and is lost on a fresh clone — which is exactly why
``.claude/rules/*.md`` was rejected for this repo (``.claude/`` is gitignored
wholesale, and ``git ls-files .claude`` is empty).

So this gate fails on either shape of the trap:

* a nested ``CLAUDE.md`` that exists on disk but was never ``git add``-ed;
* a nested ``CLAUDE.md`` that git actively IGNORES, which no ``git add`` without
  ``-f`` will ever pick up.

Discovery is a walk, not a list, so a new area file is covered the moment it
lands. Nested CHECKOUTS are skipped: a directory holding its own ``.git`` entry
belongs to another worktree (``.claude/worktrees/<name>/`` in the main checkout)
and its files are that repository's business, not this one's.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

# Test file lives at apps/alphalens-research/tests/<name>.py; the repo root is
# three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]

MEMORY_FILENAME = "CLAUDE.md"

# Directories that never hold a tracked instruction file and cost real time to
# walk.
SKIP_DIRNAMES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        "build",
        "dist",
    }
)

# Positive-control floor: the root file plus the nested area briefs. Set below
# the real count so adding one more does not turn this red, but high enough that
# a broken walk (wrong root, bad skip rule) cannot pass.
MIN_DISCOVERED = 4


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def _tracked_memory_files() -> set[Path]:
    out = _git("ls-files", "-z", f"*{MEMORY_FILENAME}")
    return {REPO_ROOT / rel for rel in out.split("\0") if rel}


def _is_ignored(path: Path) -> bool:
    proc = subprocess.run(
        # --no-index is load-bearing: by default check-ignore consults the INDEX and
        # reports a TRACKED path as not-ignored even when a rule matches it. Measured
        # on git 2.54.0: a force-added file under an ignored directory gives rc=1
        # without the flag and rc=0 with it. Without it this assertion has no power
        # of its own — the only shape left to catch is already caught by the
        # tracked-ness test, so it would die only jointly with that one.
        ["git", "check-ignore", "-q", "--no-index", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # 0 = ignored, 1 = not ignored, 128 = error (treat as "cannot tell").
    return proc.returncode == 0


def _discover_memory_files() -> list[Path]:
    """Every ``CLAUDE.md`` in this checkout, skipping nested checkouts."""
    found: list[Path] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIRNAMES:
                    continue
                # A directory carrying its own .git is a separate worktree or
                # submodule; its instruction files are not this repo's to gate.
                if (entry / ".git").exists():
                    continue
                walk(entry)
            elif entry.name == MEMORY_FILENAME:
                found.append(entry)

    walk(REPO_ROOT)
    return found


DISCOVERED = _discover_memory_files()


class TestNestedClaudeMdIsTracked(unittest.TestCase):
    def test_discovery_is_non_empty(self) -> None:
        """Positive control: the walk must actually find the instruction files."""
        self.assertGreaterEqual(
            len(DISCOVERED),
            MIN_DISCOVERED,
            f"only {len(DISCOVERED)} {MEMORY_FILENAME} file(s) discovered under "
            f"{REPO_ROOT} — the walk or its skip rules are broken, so the "
            "tracking assertions below would pass vacuously",
        )

    def test_root_file_is_discovered(self) -> None:
        """Guard the walk's root: the repo-level file must be in the set."""
        self.assertIn(REPO_ROOT / MEMORY_FILENAME, DISCOVERED)

    def test_every_discovered_file_is_tracked(self) -> None:
        tracked = _tracked_memory_files()
        untracked = sorted(str(p.relative_to(REPO_ROOT)) for p in DISCOVERED if p not in tracked)
        self.assertEqual(
            untracked,
            [],
            f"{MEMORY_FILENAME} file(s) not tracked by git: {untracked}. An area "
            "rule git does not carry exists on one machine only — `git add` them "
            "in the commit that introduces them.",
        )

    def test_no_discovered_file_is_gitignored(self) -> None:
        ignored = sorted(str(p.relative_to(REPO_ROOT)) for p in DISCOVERED if _is_ignored(p))
        self.assertEqual(
            ignored,
            [],
            f"{MEMORY_FILENAME} file(s) that git IGNORES: {ignored}. A plain "
            "`git add` will never pick these up; move the file out of the "
            "ignored directory instead of forcing it in.",
        )


if __name__ == "__main__":
    unittest.main()

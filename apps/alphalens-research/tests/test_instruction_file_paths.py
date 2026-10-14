"""Every path an instruction file names must exist, because a session will open it.

A `CLAUDE.md` is read and ACTED ON by every session in this repo, so a path in one
is an instruction, not a decoration. Three dead paths were fixed by hand in two
days:

* ``alphalens_pipeline/paper/calendar.py`` (#1763) — moved to ``market/`` by
  #1687. A session followed the rule and got an ``ImportError``.
* ``alphalens_cli/commands/research.py`` (#1763) — deleted with the
  closed-paradigm tree, while ``commands/broker.py`` points at ``CLAUDE.md`` for
  the authority. Code cited the doc, the doc cited nothing.
* ``~/.alphalens/ivolatility_cache/`` (#1773) — never existed at all.

Each was found by a person reading the file. Nothing checked them, and the
restructure that cut the root file to 92 lines (#1782) moved most of the prose
into seven area files, multiplying the places a path can rot.

WHY THIS GATE IS MOSTLY EXTRACTION RULES. A naive version — every backticked
token containing a slash must resolve from the repo root — reports 34 failures on
a clean tree, of which about three quarters are legitimate prose: glob
placeholders (``<name>/``, ``**``, ``{audit,preaudit}.py``), bare basenames whose
directory the sentence states in words, git refs (``origin/main``), bare
extensions (``.md``), and out-of-repo paths. A gate that cries that often is a
gate an operator switches off, so each of those shapes is a stated rule here,
not a silent skip:

* a token carrying a glob or placeholder metacharacter is not a claim about one
  file, so it is skipped;
* a bare basename resolves if ANY tracked file has that name — the sentence
  around it carries the directory, and pinning the directory too would be a
  second, weaker claim;
* a ``file.py:158`` suffix is stripped, because the line number is a pointer and
  not part of the path;
* a path that is genuinely outside the repo or deliberately dead goes in
  :data:`ALLOWED`, WITH ITS REASON.

:data:`ALLOWED` is red in both directions. An entry whose token no longer appears
in any instruction file, or which now resolves, fails this suite — otherwise the
allowlist quietly becomes the place where dead paths go to live, which is the
failure mode of every unreviewed exemption list in this repo.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# A backticked run with no whitespace. The instruction files use single backticks
# for every path, symbol and command they name.
_TOKEN = re.compile(r"`([^`\s]+)`")

# Metacharacters that make a token a PATTERN rather than one path.
_PLACEHOLDER = set("<>*{}?")

# Extensions that make a bare basename a path claim even without a slash.
_PATH_SUFFIXES = (
    ".py",
    ".md",
    ".ts",
    ".svelte",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".service",
    ".timer",
    ".parquet",
    ".jsonl",
    ".txt",
    ".lock",
)

# Resolution is SUFFIX matching against the tracked tree, not prefix guessing.
# The files spell a path however the sentence reads best — the root file writes
# `apps/alphalens-pipeline/alphalens_cli/commands/broker.py`, an area file writes
# `commands/broker.py`, and a fixture list writes `days/2026-05-18.json` under a
# directory the previous clause named. All three are correct prose, so a token is
# satisfied when some tracked path ENDS with it. The trade-off is deliberate: a
# bare `calendar.py` resolves against any `calendar.py` in the tree, so this gate
# proves the file exists somewhere and not that the stated directory is right.
# The defect it exists to catch is the dead path, and `paper/calendar.py` has no
# suffix match anywhere — which is why #1763's defect is a positive control below.

# Tokens that cannot resolve and must not. Each entry states WHY, because an
# exemption without a reason is indistinguishable from a defect someone gave up
# on. Both halves are checked: the token must still be named somewhere, and it
# must still fail to resolve.
ALLOWED: dict[str, str] = {
    "MEMORY.md": (
        "Auto-memory index, outside the repo under ~/.claude/projects/<slug>/memory/. "
        "Named because the worktree rule is about writing it safely."
    ),
    "ivolatility_cache/": (
        "Deliberately dead: the sentence exists to say this directory has NEVER "
        "existed, after the rule named it for months. Removing the token would "
        "remove the correction."
    ),
    "origin/main": "A git ref, not a path.",
    "alphalens.broker.arm/v2": "A JSON schema id emitted by `broker arm`, not a path.",
    "apps/web/build/": (
        "A build artefact. nginx bind-mounts it in the local Docker stack, so the "
        "rule exists to say `pnpm build` must run first; it is absent from a clean "
        "checkout by design."
    ),
}


def _tracked_instruction_files() -> list[Path]:
    """Every tracked ``CLAUDE.md``.

    Tracked, not globbed: this repository keeps sibling worktrees under
    ``.claude/worktrees/``, each with its own ``CLAUDE.md``, so a filesystem walk
    scans other branches and the result depends on which worktrees happen to
    exist on the machine.
    """
    out = subprocess.run(
        ["git", "ls-files", "*CLAUDE.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / rel for rel in out]


def tracked_tree() -> tuple[frozenset[str], frozenset[str]]:
    """``(files, directories)`` as repo-relative posix paths, from the index."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    files = frozenset(out)
    dirs: set[str] = set()
    for rel in out:
        parent = Path(rel).parent
        while parent != Path("."):
            dirs.add(parent.as_posix())
            parent = parent.parent
    return files, frozenset(dirs)


def is_path_claim(token: str) -> bool:
    """True when the token asserts that one file or directory exists."""
    if token.startswith(("~", "/", "http", "@", ".")):
        # Home-relative and absolute paths are runtime state, not repo content;
        # `@scope/pkg` is an npm package; a leading dot is a bare extension.
        return False
    if _PLACEHOLDER & set(token):
        return False
    if "..." in token:
        # An elided example path inside a sentence about layout.
        return False
    if token in ALLOWED:
        return False
    return "/" in token or token.endswith(_PATH_SUFFIXES)


def resolve(token: str, tree: tuple[frozenset[str], frozenset[str]]) -> bool:
    """True when some tracked path ends with the token."""
    files, dirs = tree
    bare = token.split("::", maxsplit=1)[0]
    bare = re.sub(r":\d+(-\d+)?$", "", bare).rstrip(":,.")
    wants_dir = bare.endswith("/")
    bare = bare.strip("/")
    if not bare:
        return False
    # A trailing slash means a directory. WITHOUT one the token may still be a
    # directory — `apps/web` and `deploy/docker/django-prod` are written that way
    # — so both haystacks are searched unless the author typed the slash.
    haystacks = (dirs,) if wants_dir else (files, dirs)
    return any(
        bare in haystack or any(entry.endswith("/" + bare) for entry in haystack)
        for haystack in haystacks
    )


def unresolved(
    files: list[Path], tree: tuple[frozenset[str], frozenset[str]]
) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for token in _TOKEN.findall(line):
                if is_path_claim(token) and not resolve(token, tree):
                    found.append((rel, number, token))
    return found


class EveryPathAnInstructionFileNamesExists(unittest.TestCase):
    """The gate itself."""

    def setUp(self) -> None:
        self.files = _tracked_instruction_files()
        self.tree = tracked_tree()

    def test_the_corpus_is_not_empty(self) -> None:
        """Premise: without this the suite passes by scanning nothing."""
        self.assertGreaterEqual(len(self.files), 2, "expected the root file plus area files")
        self.assertIn("CLAUDE.md", [p.relative_to(REPO_ROOT).as_posix() for p in self.files])

    def test_every_path_resolves(self) -> None:
        dead = unresolved(self.files, self.tree)
        self.assertEqual(
            dead,
            [],
            "An instruction file names a path that does not exist. Fix the path, or "
            "add the token to ALLOWED with the reason it cannot resolve:\n"
            + "\n".join(f"  {rel}:{num}  {tok}" for rel, num, tok in dead),
        )


class TheAllowlistIsRedInBothDirections(unittest.TestCase):
    """An exemption that stopped being needed must go, or the list stops meaning anything."""

    def setUp(self) -> None:
        self.files = _tracked_instruction_files()
        self.tree = tracked_tree()
        self.corpus = "\n".join(p.read_text() for p in self.files)

    def test_every_entry_is_still_named(self) -> None:
        for token in ALLOWED:
            with self.subTest(token=token):
                self.assertIn(
                    f"`{token}`",
                    self.corpus,
                    f"ALLOWED carries {token!r}, which no instruction file names any more. "
                    "Drop the entry.",
                )

    def test_no_entry_resolves(self) -> None:
        for token, reason in ALLOWED.items():
            with self.subTest(token=token):
                self.assertFalse(
                    resolve(token, self.tree),
                    f"ALLOWED carries {token!r} ({reason}), but it resolves now. "
                    "Drop the entry so the gate checks it.",
                )

    def test_every_entry_states_a_reason(self) -> None:
        for token, reason in ALLOWED.items():
            with self.subTest(token=token):
                self.assertGreater(len(reason.split()), 5, "a reason, not a label")


class TheGateCatchesWhatItIsFor(unittest.TestCase):
    """Positive controls over synthetic text, driven through the REAL predicates.

    Without these the rules above could rot to "nothing is a path claim" and the
    suite would still be green. Each case is one of the three real defects or one
    of the false-positive shapes the extraction rules exist to swallow.
    """

    def setUp(self) -> None:
        self.tree = tracked_tree()

    def _scan(self, text: str) -> list[str]:
        return [
            token
            for token in _TOKEN.findall(text)
            if is_path_claim(token) and not resolve(token, self.tree)
        ]

    def test_it_catches_a_moved_module(self) -> None:
        """#1763's first defect, in its original wording."""
        self.assertEqual(
            self._scan("helpers (`alphalens_pipeline/paper/calendar.py` + `sizing.py`)"),
            ["alphalens_pipeline/paper/calendar.py"],
        )

    def test_it_catches_a_deleted_module(self) -> None:
        """#1763's second defect."""
        self.assertEqual(
            self._scan("`apps/alphalens-pipeline/alphalens_cli/commands/research.py` does NOT"),
            ["apps/alphalens-pipeline/alphalens_cli/commands/research.py"],
        )

    def test_it_catches_a_directory_that_never_existed(self) -> None:
        """#1773's defect, with the token spelled as a path rather than allowlisted."""
        self.assertEqual(
            self._scan("persist to `docs/ivolatility_cache/` BEFORE processing"),
            ["docs/ivolatility_cache/"],
        )

    def test_it_swallows_a_glob_and_a_placeholder(self) -> None:
        text = (
            "goes to `apps/alphalens-pipeline/alphalens_pipeline/<name>/`, see "
            "`alphalens_cli/commands/*.py` and `commands/{audit,preaudit}.py`"
        )
        self.assertEqual(self._scan(text), [])

    def test_it_swallows_a_bare_extension_and_a_git_ref(self) -> None:
        self.assertEqual(self._scan("a `.md` file off fresh `origin/main`"), [])

    def test_it_accepts_a_basename_whose_directory_is_in_the_prose(self) -> None:
        """`test_module_dependencies.py` is named without its directory, and exists."""
        self.assertEqual(self._scan("Gate: `test_module_dependencies.py` walks it"), [])

    def test_it_accepts_a_line_suffix(self) -> None:
        self.assertEqual(self._scan("see `alphalens_cli/commands/broker.py:158` for it"), [])

    def test_it_accepts_an_area_relative_path(self) -> None:
        """An area file names its own app's path without the apps/ prefix."""
        self.assertEqual(self._scan("the seam in `thematic/sources/rss.py`"), [])

    def test_a_basename_that_exists_nowhere_is_still_caught(self) -> None:
        self.assertEqual(
            self._scan("run `test_a_file_that_does_not_exist_anywhere.py` first"),
            ["test_a_file_that_does_not_exist_anywhere.py"],
        )


if __name__ == "__main__":
    unittest.main()

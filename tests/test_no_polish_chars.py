"""Forbid Polish-specific letters in source code, tests, and CLI.

The user's policy is "English-only in code; Polish stays in CLAUDE.md, docs/,
memory, and commit messages." We use a custom check rather than ruff's
RUF001/002/003 because those rules also flag mathematical/scientific notation
(α t-stat, ρ correlation, × multiplication, − minus) which is legitimate quant
finance vocabulary in this project.

This test only catches characters that have no plausible math/quant meaning:
the Polish-specific letters ą, ę, ć, ł, ń, ś, ź, ż, ó (plus capitals).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

POLISH_CHARS_RE = re.compile(r"[ąęćłńśźżóĄĘĆŁŃŚŹŻÓ]")

SCAN_PACKAGES = (
    "alphalens",
    "alphalens_cli",
    "tests",
)

# This test file must contain the Polish letters in its regex; exempt itself
# rather than weaken the rule for the rest of the codebase.
EXEMPT_FILES = frozenset({"tests/test_no_polish_chars.py"})


def _python_sources():
    for pkg in SCAN_PACKAGES:
        for path in (REPO_ROOT / pkg).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if str(path.relative_to(REPO_ROOT)) in EXEMPT_FILES:
                continue
            yield path


class TestNoPolishChars(unittest.TestCase):
    def test_no_polish_letters_in_python_sources(self):
        offenders: list[tuple[str, int, str]] = []
        for path in _python_sources():
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if POLISH_CHARS_RE.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append((str(rel), lineno, line.strip()[:120]))

        self.assertEqual(
            offenders,
            [],
            "Polish-specific letters found in code:\n  "
            + "\n  ".join(f"{f}:{ln}: {snippet}" for f, ln, snippet in offenders),
        )


if __name__ == "__main__":
    unittest.main()

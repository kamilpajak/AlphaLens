"""GuruAgent prompt loader with pre-commit fingerprinting.

The prompt file is the primary "strategy config" — changing it counts toward
true_n_tests for multiple-testing correction (per R12 discipline). SHA-256
content hash + git SHA are captured at load time so reports can record exactly
which prompt produced which results.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GuruPromptError(ValueError):
    pass


class DirtyRepoError(RuntimeError):
    pass


@dataclass(frozen=True)
class GuruPrompt:
    text: str
    content_sha256: str
    git_sha: str
    path: str


def _capture_git_sha(*, allow_dirty: bool) -> str:
    if not allow_dirty:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        if porcelain.stdout.strip():
            raise DirtyRepoError("working tree dirty; commit prompt changes before running pilot")
    rev_parse = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return rev_parse.stdout.strip()


def load_guru_prompt(path: Path, *, allow_dirty: bool = False) -> GuruPrompt:
    path = Path(path)
    if not path.exists():
        raise GuruPromptError(f"prompt file not found: {path}")
    text = path.read_text()
    if not text.strip():
        raise GuruPromptError(f"prompt file is empty: {path}")
    content_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    git_sha = _capture_git_sha(allow_dirty=allow_dirty)
    return GuruPrompt(
        text=text,
        content_sha256=content_sha,
        git_sha=git_sha,
        path=str(path),
    )

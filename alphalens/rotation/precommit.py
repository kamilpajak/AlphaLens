"""Pre-commit discipline helpers for Tactical Sector Rotation.

The R12 protocol demands that *true multiple-testing burden* equals the number
of config-changing commits between IS baseline and OOS evaluation, not merely
``n=2`` (H1 + H2). This module exposes:

- ``count_config_commits(path)``   — total commits touching the config file
- ``check_oos_discipline(path, is_baseline_sha, baseline_n_tests)`` — how many
  commits since IS baseline, what true_n_tests is, whether the run is clean
- ``record_run(runlog, fingerprint, ...)`` — append a JSON line to the audit log

These are best-effort helpers, not gates. The CLI can print a warning when
``DisciplineStatus.clean`` is False; enforcement is cultural, not technical.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from alphalens.rotation.config import ConfigFingerprint


def count_config_commits(path: Path) -> int:
    """Count commits in the current branch that touched ``path``."""
    result = subprocess.run(
        ["git", "log", "--oneline", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return len([ln for ln in result.stdout.splitlines() if ln.strip()])


@dataclass(frozen=True)
class DisciplineStatus:
    clean: bool
    commits_since_is: int
    true_n_tests: int
    message: str


def check_oos_discipline(
    *,
    config_path: Path,
    is_baseline_sha: str,
    baseline_n_tests: int = 2,
) -> DisciplineStatus:
    """Estimate commits made to the config AFTER the IS baseline SHA.

    Heuristic: git log returns commits newest → oldest. We count commits newer
    than the baseline (i.e. appearing ABOVE the baseline row in the log).
    """
    result = subprocess.run(
        ["git", "log", "--oneline", str(config_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]

    commits_since = 0
    for line in lines:
        sha_fragment = line.split()[0] if line else ""
        if sha_fragment.startswith(
            is_baseline_sha[: len(sha_fragment)]
        ) or is_baseline_sha.startswith(sha_fragment):
            break
        commits_since += 1

    true_n_tests = commits_since + baseline_n_tests
    clean = commits_since == 0
    if clean:
        msg = "no config changes since IS baseline"
    else:
        msg = (
            f"{commits_since} config-changing commit(s) since IS baseline; "
            f"true Bonferroni n = {true_n_tests}"
        )
    return DisciplineStatus(
        clean=clean,
        commits_since_is=commits_since,
        true_n_tests=true_n_tests,
        message=msg,
    )


def record_run(
    *,
    runlog_path: Path,
    fingerprint: ConfigFingerprint,
    split: str,
    start: str,
    end: str,
    n_rebalances: int,
    sharpe_net: float,
    notes: str | None = None,
) -> None:
    runlog_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": _dt.datetime.now(_dt.UTC).isoformat(),
        "git_sha": fingerprint.git_sha,
        "content_sha256": fingerprint.content_sha256,
        "config_path": fingerprint.config_path,
        "split": split,
        "start": start,
        "end": end,
        "n_rebalances": n_rebalances,
        "sharpe_net": sharpe_net,
        "notes": notes,
    }
    with runlog_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")

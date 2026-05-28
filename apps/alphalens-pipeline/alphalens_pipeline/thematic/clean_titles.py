"""Legacy-title backfill for thematic_briefs parquets.

PR #259 fixed GDELT space-padded titles at the ingest source ("forward-only
… backfill not included"). Rows ingested before that — both in the parquet
source-of-truth at ``~/.alphalens/thematic_briefs/`` and in the Django
``Brief`` table downstream — still carry the padded form
("weekend , citing", "Alphabet ( Google )"). This module cleans them
in-place in the parquets; the operator then runs
``manage.py rebuild_briefs_cache --force`` so the Postgres mirror refreshes
from the cleaned parquets.

The CLI wrapper is ``alphalens thematic clean-titles``. It targets
``source_event_title`` only — all other text columns originate from LLM
output (no GDELT padding pathology).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from alphalens_pipeline.thematic.sources.gdelt import clean_title

logger = logging.getLogger(__name__)

DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"
TARGET_COLUMN = "source_event_title"


@dataclass(frozen=True)
class CleanResult:
    """Per-file (path, rows_cleaned) plus totals across the whole directory."""

    per_file: list[tuple[Path, int]]
    total_rows_cleaned: int
    files_touched: int


def _clean_series(s: pd.Series) -> tuple[pd.Series, int]:
    """Apply ``clean_title`` to non-null entries; return new series + delta count."""
    # Preserve None / NaN exactly — Django field is blank=True and arrives null
    # for some legacy rows; clean_title("") on null would coerce to "".
    cleaned = s.where(s.isna(), s.fillna("").astype(str).map(clean_title))
    # Compare element-wise; both branches keep NaN where original was NaN.
    changed = (cleaned != s) & s.notna()
    return cleaned, int(changed.sum())


def clean_titles_in_parquet_dir(briefs_dir: Path, *, dry_run: bool = False) -> CleanResult:
    """Walk every ``*.parquet`` under ``briefs_dir`` and clean ``source_event_title``.

    Files that do not carry the column are skipped (older snapshots pre-date
    it). Files where no row actually changes are NOT rewritten — keeps mtime
    intact so the ``rebuild_briefs_cache`` mtime gate stays accurate. With
    ``dry_run=True`` the function counts what it would clean but writes
    nothing. Idempotent: re-running on a cleaned tree is a no-op.
    """
    per_file: list[tuple[Path, int]] = []
    total = 0
    touched = 0
    for path in sorted(briefs_dir.glob("*.parquet")):
        df = pd.read_parquet(path)
        if TARGET_COLUMN not in df.columns:
            logger.info("skip %s — no %s column", path.name, TARGET_COLUMN)
            continue
        cleaned, delta = _clean_series(df[TARGET_COLUMN])
        per_file.append((path, delta))
        total += delta
        if delta == 0:
            continue
        if not dry_run:
            df[TARGET_COLUMN] = cleaned
            df.to_parquet(path)
            touched += 1
        logger.info(
            "%s %s: cleaned %d row(s)", "would clean" if dry_run else "cleaned", path.name, delta
        )
    return CleanResult(per_file=per_file, total_rows_cleaned=total, files_touched=touched)

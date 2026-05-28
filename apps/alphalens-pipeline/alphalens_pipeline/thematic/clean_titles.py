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
import os
import tempfile
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


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write parquet via temp-file + os.replace so a crash mid-write can't
    leave a half-written file in place of the source-of-truth original."""
    # delete=False + manual replace: NamedTemporaryFile would unlink on close.
    # Keep the temp file alongside the target so os.replace is intra-filesystem.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        df.to_parquet(tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def clean_titles_in_parquet_dir(briefs_dir: Path, *, dry_run: bool = False) -> CleanResult:
    """Walk every ``*.parquet`` under ``briefs_dir`` and clean ``source_event_title``.

    Files that do not carry the column are skipped (older snapshots pre-date
    it). Files where no row actually changes are NOT rewritten — keeps mtime
    intact so the ``rebuild_briefs_cache`` mtime gate stays accurate. Writes
    go through a temp file + ``os.replace`` so a crash mid-write can't
    corrupt the source-of-truth parquet. A single unreadable file is logged
    and skipped rather than aborting the whole sweep — the operator can
    inspect the offender separately. With ``dry_run=True`` the function
    counts what it would clean but writes nothing. Idempotent: re-running
    on a cleaned tree is a no-op.
    """
    per_file: list[tuple[Path, int]] = []
    total = 0
    touched = 0
    for path in sorted(briefs_dir.glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
        except Exception:  # pragma: no cover — defensive against corrupt files
            # ``logger.exception`` attaches the active traceback automatically.
            logger.exception("skip %s — unreadable parquet", path.name)
            continue
        if TARGET_COLUMN not in df.columns:
            logger.info("skip %s — no %s column", path.name, TARGET_COLUMN)
            continue
        cleaned, delta = _clean_series(df[TARGET_COLUMN])
        per_file.append((path, delta))
        total += delta
        if delta == 0:
            continue
        if dry_run:
            # Reflect the "would touch" set in the summary so dry-run output
            # matches the real-run output shape one-for-one.
            touched += 1
        else:
            df[TARGET_COLUMN] = cleaned
            _atomic_write_parquet(df, path)
            touched += 1
        logger.info(
            "%s %s: cleaned %d row(s)", "would clean" if dry_run else "cleaned", path.name, delta
        )
    return CleanResult(per_file=per_file, total_rows_cleaned=total, files_touched=touched)

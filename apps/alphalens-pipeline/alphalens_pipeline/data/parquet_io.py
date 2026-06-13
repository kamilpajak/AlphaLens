"""Atomic parquet write — temp file + ``os.replace`` so a crash mid-write can't
leave a half-written file in place of a source-of-truth original.

The temp file is created in the SAME directory as the target so ``os.replace`` is
an intra-filesystem rename (atomic; no cross-device ``EXDEV``). One canonical
implementation shared by the thematic title cleaner and the experts enrichment
driver — both rewrite a daily brief parquet that the 6x/day Django re-ingest reads
concurrently, so a truncated-read race must be impossible.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd


def write_parquet_atomic(df: pd.DataFrame, path: Path, *, index: bool = True) -> None:
    """Write ``df`` to ``path`` atomically (temp file in ``path.parent`` + replace).

    ``index`` defaults to ``True`` to match :func:`pandas.DataFrame.to_parquet`'s
    own default — callers that previously wrote with the bare default keep their
    exact bytes. The brief-enrichment path passes ``index=False`` (its frames carry
    a meaningless ``RangeIndex``).

    On any failure the temp file is removed and the exception re-raised, leaving the
    pre-existing target untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        df.to_parquet(tmp_path, index=index)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


__all__ = ["write_parquet_atomic"]

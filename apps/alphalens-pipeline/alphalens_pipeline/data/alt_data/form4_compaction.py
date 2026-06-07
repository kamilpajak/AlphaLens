"""Form-4 parquet compaction — merge per-year part files into one.

After an append (bulk backfill flush or daily-incremental write), each
``transaction_year=YYYY`` partition can contain several
``part-<ts>-<hex>.parquet`` files. ``pyarrow.dataset`` open time scales
linearly with file count, so this compactor merges every part file in a
year-partition into a single ``compacted.parquet`` and atomically removes
the originals.

Idempotent: a no-op on an already-compacted partition, and re-running after a
new append correctly merges the new ``part-*.parquet`` with the existing
``compacted.parquet``. Dedup is a full-row ``drop_duplicates`` — since
``accession_number`` is unique, overlapping re-fetches (the daily-incremental
lookback window deliberately overlaps the previous run) collapse here.

This lives in the pipeline package because it operates on the pipeline's own
PIT parquet store: both the bulk backfill (research-side runner) and the live
daily-incremental engine compact through this single primitive, so neither has
to reach across the workspace split for it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

_COMPACTED_NAME = "compacted.parquet"


def compact_partition(partition_dir: Path) -> None:
    """Merge all ``*.parquet`` files in ``partition_dir`` into ``compacted.parquet``.

    No-op when the partition is empty or already contains a single
    ``compacted.parquet`` and no other part files. When other files coexist
    with ``compacted.parquet`` (e.g. a fresh append), all are merged into a
    new ``compacted.parquet``.
    """
    files = sorted(partition_dir.glob("*.parquet"))
    if not files:
        return
    if len(files) == 1 and files[0].name == _COMPACTED_NAME:
        return

    logger.info("compacting %s: %d files", partition_dir, len(files))

    table = ds.dataset(
        [str(f) for f in files],
        partitioning=None,
        format="parquet",
    ).to_table()

    # Dedup: a write appends part files before the run is marked done; a
    # resumed run or an overlapping daily lookback re-writes the same rows with
    # a fresh filename. Collapse them so the compacted dataset is the
    # unique-row truth (accession_number is unique, so a re-fetch is identical).
    n_before = table.num_rows
    df = table.to_pandas().drop_duplicates()
    if len(df) < n_before:
        logger.info("compact %s: deduped %d -> %d rows", partition_dir, n_before, len(df))
    table = pa.Table.from_pandas(df, schema=table.schema, preserve_index=False)

    # Write to a temp file, then atomically rename. Only delete the originals
    # after the rename succeeds — guards against a half-finished merge leaving
    # the partition inconsistent.
    tmp_path = partition_dir / f"{_COMPACTED_NAME}.tmp"
    pq.write_table(table, tmp_path)
    final_path = partition_dir / _COMPACTED_NAME
    tmp_path.replace(final_path)

    for f in files:
        if f == final_path:
            continue
        f.unlink()


def compact_root(parquet_root: Path) -> None:
    """Compact every ``transaction_year=*`` partition under ``parquet_root``."""
    if not parquet_root.is_dir():
        logger.warning("parquet root does not exist: %s", parquet_root)
        return
    for entry in sorted(parquet_root.iterdir()):
        if not entry.is_dir():
            continue
        if not entry.name.startswith("transaction_year="):
            continue
        compact_partition(entry)

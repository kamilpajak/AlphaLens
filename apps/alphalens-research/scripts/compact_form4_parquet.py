"""Form-4 parquet compaction — merge per-year part files into one.

After the multi-day backfill, each ``transaction_year=YYYY`` partition
contains one ``part-<ts>-<hex>.parquet`` per flush batch (typically
hundreds for big years, after the orchestrator's batched-flush rewrite).
``pyarrow.dataset`` open time scales linearly with file count: a query
against ~100k tiny files spends 30+ seconds in ``stat()`` syscalls before
reading a single byte of data.

This compactor merges every part file in each year-partition into a
single ``compacted.parquet``, then atomically removes the originals.
Idempotent: a no-op on already-compacted partitions, and re-running after
a resumed backfill correctly merges the new ``part-*.parquet`` files with
the existing ``compacted.parquet``.

Usage::

  .venv/bin/python scripts/compact_form4_parquet.py \\
      --parquet-root ~/.alphalens/form4_parquet
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

_COMPACTED_NAME = "compacted.parquet"


def compact_partition(partition_dir: Path) -> None:
    """Merge all ``*.parquet`` files in ``partition_dir`` into ``compacted.parquet``.

    No-op when the partition is empty or already contains a single
    ``compacted.parquet`` and no other part files. When other files
    coexist with ``compacted.parquet`` (e.g. resumed backfill), all are
    merged into a fresh ``compacted.parquet``.
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

    # Dedup: _flush_batch writes parquet before marking CIKs complete in
    # the manifest. If the run dies in between, the next resume re-fetches
    # the in-buffer CIKs and writes a SECOND parquet file with duplicate
    # rows (timestamp+hex suffix prevents filename collision). Collapse
    # them here so the compacted dataset is the unique-row truth.
    n_before = table.num_rows
    df = table.to_pandas().drop_duplicates()
    if len(df) < n_before:
        logger.info("compact %s: deduped %d -> %d rows", partition_dir, n_before, len(df))
    table = pa.Table.from_pandas(df, schema=table.schema, preserve_index=False)

    # Write to a temp file, then atomically rename. If the rename succeeds,
    # only then delete the originals — guards against half-finished merges
    # leaving the partition inconsistent.
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--parquet-root",
        type=Path,
        default=Path.home() / ".alphalens" / "form4_parquet",
        help="Root of the hive-partitioned Form-4 parquet store.",
    )
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    compact_root(args.parquet_root)
    logger.info("Compaction complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

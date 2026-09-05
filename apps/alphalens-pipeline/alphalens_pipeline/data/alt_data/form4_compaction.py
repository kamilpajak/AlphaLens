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


def _dedup_null_vs_concrete_is_other(df):
    """Collapse rows identical except a null-vs-concrete ``is_other``.

    A catch-up overlap re-fetches rows that already sit in a legacy compacted
    file without the is_other column (null after schema unification). Full-row
    dedup treats null != False and would keep BOTH copies, double-counting the
    trade forever. Rows identical in accession+owner+tx come from the same XML
    node, so the concrete value supersedes the unknown. No-op when the column
    is absent or has no nulls (uniform-schema partitions keep today's exact
    behavior).
    """
    if "is_other" not in df.columns or not df["is_other"].isna().any():
        return df
    subset = [c for c in df.columns if c != "is_other"]
    deduped = (
        df.sort_values("is_other", na_position="last", kind="stable")
        .drop_duplicates(subset=subset, keep="first")
        .sort_index()
    )
    return deduped


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

    # Unify schemas across fragments: without an explicit schema the dataset
    # infers it from the FIRST fragment only, and "compacted.parquet" sorts
    # before "part-*", so a column added to the writer (is_other, #82) would
    # be silently projected away on every re-compaction of a legacy
    # partition. Fragments missing a unified field read as nulls.
    #
    # ``permissive`` because the live store mixes arrow WIDTHS for the same
    # logical column: every legacy compacted.parquet carries `string` while
    # the current writer emits `large_string` (measured on all 35 partitions,
    # 2026-09-05). Both paths were measured on those files, not assumed: the
    # pre-#82 inference reads them fine (it casts the wider parts down to
    # `string`), while the strict default raises ArrowTypeError — which is
    # what took the daily ingest down for a cycle (#1329).
    #
    # Permissive is right for THIS call site specifically: one writer owns
    # every fragment here (the seed backfill and the daily incremental both
    # go through ``write_records_to_parquet``), so a difference is an arrow
    # width/nullability artifact, never two sources disagreeing about a
    # column's meaning. It is not blanket-safe: permissive also widens
    # int32->int64 and int64->double silently. That is unreachable today
    # because the store has NO integer column (the only numerics are the two
    # `double` transaction fields) — revisit this call site if one is added.
    unified = pa.unify_schemas([pq.read_schema(f) for f in files], promote_options="permissive")
    table = ds.dataset(
        [str(f) for f in files],
        partitioning=None,
        format="parquet",
        schema=unified,
    ).to_table()

    # Dedup: a write appends part files before the run is marked done; a
    # resumed run or an overlapping daily lookback re-writes the same rows with
    # a fresh filename. Collapse them so the compacted dataset is the
    # unique-row truth (accession_number is unique, so a re-fetch is identical).
    n_before = table.num_rows
    df = table.to_pandas().drop_duplicates()
    df = _dedup_null_vs_concrete_is_other(df)
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

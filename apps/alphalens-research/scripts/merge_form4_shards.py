"""Form-4 cross-shard merge — strict 4-digit canonical copy with quarantine DLQ.

The Form-4 backfill is split across N CIK shards (Mac + VPS round-robin
per ``scripts/split_cik_list.py``). After each shard's per-tree
compaction, this script unions them: source partitions are copied into
target partitions, with filename collisions sidestepped by appending a
short random suffix.

**Strict 4-digit policy.** Only ``transaction_year=YYYY`` partitions
where ``YYYY`` is exactly 4 digits are treated as canonical. Anything
else (``=22``, ``=15``, ``=999``, …) is **malformed**: the upstream
``parse_form4_xml`` step occasionally interprets a 2-digit transaction
date as year-22-AD, producing a directory whose name AND underlying
``transaction_date`` column are both wrong. Canonicalising the directory
name without touching the column would create a parquet file whose
partition path says ``=2022`` while its column statistics say year 22 —
silently corrupt under predicate pushdown. We refuse to do that.

**Dead-letter queue.** With ``--quarantine-malformed PATH``, malformed
partitions are *moved* (preserving partition layout) to PATH for later
forensic inspection. Without it, malformed partitions are deleted.

**Target-side cleanup.** The target tree is also swept for malformed
orphans before the copy begins — Mac shard 1 of the 2026-05-08 backfill
discovered an orphan ``=15`` partition from the same upstream bug.

Sharding is round-robin by CIK so shards are CIK-disjoint and therefore
accession-disjoint; no genuine cross-shard duplicates exist. Within-shard
flush/resume duplicates are already collapsed by the per-shard compactor
(``scripts/compact_form4_parquet.py``) before archive.

Usage::

  .venv/bin/python scripts/merge_form4_shards.py \\
      --source /tmp/vps_form4 \\
      --target ~/.alphalens/form4_parquet \\
      [--quarantine-malformed ~/.alphalens/form4_quarantine] \\
      [--compact]
"""

from __future__ import annotations

import argparse
import logging
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.compact_form4_parquet import compact_root

logger = logging.getLogger(__name__)

_PARTITION_RE = re.compile(r"^transaction_year=(\d+)$")

# Exit code emitted when malformed partitions were dropped with no
# --quarantine-malformed dir: the merge irrecoverably deleted data, so the run
# must NOT look green to automation or the operator. NOTE: main() RETURNS this
# code; callers MUST propagate it (the __main__ entry point does via
# sys.exit(main())). A programmatic wrapper that ignores main()'s return value
# would swallow the data-loss signal.
_EXIT_DATA_LOSS = 2


def _classify_partition(name: str) -> str:
    """Return ``"canonical"`` (4-digit year), ``"malformed"`` (non-4-digit
    year), or ``"non_partition"`` (does not match the partition naming
    convention at all).
    """
    m = _PARTITION_RE.match(name)
    if not m:
        return "non_partition"
    return "canonical" if len(m.group(1)) == 4 else "malformed"


def _unique_target_path(target_partition: Path, source_filename: str) -> Path:
    """Pick a non-colliding filename in ``target_partition``.

    If ``source_filename`` doesn't already exist in target, keep it as-is.
    Otherwise append a timestamp+hex suffix, mirroring the convention used
    by ``write_records_to_parquet`` for part-* files.
    """
    candidate = target_partition / source_filename
    if not candidate.exists():
        return candidate
    stem = Path(source_filename).stem
    suffix = Path(source_filename).suffix
    unique = f"{stem}-merged-{int(time.time() * 1000)}-{secrets.token_hex(4)}{suffix}"
    return target_partition / unique


def _handle_malformed_partition(
    partition_dir: Path,
    quarantine_dir: Path | None,
) -> tuple[int, int]:
    """Quarantine (preserving layout) or delete a malformed partition.

    Returns ``(quarantined, deleted)``: the number of files moved to
    quarantine and the number of files irrecoverably deleted (the latter is
    non-zero only when ``quarantine_dir`` is None). Counting the deleted
    files before ``rmtree`` is what lets the merge report data loss instead of
    under-reporting it as zero.
    """
    files = list(partition_dir.glob("*.parquet"))
    quarantined = 0
    deleted = 0
    if quarantine_dir is not None:
        dest_partition = quarantine_dir / partition_dir.name
        dest_partition.mkdir(parents=True, exist_ok=True)
        for src in files:
            dst = _unique_target_path(dest_partition, src.name)
            shutil.move(str(src), str(dst))
            quarantined += 1
    else:
        deleted = len(files)
    # shutil.rmtree handles both the "files moved" and "files still there" cases.
    shutil.rmtree(partition_dir)
    return quarantined, deleted


def _sweep_malformed(
    root: Path,
    quarantine_dir: Path | None,
) -> tuple[int, int, int]:
    """Walk ``root`` once; quarantine/delete any malformed partition.

    Returns ``(malformed_count, quarantined_files, deleted_files)``.
    """
    malformed_count = 0
    quarantined_files = 0
    deleted_files = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if _classify_partition(entry.name) != "malformed":
            continue
        quarantined, deleted = _handle_malformed_partition(entry, quarantine_dir)
        quarantined_files += quarantined
        deleted_files += deleted
        malformed_count += 1
    return malformed_count, quarantined_files, deleted_files


def merge_shards(
    source_root: Path,
    target_root: Path,
    *,
    quarantine_dir: Path | None = None,
) -> dict[str, int]:
    """Copy ``source_root``'s canonical partitions into ``target_root``.

    Both trees use hive layout ``transaction_year=YYYY/``. Strict
    4-digit canonical partitions are copied (not moved); malformed
    partitions on either side are quarantined or dropped.

    Args:
        source_root: Root of the shard's parquet tree (post per-shard compact).
        target_root: Root to merge into. Created if missing. Existing
            malformed partitions in target are also cleaned (target may
            have its own orphans from the same upstream bug).
        quarantine_dir: If set, malformed partitions move here preserving
            their original directory name. If None, malformed partitions
            are deleted.

    Returns:
        Stats dict: ``partitions_merged``, ``files_copied``,
        ``source_malformed_partitions``, ``target_malformed_partitions``,
        ``files_quarantined``, ``files_deleted``. ``files_deleted`` counts
        files irrecoverably dropped because no ``quarantine_dir`` was set —
        it is the data-loss figure the merge must surface, not under-report.
    """
    if not source_root.is_dir():
        raise ValueError(f"source_root does not exist or is not a directory: {source_root}")
    target_root.mkdir(parents=True, exist_ok=True)

    stats = {
        "partitions_merged": 0,
        "files_copied": 0,
        "source_malformed_partitions": 0,
        "target_malformed_partitions": 0,
        "files_quarantined": 0,
        "files_deleted": 0,
    }

    # files_deleted accumulates from two handlers with the SAME meaning
    # ("files that did not survive into the target"): the target-side handler
    # counts files removed by rmtree, the source-side handler counts files it
    # skipped (never copied). Keep them in one bucket only because both feed the
    # same data-loss exit-code decision below — if either contract changes, split
    # the accounting so the exit code cannot silently skew.
    # Step 1: clean malformed orphans in the target (e.g. Mac =15).
    tgt_malformed, tgt_quarantined, tgt_deleted = _sweep_malformed(target_root, quarantine_dir)
    stats["target_malformed_partitions"] = tgt_malformed
    stats["files_quarantined"] += tgt_quarantined
    stats["files_deleted"] += tgt_deleted

    # Step 2: walk source, classify, copy canonical, quarantine malformed.
    for entry in sorted(source_root.iterdir()):
        if not entry.is_dir():
            continue
        kind = _classify_partition(entry.name)
        if kind == "non_partition":
            logger.info("skipping non-partition entry: %s", entry.name)
            continue
        if kind == "malformed":
            quarantined, dropped = _quarantine_source_partition(entry, quarantine_dir)
            stats["source_malformed_partitions"] += 1
            stats["files_quarantined"] += quarantined
            stats["files_deleted"] += dropped
            continue
        # canonical
        target_partition = target_root / entry.name
        target_partition.mkdir(parents=True, exist_ok=True)

        copied_in_partition = 0
        for src_file in sorted(entry.glob("*.parquet")):
            dst = _unique_target_path(target_partition, src_file.name)
            shutil.copy2(src_file, dst)
            copied_in_partition += 1

        if copied_in_partition:
            stats["partitions_merged"] += 1
            stats["files_copied"] += copied_in_partition

    return stats


def _quarantine_source_partition(
    partition_dir: Path, quarantine_dir: Path | None
) -> tuple[int, int]:
    """Source-side malformed handling: COPY into quarantine (preserve source
    tree intact), or just count the lost files if no quarantine_dir.

    Returns ``(quarantined, dropped)``. The source tree is left intact in
    both branches (canonical merge copies, never moves), so ``dropped`` counts
    the files that would NOT survive into the target — the loss the merge must
    surface rather than swallow as zero.
    """
    files = list(partition_dir.glob("*.parquet"))
    if quarantine_dir is None:
        return 0, len(files)
    dest_partition = quarantine_dir / partition_dir.name
    dest_partition.mkdir(parents=True, exist_ok=True)
    quarantined = 0
    for src in files:
        dst = _unique_target_path(dest_partition, src.name)
        shutil.copy2(src, dst)
        quarantined += 1
    return quarantined, 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", type=Path, required=True, help="Source shard parquet root.")
    ap.add_argument("--target", type=Path, required=True, help="Target tree to merge into.")
    ap.add_argument(
        "--quarantine-malformed",
        type=Path,
        default=None,
        help="If set, move malformed partitions (non-4-digit year) to this "
        "directory preserving their original layout. If unset, malformed "
        "partitions are deleted.",
    )
    ap.add_argument(
        "--compact",
        action="store_true",
        help="Run scripts/compact_form4_parquet.compact_root on target after merge.",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    stats = merge_shards(
        args.source,
        args.target,
        quarantine_dir=args.quarantine_malformed,
    )
    logger.info("merge stats: %s", stats)

    if args.compact:
        logger.info("running compact_root on %s", args.target)
        compact_root(args.target)
        logger.info("compaction complete")

    # Fail loud on irrecoverable loss: malformed partitions were dropped and
    # no quarantine dir was given to preserve them. Report the partitions and
    # file counts at ERROR level and exit non-zero so a future malformed
    # partition surfaces instead of being swallowed by a green exit-0 run.
    malformed_dropped = (
        args.quarantine_malformed is None
        and (stats["source_malformed_partitions"] + stats["target_malformed_partitions"]) > 0
    )
    if malformed_dropped:
        logger.error(
            "DATA LOSS: deleted %d malformed partition(s) (%d source, %d target) "
            "totalling %d parquet file(s) with NO --quarantine-malformed dir. "
            "Re-run with --quarantine-malformed to preserve them for inspection.",
            stats["source_malformed_partitions"] + stats["target_malformed_partitions"],
            stats["source_malformed_partitions"],
            stats["target_malformed_partitions"],
            stats["files_deleted"],
        )
        return _EXIT_DATA_LOSS

    return 0


if __name__ == "__main__":
    sys.exit(main())

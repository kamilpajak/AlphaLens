"""Split a CIK universe into N shards for parallel SEC backfill across IPs.

SEC EDGAR's polite-rate limit is per-IP (10 req/s). With multiple machines
on distinct IPs, the backfill walks through ~10x faster: each machine
fetches its assigned shard with no contention against the others.

Round-robin sharding interleaves small and large filers so no single
machine ends up stuck on a long tail of prolific issuers (the input list
is typically sorted by CIK, which correlates with filer age and filing
volume).

Usage::

  .venv/bin/python scripts/split_cik_list.py \\
      data/form4_cik_universe.txt \\
      --num-shards 5 \\
      --output-dir data/shards/

Produces ``data/shards/ciks_shard_{1..5}_of_5.txt``. Hand each file to a
different machine via the existing ``--cik-list`` flag of
``scripts/run_form4_backfill.py``.

Merging the parallel parquet outputs is a plain rsync of every machine's
``--parquet-root`` into a central tree, then ``compact_form4_parquet.py``
to produce one ``compacted.parquet`` per ``transaction_year=YYYY``
partition. See deploy/systemd/README.md for the full recipe.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)


def shard_cik_list(ciks: list[str], *, num_shards: int) -> list[list[str]]:
    """Split ``ciks`` into ``num_shards`` lists round-robin.

    Round-robin (vs contiguous slice) interleaves small and large filers
    across shards so each machine sees a representative mix.
    """
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")
    shards: list[list[str]] = [[] for _ in range(num_shards)]
    for idx, cik in enumerate(ciks):
        shards[idx % num_shards].append(cik)
    return shards


def write_shards(shards: list[list[str]], *, output_dir: Path) -> list[Path]:
    """Write one ``ciks_shard_{i}_of_{N}.txt`` file per shard. Returns paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    n = len(shards)
    paths: list[Path] = []
    for i, shard in enumerate(shards, start=1):
        path = output_dir / f"ciks_shard_{i}_of_{n}.txt"
        path.write_text("\n".join(shard) + ("\n" if shard else ""))
        paths.append(path)
    return paths


def _load_cik_list(path: Path) -> list[str]:
    """One CIK per line, 10-digit zero-padded; lines starting with '#' are comments.

    Mirrors :func:`scripts.run_form4_backfill._load_cik_list` semantics —
    must produce the same shard contents that the runner expects, so SEC
    submissions URLs (CIK{cik}.json) resolve correctly.
    """
    out: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cik_int = int(line)
        except ValueError:
            logger.warning("skipping non-numeric CIK line: %r", line)
            continue
        out.append(f"{cik_int:010d}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cik_list", type=Path, help="Input file: one CIK per line.")
    ap.add_argument(
        "--num-shards",
        type=int,
        required=True,
        help="Number of output shards (== number of machines).",
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for ciks_shard_{i}_of_{N}.txt files.",
    )
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ciks = _load_cik_list(args.cik_list)
    shards = shard_cik_list(ciks, num_shards=args.num_shards)
    paths = write_shards(shards, output_dir=args.output_dir)

    for path, shard in zip(paths, shards, strict=True):
        logger.info("wrote %s (%d CIKs)", path, len(shard))
    logger.info("split %d CIKs into %d shards.", len(ciks), len(shards))
    return 0


if __name__ == "__main__":
    sys.exit(main())

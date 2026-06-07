"""Form-4 parquet compaction CLI — merge per-year part files into one.

Thin operator wrapper around the canonical compaction primitive, which lives
in the pipeline package (``alphalens_pipeline.data.alt_data.form4_compaction``)
because both the bulk backfill and the live daily-incremental engine compact
the same PIT store. This script is just the command-line entry point.

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

from alphalens_pipeline.data.alt_data.form4_compaction import (
    compact_partition,  # re-exported for callers importing from this script
    compact_root,
)

logger = logging.getLogger(__name__)

__all__ = ["compact_partition", "compact_root", "main"]


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

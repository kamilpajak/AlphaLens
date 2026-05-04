"""Pre-flight integrity check on synced datasets before an experiment runs.

Confirms each dataset directory exists, contains the expected file count
range, and that a sampled subset is readable. Designed to fail fast before
a multi-hour experiment hits a missing-data error mid-run.

Usage on the pod:
    .venv/bin/python runpod/verify_data.py
    .venv/bin/python runpod/verify_data.py --strict   # fail on any warning
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# (dataset_relpath, min_files, max_files_warning, sample_size, file_glob).
# Maintenance note: thresholds are deliberately wide-banded but still
# constants — when a dataset refresh adds new sources or shifts the
# parquet count past `max_files_warning`, update both bounds here.
# False FAILs at this gate abort experiments before they start, which
# is the desired safety bias.
EXPECTED_DATASETS = [
    ("companyfacts_parquet", 2500, 4000, 5, "*.parquet"),
    ("ivolatility_smd", 100, 5000, 5, "*.parquet"),
    ("prices", 1000, 5000, 5, "*.parquet"),
    ("factors", 1, 100, 1, "*.csv"),
]


def _check_dataset(
    root: Path,
    relpath: str,
    min_files: int,
    max_files_warning: int,
    sample_size: int,
    glob: str,
) -> tuple[bool, str]:
    target = root / relpath
    if not target.exists():
        return False, f"missing dataset directory: {target}"
    files = sorted(target.glob(glob))
    n = len(files)
    if n < min_files:
        return False, f"{relpath}: {n} files < min {min_files}"

    rng = random.Random(0)
    sample = rng.sample(files, k=min(sample_size, n))
    for f in sample:
        try:
            size = f.stat().st_size
        except OSError as exc:
            return False, f"{relpath}: cannot stat {f}: {exc}"
        if size <= 0:
            return False, f"{relpath}: empty file {f}"

    msg = f"{relpath}: {n} files (sampled {len(sample)} ok)"
    if n > max_files_warning:
        msg += f" [WARN: count exceeds {max_files_warning}]"
    return True, msg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path.home() / ".alphalens",
        help="Root of the synced ~/.alphalens tree (default: ~/.alphalens)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail (exit 1) on any WARN message, not only on hard errors",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.data_root.exists():
        print(f"ERROR: data root {args.data_root} does not exist", file=sys.stderr)
        return 1

    print(f"Checking {args.data_root}")
    failures = 0
    warnings = 0
    for relpath, min_files, max_files, sample_size, glob in EXPECTED_DATASETS:
        ok, msg = _check_dataset(args.data_root, relpath, min_files, max_files, sample_size, glob)
        prefix = "[ok]  " if ok else "[FAIL]"
        print(f"{prefix} {msg}")
        if not ok:
            failures += 1
        elif "WARN" in msg:
            warnings += 1

    if failures:
        print(f"\n{failures} dataset(s) FAILED verification")
        return 1
    if warnings and args.strict:
        print(f"\n{warnings} warning(s) -- failing under --strict")
        return 1
    print("\nAll datasets verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

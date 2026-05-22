"""Convert ~/.alphalens/companyfacts/{cik}.json -> companyfacts_parquet/{cik}.parquet.

Idempotent: skips CIKs whose parquet already exists unless ``--force``. The
``--verify`` mode round-trips every entry through preprocessor + reader and
asserts the recovered (period_end, filed_date, val) tuples equal the source
JSON entries -- catching schema or parser regressions before they reach
production stores.

Layout invariant: source JSONs live in ``~/.alphalens/companyfacts/`` and stay
read-only post-migration; parquet output lives in
``~/.alphalens/companyfacts_parquet/`` (parallel directory, never overlapped).
The legacy JSON tree may be deleted manually after several days of confirmed
parquet operation.

Usage:
    .venv/bin/python scripts/migrate_companyfacts_to_parquet.py
    .venv/bin/python scripts/migrate_companyfacts_to_parquet.py --verify
    .venv/bin/python scripts/migrate_companyfacts_to_parquet.py --force --verify
    .venv/bin/python scripts/migrate_companyfacts_to_parquet.py --sample 10  # smoke
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq
from alphalens_research.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    companyfacts_json_to_parquet_table,
)

logger = logging.getLogger(__name__)

HOME = Path.home()
JSON_DIR = HOME / ".alphalens" / "companyfacts"
PARQUET_DIR = HOME / ".alphalens" / "companyfacts_parquet"


def _iter_json_paths(json_dir: Path, sample: int | None) -> Iterable[Path]:
    paths = sorted(json_dir.glob("*.json"))
    if sample is not None and sample < len(paths):
        rng = random.Random(0)
        paths = sorted(rng.sample(paths, sample))
    yield from paths


def _convert_one(json_path: Path, parquet_path: Path) -> tuple[bool, int]:
    """Convert one JSON -> parquet. Returns (success, n_rows_written)."""
    try:
        facts = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Skip %s: %s", json_path.name, exc)
        return False, 0

    table = companyfacts_json_to_parquet_table(facts)
    pq.write_table(table, parquet_path, compression="zstd")
    return True, table.num_rows


def _verify_one(json_path: Path, parquet_path: Path, reader: CompanyfactsParquetReader) -> bool:
    """Round-trip JSON -> parquet -> reader; assert source entries recoverable.

    SEC permits multiple entries that share (taxonomy, concept, unit,
    period_end, filed_date) but differ in ``accn`` / ``val`` (e.g. preliminary
    8-K vs subsequent 10-Q). We therefore treat the full 6-tuple
    (..., filed_date, val) as the verification key and assert the source set
    is a subset of the recovered set.
    """
    cik_padded = parquet_path.stem
    table = reader.get_cik_table(cik_padded)
    if table is None:
        logger.error("VERIFY: parquet missing for %s", cik_padded)
        return False

    recovered: set[tuple[str, str, str, str, str, float]] = set()
    for row in table.to_pylist():
        recovered.add(
            (
                row["taxonomy"],
                row["concept"],
                row["unit"],
                row["period_end"].isoformat(),
                row["filed_date"].isoformat(),
                float(row["val"]),
            )
        )

    facts = json.loads(json_path.read_text())
    missing = 0
    checked = 0
    sample_missing: list[tuple] = []
    for taxonomy, concepts in facts.get("facts", {}).items():
        if not isinstance(concepts, dict):
            continue
        for concept, concept_block in concepts.items():
            if not isinstance(concept_block, dict):
                continue
            units = concept_block.get("units", {})
            if not isinstance(units, dict):
                continue
            for unit, entries in units.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    end = entry.get("end")
                    filed = entry.get("filed")
                    val = entry.get("val")
                    if end is None or filed is None or val is None:
                        continue
                    checked += 1
                    key = (taxonomy, concept, unit, end, filed, float(val))
                    if key not in recovered:
                        missing += 1
                        if len(sample_missing) < 3:
                            sample_missing.append(key)

    if missing:
        logger.error(
            "VERIFY %s: %d/%d source entries not found in parquet (samples: %s)",
            cik_padded,
            missing,
            checked,
            sample_missing,
        )
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-dir", type=Path, default=JSON_DIR)
    parser.add_argument("--parquet-dir", type=Path, default=PARQUET_DIR)
    parser.add_argument("--force", action="store_true", help="Overwrite existing parquet files")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Round-trip-verify every converted CIK against its source JSON",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Convert/verify only N randomly-sampled CIKs (for smoke runs)",
    )
    parser.add_argument("--log-every", type=int, default=200)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.json_dir.exists():
        print(f"ERROR: source dir {args.json_dir} not found", file=sys.stderr)
        return 1
    args.parquet_dir.mkdir(parents=True, exist_ok=True)

    paths = list(_iter_json_paths(args.json_dir, args.sample))
    print(f"Source: {args.json_dir}  -> Target: {args.parquet_dir}")
    print(f"Discovered {len(paths)} JSON files")
    if args.sample:
        print(f"Sampling {args.sample} CIKs (deterministic seed=0)")

    converted = 0
    skipped = 0
    failed = 0
    total_rows = 0
    t_start = time.time()

    for i, json_path in enumerate(paths, 1):
        cik_padded = json_path.stem
        parquet_path = args.parquet_dir / f"{cik_padded}.parquet"
        if parquet_path.exists() and not args.force:
            skipped += 1
        else:
            ok, n_rows = _convert_one(json_path, parquet_path)
            if not ok:
                failed += 1
                continue
            converted += 1
            total_rows += n_rows
        if i % args.log_every == 0:
            print(
                f"  [{i}/{len(paths)}] converted={converted} skipped={skipped} "
                f"failed={failed} rows={total_rows} ({time.time() - t_start:.0f}s)"
            )

    elapsed = time.time() - t_start
    print(
        f"Migration done: converted={converted} skipped={skipped} failed={failed} "
        f"total_rows={total_rows} in {elapsed:.1f}s"
    )

    if args.verify:
        print("\nVerifying round-trip on every converted CIK...")
        reader = CompanyfactsParquetReader(args.parquet_dir, cache_capacity=64)
        v_ok = 0
        v_fail = 0
        v_skip = 0
        t_v = time.time()
        for i, json_path in enumerate(paths, 1):
            cik_padded = json_path.stem
            parquet_path = args.parquet_dir / f"{cik_padded}.parquet"
            if not parquet_path.exists():
                v_skip += 1
                continue
            if _verify_one(json_path, parquet_path, reader):
                v_ok += 1
            else:
                v_fail += 1
            if i % args.log_every == 0:
                print(
                    f"  [{i}/{len(paths)}] verify_ok={v_ok} verify_fail={v_fail} "
                    f"verify_skip={v_skip} ({time.time() - t_v:.0f}s)"
                )
        print(f"Verify done: ok={v_ok} fail={v_fail} skip={v_skip} in {time.time() - t_v:.1f}s")
        if v_fail:
            return 2

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

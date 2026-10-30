#!/usr/bin/env python
"""Backfill ``market_ret5_{spy,qqq,iwm}`` onto PUBLISHED brief parquets.

WHAT IT IS FOR
--------------
The brief's market-context strip showed one four-state word that watches SPY
only, so it read 'quiet' through a window where SPY fell 1.08% and the Nasdaq
drew down 6.2%. The three raw broad-index 5-session returns are the missing
breadth. The ``score`` stage stamps them from now on; this script puts them on
the dates already published, which is what makes the strip comparable backwards.

Chosen over a separate store on purpose: no new store, no new mount, no new
command. Django mounts ``thematic_briefs`` and ``population_ladders`` and NOT the
price store, so it cannot compute these at read time — the numbers have to arrive
as data. The Postgres cache re-ingests a date when its parquet mtime changes
(``DayMeta.parquet_mtime``), so rewriting the parquet is the MECHANISM, not a
side effect.

WHY REWRITING A PUBLISHED FILE IS ALLOWED HERE
----------------------------------------------
The #1482 publication freeze says a brief is published once per date and not
changed after. This rewrite was authorised against it on one ground: these
numbers change none of the four things the freeze protects — the candidate list,
its order, the prose, the trade levels — and could never have had a different
value, because they are arithmetic on closes that were already final.

That licence holds ONLY while the rewrite is provably additive, so the proof is
built in rather than asserted. Per date: read the table, append the three
columns, write a TEMP file, re-read it, and compare the re-read against the
original field by field — same Arrow field (name, type, nullability), same
values with NaN treated as equal to NaN, same row count. Nothing is replaced
until EVERY date has passed, and one failure refuses the whole run. A partial
landing is the state in which nobody can check the licence afterwards.

Three measured reasons for the details:

* **pyarrow, not pandas.** ``pd.read_parquet`` + ``to_parquet`` rewrites 21-22
  text columns from Arrow ``string`` to ``large_string`` on the OLDER brief
  dates (measured on 6 of 112 local parquets), and Django would never notice
  because pandas reads both as ``object``. The arrow append path preserved every
  field on 112 of 112.
* **No fixed schema.** The published store is schema-heterogeneous — eighteen
  distinct column counts from 63 to 164 across 112 local files, and half of them
  carry no ``market_state`` column at all. So the check is "this date's
  post-state equals this date's own pre-state plus the new names", never one
  expected schema for every date.
* **A stale store is refused, not stamped.** The enricher answers NaN when the
  newest stored session lags the brief date past its staleness bound, and this
  script treats that NaN as a REFUSAL by default. Measured on a Mac mirror whose
  store ended 2026-09-03: without that refusal, 2026-10-14, 2026-10-27 and
  2026-10-28 all took the identical frozen trio. Pass ``--allow-missing`` to
  stamp NULLs on purpose for dates the store genuinely cannot reach.

An unknown value is stored as NULL, not as NaN. Both ingest to NULL in Django
(``coerce_float`` maps NaN to None), but NULL says "no answer" without the
reader having to know that a float column's NaN is a sentinel.

DRY-RUN BY DEFAULT. Pass ``--write`` to persist. Run it where the grouped store
reaches the brief dates (the VPS), then re-ingest:

    docker compose --profile maintenance run --rm rebuild-cache \\
        python manage.py rebuild_briefs_cache \\
        --briefs-dir /var/lib/alphalens/thematic_briefs --force

Usage:
    python apps/alphalens-research/scripts/backfill_market_index_series.py
    python apps/alphalens-research/scripts/backfill_market_index_series.py --write
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.market.market_state import (
    MARKET_SERIES_COLUMNS,
    five_session_index_returns,
)

#: Report envelope version. Only additive changes within a major.
REPORT_SCHEMA = "alphalens.backfill.market_ret5/v1"

DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"

_EXIT_OK = 0
_EXIT_FAILED = 1
_EXIT_USAGE = 2


class NotAdditiveError(RuntimeError):
    """The staged rewrite of one date changed something it must not."""


def _values_equal(left: Any, right: Any) -> bool:
    """Deep equality with NaN == NaN.

    pyarrow's own ``equals`` reports ``NaN != NaN`` (measured on pyarrow 24), and
    brief parquets are full of NaN, so a check built on it would flag untouched
    columns as changed and the refusal would fire on every date.
    """
    if isinstance(left, float) and isinstance(right, float):
        return left == right or (math.isnan(left) and math.isnan(right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_values_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _values_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def augment(table: pa.Table, values: dict[str, float], *, date: dt.date) -> pa.Table:
    """``table`` plus the three index-return columns, as ``float64``.

    An unknown value becomes NULL rather than NaN. A column already present
    (a re-run) is REPLACED in place, so the operation is idempotent and the
    column order does not drift between runs.

    ``date`` is taken only so a test double can act per date; the arithmetic is
    already done by the time this is called.
    """
    del date  # the values are supplied; the date is here for test doubles
    out = table
    for column in MARKET_SERIES_COLUMNS:
        raw = values[column]
        cell = None if raw is None or math.isnan(raw) else float(raw)
        array = pa.array([cell] * out.num_rows, type=pa.float64())
        field = pa.field(column, pa.float64())
        index = out.schema.get_field_index(column)
        out = out.append_column(field, array) if index < 0 else out.set_column(index, field, array)
    return out


def check_additive(before: pa.Table, after: pa.Table, *, date: dt.date) -> list[str]:
    """Every violation of "``after`` is ``before`` plus the three names".

    Returns a list of human-readable violations, empty when the rewrite is
    additive. Each is reported, not raised, so one date names ALL of its
    problems instead of only the first.
    """
    problems: list[str] = []
    if after.num_rows != before.num_rows:
        problems.append(f"row count {before.num_rows} -> {after.num_rows}")

    added = [c for c in after.column_names if c not in set(before.column_names)]
    unexpected = [c for c in added if c not in set(MARKET_SERIES_COLUMNS)]
    if unexpected:
        problems.append(f"unexpected new columns: {unexpected}")

    for field in before.schema:
        index = after.schema.get_field_index(field.name)
        if index < 0:
            problems.append(f"{field.name}: dropped")
            continue
        if after.schema.field(index) != field:
            problems.append(f"{field.name}: field {field} -> {after.schema.field(index)}")
            continue
        if field.name in set(MARKET_SERIES_COLUMNS):
            continue  # this family is what the run is allowed to rewrite
        lhs = before.column(field.name).to_pylist()
        rhs = after.column(index).to_pylist()
        differing = [
            i for i, (a, b) in enumerate(zip(lhs, rhs, strict=False)) if not _values_equal(a, b)
        ]
        if differing:
            i = differing[0]
            problems.append(
                f"{field.name}: {len(differing)} cell(s) changed, first at row {i} "
                f"({lhs[i]!r} -> {rhs[i]!r})"
            )
    return [f"{date.isoformat()} {p}" for p in problems]


def _brief_dates(briefs_dir: Path, start: dt.date | None, end: dt.date | None):
    for path in sorted(briefs_dir.glob("*.parquet")):
        if len(path.stem) != 10:
            continue
        try:
            date = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if (start is None or date >= start) and (end is None or date <= end):
            yield date, path


def run_backfill(
    *,
    briefs_dir: Path,
    grouped_root: Path | None = None,
    write: bool = False,
    start: dt.date | None = None,
    end: dt.date | None = None,
    allow_missing: bool = False,
) -> dict[str, Any]:
    """Stage every date, verify every date, then replace every date (or none).

    Raises :class:`NotAdditiveError` when a staged rewrite is not additive, and
    ``RuntimeError`` when a date's returns are unknown and ``allow_missing`` is
    false. In both cases NO published file has been touched: the verification
    runs against the temp file, before ``os.replace``.
    """
    briefs_dir = Path(briefs_dir)
    if not briefs_dir.is_dir():
        raise RuntimeError(f"briefs dir not found: {briefs_dir}")

    rows: list[dict[str, Any]] = []
    staged: list[tuple[Path, Path]] = []
    missing: list[str] = []
    violations: list[str] = []

    try:
        for date, path in _brief_dates(briefs_dir, start, end):
            before = pq.read_table(path)
            values = five_session_index_returns(date, grouped_root=grouped_root)
            unknown = [c for c, v in values.items() if v is None or math.isnan(v)]
            rows.append(
                {
                    "date": date.isoformat(),
                    "rows": before.num_rows,
                    "already_present": all(c in before.column_names for c in MARKET_SERIES_COLUMNS),
                    "unknown": unknown,
                    **{c: (None if c in unknown else values[c]) for c in MARKET_SERIES_COLUMNS},
                }
            )
            if unknown and not allow_missing:
                missing.append(f"{date.isoformat()} unknown: {unknown}")
                continue
            if not write:
                continue

            after = augment(before, values, date=date)
            tmp = path.with_suffix(path.suffix + ".backfill-tmp")
            pq.write_table(after, tmp, compression="snappy")
            staged.append((tmp, path))
            # Verify what is ON DISK, not the in-memory table: the write is part
            # of what has to be additive.
            violations.extend(check_additive(before, pq.read_table(tmp), date=date))

        if missing:
            raise RuntimeError(
                "refusing the whole run: the grouped store cannot answer "
                + str(len(missing))
                + " date(s). Run where the store reaches them (the VPS), or pass "
                "--allow-missing to stamp NULLs deliberately. " + "; ".join(missing)
            )
        if violations:
            raise NotAdditiveError(
                "refusing the whole run: a staged rewrite was not additive. "
                + "; ".join(violations)
            )

        written = 0
        if write:
            for tmp, path in staged:
                os.replace(tmp, path)
                written += 1
            staged = []
    finally:
        for tmp, _path in staged:
            tmp.unlink(missing_ok=True)

    return {
        "schema": REPORT_SCHEMA,
        "dry_run": not write,
        "briefs_dir": str(briefs_dir),
        "dates": rows,
        "written": written,
        "refused": [],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--briefs-dir", type=Path, default=DEFAULT_BRIEFS_DIR)
    ap.add_argument(
        "--grouped-root",
        type=Path,
        default=None,
        help="grouped-daily store (default: ~/.alphalens/grouped_daily_history)",
    )
    ap.add_argument("--start", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--write", action="store_true", help="persist (default: dry-run)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit no-op complement of --write (dry-run is the default)",
    )
    ap.add_argument(
        "--allow-missing",
        action="store_true",
        help="stamp NULLs for dates the grouped store cannot reach (default: refuse)",
    )
    ap.add_argument("--format", choices=("human", "json"), default="human")
    args = ap.parse_args(argv)

    if args.write and args.dry_run:
        print("--write and --dry-run are mutually exclusive", file=sys.stderr)
        return _EXIT_USAGE

    try:
        report = run_backfill(
            briefs_dir=args.briefs_dir,
            grouped_root=args.grouped_root,
            write=args.write and not args.dry_run,
            start=args.start,
            end=args.end,
            allow_missing=args.allow_missing,
        )
    except (NotAdditiveError, RuntimeError) as exc:
        if args.format == "json":
            json.dump(
                {
                    "schema": REPORT_SCHEMA,
                    "error": {"code": type(exc).__name__, "message": str(exc)},
                },
                sys.stderr,
            )
            sys.stderr.write("\n")
        else:
            print(f"REFUSED: {exc}", file=sys.stderr)
        return _EXIT_FAILED

    if args.format == "json":
        json.dump(report, sys.stdout)
        sys.stdout.write("\n")
        return _EXIT_OK

    mode = "DRY-RUN" if report["dry_run"] else "WROTE"
    print(f"{mode} {len(report['dates'])} date(s) under {report['briefs_dir']}")
    for row in report["dates"]:
        trio = " ".join(
            f"{c.removeprefix('market_ret5_')}="
            + ("n/a" if row[c] is None else f"{row[c] * 100:+.2f}%")
            for c in MARKET_SERIES_COLUMNS
        )
        print(f"  {row['date']}  rows={row['rows']:4d}  {trio}")
    print(f"written={report['written']}")
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

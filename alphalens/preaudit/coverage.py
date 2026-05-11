"""Pure-function coverage checks for the pre-audit smoke framework.

Dispatches on :class:`CheckType` so the framework stays strategy-
agnostic. Each CheckType maps to ONE branch here; adding a new data
source means adding a CheckType member + one branch, never branching
on ``dep.name``.

The functions are pure: caller passes ``root`` (a directory containing
the data dirs by name, e.g. ``~/.alphalens``), so unit tests can drive
them against synthetic fixture dirs without touching real data.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import date
from pathlib import Path

import pandas as pd

from alphalens.preaudit.profiles import (
    CheckType,
    CoverageCheck,
    CoverageReport,
    CoverageStatus,
    DataDep,
    SmokeProfile,
)


def check_coverage(dep: DataDep, *, root: Path) -> CoverageCheck:
    """Check that ``root/dep.name/`` satisfies the dep's coverage contract."""
    dep_dir = root / dep.name
    if not dep_dir.is_dir():
        return CoverageCheck(
            dep=dep,
            status=CoverageStatus.FAIL_MISSING,
            detail=f"directory not found: {dep_dir}",
        )

    if dep.check_type is CheckType.EXISTS_NONEMPTY:
        return _check_exists_nonempty(dep, dep_dir)
    if dep.check_type is CheckType.PARQUET_PARTITION:
        return _check_parquet_partition(dep, dep_dir)
    if dep.check_type is CheckType.FLAT_PARQUET:
        return _check_flat_parquet(dep, dep_dir)
    if dep.check_type is CheckType.YAML_DIR:
        return _check_yaml_dir(dep, dep_dir)
    # Defensive: unreachable while CheckType is exhaustive, but if a new
    # member is added without a branch here, fail loudly rather than
    # silently passing.
    raise NotImplementedError(f"check_type {dep.check_type!r} not handled")


def check_all_deps(profile: SmokeProfile, *, root: Path) -> CoverageReport:
    """Run :func:`check_coverage` on every dep in ``profile``."""
    checks = tuple(check_coverage(dep, root=root) for dep in profile.data_deps)
    return CoverageReport(checks=checks)


# -------------------------------------------------------------------------
# Internal: one helper per CheckType.
# -------------------------------------------------------------------------


def _check_exists_nonempty(dep: DataDep, dep_dir: Path) -> CoverageCheck:
    if not any(dep_dir.iterdir()):
        return CoverageCheck(dep=dep, status=CoverageStatus.FAIL_EMPTY, detail=f"empty: {dep_dir}")
    return CoverageCheck(dep=dep, status=CoverageStatus.PASS)


def _check_parquet_partition(dep: DataDep, dep_dir: Path) -> CoverageCheck:
    """Verify hive-partitioned ``<partition_key>=<NNNN>`` subdirs cover years.

    ``dep.pattern`` is the partition key (e.g. ``"transaction_year"``);
    defaults to ``"transaction_year"`` if None.
    """
    key = dep.pattern or "transaction_year"
    subdirs = [p for p in dep_dir.iterdir() if p.is_dir() and p.name.startswith(f"{key}=")]
    if not subdirs:
        return CoverageCheck(
            dep=dep,
            status=CoverageStatus.FAIL_EMPTY,
            detail=f"no {key}=NNNN partitions in {dep_dir}",
        )
    if dep.min_date is None or dep.max_date is None:
        return CoverageCheck(dep=dep, status=CoverageStatus.PASS)

    years_present: set[int] = set()
    for sub in subdirs:
        try:
            years_present.add(int(sub.name.split("=", 1)[1]))
        except (IndexError, ValueError):
            continue
    required = set(range(dep.min_date.year, dep.max_date.year + 1))
    missing = sorted(required - years_present)
    if missing:
        return CoverageCheck(
            dep=dep,
            status=CoverageStatus.FAIL_GAP,
            detail=f"missing partition years: {missing} (window {dep.min_date}..{dep.max_date})",
        )
    return CoverageCheck(dep=dep, status=CoverageStatus.PASS)


def _check_flat_parquet(dep: DataDep, dep_dir: Path) -> CoverageCheck:
    """Sample N parquet files; require ``min_pass_ratio`` to span window.

    Single-ticker peek (e.g. AAPL only) false-passes when most of the
    universe has shorter history — this caused today's launch bug.
    Defaults to ``sample_size=10`` (see :class:`DataDep`). The date
    column name comes from ``dep.pattern`` (e.g. ``"tradeDate"`` for
    iVolatility SMD, ``"date"`` for prices).
    """
    files = sorted(dep_dir.glob("*.parquet"))
    if not files:
        return CoverageCheck(
            dep=dep,
            status=CoverageStatus.FAIL_EMPTY,
            detail=f"no *.parquet files in {dep_dir}",
        )
    if dep.min_date is None or dep.max_date is None:
        return CoverageCheck(dep=dep, status=CoverageStatus.PASS)

    n = min(dep.sample_size, len(files))
    # random.sample is used for spot-checking data coverage, not for
    # any cryptographic / security purpose. Tests pin random.seed(0)
    # for reproducibility, so `secrets`/`SystemRandom` would actively
    # defeat the determinism requirement.
    sample = random.sample(files, n)  # NOSONAR S2245
    date_col = dep.pattern or "date"

    short_history, unreadable = _classify_flat_parquet_sample(
        sample, date_col, dep.min_date, dep.max_date
    )
    if unreadable is not None:
        return CoverageCheck(
            dep=dep,
            status=CoverageStatus.FAIL_GAP,
            detail=f"cannot find date column {date_col!r} or DatetimeIndex in {unreadable}",
        )

    n_pass = n - len(short_history)
    pass_ratio = n_pass / n if n > 0 else 0.0
    if pass_ratio < dep.min_pass_ratio:
        return CoverageCheck(
            dep=dep,
            status=CoverageStatus.FAIL_GAP,
            detail=(
                f"{n_pass}/{n} ({pass_ratio:.0%}) sampled files span window "
                f"{dep.min_date}..{dep.max_date}; required ≥ "
                f"{dep.min_pass_ratio:.0%}. "
                f"Examples missing: "
                + "; ".join(short_history[:3])
                + (" ..." if len(short_history) > 3 else "")
            ),
        )
    return CoverageCheck(dep=dep, status=CoverageStatus.PASS)


def _classify_flat_parquet_sample(
    sample: list[Path],
    date_col: str,
    min_date: date,
    max_date: date,
) -> tuple[list[str], str | None]:
    """Walk the sampled files; return (short_history, unreadable_filename).

    Extracted from :func:`_check_flat_parquet` to keep cognitive complexity
    of the dispatch function below the SonarCloud threshold.
    """
    min_required = pd.Timestamp(min_date)
    max_required = pd.Timestamp(max_date)
    short_history: list[str] = []
    for f in sample:
        col = _peek_dates(f, date_col)
        if col is None:
            return short_history, f.name
        if len(col) == 0:
            short_history.append(f.name)
            continue
        if col.min() > min_required or col.max() < max_required:
            short_history.append(f"{f.name} [{col.min().date()}..{col.max().date()}]")
    return short_history, None


def _peek_dates(parquet_path: Path, date_col: str) -> pd.Series | None:
    """Read just the date column from a parquet file.

    Tries ``columns=[date_col]`` first (cheapest); falls back to reading
    the whole file and using the DatetimeIndex when the data has no
    explicit date column (OHLCV layout: dates live on the index).
    Returns None if neither path yields dates.

    Per zen 2026-05-11 review: catch only the narrow class of
    "column not found / schema mismatch" errors silently. Any other
    failure (corrupt parquet, OS-level error, permissions) is logged
    at debug so it doesn't silently masquerade as a coverage gap.
    """
    log = logging.getLogger(__name__)

    try:
        from pyarrow.lib import ArrowInvalid
    except ImportError:  # pragma: no cover - pyarrow is a hard repo dep
        ArrowInvalid = Exception  # type: ignore[assignment,misc]
    schema_mismatch = (KeyError, ValueError, ArrowInvalid)

    try:
        df = pd.read_parquet(parquet_path, columns=[date_col])
        if date_col in df.columns:
            return pd.to_datetime(df[date_col])
    except schema_mismatch:
        pass  # expected when date_col is the parquet's index, not a data column
    except Exception as exc:
        log.debug("preaudit: column-filtered read of %s failed: %s", parquet_path, exc)

    try:
        df = pd.read_parquet(parquet_path)
    except Exception as exc:
        log.debug("preaudit: full read of %s failed: %s", parquet_path, exc)
        return None
    if isinstance(df.index, pd.DatetimeIndex):
        return pd.Series(df.index)
    if date_col in df.columns:
        return pd.to_datetime(df[date_col])
    return None


def _check_yaml_dir(dep: DataDep, dep_dir: Path) -> CoverageCheck:
    """Filename-encoded date check (PIT universe yamls).

    ``dep.pattern`` is a regex with ONE capture group yielding an ISO
    date (e.g. ``r"r2000_pit_(\\d{4}-\\d{2}-\\d{2})\\.yaml"``).
    """
    pat = dep.pattern or r"(\d{4}-\d{2}-\d{2})"
    rx = re.compile(pat)
    files = list(dep_dir.iterdir())
    if not files:
        return CoverageCheck(dep=dep, status=CoverageStatus.FAIL_EMPTY, detail=f"empty {dep_dir}")
    dates: list[date] = []
    for f in files:
        m = rx.search(f.name)
        if not m:
            continue
        try:
            dates.append(date.fromisoformat(m.group(1)))
        except ValueError:
            continue
    if not dates:
        return CoverageCheck(
            dep=dep,
            status=CoverageStatus.FAIL_EMPTY,
            detail=f"no filenames matched {pat!r} in {dep_dir}",
        )
    if dep.min_date is None or dep.max_date is None:
        return CoverageCheck(dep=dep, status=CoverageStatus.PASS)
    if min(dates) > dep.min_date or max(dates) < dep.max_date:
        return CoverageCheck(
            dep=dep,
            status=CoverageStatus.FAIL_GAP,
            detail=(
                f"filename dates {min(dates)}..{max(dates)} do not straddle "
                f"window {dep.min_date}..{dep.max_date}"
            ),
        )
    return CoverageCheck(dep=dep, status=CoverageStatus.PASS)

"""Build the SQLite cache from ``~/.alphalens/thematic_briefs/*.parquet``.

The cache is a derived artifact: parquet remains the canonical write target,
SQLite is rebuilt incrementally (per-date mtime gate) so it can be deleted and
regenerated at any time.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens.api import db as db_module
from alphalens.api.schema import (
    CANDIDATE_COLUMN_NAMES,
    CANDIDATE_COLUMNS,
    SCHEMA_VERSION,
)

DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RebuildResult:
    """Summary of one cache rebuild pass — useful for CLI output and tests."""

    rebuilt_dates: tuple[str, ...]
    skipped_dates: tuple[str, ...]
    deleted_dates: tuple[str, ...]
    total_briefs: int
    db_path: Path

    @property
    def n_rebuilt(self) -> int:
        return len(self.rebuilt_dates)

    @property
    def n_skipped(self) -> int:
        return len(self.skipped_dates)

    @property
    def n_deleted(self) -> int:
        return len(self.deleted_dates)


def _create_schema(conn: sqlite3.Connection) -> None:
    columns_sql = ",\n  ".join(f"{c.name} {c.sql_type}" for c in CANDIDATE_COLUMNS)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS briefs (
          date TEXT NOT NULL,
          {columns_sql},
          PRIMARY KEY (date, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_briefs_theme  ON briefs(theme);
        CREATE INDEX IF NOT EXISTS idx_briefs_ticker ON briefs(ticker);
        CREATE INDEX IF NOT EXISTS idx_briefs_score  ON briefs(layer4_weighted_score);

        CREATE TABLE IF NOT EXISTS days_meta (
          date              TEXT PRIMARY KEY,
          n_candidates      INTEGER NOT NULL,
          n_themes          INTEGER NOT NULL,
          top_theme         TEXT,
          theme_counts_json TEXT NOT NULL,
          parquet_mtime     REAL NOT NULL,
          rebuilt_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )


def _stored_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else None


def _set_schema_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def _stored_mtimes(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("SELECT date, parquet_mtime FROM days_meta").fetchall()
    return {r["date"]: float(r["parquet_mtime"]) for r in rows}


def _coerce_str(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


def _coerce_float(value: Any) -> Any:
    v = float(value)
    return None if math.isnan(v) else v


def _coerce_int(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    return int(value)


def _coerce_bool(value: Any) -> int:
    return 1 if bool(value) else 0


def _coerce_list_str(value: Any) -> str:
    # numpy arrays + pandas object cells both behave list-like
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, str):
        return json.dumps([value])
    return json.dumps([str(v) for v in value])


def _coerce_datetime(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, dt.datetime)):
        return value.isoformat()
    return str(value)


_DISPATCH = {
    "str": _coerce_str,
    "float": _coerce_float,
    "int": _coerce_int,
    "bool": _coerce_bool,
    "list_str": _coerce_list_str,
    "datetime": _coerce_datetime,
}


def _coerce(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        return _DISPATCH[kind](value)
    except KeyError as exc:
        raise ValueError(f"unknown column kind: {kind}") from exc


def _row_to_tuple(date: str, row: pd.Series) -> tuple[Any, ...]:
    out: list[Any] = [date]
    for col in CANDIDATE_COLUMNS:
        out.append(_coerce(row.get(col.name), col.py_kind))
    return tuple(out)


def _theme_counts(df: pd.DataFrame) -> dict[str, int]:
    if "theme" not in df.columns or df.empty:
        return {}
    return {str(k): int(v) for k, v in df["theme"].value_counts().to_dict().items()}


def _top_theme(theme_counts: dict[str, int]) -> str | None:
    if not theme_counts:
        return None
    return sorted(theme_counts.keys(), key=lambda k: (-theme_counts[k], k))[0]


REQUIRED_PARQUET_COLUMNS = frozenset({"ticker", "theme"})


def _rebuild_one_date(
    conn: sqlite3.Connection,
    *,
    date: str,
    parquet_path: Path,
    parquet_mtime: float,
    now_iso: str,
) -> int:
    """Load one parquet day into the cache. Returns row count written.

    Older parquets predate the full 70-column brief schema (e.g. 2023-era
    files lack ``catalyst_*`` / ``also_in_themes`` / ``deep_drawdown_reversal``).
    Missing optional columns are stored as NULL; only ``ticker`` and ``theme``
    are mandatory because the primary key and theme aggregates depend on them.
    """
    df = pd.read_parquet(parquet_path)

    missing_required = REQUIRED_PARQUET_COLUMNS - set(df.columns)
    if missing_required:
        raise ValueError(
            f"parquet {parquet_path.name} missing required columns: {sorted(missing_required)}"
        )

    placeholders = ",".join(["?"] * (1 + len(CANDIDATE_COLUMNS)))
    columns_sql = ",".join(["date", *CANDIDATE_COLUMN_NAMES])
    insert_sql = f"INSERT INTO briefs ({columns_sql}) VALUES ({placeholders})"

    rows = [_row_to_tuple(date, row) for _, row in df.iterrows()]

    conn.execute("DELETE FROM briefs WHERE date=?", (date,))
    if rows:
        conn.executemany(insert_sql, rows)

    theme_counts = _theme_counts(df)
    conn.execute(
        """
        INSERT OR REPLACE INTO days_meta
          (date, n_candidates, n_themes, top_theme, theme_counts_json,
           parquet_mtime, rebuilt_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            len(df),
            len(theme_counts),
            _top_theme(theme_counts),
            json.dumps(theme_counts),
            parquet_mtime,
            now_iso,
        ),
    )
    return len(rows)


def rebuild_from_parquet(
    briefs_dir: Path | None = None,
    db_path: Path | None = None,
    *,
    force: bool = False,
) -> RebuildResult:
    """Bring the SQLite cache in line with the parquet directory.

    - Adds / refreshes every date whose parquet mtime differs from the cached
      mtime (or every date when ``force=True``).
    - Removes ``days_meta`` + ``briefs`` rows for dates whose parquet file is
      gone, so the cache mirrors the directory.
    - Bumping ``SCHEMA_VERSION`` triggers a full rebuild on next call.
    """
    briefs_dir = briefs_dir or DEFAULT_BRIEFS_DIR
    db_path = db_path or db_module.DEFAULT_DB_PATH
    briefs_dir = Path(briefs_dir)
    db_path = Path(db_path)

    conn = db_module.connect(db_path)
    try:
        _create_schema(conn)
        stored_version = _stored_schema_version(conn)
        if stored_version is None:
            _set_schema_version(conn)
        elif stored_version != SCHEMA_VERSION:
            logger.info(
                "schema version mismatch (stored=%s expected=%s) — forcing full rebuild",
                stored_version,
                SCHEMA_VERSION,
            )
            # DROP, not DELETE: CREATE TABLE IF NOT EXISTS won't add new columns
            # to an existing table, so a schema bump that adds columns would
            # crash the subsequent INSERT. Dropping forces a fresh schema.
            conn.execute("DROP TABLE IF EXISTS briefs")
            conn.execute("DROP TABLE IF EXISTS days_meta")
            _create_schema(conn)
            _set_schema_version(conn)
            force = True

        parquet_by_date: dict[str, Path] = {}
        if briefs_dir.exists():
            for path in sorted(briefs_dir.glob("*.parquet")):
                parquet_by_date[path.stem] = path

        stored_mtimes = _stored_mtimes(conn)

        rebuilt: list[str] = []
        skipped: list[str] = []
        total_briefs = 0
        now_iso = dt.datetime.now(dt.UTC).isoformat()

        for date in sorted(parquet_by_date):
            parquet_path = parquet_by_date[date]
            mtime = parquet_path.stat().st_mtime
            if not force and abs(stored_mtimes.get(date, -1.0) - mtime) < 1e-6:
                skipped.append(date)
                continue
            n = _rebuild_one_date(
                conn,
                date=date,
                parquet_path=parquet_path,
                parquet_mtime=mtime,
                now_iso=now_iso,
            )
            rebuilt.append(date)
            total_briefs += n
            logger.info("cache: rebuilt %s (%d rows)", date, n)

        deleted = sorted(set(stored_mtimes) - set(parquet_by_date))
        for date in deleted:
            conn.execute("DELETE FROM briefs WHERE date=?", (date,))
            conn.execute("DELETE FROM days_meta WHERE date=?", (date,))
            logger.info("cache: dropped %s (parquet missing)", date)

        if force:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_full_rebuild_at', ?)",
                (now_iso,),
            )

        conn.commit()
    finally:
        conn.close()

    return RebuildResult(
        rebuilt_dates=tuple(rebuilt),
        skipped_dates=tuple(skipped),
        deleted_dates=tuple(deleted),
        total_briefs=total_briefs,
        db_path=db_path,
    )

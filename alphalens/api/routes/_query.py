"""Shared SQL fragments for read endpoints.

Keeping the column list and the row decoder in one place lets each route module
stay short — they assemble WHERE clauses, pagination, and call into here.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from fastapi import HTTPException

from alphalens.api.models import Candidate, candidate_from_row
from alphalens.api.schema import CANDIDATE_COLUMN_NAMES

SELECT_COLUMNS = ", ".join(["date", *CANDIDATE_COLUMN_NAMES])
DEFAULT_ORDER = "ORDER BY date DESC, layer4_weighted_score DESC, ticker ASC"
MAX_LIMIT = 200


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return 50
    if limit < 1:
        return 1
    return min(limit, MAX_LIMIT)


def normalise_offset(offset: int | None) -> int:
    if offset is None or offset < 0:
        return 0
    return offset


def validate_date(value: str, field_name: str = "date") -> str:
    """Enforce a real calendar YYYY-MM-DD before letting it into a SQL parameter.

    ``date.fromisoformat`` rejects both bad shapes (``05/18/2026``) and bad
    calendar values (``2026-02-30``, ``2026-13-01``). Returning the parsed-
    then-re-serialised form normalises any tolerated input variants.
    """
    try:
        return dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be a valid YYYY-MM-DD date",
        ) from exc


def count_rows(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> int:
    sql = f"SELECT COUNT(*) AS n FROM briefs {where}"
    row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def fetch_candidates(
    conn: sqlite3.Connection,
    where: str,
    params: tuple[Any, ...],
    *,
    limit: int,
    offset: int,
    order: str = DEFAULT_ORDER,
) -> list[Candidate]:  # type: ignore[valid-type]
    sql = f"SELECT {SELECT_COLUMNS} FROM briefs {where} {order} LIMIT ? OFFSET ?"
    rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    return [candidate_from_row(row) for row in rows]

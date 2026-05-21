"""Theme-aggregation endpoints."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query

from alphalens.api.deps import get_db
from alphalens.api.models import PageMeta, PaginatedCandidate, PaginatedThemeSummary, ThemeSummary
from alphalens.api.routes._query import (
    clamp_limit,
    count_rows,
    fetch_candidates,
    normalise_offset,
    validate_date,
)

router = APIRouter(prefix="/v1/themes", tags=["themes"])


def _theme_where(date_from: str | None, date_to: str | None) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    if date_from:
        parts.append("date >= ?")
        params.append(validate_date(date_from, "from"))
    if date_to:
        parts.append("date <= ?")
        params.append(validate_date(date_to, "to"))
    where = ("WHERE " + " AND ".join(parts)) if parts else ""
    return where, params


@router.get(
    "",
    response_model=PaginatedThemeSummary,
    summary="Distinct themes with appearance counts.",
)
def list_themes(
    conn: sqlite3.Connection = Depends(get_db),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where, params = _theme_where(date_from, date_to)

    total_row = conn.execute(
        f"SELECT COUNT(DISTINCT theme) AS n FROM briefs {where}", tuple(params)
    ).fetchone()
    total = int(total_row["n"]) if total_row else 0

    rows = conn.execute(
        "SELECT theme, COUNT(*) AS n_candidates, COUNT(DISTINCT date) AS n_days, "
        "MIN(date) AS first_seen, MAX(date) AS last_seen "
        f"FROM briefs {where} "
        "GROUP BY theme ORDER BY n_candidates DESC, theme ASC LIMIT ? OFFSET ?",
        (*params, clamp_limit(limit), normalise_offset(offset)),
    ).fetchall()

    return {
        "data": [
            ThemeSummary(
                theme=r["theme"],
                n_candidates=int(r["n_candidates"]),
                n_days=int(r["n_days"]),
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
            )
            for r in rows
        ],
        "meta": PageMeta(total=total, limit=clamp_limit(limit), offset=normalise_offset(offset)),
    }


@router.get(
    "/{theme}/candidates",
    response_model=PaginatedCandidate,
    summary="Candidates filtered by exact theme match.",
)
def list_theme_candidates(
    theme: str,
    conn: sqlite3.Connection = Depends(get_db),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where_parts = ["theme = ?"]
    params: list[Any] = [theme]
    if date_from:
        where_parts.append("date >= ?")
        params.append(validate_date(date_from, "from"))
    if date_to:
        where_parts.append("date <= ?")
        params.append(validate_date(date_to, "to"))
    where = "WHERE " + " AND ".join(where_parts)

    total = count_rows(conn, where, tuple(params))
    candidates = fetch_candidates(
        conn,
        where,
        tuple(params),
        limit=clamp_limit(limit),
        offset=normalise_offset(offset),
    )
    return {
        "data": candidates,
        "meta": PageMeta(total=total, limit=clamp_limit(limit), offset=normalise_offset(offset)),
    }

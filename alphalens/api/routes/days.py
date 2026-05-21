"""Day-level endpoints — list of days and full day brief."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from alphalens.api.deps import get_db
from alphalens.api.models import (
    DayBrief,
    DayMeta,
    PageMeta,
    PaginatedCandidate,
    PaginatedDayMeta,
    candidate_from_row,
)
from alphalens.api.routes._query import (
    SELECT_COLUMNS,
    clamp_limit,
    count_rows,
    fetch_candidates,
    normalise_offset,
    validate_date,
)

router = APIRouter(prefix="/v1/days", tags=["days"])


def _day_meta_from_row(row: sqlite3.Row) -> DayMeta:
    return DayMeta(
        date=row["date"],
        n_candidates=int(row["n_candidates"]),
        n_themes=int(row["n_themes"]),
        top_theme=row["top_theme"],
    )


@router.get(
    "",
    response_model=PaginatedDayMeta,
    summary="List days with brief metadata (most recent first).",
)
def list_days(
    conn: sqlite3.Connection = Depends(get_db),
    date_from: str | None = Query(None, alias="from", description="Inclusive ISO date."),
    date_to: str | None = Query(None, alias="to", description="Inclusive ISO date."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where_parts: list[str] = []
    params: list[Any] = []
    if date_from:
        where_parts.append("date >= ?")
        params.append(validate_date(date_from, "from"))
    if date_to:
        where_parts.append("date <= ?")
        params.append(validate_date(date_to, "to"))
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM days_meta {where}", tuple(params)
    ).fetchone()
    total = int(total_row["n"]) if total_row else 0

    rows = conn.execute(
        f"SELECT date, n_candidates, n_themes, top_theme FROM days_meta {where} "
        "ORDER BY date DESC LIMIT ? OFFSET ?",
        (*params, clamp_limit(limit), normalise_offset(offset)),
    ).fetchall()
    return {
        "data": [_day_meta_from_row(r) for r in rows],
        "meta": PageMeta(total=total, limit=clamp_limit(limit), offset=normalise_offset(offset)),
    }


@router.get(
    "/{date}",
    response_model=DayBrief,
    responses={404: {"description": "No brief for that date."}},
    summary="Full payload for one day (metadata + every candidate).",
)
def get_day(
    date: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> DayBrief:
    validate_date(date)
    meta_row = conn.execute(
        "SELECT date, n_candidates, n_themes, top_theme, theme_counts_json "
        "FROM days_meta WHERE date=?",
        (date,),
    ).fetchone()
    if meta_row is None:
        raise HTTPException(status_code=404, detail=f"no brief for date={date}")

    rows = conn.execute(
        f"SELECT {SELECT_COLUMNS} FROM briefs WHERE date=? "
        "ORDER BY layer4_weighted_score DESC, ticker ASC",
        (date,),
    ).fetchall()

    return DayBrief(
        date=meta_row["date"],
        n_candidates=int(meta_row["n_candidates"]),
        n_themes=int(meta_row["n_themes"]),
        top_theme=meta_row["top_theme"],
        theme_counts=json.loads(meta_row["theme_counts_json"] or "{}"),
        candidates=[candidate_from_row(r) for r in rows],
    )


@router.get(
    "/{date}/candidates",
    response_model=PaginatedCandidate,
    responses={404: {"description": "No brief for that date."}},
    summary="Filterable candidates for one day.",
)
def list_day_candidates(
    date: str,
    conn: sqlite3.Connection = Depends(get_db),
    theme: str | None = Query(None, description="Exact theme match."),
    min_score: int | None = Query(None, ge=0, description="Layer-4 weighted score floor."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    validate_date(date)

    exists = conn.execute("SELECT 1 FROM days_meta WHERE date=?", (date,)).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"no brief for date={date}")

    where_parts = ["date = ?"]
    params: list[Any] = [date]
    if theme:
        where_parts.append("theme = ?")
        params.append(theme)
    if min_score is not None:
        where_parts.append("layer4_weighted_score >= ?")
        params.append(int(min_score))
    where = "WHERE " + " AND ".join(where_parts)

    total = count_rows(conn, where, tuple(params))
    candidates = fetch_candidates(
        conn, where, tuple(params), limit=clamp_limit(limit), offset=normalise_offset(offset)
    )
    return {
        "data": candidates,
        "meta": PageMeta(total=total, limit=clamp_limit(limit), offset=normalise_offset(offset)),
    }

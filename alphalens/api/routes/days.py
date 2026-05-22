"""Day-level endpoints — list of days and full day brief."""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

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

DbDep = Annotated[sqlite3.Connection, Depends(get_db)]
DateFromQ = Annotated[str | None, Query(alias="from", description="Inclusive ISO date.")]
DateToQ = Annotated[str | None, Query(alias="to", description="Inclusive ISO date.")]
LimitQ = Annotated[int, Query(ge=1, le=200)]
OffsetQ = Annotated[int, Query(ge=0)]
ThemeQ = Annotated[str | None, Query(description="Exact theme match.")]
MinScoreQ = Annotated[int | None, Query(ge=0, description="Layer-4 weighted score floor.")]


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
    conn: DbDep,
    date_from: DateFromQ = None,
    date_to: DateToQ = None,
    limit: LimitQ = 50,
    offset: OffsetQ = 0,
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
    responses={404: {"description": "No brief for that date."}},
    summary="Full payload for one day (metadata + every candidate).",
)
def get_day(date: str, conn: DbDep) -> DayBrief:
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
        "ORDER BY COALESCE(rank_in_day, 999999) ASC, ticker ASC",
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
    conn: DbDep,
    theme: ThemeQ = None,
    min_score: MinScoreQ = None,
    limit: LimitQ = 50,
    offset: OffsetQ = 0,
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

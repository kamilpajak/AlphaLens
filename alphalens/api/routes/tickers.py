"""Per-ticker history endpoint."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query

from alphalens.api.deps import get_db
from alphalens.api.models import PageMeta, PaginatedCandidate
from alphalens.api.routes._query import (
    clamp_limit,
    count_rows,
    fetch_candidates,
    normalise_offset,
    validate_date,
)

router = APIRouter(prefix="/v1/tickers", tags=["tickers"])


@router.get(
    "/{ticker}/history",
    response_model=PaginatedCandidate,
    summary="Every brief appearance for one ticker (most recent first).",
)
def get_ticker_history(
    ticker: str,
    conn: sqlite3.Connection = Depends(get_db),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where_parts = ["ticker = ?"]
    params: list[Any] = [ticker.upper()]
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
        order="ORDER BY date DESC",
    )
    return {
        "data": candidates,
        "meta": PageMeta(total=total, limit=clamp_limit(limit), offset=normalise_offset(offset)),
    }

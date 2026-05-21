"""Per-ticker history endpoint."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

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

DbDep = Annotated[sqlite3.Connection, Depends(get_db)]
DateFromQ = Annotated[str | None, Query(alias="from")]
DateToQ = Annotated[str | None, Query(alias="to")]
LimitQ = Annotated[int, Query(ge=1, le=200)]
OffsetQ = Annotated[int, Query(ge=0)]


@router.get(
    "/{ticker}/history",
    response_model=PaginatedCandidate,
    summary="Every brief appearance for one ticker (most recent first).",
)
def get_ticker_history(
    ticker: str,
    conn: DbDep,
    date_from: DateFromQ = None,
    date_to: DateToQ = None,
    limit: LimitQ = 50,
    offset: OffsetQ = 0,
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

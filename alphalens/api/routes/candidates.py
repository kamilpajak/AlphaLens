"""Single-candidate endpoint."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from alphalens.api.deps import get_db
from alphalens.api.models import Candidate, candidate_from_row
from alphalens.api.routes._query import SELECT_COLUMNS, validate_date

router = APIRouter(prefix="/v1/candidates", tags=["candidates"])


@router.get(
    "/{date}/{ticker}",
    responses={404: {"description": "No candidate for that date/ticker."}},
    summary="Fetch a single candidate by (date, ticker).",
)
def get_candidate(
    date: str,
    ticker: str,
    conn: Annotated[sqlite3.Connection, Depends(get_db)],
) -> Candidate:  # type: ignore[valid-type]
    validate_date(date)
    row = conn.execute(
        f"SELECT {SELECT_COLUMNS} FROM briefs WHERE date=? AND ticker=?",
        (date, ticker.upper()),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no candidate for date={date} ticker={ticker}")
    return candidate_from_row(row)

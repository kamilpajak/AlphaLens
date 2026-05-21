"""Aggregate stats endpoint."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from alphalens.api.deps import get_db
from alphalens.api.models import Stats, TopTheme

router = APIRouter(prefix="/v1/stats", tags=["stats"])


@router.get(
    "",
    response_model=Stats,
    summary="Top-line counters and most-frequent themes.",
)
def get_stats(
    conn: sqlite3.Connection = Depends(get_db),
    top_n: int = Query(10, ge=1, le=50, description="How many top themes to include."),
) -> Stats:
    counts = conn.execute(
        "SELECT COUNT(*) AS n_days, SUM(n_candidates) AS n_candidates, "
        "MIN(date) AS earliest, MAX(date) AS latest, MAX(rebuilt_at) AS last_rebuild "
        "FROM days_meta"
    ).fetchone()

    distinct_themes = conn.execute("SELECT COUNT(DISTINCT theme) AS n FROM briefs").fetchone()

    top_rows = conn.execute(
        "SELECT theme, COUNT(*) AS n_candidates, COUNT(DISTINCT date) AS n_days "
        "FROM briefs GROUP BY theme "
        "ORDER BY n_candidates DESC, theme ASC LIMIT ?",
        (top_n,),
    ).fetchall()

    return Stats(
        n_days=int(counts["n_days"] or 0),
        n_candidates=int(counts["n_candidates"] or 0),
        n_themes=int(distinct_themes["n"] or 0),
        earliest_date=counts["earliest"],
        latest_date=counts["latest"],
        last_rebuild_at=counts["last_rebuild"],
        top_themes=[
            TopTheme(
                theme=r["theme"],
                n_days=int(r["n_days"]),
                n_candidates=int(r["n_candidates"]),
            )
            for r in top_rows
        ],
    )

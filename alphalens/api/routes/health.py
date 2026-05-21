"""Liveness + readiness probes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from alphalens.api import db as db_module
from alphalens.api.models import Health, Ready

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe (no DB).")
def healthz() -> Health:
    return Health(status="ok")


def _last_rebuild_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(rebuilt_at) AS last_rebuild_at FROM days_meta").fetchone()
    return row["last_rebuild_at"] if row else None


@router.get(
    "/readyz",
    summary="Readiness probe — confirms cache DB is reachable.",
    responses={503: {"description": "Cache DB missing or unreadable."}},
)
def readyz(request: Request) -> Ready:
    db_path: Path = request.app.state.db_path
    if not db_path.exists():
        raise HTTPException(status_code=503, detail=f"cache DB not found: {db_path}")
    try:
        conn = db_module.connect(db_path, read_only=True)
    except (sqlite3.Error, FileNotFoundError) as exc:
        raise HTTPException(status_code=503, detail=f"cache DB unreadable: {exc}") from exc
    try:
        n_days_row = conn.execute("SELECT COUNT(*) AS n FROM days_meta").fetchone()
        n_briefs_row = conn.execute("SELECT COUNT(*) AS n FROM briefs").fetchone()
        last = _last_rebuild_at(conn)
    finally:
        conn.close()
    return Ready(
        status="ok",
        db_path=str(db_path),
        last_rebuild_at=last,
        n_days=int(n_days_row["n"]),
        n_candidates=int(n_briefs_row["n"]),
    )

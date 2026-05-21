"""FastAPI dependencies.

``get_db`` reads the configured cache DB path from ``app.state`` so route
modules don't need a module-level reference to it. Each request opens a fresh
read-only SQLite connection — cheap because SQLite is in-process.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import HTTPException, Request

from alphalens.api import db as db_module


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    db_path: Path = request.app.state.db_path
    if not db_path.exists():
        raise HTTPException(status_code=503, detail=f"cache DB not found: {db_path}")
    try:
        conn = db_module.connect(db_path, read_only=True)
    except (sqlite3.Error, FileNotFoundError) as exc:
        raise HTTPException(status_code=503, detail=f"cache DB unreadable: {exc}") from exc
    try:
        yield conn
    finally:
        conn.close()

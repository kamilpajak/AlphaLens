"""SQLite connection helpers for the API.

Write path (``cache.rebuild_from_parquet``) opens in WAL mode so the
rebuild itself can proceed concurrently. Read path (api request handlers)
opens with ``?mode=ro&immutable=1`` so the api container can serve from a
``:ro`` bind-mount where SQLite cannot create ``-wal``/``-shm`` sidecars;
the writer must checkpoint WAL into the main file before exit. Read
connections are per-request via the ``get_db`` FastAPI dependency.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".alphalens" / "api" / "briefs.db"

ENV_DB_PATH = "ALPHALENS_CACHE_DB"


def resolve_db_path(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the cache DB path with override > env > default precedence."""
    if override is not None:
        return Path(override)
    env = os.environ.get(ENV_DB_PATH)
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def connect(db_path: str | os.PathLike[str], *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection to ``db_path``.

    Write path: WAL journal mode + ``synchronous=NORMAL`` for ingest speed.
    Read path: ``?mode=ro&immutable=1`` so the connection can be served
    from a ``:ro`` bind-mount (SQLite would otherwise need to create
    ``-wal``/``-shm`` sidecars in the containing directory). The writer
    must checkpoint the WAL into the main db file before exit — see
    ``cache.rebuild_from_parquet``.
    """
    path = Path(db_path)
    if read_only:
        if not path.exists():
            raise FileNotFoundError(f"cache DB not found: {path}")
        uri = f"file:{path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db_factory(db_path: str | os.PathLike[str]):
    """Build a FastAPI dependency that yields a fresh read-only connection."""

    resolved = Path(db_path)

    def _dep() -> Iterator[sqlite3.Connection]:
        conn = connect(resolved, read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    return _dep

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def default_queue_path() -> Path:
    return Path.home() / ".alphalens" / "watchdog" / "auto_trigger_queue.db"


class AutoTriggerQueue:
    """SQLite-backed work queue for Layer 3 auto-analysis requests.

    Status lifecycle: pending → in_progress → (done | failed)
    """

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_queue_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS auto_trigger_queue ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ticker TEXT NOT NULL,"
            " accession_number TEXT NOT NULL,"
            " trigger_url TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " enqueued_at TEXT NOT NULL,"
            " started_at TEXT,"
            " finished_at TEXT,"
            " decision TEXT,"
            " error TEXT"
            ")"
        )
        self._conn.commit()

    def enqueue(self, ticker: str, accession: str, trigger_url: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO auto_trigger_queue "
            "(ticker, accession_number, trigger_url, status, enqueued_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (ticker, accession, trigger_url, _now_iso()),
        )
        self._conn.commit()
        return cur.lastrowid

    def claim_next(self) -> dict | None:
        """Atomically pick the oldest pending job and mark in_progress."""
        cur = self._conn.execute(
            "SELECT * FROM auto_trigger_queue WHERE status = 'pending' "
            "ORDER BY id LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return None

        now = _now_iso()
        self._conn.execute(
            "UPDATE auto_trigger_queue SET status = 'in_progress', started_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, row["id"]),
        )
        self._conn.commit()
        return {**dict(row), "status": "in_progress", "started_at": now}

    def mark_done(self, job_id: int, decision: str) -> None:
        self._conn.execute(
            "UPDATE auto_trigger_queue SET status = 'done', decision = ?, finished_at = ? "
            "WHERE id = ?",
            (decision, _now_iso(), job_id),
        )
        self._conn.commit()

    def mark_failed(self, job_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE auto_trigger_queue SET status = 'failed', error = ?, finished_at = ? "
            "WHERE id = ?",
            (error, _now_iso(), job_id),
        )
        self._conn.commit()

    def list_by_status(self, status: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM auto_trigger_queue WHERE status = ? ORDER BY id",
            (status,),
        )
        return [dict(r) for r in cur.fetchall()]

    def count_done_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM auto_trigger_queue "
            "WHERE status = 'done' AND DATE(finished_at) = ?",
            (today,),
        )
        return cur.fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> AutoTriggerQueue:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

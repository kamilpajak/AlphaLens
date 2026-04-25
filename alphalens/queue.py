"""SQLite-backed priority queue for candidate analysis.

Implements `CandidateSink`. Status lifecycle:

    pending → in_progress → (done | pending [retry] | dead)

Retry: on `mark_failure`, attempts += 1; `next_retry_at = now + base_retry_s * 2^(attempts-1)`.
After `max_attempts` failures the row becomes `status='dead'`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .candidates import Candidate

DEFAULT_BASE_RETRY_S = 60
DEFAULT_MAX_ATTEMPTS = 5


def default_queue_path() -> Path:
    return Path.home() / ".alphalens" / "candidates.db"


class CandidateQueue:
    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        base_retry_s: int = DEFAULT_BASE_RETRY_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        self.db_path = Path(db_path) if db_path else default_queue_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.base_retry_s = base_retry_s
        self.max_attempts = max_attempts

        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                source TEXT NOT NULL,
                priority INTEGER NOT NULL,
                payload TEXT NOT NULL,
                dedup_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                enqueued_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                decision TEXT,
                cost_usd REAL,
                duration_sec REAL,
                model_used TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_claim
                ON candidates(status, priority, enqueued_at);
            CREATE INDEX IF NOT EXISTS idx_candidates_retry
                ON candidates(status, next_retry_at);
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------ submit

    def submit(self, candidates: Iterable[Candidate]) -> int:
        now = _now_iso()
        inserted = 0
        for c in candidates:
            try:
                self._conn.execute(
                    "INSERT INTO candidates "
                    "(ticker, source, priority, payload, dedup_key, status, enqueued_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        c.ticker,
                        c.source,
                        c.priority,
                        json.dumps(c.payload, default=str),
                        c.dedup_key,
                        now,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # dedup_key UNIQUE violation — silently skip duplicates
                pass
        self._conn.commit()
        return inserted

    # ------------------------------------------------------------------- claim

    def claim_next(self) -> dict | None:
        return self._claim(respect_retry_window=True)

    def claim_next_ignoring_retry_window(self) -> dict | None:
        """Test helper: claim even if next_retry_at is in the future."""
        return self._claim(respect_retry_window=False)

    def _claim(self, *, respect_retry_window: bool) -> dict | None:
        now_iso = _now_iso()
        if respect_retry_window:
            sql = (
                "SELECT * FROM candidates "
                "WHERE status = 'pending' "
                "  AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY priority ASC, enqueued_at ASC LIMIT 1"
            )
            params: tuple = (now_iso,)
        else:
            sql = (
                "SELECT * FROM candidates "
                "WHERE status = 'pending' "
                "ORDER BY priority ASC, enqueued_at ASC LIMIT 1"
            )
            params = ()

        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None

        self._conn.execute(
            "UPDATE candidates SET status = 'in_progress', started_at = ? WHERE id = ?",
            (now_iso, row["id"]),
        )
        self._conn.commit()
        return {**dict(row), "status": "in_progress", "started_at": now_iso}

    # -------------------------------------------------------- result recording

    def mark_success(
        self,
        candidate_id: int,
        *,
        decision: str,
        duration_sec: float,
        cost_usd: float | None,
        model_used: str,
    ) -> None:
        self._conn.execute(
            "UPDATE candidates SET status = 'done', decision = ?, duration_sec = ?, "
            "cost_usd = ?, model_used = ?, finished_at = ? WHERE id = ?",
            (decision, duration_sec, cost_usd, model_used, _now_iso(), candidate_id),
        )
        self._conn.commit()

    def mark_failure(self, candidate_id: int, *, error: str) -> None:
        row = self._conn.execute(
            "SELECT attempts FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"]) + 1

        if attempts >= self.max_attempts:
            self._conn.execute(
                "UPDATE candidates SET status = 'dead', attempts = ?, error = ?, "
                "finished_at = ? WHERE id = ?",
                (attempts, error, _now_iso(), candidate_id),
            )
        else:
            delay = self.base_retry_s * (2 ** (attempts - 1))
            next_retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            self._conn.execute(
                "UPDATE candidates SET status = 'pending', attempts = ?, "
                "next_retry_at = ?, error = ?, started_at = NULL WHERE id = ?",
                (attempts, next_retry_at, error, candidate_id),
            )
        self._conn.commit()

    # ------------------------------------------------------------------ queries

    def list_by_status(self, status: str) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM candidates WHERE status = ? ORDER BY id", (status,))
        return [dict(r) for r in cur.fetchall()]

    def count_done_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status = 'done' AND DATE(finished_at) = ?",
            (today,),
        )
        return cur.fetchone()[0]

    # -------------------------------------------------------------- life-cycle

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CandidateQueue:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()

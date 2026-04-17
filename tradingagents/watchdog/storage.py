from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .types import Event


def default_db_path() -> Path:
    return Path.home() / ".tradingagents" / "watchdog" / "seen_events.db"


class SeenEventStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_events (accession_number TEXT PRIMARY KEY)"
        )
        self._conn.commit()

    def mark_seen(self, accession_number: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_events (accession_number) VALUES (?)",
            (accession_number,),
        )
        self._conn.commit()

    def has_seen(self, accession_number: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seen_events WHERE accession_number = ? LIMIT 1",
            (accession_number,),
        )
        return cur.fetchone() is not None

    def filter_unseen(self, events: Iterable[Event]) -> list[Event]:
        return [e for e in events if not self.has_seen(e.accession_number)]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SeenEventStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

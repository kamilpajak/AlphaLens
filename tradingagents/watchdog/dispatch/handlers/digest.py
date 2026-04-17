from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Protocol

from ...classifier import ClassifiedEvent, Severity
from ...portfolio import Relevance
from ...types import Event, FormType
from .base import AlertHandler

logger = logging.getLogger(__name__)


class Sender(Protocol):
    def send_message(self, text: str) -> None: ...


class DigestHandler(AlertHandler):
    """Buffered handler — accumulates events and flushes as one message on demand."""

    def __init__(self, db_path: Path | str, sender: Sender):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sender = sender
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS digest_buffer ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " payload TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    def handle(self, classified: ClassifiedEvent) -> None:
        payload = _serialize(classified)
        self._conn.execute("INSERT INTO digest_buffer (payload) VALUES (?)", (payload,))
        self._conn.commit()

    def buffered(self) -> list[ClassifiedEvent]:
        rows = self._conn.execute(
            "SELECT payload FROM digest_buffer ORDER BY id"
        ).fetchall()
        return [_deserialize(r[0]) for r in rows]

    def flush(self) -> None:
        items = self.buffered()
        if not items:
            return
        message = _format_digest(items)
        self.sender.send_message(message)
        self._conn.execute("DELETE FROM digest_buffer")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _serialize(c: ClassifiedEvent) -> str:
    return json.dumps({
        "ticker": c.event.ticker,
        "form_type": c.event.form_type.value,
        "accession_number": c.event.accession_number,
        "filed_at": c.event.filed_at.isoformat(),
        "url": c.event.url,
        "raw_data": c.event.raw_data,
        "severity": c.severity.value,
        "relevance": c.relevance.value,
        "action": c.action.value,
    })


def _deserialize(payload: str) -> ClassifiedEvent:
    from datetime import datetime

    from ...classifier import Action

    d = json.loads(payload)
    event = Event(
        ticker=d["ticker"],
        form_type=FormType(d["form_type"]),
        accession_number=d["accession_number"],
        filed_at=datetime.fromisoformat(d["filed_at"]),
        url=d["url"],
        raw_data=d["raw_data"],
    )
    return ClassifiedEvent(
        event=event,
        severity=Severity(d["severity"]),
        relevance=Relevance(d["relevance"]),
        action=Action(d["action"]),
    )


def _format_digest(items: list[ClassifiedEvent]) -> str:
    lines = [f"📋 Daily digest ({len(items)} alerts)", ""]
    for c in items:
        lines.append(
            f"• {c.event.ticker} {c.event.form_type.value} "
            f"[{c.severity.name}/{c.relevance.value}] — {c.event.url}"
        )
    return "\n".join(lines)

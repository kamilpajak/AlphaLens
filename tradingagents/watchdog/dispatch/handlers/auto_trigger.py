from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ...classifier import ClassifiedEvent
from .base import AlertHandler

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_PER_DAY = 5


class Sender(Protocol):
    def send_message(self, text: str) -> None: ...


class TradingAgentsProtocol(Protocol):
    def propagate(self, ticker: str, date: str): ...


class AutoTriggerHandler(AlertHandler):
    """Runs TradingAgents deep analysis on the event's ticker, subject to daily budget."""

    def __init__(
        self,
        ta_graph: TradingAgentsProtocol,
        notifier: Sender,
        budget_path: Path | str,
        budget_per_day: int = DEFAULT_BUDGET_PER_DAY,
    ):
        self.ta_graph = ta_graph
        self.notifier = notifier
        self.budget_path = Path(budget_path)
        self.budget_path.parent.mkdir(parents=True, exist_ok=True)
        self.budget_per_day = budget_per_day
        self._conn = sqlite3.connect(str(self.budget_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS trigger_counter (date TEXT PRIMARY KEY, count INTEGER NOT NULL)"
        )
        self._conn.commit()

    def handle(self, classified: ClassifiedEvent) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        used = self._count_for(today)
        ticker = classified.event.ticker

        if used >= self.budget_per_day:
            msg = (
                f"🛑 Auto-trigger budget exhausted ({used}/{self.budget_per_day} for {today}). "
                f"Skipping {ticker}. Manual trigger still available."
            )
            logger.warning(msg)
            self._notify(msg)
            return

        self._increment(today)
        date_str = classified.event.filed_at.date().isoformat()

        try:
            _, decision = self.ta_graph.propagate(ticker, date_str)
            msg = (
                f"🤖 Auto-analysis done — {ticker}\n"
                f"Decision: *{decision}*\n"
                f"Trigger: {classified.event.form_type.value} "
                f"({classified.severity.name}/{classified.relevance.value})\n"
                f"{classified.event.url}"
            )
            self._notify(msg)
        except Exception as exc:  # noqa: BLE001 — defensive, loop must continue
            logger.error("Auto-trigger propagate failed for %s: %s", ticker, exc, exc_info=True)
            self._notify(f"❌ Auto-analysis failed for {ticker}: {exc}")

    def _count_for(self, date: str) -> int:
        cur = self._conn.execute(
            "SELECT count FROM trigger_counter WHERE date = ?", (date,)
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def _increment(self, date: str) -> None:
        self._conn.execute(
            "INSERT INTO trigger_counter (date, count) VALUES (?, 1) "
            "ON CONFLICT(date) DO UPDATE SET count = count + 1",
            (date,),
        )
        self._conn.commit()

    def _notify(self, msg: str) -> None:
        try:
            self.notifier.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            logger.error("Notifier send failed: %s", exc)

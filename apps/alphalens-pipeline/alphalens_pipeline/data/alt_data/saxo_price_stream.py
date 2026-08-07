"""Saxo LIVE price stream: one long-lived WebSocket thread + a quote cache.

Price subscriptions stream DELTAS - an unchanged field is OMITTED. This differs
from the positions/orders stream, whose reader deliberately never merges and
re-reads full REST state instead. There is no cheap full re-read for a quote, so
this cache MUST merge: a message carrying only a Bid must leave the Ask intact.

The socket loop only decodes and applies. Every decision about whether a cached
quote may drive an order lives in the feed adapter's freshness gate.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

LIVE_STREAM_URL = "wss://live-streaming.saxobank.com/oapi/streaming/ws/connect"


def _parse_utc(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Quote:
    uic: int
    bid: float | None
    ask: float | None
    event_time: dt.datetime | None
    delayed_by_minutes: int | None
    received_at: dt.datetime


class QuoteCache:
    """Thread-safe per-uic quote state with delta merging."""

    def __init__(self) -> None:
        self._quotes: dict[int, Quote] = {}
        self._lock = threading.Lock()

    def apply(self, row: dict[str, Any], *, received_at: dt.datetime) -> None:
        raw_uic = row.get("Uic")
        if raw_uic is None:
            return
        uic = int(raw_uic)
        event_time = _parse_utc(row.get("LastUpdated"))
        quote_block = row.get("Quote") or {}
        with self._lock:
            prev = self._quotes.get(uic)
            if prev is not None and prev.event_time and event_time and event_time < prev.event_time:
                return  # sequence regression: an older quote never overwrites a newer one
            merged = Quote(
                uic=uic,
                bid=quote_block.get("Bid", prev.bid if prev else None),
                ask=quote_block.get("Ask", prev.ask if prev else None),
                event_time=event_time or (prev.event_time if prev else None),
                delayed_by_minutes=quote_block.get(
                    "DelayedByMinutes", prev.delayed_by_minutes if prev else None
                ),
                received_at=received_at,
            )
            self._quotes[uic] = merged

    def get(self, uic: int) -> Quote | None:
        with self._lock:
            return self._quotes.get(uic)

    def forget(self, uic: int) -> None:
        with self._lock:
            self._quotes.pop(uic, None)

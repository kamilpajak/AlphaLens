"""Shared domain model for screener → TradingAgents handoff.

Every screener (watchdog SEC events, momentum, prescreener) produces `Candidate`
instances. The unified `CandidateSink` (see `alphalens.queue.CandidateQueue`)
consumes them; the worker drains the queue, invokes TradingAgents, and records
an `AnalysisResult`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Protocol


@dataclass(frozen=True)
class Candidate:
    ticker: str
    source: str
    detected_at: datetime
    priority: int                   # lower = higher priority (0 top)
    payload: dict[str, Any]         # JSON-serialisable; interpretation is source-specific
    dedup_key: str                  # stable hash; UNIQUE in the queue

    @classmethod
    def from_screener(
        cls,
        ticker: str,
        source: str,
        priority: int,
        payload: dict[str, Any],
        discriminator: str,
        detected_at: Optional[datetime] = None,
    ) -> "Candidate":
        key_input = f"{ticker}|{source}|{discriminator}".encode("utf-8")
        dedup = hashlib.sha256(key_input).hexdigest()
        return cls(
            ticker=ticker,
            source=source,
            detected_at=detected_at or datetime.now(timezone.utc),
            priority=priority,
            payload=dict(payload),
            dedup_key=dedup,
        )


@dataclass(frozen=True)
class AnalysisResult:
    candidate_id: int
    ticker: str
    source: str
    rating: str
    duration_sec: float
    cost_usd: Optional[float]
    model_used: str
    completed_at: datetime
    final_state: dict[str, Any] = field(default_factory=dict)


class CandidateSink(Protocol):
    def submit(self, candidates: Iterable[Candidate]) -> int: ...

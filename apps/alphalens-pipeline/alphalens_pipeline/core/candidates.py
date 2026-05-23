"""Shared domain model for the candidate queue.

Every screener (watchdog SEC events, momentum, prescreener) produces `Candidate`
instances. The unified `CandidateSink` (see `alphalens_pipeline.core.queue.CandidateQueue`)
records them; `AnalysisResult` rows are kept as a historical viewer over past
Layer 3 runs even though no live consumer drains the queue today.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Candidate:
    ticker: str
    source: str
    detected_at: datetime
    priority: int  # lower = higher priority (0 top)
    payload: dict[str, Any]  # JSON-serialisable, source-specific shape
    dedup_key: str  # stable hash; UNIQUE in the queue

    @classmethod
    def from_screener(
        cls,
        ticker: str,
        source: str,
        priority: int,
        payload: dict[str, Any],
        discriminator: str,
        detected_at: datetime | None = None,
    ) -> Candidate:
        key_input = f"{ticker}|{source}|{discriminator}".encode()
        dedup = hashlib.sha256(key_input).hexdigest()
        return cls(
            ticker=ticker,
            source=source,
            detected_at=detected_at or datetime.now(UTC),
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
    cost_usd: float | None
    model_used: str
    completed_at: datetime
    final_state: dict[str, Any] = field(default_factory=dict)


class CandidateSink(Protocol):
    def submit(self, candidates: Iterable[Candidate]) -> int: ...

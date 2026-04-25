"""Schema + validation for the Lean algo's output JSON.

Produced by `lean_project/main.py::on_end_of_algorithm`, consumed by the host
orchestrator (`runner.py` → `pipeline.py`). Keep in sync with `SCHEMA_VERSION`
inside the algo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_VERSION = "1.0"

_REQUIRED_TOP_LEVEL = (
    "status",
    "timestamp",
    "version",
    "total_scored",
    "universe_size",
    "rankings",
)
_REQUIRED_RANKING_FIELDS = (
    "ticker",
    "rank",
    "score",
    "roc5",
    "roc20",
    "roc60",
    "volume_surprise",
    "trend_strength",
    "breakout",
    "near_high",
    "last_close",
    "avg_dollar_volume",
)


@dataclass(frozen=True)
class RankingRow:
    ticker: str
    rank: int
    score: float
    roc5: float
    roc20: float
    roc60: float
    volume_surprise: float
    trend_strength: float
    breakout: bool
    near_high: float
    last_close: float
    avg_dollar_volume: float


@dataclass(frozen=True)
class LeanOutput:
    status: str
    timestamp: str
    version: str
    total_scored: int
    universe_size: int
    rankings: list[RankingRow] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LeanOutput:
        for key in _REQUIRED_TOP_LEVEL:
            if key not in payload:
                raise ValueError(f"Lean output missing top-level '{key}'")
        if payload["version"] != SUPPORTED_VERSION:
            raise ValueError(
                f"Lean output version '{payload['version']}' != expected '{SUPPORTED_VERSION}'"
            )
        rankings = [_ranking_from_dict(row) for row in payload["rankings"]]
        return cls(
            status=str(payload["status"]),
            timestamp=str(payload["timestamp"]),
            version=str(payload["version"]),
            total_scored=int(payload["total_scored"]),
            universe_size=int(payload["universe_size"]),
            rankings=rankings,
        )

    @classmethod
    def from_file(cls, path: Path) -> LeanOutput:
        text = Path(path).read_text()
        return cls.from_dict(json.loads(text))


def _ranking_from_dict(row: dict[str, Any]) -> RankingRow:
    for key in _REQUIRED_RANKING_FIELDS:
        if key not in row:
            raise ValueError(f"Ranking row missing '{key}': {row!r}")
    return RankingRow(
        ticker=str(row["ticker"]).upper(),
        rank=int(row["rank"]),
        score=float(row["score"]),
        roc5=float(row["roc5"]),
        roc20=float(row["roc20"]),
        roc60=float(row["roc60"]),
        volume_surprise=float(row["volume_surprise"]),
        trend_strength=float(row["trend_strength"]),
        breakout=bool(row["breakout"]),
        near_high=float(row["near_high"]),
        last_close=float(row["last_close"]),
        avg_dollar_volume=float(row["avg_dollar_volume"]),
    )

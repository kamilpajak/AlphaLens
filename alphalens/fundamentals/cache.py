"""Disk-backed TTL cache for fundamentals features.

One JSON file per ticker at `~/.alphalens/fundamentals/{TICKER}.json`:
  {"features": {...}, "fetched_at": "2026-04-21T14:00:00+00:00"}

TTL defaults to 90 days (quarterly reporting cycle). Fresh entries are
returned from disk without hitting Alpha Vantage. Stale/missing entries
trigger a fetch via `get_or_fetch(ticker, fetcher_fn)`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def _default_root() -> Path:
    return Path.home() / ".alphalens" / "fundamentals"


class FundamentalsCache:
    def __init__(self, root: Path | None = None, ttl_days: int = 90):
        self.root = root or _default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(days=ttl_days)

    def _path(self, ticker: str) -> Path:
        return self.root / f"{ticker.upper()}.json"

    def get(self, ticker: str) -> dict | None:
        path = self._path(ticker)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("fundamentals cache corrupted for %s: %s", ticker, exc)
            return None
        fetched_at_str = raw.get("fetched_at")
        if not fetched_at_str:
            return None
        try:
            fetched_at = datetime.fromisoformat(fetched_at_str)
        except ValueError:
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - fetched_at > self.ttl:
            return None
        return raw.get("features")

    def put(self, ticker: str, features: dict) -> None:
        path = self._path(ticker)
        payload = {
            "features": features,
            "fetched_at": datetime.now(UTC).isoformat(),
        }
        path.write_text(json.dumps(payload, default=str))

    def get_or_fetch(self, ticker: str, fetcher: Callable[[str], dict]) -> dict:
        cached = self.get(ticker)
        if cached is not None:
            return cached
        features = fetcher(ticker)
        self.put(ticker, features)
        return features

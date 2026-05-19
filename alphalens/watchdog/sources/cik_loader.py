from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from alphalens.data.alt_data.sec_edgar_client import (
    SecEdgarClient,
    get_default_sec_client,
)

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 7 * 86400  # 7 days


def default_cik_cache_path() -> Path:
    return Path.home() / ".alphalens" / "watchdog" / "company_tickers.json"


class CIKLoader:
    """Resolve ticker → 10-digit zero-padded CIK against SEC's master mapping.

    HTTP fetch is delegated to :class:`SecEdgarClient` (single canonical SEC
    transport in the repo); the loader owns only the local 7-day file-TTL
    cache on top.
    """

    def __init__(
        self,
        cache_path: Path | str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        sec_client: SecEdgarClient | None = None,
    ):
        self.cache_path = Path(cache_path) if cache_path else default_cik_cache_path()
        self.ttl_seconds = ttl_seconds
        self._sec = sec_client or get_default_sec_client()
        self._mapping: dict[str, str] = {}

    def load(self) -> None:
        if self._cache_is_fresh():
            payload = json.loads(self.cache_path.read_text())
        else:
            payload = self._sec.fetch_company_tickers()
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(payload))
        self._mapping = self._build_mapping(payload)

    def get_cik(self, ticker: str) -> str | None:
        return self._mapping.get(ticker.upper())

    def _cache_is_fresh(self) -> bool:
        if not self.cache_path.exists():
            return False
        age = time.time() - self.cache_path.stat().st_mtime
        return age < self.ttl_seconds

    @staticmethod
    def _build_mapping(payload: dict) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            ticker = entry.get("ticker")
            cik = entry.get("cik_str")
            if ticker and cik is not None:
                mapping[str(ticker).upper()] = str(cik).zfill(10)
        return mapping

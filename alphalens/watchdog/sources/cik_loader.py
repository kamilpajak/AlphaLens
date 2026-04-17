from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DEFAULT_TTL_SECONDS = 7 * 86400  # 7 days


def default_cik_cache_path() -> Path:
    return Path.home() / ".alphalens" / "watchdog" / "company_tickers.json"


class CIKLoader:
    def __init__(
        self,
        user_agent: str,
        cache_path: Path | str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        if not user_agent:
            raise ValueError("user_agent required (SEC mandates real contact info)")
        self.user_agent = user_agent
        self.cache_path = Path(cache_path) if cache_path else default_cik_cache_path()
        self.ttl_seconds = ttl_seconds
        self._mapping: dict[str, str] = {}

    def load(self) -> None:
        if self._cache_is_fresh():
            payload = json.loads(self.cache_path.read_text())
        else:
            payload = self._download()
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

    def _download(self) -> dict:
        resp = requests.get(
            SEC_TICKERS_URL,
            headers={"User-Agent": self.user_agent},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

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

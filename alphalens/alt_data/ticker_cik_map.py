"""Ticker → SEC CIK lookup.

CIK (Central Index Key) identifies a filer in SEC EDGAR. EDGAR URLs require
the CIK zero-padded to 10 digits (e.g. Apple's 320193 → ``0000320193``).

The authoritative source is SEC's ``company_tickers.json`` endpoint; this
loader consumes a pre-downloaded snapshot YAML for deterministic tests and
offline use. A small refresher script belongs in Phase 2 tooling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


def _normalize_cik(raw: object) -> str:
    """Return a CIK as a zero-padded 10-digit string or raise ValueError."""
    if isinstance(raw, int):
        digits = str(raw)
    elif isinstance(raw, str):
        digits = raw.strip()
    else:
        raise ValueError(f"Unsupported CIK type: {type(raw).__name__}")
    if not digits.isdigit():
        raise ValueError(f"CIK must be numeric, got {digits!r}")
    if len(digits) > 10:
        raise ValueError(f"CIK exceeds 10 digits: {digits!r}")
    return digits.zfill(10)


@dataclass(frozen=True)
class TickerCikMap:
    _by_ticker: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> TickerCikMap:
        if not path.exists():
            raise FileNotFoundError(f"Ticker→CIK map not found: {path}")
        data = yaml.safe_load(path.read_text()) or {}
        normalized: dict[str, str] = {}
        for ticker, cik in data.items():
            normalized[str(ticker).upper()] = _normalize_cik(cik)
        return cls(_by_ticker=normalized)

    def lookup(self, ticker: str) -> str | None:
        return self._by_ticker.get(ticker.upper())

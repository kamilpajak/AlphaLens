"""Refresh the ticker↔CIK map from SEC's master table.

The refreshed YAML is compatible with :class:`alphalens.alt_data.ticker_cik_map.TickerCikMap`,
so a production workflow is::

    client = SecEdgarClient(user_agent="AlphaLens research@example.com")
    refresh_ticker_cik_map(client, Path("alphalens/alt_data/data/ticker_cik_map.yaml"))
    TickerCikMap.load(path).lookup("AAPL")  # → "0000320193"

SEC publishes the mapping at ``/files/company_tickers.json`` as a dict keyed
by an integer index, so we iterate values() ignoring keys. Duplicates (which
SEC shouldn't publish but we can't guarantee) are resolved last-wins.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from .sec_edgar_client import SecEdgarClient


def parse_sec_company_tickers(payload: Mapping[str, Mapping]) -> dict[str, int]:
    """Normalise SEC's ``company_tickers.json`` into a ``{TICKER: cik_int}`` dict."""
    out: dict[str, int] = {}
    for entry in payload.values():
        if "cik_str" not in entry:
            raise ValueError(f"missing cik_str in entry: {entry!r}")
        if "ticker" not in entry:
            raise ValueError(f"missing ticker in entry: {entry!r}")
        ticker = str(entry["ticker"]).upper()
        out[ticker] = int(entry["cik_str"])
    return out


def refresh_ticker_cik_map(
    edgar_client: SecEdgarClient,
    output_path: Path,
) -> int:
    """Download SEC's master map, write yaml, return count written."""
    payload = edgar_client.fetch_company_tickers()
    normalised = parse_sec_company_tickers(payload)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(normalised, sort_keys=True))
    return len(normalised)

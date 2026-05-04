"""Refresh iShares ETF holdings as PIT universe snapshots.

Generic refresher analogous to ``alphalens.data.alt_data.iwm_refresher`` but
written specifically for index-membership PIT loaders that consume the
``{as_of, source, tickers}`` schema (vs the IWM loader's ``{tickers}`` only).

URLs verified 2026-05-03 against iShares retail catalog:
  - IJH (Core S&P Mid-Cap, tracks S&P 400)
  - IJR (Core S&P Small-Cap, tracks S&P 600)
  - IVV (Core S&P 500) — provided for completeness; existing
    ``data/sp500_pit/`` snapshots have their own scrape pipeline.

Survivorship caveat: a refresh produces a CURRENT-MEMBERSHIP snapshot
labeled with today's date. Companies that left the index in the past are
absent. Acceptable for breadth audits and Phase-1 MVP work; flagged in v4
verdict memo.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path

import requests
import yaml

from alphalens.data.alt_data.iwm_refresher import parse_ishares_csv

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "AlphaLens (research / personal use)"

# iShares AJAX URL pattern is identical across funds; only the product ID
# and ticker filename change. URLs sourced 2026-05-03 from ishares.com.
ETF_URLS: dict[str, str] = {
    "IJH": (
        "https://www.ishares.com/us/products/239763/ishares-core-sp-midcap-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IJH_holdings&dataType=fund"
    ),
    "IJR": (
        "https://www.ishares.com/us/products/239774/ishares-core-sp-smallcap-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IJR_holdings&dataType=fund"
    ),
    "IVV": (
        "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund"
    ),
}


def _default_fetcher(url: str) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": _DEFAULT_USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def refresh_ishares_snapshot(
    *,
    etf_symbol: str,
    output_path: Path,
    as_of: date,
    source: str | None = None,
    notes: str | None = None,
    csv_text_fetcher: Callable[[str], str] | None = None,
    fallback_path: Path | None = None,
) -> int:
    """Fetch ``etf_symbol`` holdings from iShares, write PIT snapshot YAML.

    Output schema: ``{as_of, source, notes?, tickers}`` — compatible with
    :func:`alphalens.data.universes.sp1500_pit._load_for_date`.

    On fetch/parse failure, copies ``fallback_path`` → ``output_path`` if
    provided. Raises original exception otherwise.

    Returns the count of tickers written.
    """
    if etf_symbol not in ETF_URLS:
        raise ValueError(f"Unknown iShares ETF: {etf_symbol}. Known: {sorted(ETF_URLS)}")

    fetcher = csv_text_fetcher or _default_fetcher
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        csv_text = fetcher(ETF_URLS[etf_symbol])
        tickers = parse_ishares_csv(csv_text)
    except Exception as exc:
        if fallback_path is None:
            raise
        logger.warning(
            "iShares %s refresh failed (%s); using fallback %s",
            etf_symbol,
            exc,
            fallback_path,
        )
        shutil.copyfile(fallback_path, output_path)
        with open(output_path) as f:
            data = yaml.safe_load(f) or {}
        return len(data.get("tickers") or [])

    payload: dict[str, object] = {
        "as_of": as_of.isoformat(),
        "source": source or f"iShares {etf_symbol} current snapshot (AJAX CSV)",
        "tickers": tickers,
    }
    if notes:
        payload["notes"] = notes
    output_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return len(tickers)

"""Refresh the IWM (Russell 2000 ETF) current constituents snapshot.

iShares does not publish an officially-documented bulk CSV endpoint for IWM
holdings; the undocumented AJAX URL below has been used by retail quants
for years but carries no SLA (Perplexity R7 consultation, 2026-04-22). The
refresher therefore supports an optional ``fallback_path`` pointing to a
manually-curated YAML snapshot committed to the repo — used when the live
fetch fails or returns malformed data.

iShares CSV payload shape::

    "iShares Russell 2000 ETF"         <- junk preamble rows
    "Fund Holdings as of Apr 18 2026"
    ""
    "Ticker","Name","Sector","Asset Class",...<- real header row
    "UPST","Upstart Holdings","Financial","Equity",...
    "-","USD Cash","--","Cash",...     <- cash rows we drop
    ...

We keep only ``Asset Class == "Equity"`` rows and a non-empty, non-``-``
ticker. Duplicates are deduplicated preserving first occurrence.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import requests
import yaml

# Strict ticker pattern: 1-6 uppercase letters, optional class suffix (e.g. BRK.B, GOOG-L).
# Rejects iShares CSV footer disclaimers that otherwise slip into the Ticker column.
_VALID_TICKER_RE = re.compile(r"^[A-Z]{1,6}([.\-][A-Z]{1,2})?$")

logger = logging.getLogger(__name__)

IWM_AJAX_URL = (
    "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
)
_DEFAULT_USER_AGENT = "AlphaLens (research / personal use)"


class IsharesCsvFormatError(ValueError):
    """Raised when the iShares CSV payload doesn't contain a header row."""


def _extract_equity_ticker(record: dict) -> str | None:
    """Return canonical ticker if record is a valid equity row, else None.

    Rejects: missing/dash ticker, non-equity asset class, non-ticker strings
    (footer disclaimers smashed into the first column).
    """
    ticker = (record.get("Ticker") or "").strip().upper()
    if not ticker or ticker == "-":
        return None
    asset_class = (record.get("Asset Class") or "").strip()
    if asset_class and asset_class.lower() != "equity":
        return None
    if not _VALID_TICKER_RE.match(ticker):
        return None
    return ticker


def parse_ishares_csv(csv_text: str) -> list[str]:
    """Extract the list of equity tickers from an iShares IWM holdings CSV."""
    reader = csv.reader(io.StringIO(csv_text))
    header_cols: list[str] | None = None
    seen: set[str] = set()
    out: list[str] = []

    for row in reader:
        if header_cols is None:
            if row and row[0].strip().lower() == "ticker":
                header_cols = [c.strip() for c in row]
            continue
        if not row or all(not cell.strip() for cell in row):
            continue
        ticker = _extract_equity_ticker(dict(zip(header_cols, row, strict=False)))
        if ticker is None or ticker in seen:
            continue
        seen.add(ticker)
        out.append(ticker)

    if header_cols is None:
        raise IsharesCsvFormatError("no 'Ticker' header row found in iShares CSV")
    return out


def _default_fetcher() -> str:
    resp = requests.get(
        IWM_AJAX_URL,
        headers={"User-Agent": _DEFAULT_USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def refresh_iwm_current(
    output_path: Path,
    *,
    csv_text_fetcher: Callable[[], str] | None = None,
    fallback_path: Path | None = None,
) -> int:
    """Fetch IWM holdings, write yaml compatible with load_iwm_current, return count.

    On fetch/parse failure copies ``fallback_path`` → ``output_path`` (warning
    logged). When no fallback is supplied, the underlying exception propagates.
    """
    fetcher = csv_text_fetcher or _default_fetcher
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        csv_text = fetcher()
        tickers = parse_ishares_csv(csv_text)
    except Exception as exc:
        if fallback_path is None:
            raise
        logger.warning("iShares IWM refresh failed (%s); using fallback %s", exc, fallback_path)
        shutil.copyfile(fallback_path, output_path)
        with open(output_path) as f:
            data = yaml.safe_load(f) or {}
        return len(data.get("tickers") or [])

    payload = {"tickers": tickers}
    output_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return len(tickers)

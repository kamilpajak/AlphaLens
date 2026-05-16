"""SEC NPORT-P ETF holdings reader for the Layer 3 verification gate.

Each thematic ETF (mapped in ``config/theme_etfs.yaml``) files NPORT-P
quarterly via its parent trust. Trusts house multiple funds, so a single
NPORT-P filing corresponds to one series — we identify the correct one by
matching the ``<seriesName>`` substring.

Public API:
- :func:`load_theme_etf_config` — theme → list of {etf, series_name}
- :func:`find_latest_filing` — SEC full-text search for the most recent
  NPORT-P whose ``seriesName`` matches a target substring
- :func:`fetch_holdings` — download + parse + cache one ETF's holdings
- :func:`is_in_thematic_etf` — verification gate: does ``ticker`` (or its
  resolved company name) appear in any ETF mapped to any of ``themes``?
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_etf_holdings"
THEME_ETFS_PATH = Path(__file__).parent.parent / "config" / "theme_etfs.yaml"
DEFAULT_USER_AGENT = "AlphaLens-thematic pajakkamil@gmail.com"
USER_AGENT_ENV = "THEMATIC_USER_AGENT"
SEC_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
DEFAULT_MAX_AGE_DAYS = 100  # NPORT-P files quarterly; refresh every ~14 weeks
SERIES_DISAMBIG_MAX_FETCHES = 5  # cap N+1 EDGAR primary_doc.xml lookups per search


def _user_agent() -> str:
    return os.environ.get(USER_AGENT_ENV) or DEFAULT_USER_AGENT


def _http_get(url: str, *, accept: str = "*/*", timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(), "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@lru_cache(maxsize=1)
def load_theme_etf_config() -> dict[str, list[dict]]:
    """Return the ``theme -> [{etf, series_name}, ...]`` mapping."""
    with THEME_ETFS_PATH.open() as f:
        data = yaml.safe_load(f)
    return dict(data.get("themes") or {})


# --- XML parsing ----------------------------------------------------------


def _strip_namespace(el: ET.Element) -> None:
    el.tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
    for child in el:
        _strip_namespace(child)


def _text(el: ET.Element, path: str, default: str = "") -> str:
    n = el.find(path)
    if n is None or n.text is None:
        return default
    return n.text.strip()


def parse_nport_p(xml_text: str) -> tuple[dict, pd.DataFrame]:
    """Parse a primary_doc.xml NPORT-P filing.

    Returns ``({series_name, series_id, report_date}, holdings_df)``.
    The holdings frame has columns: ``name, cusip, ticker, pct_val, asset_cat``.
    """
    root = ET.fromstring(xml_text)
    _strip_namespace(root)

    meta = {
        "series_name": _text(root, ".//seriesName"),
        "series_id": _text(root, ".//seriesId"),
        "report_date": _text(root, ".//repPdDate"),
    }

    rows: list[dict] = []
    for h in root.findall(".//invstOrSec"):
        ticker_el = h.find(".//identifiers/ticker")
        ticker = (
            ticker_el.attrib.get("value", "") if ticker_el is not None else _text(h, ".//ticker")
        )
        try:
            pct = float(_text(h, "pctVal", "0") or "0")
        except ValueError:
            pct = 0.0
        rows.append(
            {
                "name": _text(h, "name"),
                "cusip": _text(h, "cusip"),
                "ticker": ticker.strip(),
                "pct_val": pct,
                "asset_cat": _text(h, "assetCat"),
            }
        )
    df = pd.DataFrame(rows, columns=["name", "cusip", "ticker", "pct_val", "asset_cat"])
    return meta, df


# --- SEC EDGAR search -----------------------------------------------------


def _search_nport_p(query: str, *, max_results: int = 25) -> dict:
    """Run an SEC EDGAR full-text search restricted to NPORT-P filings."""
    params = {
        "q": f'"{query}"',
        "forms": "NPORT-P",
        "dateRange": "custom",
        "startdt": (dt.date.today() - dt.timedelta(days=365)).isoformat(),
        "enddt": dt.date.today().isoformat(),
    }
    url = f"{SEC_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    body = _http_get(url, accept="application/json")
    data = json.loads(body)
    hits = data.get("hits", {}).get("hits", [])[:max_results]
    return {"hits": {"hits": hits}}


def _fetch_series_name(cik: str, adsh: str) -> str:
    """Fetch primary_doc.xml for ``(cik, adsh)`` and return the ``seriesName``."""
    adsh_clean = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh_clean}/primary_doc.xml"
    xml_bytes = _http_get(url, accept="application/xml")
    root = ET.fromstring(xml_bytes)
    _strip_namespace(root)
    return _text(root, ".//seriesName")


def _fetch_primary_doc(cik: str, adsh: str) -> str:
    adsh_clean = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh_clean}/primary_doc.xml"
    return _http_get(url, accept="application/xml").decode("utf-8", errors="replace")


def find_latest_filing(*, series_name: str) -> dict | None:
    """Locate the most recent NPORT-P whose ``seriesName`` matches ``series_name``.

    EDGAR full-text search returns trust-level matches; we then disambiguate
    by inspecting each candidate's primary_doc.xml until we find a series
    name containing ``series_name`` (case-insensitive). Returns
    ``{cik, adsh, file_date, primary_doc_url, matched_series_name}`` or
    ``None`` if no candidate's series name matches.
    """
    raw = _search_nport_p(series_name)
    target = series_name.lower()
    # Cap per-search primary_doc.xml fetches — N+1 disambiguation lookups
    # against EDGAR scale poorly on busy trusts (Tidal Trust II files 30+
    # series per quarter); 5 hits is plenty since `_search_nport_p` already
    # ranks by date.
    for hit in (raw.get("hits", {}).get("hits", []))[:SERIES_DISAMBIG_MAX_FETCHES]:
        src = hit.get("_source", {}) or {}
        ciks = src.get("ciks") or []
        if not ciks:
            continue
        cik = ciks[0]
        adsh = src.get("adsh", "")
        file_date = src.get("file_date", "")
        try:
            found_name = _fetch_series_name(cik, adsh)
        except Exception as exc:
            logger.warning("series name fetch failed for %s/%s: %s", cik, adsh, exc)
            continue
        if target in found_name.lower():
            adsh_clean = adsh.replace("-", "")
            return {
                "cik": cik,
                "adsh": adsh,
                "file_date": file_date,
                "primary_doc_url": (
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                    f"{adsh_clean}/primary_doc.xml"
                ),
                "matched_series_name": found_name,
            }
    return None


# --- Top-level fetch + cache ---------------------------------------------


def _find_cached(etf: str, cache_dir: Path, max_age_days: int) -> Path | None:
    """Return the freshest cached parquet for ``etf`` under ``max_age_days``."""
    if not cache_dir.exists():
        return None
    candidates = sorted(cache_dir.glob(f"{etf}_*.parquet"))
    if not candidates:
        return None
    latest = candidates[-1]
    m = re.match(rf"{re.escape(etf)}_(\d{{4}}-\d{{2}}-\d{{2}})\.parquet$", latest.name)
    if not m:
        return latest  # accept anyway if naming drift
    try:
        report_date = dt.date.fromisoformat(m.group(1))
    except ValueError:
        return latest
    if (dt.date.today() - report_date).days <= max_age_days:
        return latest
    return None


def fetch_holdings(
    *,
    etf: str,
    series_name: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    force: bool = False,
) -> pd.DataFrame:
    """Download + parse the latest NPORT-P for ``series_name``; cache + return."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not force:
        cached = _find_cached(etf, cache_dir, max_age_days)
        if cached is not None:
            return pd.read_parquet(cached)

    filing = find_latest_filing(series_name=series_name)
    if filing is None:
        logger.warning("no NPORT-P filing found for series %r", series_name)
        return pd.DataFrame(columns=["name", "cusip", "ticker", "pct_val", "asset_cat"])

    xml_text = _fetch_primary_doc(filing["cik"], filing["adsh"])
    meta, df = parse_nport_p(xml_text)
    report_date = meta.get("report_date") or filing["file_date"]
    cache_path = cache_dir / f"{etf}_{report_date}.parquet"
    df.to_parquet(cache_path, index=False)
    return df


# --- Verification gate API ------------------------------------------------


def _load_etf_holdings(etf: str, cache_dir: Path) -> pd.DataFrame:
    candidates = sorted(cache_dir.glob(f"{etf}_*.parquet"))
    if not candidates:
        return pd.DataFrame(columns=["name", "cusip", "ticker", "pct_val", "asset_cat"])
    return pd.read_parquet(candidates[-1])


def is_in_thematic_etf(
    *,
    ticker: str,
    themes: Iterable[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    ticker_to_name: dict[str, str] | None = None,
) -> bool:
    """Return ``True`` iff ``ticker`` is held by any ETF mapped to any of ``themes``.

    Matching strategy:
    1. Exact ticker match against the NPORT-P ``ticker`` column.
    2. If that misses AND ``ticker_to_name[ticker]`` is provided, fall back to
       case-insensitive substring match on the ``name`` column. NPORT-P
       filings often leave the ``<ticker>`` element empty, so this
       company-name fallback is the workhorse path for many issuers.
    """
    cfg = load_theme_etf_config()
    relevant_etfs: list[str] = []
    for theme in themes:
        for entry in cfg.get(theme, []) or []:
            relevant_etfs.append(entry["etf"])
    if not relevant_etfs:
        return False

    ticker_upper = ticker.upper()
    name_query = (ticker_to_name or {}).get(ticker_upper, "").lower()
    for etf in relevant_etfs:
        df = _load_etf_holdings(etf, cache_dir)
        if df.empty:
            continue
        if (df["ticker"].str.upper() == ticker_upper).any():
            return True
        if name_query:
            # Word-boundary match — "sun" must not match "sunrun" / "sunoco" /
            # "sunset", but it should still match "Sun Microsystems Inc".
            pattern = rf"\b{re.escape(name_query)}\b"
            mask = df["name"].fillna("").str.lower().str.contains(pattern, regex=True, na=False)
            if mask.any():
                return True
    return False


__all__ = [
    "DEFAULT_CACHE_DIR",
    "fetch_holdings",
    "find_latest_filing",
    "is_in_thematic_etf",
    "load_theme_etf_config",
    "parse_nport_p",
]

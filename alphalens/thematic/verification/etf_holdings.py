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
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

from alphalens.data.alt_data.sec_edgar_client import get_default_sec_client

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_etf_holdings"
THEME_ETFS_PATH = Path(__file__).parent.parent / "config" / "theme_etfs.yaml"
SEC_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
DEFAULT_MAX_AGE_DAYS = 100  # NPORT-P files quarterly; refresh every ~14 weeks
SERIES_DISAMBIG_MAX_FETCHES = 5  # cap N+1 EDGAR primary_doc.xml lookups per search


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
    data = get_default_sec_client().get_json(url)
    hits = data.get("hits", {}).get("hits", [])[:max_results]
    return {"hits": {"hits": hits}}


def _fetch_series_name(cik: str, adsh: str) -> str:
    """Fetch primary_doc.xml for ``(cik, adsh)`` and return the ``seriesName``."""
    adsh_clean = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh_clean}/primary_doc.xml"
    xml_bytes = get_default_sec_client().get_bytes(url)
    root = ET.fromstring(xml_bytes)
    _strip_namespace(root)
    return _text(root, ".//seriesName")


def _fetch_primary_doc(cik: str, adsh: str) -> str:
    adsh_clean = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh_clean}/primary_doc.xml"
    return get_default_sec_client().get_text(url, encoding="utf-8")


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


def _resolve_theme_keys(theme: str, cfg: dict) -> list[str]:
    """Map a Layer-2 theme label to one or more config keys.

    Layer 2 LLM emits richer labels (``quantum_computing``,
    ``quantum_error_correction``) than the canonical YAML keys
    (``quantum``). Exact match wins; otherwise token-prefix match — config
    key ``quantum`` matches any theme that starts with ``quantum_``.
    """
    t = theme.lower()
    if t in cfg:
        return [t]
    return [k for k in cfg if t.startswith(k.lower() + "_")]


def _load_etf_holdings(etf: str, cache_dir: Path, *, asof: dt.date | None = None) -> pd.DataFrame:
    """Load the most-recent cached holdings parquet for ``etf``.

    With ``asof=None`` (default, live flow): pick the alphabetically-last
    parquet — preserves legacy behaviour.

    With ``asof`` set (PIT flow): only consider parquets whose filename
    date prefix is ``≤ asof``, then pick the latest of the remaining.
    Empty DataFrame when no file qualifies — caller treats as "gate
    unknown" rather than false-negative.
    """
    candidates = sorted(cache_dir.glob(f"{etf}_*.parquet"))
    if not candidates:
        return pd.DataFrame(columns=["name", "cusip", "ticker", "pct_val", "asset_cat"])
    if asof is not None:
        eligible: list[Path] = []
        for path in candidates:
            # Cache filename shape: "{etf}_{YYYY-MM-DD}.parquet". Use rsplit
            # so an ETF symbol with an underscore can't shift the date slice
            # and silently mis-classify the file.
            date_str = path.stem.rsplit("_", 1)[-1]
            try:
                file_date = dt.date.fromisoformat(date_str)
            except ValueError:
                continue
            if file_date <= asof:
                eligible.append(path)
        if not eligible:
            return pd.DataFrame(columns=["name", "cusip", "ticker", "pct_val", "asset_cat"])
        candidates = eligible
    return pd.read_parquet(candidates[-1])


def _collect_relevant_etfs(themes: Iterable[str], cfg: dict) -> list[tuple[str, str]]:
    """Resolve themes → unique (etf, series_name) tuples (preserves first-seen order)."""
    relevant: list[tuple[str, str]] = []
    seen: set[str] = set()
    for theme in themes:
        for cfg_key in _resolve_theme_keys(theme, cfg):
            for entry in cfg.get(cfg_key, []) or []:
                if entry["etf"] in seen:
                    continue
                seen.add(entry["etf"])
                relevant.append((entry["etf"], entry["series_name"]))
    return relevant


def _load_or_prime(
    etf: str,
    series_name: str,
    cache_dir: Path,
    *,
    effective_prime: bool,
    max_age_days: int,
    asof: dt.date | None,
) -> pd.DataFrame:
    df = _load_etf_holdings(etf, cache_dir, asof=asof)
    if not df.empty or not effective_prime:
        return df
    try:
        return fetch_holdings(
            etf=etf,
            series_name=series_name,
            cache_dir=cache_dir,
            max_age_days=max_age_days,
        )
    except Exception as exc:
        logger.warning("ETF lazy-prime failed for %s: %s", etf, exc, exc_info=True)
        return pd.DataFrame()


def _matches_ticker_or_name(df: pd.DataFrame, ticker_upper: str, name_query: str) -> bool:
    if (df["ticker"].str.upper() == ticker_upper).any():
        return True
    if not name_query:
        return False
    # Word-boundary match — "sun" must not match "sunrun" / "sunoco" /
    # "sunset", but it should still match "Sun Microsystems Inc".
    pattern = rf"\b{re.escape(name_query)}\b"
    mask = df["name"].fillna("").str.lower().str.contains(pattern, regex=True, na=False)
    return bool(mask.any())


def is_in_thematic_etf(
    *,
    ticker: str,
    themes: Iterable[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    ticker_to_name: dict[str, str] | None = None,
    prime: bool = True,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    asof: dt.date | None = None,
) -> bool | None:
    """Return tri-state membership check for ``ticker`` across themed ETFs.

    Returns:
        - ``True``  — ticker is held by at least one mapped ETF (loaded df).
        - ``False`` — at least one mapped ETF loaded successfully and the
          ticker was simply not in its holdings ("ran-and-said-no").
        - ``None``  — no mapped ETF resolved (theme unmapped) OR every mapped
          ETF failed to load (cold cache + prime errored / disabled). The
          orchestrator records this as ``gates_unknown``, distinct from a
          real false-negative.

    Matching strategy unchanged: exact ticker match first; optional
    ``ticker_to_name`` word-boundary fallback for NPORT-P rows with empty
    ``<ticker>``. With ``prime=True`` (default), a cold cache triggers a
    one-time SEC NPORT-P download via :func:`fetch_holdings`; subsequent
    calls reuse the parquet within ``max_age_days``. Pass ``prime=False``
    to keep the gate read-only (offline / under test).
    """
    cfg = load_theme_etf_config()
    relevant = _collect_relevant_etfs(themes, cfg)
    if not relevant:
        return None

    ticker_upper = ticker.upper()
    name_query = (ticker_to_name or {}).get(ticker_upper, "").lower()
    # For historical asof, never lazy-prime: fetch_holdings would only
    # return today's NPORT-P filing — a look-ahead leak for a past asof.
    # Caller treats empty result as gates_unknown rather than false-negative.
    pit_replay = asof is not None and asof < dt.date.today()
    effective_prime = prime and not pit_replay
    any_loaded = False
    for etf, series_name in relevant:
        df = _load_or_prime(
            etf,
            series_name,
            cache_dir,
            effective_prime=effective_prime,
            max_age_days=max_age_days,
            asof=asof,
        )
        if df.empty:
            continue
        any_loaded = True
        if _matches_ticker_or_name(df, ticker_upper, name_query):
            return True
    return False if any_loaded else None


__all__ = [
    "DEFAULT_CACHE_DIR",
    "fetch_holdings",
    "find_latest_filing",
    "is_in_thematic_etf",
    "load_theme_etf_config",
    "parse_nport_p",
]

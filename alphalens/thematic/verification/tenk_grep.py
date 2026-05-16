"""10-K business-description keyword evidence — Layer 3 verification gate.

For each candidate ticker, fetch its most recent 10-K filing once, strip the
HTML to plain text, cache locally, and grep for theme keywords. SEC's
``data.sec.gov/submissions/CIK{cik}.json`` is the authoritative filing index;
``find_latest_10k`` picks the freshest 10-K entry from the ``filings.recent``
arrays.

The cache lives at ``~/.alphalens/thematic_tenk/{TICKER}_{filing_date}.txt``
and is one filing per ticker (10-Ks are annual; refresh ~yearly).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_tenk"
SEC_USER_AGENT = "AlphaLens-thematic pajakkamil@gmail.com"

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _http_get(url: str, *, accept: str = "*/*", timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@lru_cache(maxsize=1)
def _load_ticker_to_cik() -> dict[str, str]:
    """Pull SEC's company_tickers.json once and index by ticker."""
    body = _http_get("https://www.sec.gov/files/company_tickers.json", accept="application/json")
    payload = json.loads(body)
    mapping: dict[str, str] = {}
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        t = entry.get("ticker")
        cik = entry.get("cik_str")
        if t and cik is not None:
            mapping[str(t).upper()] = str(cik).zfill(10)
    return mapping


def _resolve_cik(ticker: str) -> str | None:
    return _load_ticker_to_cik().get(ticker.upper())


def _fetch_submissions_json(cik: str) -> dict:
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    return json.loads(_http_get(url, accept="application/json"))


def _fetch_filing_html(cik: str, accession: str, primary_doc: str) -> str:
    accession_clean = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_clean}/{primary_doc}"
    return _http_get(url).decode("utf-8", errors="replace")


def extract_text(html: str) -> str:
    """Strip HTML to plain text, collapsing whitespace."""
    no_tags = _HTML_TAG.sub(" ", html)
    return _WHITESPACE.sub(" ", no_tags).strip()


def grep_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    """Return the subset of ``keywords`` that appear in ``text`` (case-insensitive)."""
    text_lc = text.lower()
    hits: list[str] = []
    for kw in keywords:
        if not kw:
            continue
        if kw.lower() in text_lc:
            hits.append(kw)
    return hits


def find_latest_10k(submissions_payload: dict) -> dict | None:
    """Pick the most recent 10-K from ``filings.recent`` arrays.

    Returns ``{accession, filing_date, primary_doc}`` or ``None`` if no 10-K
    is in the recent filings window (SEC's submissions JSON typically holds
    the last ~1000 filings; for older 10-Ks the ``filings.files`` paginated
    pointers would be needed — not implemented since 10-Ks are annual).
    """
    recent = submissions_payload.get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []
    best: dict | None = None
    for form, acc, dt_, doc in zip(forms, accessions, dates, docs, strict=False):
        if form != "10-K":
            continue
        if best is None or dt_ > best["filing_date"]:
            best = {"accession": acc, "filing_date": dt_, "primary_doc": doc}
    return best


def _find_cached(ticker: str, cache_dir: Path) -> Path | None:
    if not cache_dir.exists():
        return None
    candidates = sorted(cache_dir.glob(f"{ticker.upper()}_*.txt"))
    return candidates[-1] if candidates else None


def fetch_10k_text(*, ticker: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> str:
    """Return the most recent 10-K's plain text; cache on first fetch."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _find_cached(ticker, cache_dir)
    if cached is not None:
        return cached.read_text()

    cik = _resolve_cik(ticker)
    if cik is None:
        raise RuntimeError(f"no CIK mapping for ticker {ticker}")

    submissions = _fetch_submissions_json(cik)
    rec = find_latest_10k(submissions)
    if rec is None:
        raise RuntimeError(f"no recent 10-K for {ticker} (CIK {cik})")

    html = _fetch_filing_html(cik, rec["accession"], rec["primary_doc"])
    text = extract_text(html)
    cache_path = cache_dir / f"{ticker.upper()}_{rec['filing_date']}.txt"
    cache_path.write_text(text)
    return text


def has_theme_keywords_in_10k(
    *,
    ticker: str,
    keywords: Iterable[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> bool:
    """Verification gate: any ``keyword`` substring-present in ``ticker``'s 10-K?"""
    try:
        text = fetch_10k_text(ticker=ticker, cache_dir=cache_dir)
    except Exception as exc:
        logger.warning("10-K fetch failed for %s: %s", ticker, exc)
        return False
    return len(grep_keywords(text, keywords)) > 0


__all__ = [
    "DEFAULT_CACHE_DIR",
    "extract_text",
    "fetch_10k_text",
    "find_latest_10k",
    "grep_keywords",
    "has_theme_keywords_in_10k",
]

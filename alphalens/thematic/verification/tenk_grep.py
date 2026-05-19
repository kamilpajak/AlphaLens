"""10-K business-description keyword evidence — Layer 3 verification gate.

For each candidate ticker, fetch its most recent 10-K filing once, strip the
HTML to plain text, cache locally, and grep for theme keywords. SEC's
``data.sec.gov/submissions/CIK{cik}.json`` is the authoritative filing index;
``find_latest_10k`` picks the freshest 10-K entry from the ``filings.recent``
arrays.

All SEC HTTP goes through the canonical :class:`SecEdgarClient` singleton —
no parallel transport, no separate User-Agent. The cache lives at
``~/.alphalens/thematic_tenk/{TICKER}_{filing_date}.txt`` and is one filing
per ticker (10-Ks are annual; refresh ~yearly).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from bs4 import BeautifulSoup

from alphalens.data.alt_data.sec_edgar_client import get_default_sec_client

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_tenk"
CIK_LOADER_CACHE_PATH = Path.home() / ".alphalens" / "thematic_cik_cache.json"
TICKER_CIK_YAML_PATH = Path.home() / ".alphalens" / "ticker_cik_map.yaml"

_WHITESPACE = re.compile(r"\s+")


@lru_cache(maxsize=1)
def _load_ticker_to_cik() -> dict[str, str]:
    """Pull SEC's company_tickers.json once and index by ticker.

    Returns ``{}`` on any fetch / parse failure so the fallback chain in
    :func:`_resolve_cik` (CIKLoader + YAML snapshot) can proceed. The whole
    point of a fallback chain is to survive primary-tier outages.
    """
    try:
        payload = get_default_sec_client().fetch_company_tickers()
    except Exception as exc:
        logger.warning("SEC company_tickers.json fetch failed: %s", exc)
        return {}
    mapping: dict[str, str] = {}
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        t = entry.get("ticker")
        cik = entry.get("cik_str")
        if t and cik is not None:
            mapping[str(t).upper()] = str(cik).zfill(10)
    return mapping


@lru_cache(maxsize=1)
def _get_cik_loader():
    """Lazily build the TTL'd CIKLoader reused from the watchdog stack."""
    from alphalens.watchdog.sources.cik_loader import CIKLoader

    loader = CIKLoader(cache_path=CIK_LOADER_CACHE_PATH)
    try:
        loader.load()
    except Exception as exc:
        logger.warning("CIKLoader load failed: %s", exc)
    return loader


@lru_cache(maxsize=1)
def _get_yaml_snapshot():
    """Optional 3rd-tier YAML snapshot of ticker→CIK; absent path returns None."""
    if not TICKER_CIK_YAML_PATH.exists():
        return None
    from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

    try:
        return TickerCikMap.load(TICKER_CIK_YAML_PATH)
    except Exception as exc:
        logger.warning("TickerCikMap snapshot load failed: %s", exc)
        return None


def _resolve_cik(ticker: str) -> str | None:
    """Three-tier ticker→CIK resolution: live SEC → cached CIKLoader → YAML snapshot.

    Returns ``None`` when all three tiers miss (foreign listings, recent IPOs
    without US presence). Callers MUST treat ``None`` as "couldn't determine"
    not "no match", so the orchestrator can record `gates_unknown` instead of
    silently failing closed.
    """
    upper = ticker.upper()
    primary = _load_ticker_to_cik().get(upper)
    if primary is not None:
        return primary
    loader = _get_cik_loader()
    via_loader = loader.get_cik(upper) if loader is not None else None
    if via_loader is not None:
        return via_loader
    snapshot = _get_yaml_snapshot()
    if snapshot is not None:
        return snapshot.lookup(upper)
    return None


def _fetch_submissions_json(cik: str) -> dict:
    return get_default_sec_client().fetch_submissions(cik)


def _fetch_filing_html(cik: str, accession: str, primary_doc: str) -> str:
    accession_clean = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_clean}/{primary_doc}"
    return get_default_sec_client().get_text(url, encoding="utf-8")


def extract_text(html: str) -> str:
    """Strip HTML to plain text. Removes ``<script>``/``<style>`` content
    entirely (regex tag-strip would leave their inner JS/CSS in the haystack)
    then collapses whitespace.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return _WHITESPACE.sub(" ", text).strip()


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


def _find_cached(ticker: str, cache_dir: Path, *, asof: dt.date | None = None) -> Path | None:
    """Locate the most-recent cached 10-K text file for ``ticker``.

    With ``asof=None`` (live flow): pick alphabetically last file —
    preserves legacy behaviour.

    With ``asof`` set (PIT flow): only consider files whose filename
    date suffix is ``≤ asof``, pick latest of those. ``None`` when no
    file qualifies — caller treats as gate unknown.
    """
    if not cache_dir.exists():
        return None
    candidates = sorted(cache_dir.glob(f"{ticker.upper()}_*.txt"))
    if not candidates:
        return None
    if asof is not None:
        prefix_len = len(ticker) + 1  # "NVDA_"
        eligible: list[Path] = []
        for path in candidates:
            date_str = path.stem[prefix_len:]
            try:
                file_date = dt.date.fromisoformat(date_str)
            except ValueError:
                continue
            if file_date <= asof:
                eligible.append(path)
        if not eligible:
            return None
        candidates = eligible
    return candidates[-1]


def fetch_10k_text(
    *,
    ticker: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    asof: dt.date | None = None,
) -> str | None:
    """Return the most recent 10-K's plain text; cache on first fetch.

    Returns ``None`` when CIK can't be resolved or no recent 10-K exists for
    the ticker (foreign listing, recent IPO, etc.). Network/parse errors
    still raise so callers can distinguish "no data" from "fetch broke".

    PIT: when ``asof < today`` and the cache has no file ≤ asof, return
    ``None`` instead of fetching — a live SEC fetch would only surface the
    newest 10-K, leaking future content into a historical replay.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _find_cached(ticker, cache_dir, asof=asof)
    if cached is not None:
        return cached.read_text()

    if asof is not None and asof < dt.date.today():
        # PIT replay miss — caller surfaces this as gates_unknown.
        return None

    cik = _resolve_cik(ticker)
    if cik is None:
        logger.info("no CIK mapping for ticker %s — 10-K gate unknown", ticker)
        return None

    submissions = _fetch_submissions_json(cik)
    rec = find_latest_10k(submissions)
    if rec is None:
        logger.info("no recent 10-K for %s (CIK %s) — 10-K gate unknown", ticker, cik)
        return None

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
    asof: dt.date | None = None,
) -> bool | None:
    """Verification gate: any ``keyword`` substring-present in ``ticker``'s 10-K?

    Tri-state: ``True`` (keyword hit), ``False`` (10-K fetched but no hit),
    ``None`` (CIK unresolvable or fetch failed — orchestrator records as
    ``gates_unknown``, NOT a false negative).
    """
    try:
        text = fetch_10k_text(ticker=ticker, cache_dir=cache_dir, asof=asof)
    except Exception as exc:
        logger.warning("10-K fetch failed for %s: %s", ticker, exc)
        return None
    if text is None:
        return None
    return len(grep_keywords(text, keywords)) > 0


__all__ = [
    "DEFAULT_CACHE_DIR",
    "extract_text",
    "fetch_10k_text",
    "find_latest_10k",
    "grep_keywords",
    "has_theme_keywords_in_10k",
]

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

from alphalens_pipeline.data.alt_data.sec_edgar_client import get_default_sec_client

logger = logging.getLogger(__name__)

_ALPHALENS_HOME = Path.home() / ".alphalens"
DEFAULT_CACHE_DIR = _ALPHALENS_HOME / "thematic_tenk"
CIK_LOADER_CACHE_PATH = _ALPHALENS_HOME / "thematic_cik_cache.json"
TICKER_CIK_YAML_PATH = _ALPHALENS_HOME / "ticker_cik_map.yaml"

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
    """Lazily build the TTL'd CIKLoader reused from the edgar_detector stack."""
    from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader

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
    from alphalens_pipeline.data.alt_data.ticker_cik_map import TickerCikMap

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


def _fetch_submissions_overflow(name: str) -> dict:
    """Fetch a paginated submissions overflow shard (``filings.files`` pointer).

    Module-level wrapper so the canonical client stays the only SEC transport
    and tests can patch this like the other ``_fetch_*`` helpers.
    """
    return get_default_sec_client().fetch_submissions_overflow(name)


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


def find_latest_10k(submissions_payload: dict, asof: dt.date | None = None) -> dict | None:
    """Pick the most recent 10-K from ``filings.recent`` arrays, ≤ asof.

    Returns ``{accession, filing_date, primary_doc}`` or ``None`` if no 10-K
    is in the recent filings window (SEC's submissions JSON typically holds
    the last ~1000 filings; for older 10-Ks the ``filings.files`` paginated
    pointers would be needed — not implemented since 10-Ks are annual).

    With ``asof`` set, filings dated after asof are skipped — the function
    returns the latest 10-K whose ``filingDate`` is ≤ ``asof``. This is the
    primary PIT correctness gate for the 10-K verification path: a
    ``find_latest_10k`` that respected ``asof`` natively eliminates the
    post-fetch check + arbitrary day-staleness guard the prior shape needed.
    """
    asof_str = asof.isoformat() if asof is not None else None
    recent = submissions_payload.get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []
    best: dict | None = None
    for form, acc, dt_, doc in zip(forms, accessions, dates, docs, strict=False):
        if form != "10-K":
            continue
        if asof_str is not None and dt_ > asof_str:
            continue
        if best is None or dt_ > best["filing_date"]:
            best = {"accession": acc, "filing_date": dt_, "primary_doc": doc}
    return best


def find_10ks(
    submissions_payload: dict,
    *,
    asof: dt.date | None = None,
    max_count: int | None = None,
) -> list[dict]:
    """Return ALL 10-K records in one ``filings.recent``-shaped block, ≤ asof.

    Generalizes :func:`find_latest_10k` (which returns only the single freshest
    record) to the full list, sorted newest-first. Pure — no I/O. Each record is
    ``{accession, filing_date, primary_doc}``. The main submissions index AND
    each ``filings.files`` overflow shard share the ``{recent: {...}}`` shape, so
    the multi-year fetcher reuses this on both. ``max_count`` caps the list.

    NOTE: this is intentionally a separate function, NOT a refactor of
    ``find_latest_10k`` — the latter is on the live daily-brief path and is kept
    byte-for-byte unchanged (#505 live-path safety).
    """
    asof_str = asof.isoformat() if asof is not None else None
    recent = submissions_payload.get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []
    out: list[dict] = []
    for form, acc, dt_, doc in zip(forms, accessions, dates, docs, strict=False):
        if form != "10-K":
            continue
        if asof_str is not None and dt_ > asof_str:
            continue
        out.append({"accession": acc, "filing_date": dt_, "primary_doc": doc})
    out.sort(key=lambda r: r["filing_date"], reverse=True)
    if max_count is not None:
        out = out[:max_count]
    return out


_CACHE_TTL_DAYS = 380  # 10-Ks are annual; refresh once the latest cached file is more than ~one filing cycle stale, so a SEC index check supersedes a long-since-obsolete cache entry.


def _find_cached(ticker: str, cache_dir: Path, *, asof: dt.date | None = None) -> Path | None:
    """Locate the most-recent cached 10-K text file for ``ticker``.

    With ``asof=None`` (live flow): pick alphabetically last file —
    preserves legacy behaviour.

    With ``asof`` set (PIT flow): only consider files whose filename
    date suffix is ``≤ asof``, pick latest of those. ``None`` when no
    file qualifies — caller treats as gate unknown.

    Also returns ``None`` when the latest eligible file is older than
    ``_CACHE_TTL_DAYS`` relative to ``asof`` (or today, when ``asof`` is
    None): the caller is then forced to re-consult SEC submissions for a
    fresher 10-K, preventing the cache from masking a newer filing
    indefinitely.
    """
    if not cache_dir.exists():
        return None
    candidates = sorted(cache_dir.glob(f"{ticker.upper()}_*.txt"))
    if not candidates:
        return None
    # Cache filename shape: ``{TICKER}_{YYYY-MM-DD}.txt``. Use rsplit so
    # tickers that themselves contain an underscore (e.g. BRK_B) don't shift
    # the date slice and silently mis-classify the file.
    dated: list[tuple[dt.date, Path]] = []
    for path in candidates:
        date_str = path.stem.rsplit("_", 1)[-1]
        try:
            file_date = dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        if asof is not None and file_date > asof:
            continue
        dated.append((file_date, path))
    if not dated:
        return None
    dated.sort()
    file_date, path = dated[-1]
    horizon = asof if asof is not None else dt.date.today()
    if (horizon - file_date).days > _CACHE_TTL_DAYS:
        return None
    return path


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

    PIT correctness lives in :func:`find_latest_10k` — it filters the SEC
    submissions index to filings ``≤ asof`` so a 10-K filed today doesn't
    bleed into yesterday's verdict AND a stale prior-year filing is still
    picked up when the latest filing post-dates ``asof``. The previously
    needed ``asof < today - 1 day`` guard and post-fetch correction helper
    are gone now that the asof filter happens at the index source.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _find_cached(ticker, cache_dir, asof=asof)
    if cached is not None:
        return cached.read_text()

    cik = _resolve_cik(ticker)
    if cik is None:
        logger.info("no CIK mapping for ticker %s — 10-K gate unknown", ticker)
        return None

    submissions = _fetch_submissions_json(cik)
    rec = find_latest_10k(submissions, asof=asof)
    if rec is None:
        logger.info("no recent 10-K for %s (CIK %s) — 10-K gate unknown", ticker, cik)
        return None

    # Anti-hammering: if SEC's latest-≤asof matches a 10-K we already wrote
    # to disk for this ticker, skip the HTML fetch + extract + write cycle.
    # This guards against re-fetching the same filing every call once the
    # TTL re-arms the cache check above.
    cache_path = cache_dir / f"{ticker.upper()}_{rec['filing_date']}.txt"
    if cache_path.exists():
        return cache_path.read_text()

    html = _fetch_filing_html(cik, rec["accession"], rec["primary_doc"])
    text = extract_text(html)
    cache_path.write_text(text)
    return text


# Multi-year / peer reach (#505) — additive, reachable ONLY from the opt-in
# Buffett `--qualitative` CLI, never from the daily thematic pipeline.
_DEFAULT_MULTI_YEAR_COUNT = 3
_DEFAULT_MAX_PEERS = 3


def _fetch_and_cache_10k(ticker: str, cik: str, rec: dict, cache_dir: Path) -> str:
    """Fetch + cache ONE 10-K's plain text for an explicit submissions record.

    Mirrors :func:`fetch_10k_text`'s cache contract (``{TICKER}_{date}.txt``)
    but for an arbitrary record rather than the latest-≤asof one — so an
    overflow-sourced older filing (invisible to ``find_latest_10k``) is fetched
    directly. Reuses the same cache namespace as ``fetch_10k_text`` so a year
    already warmed by the daily path is not re-fetched.
    """
    cache_path = cache_dir / f"{ticker.upper()}_{rec['filing_date']}.txt"
    if cache_path.exists():
        return cache_path.read_text()
    html = _fetch_filing_html(cik, rec["accession"], rec["primary_doc"])
    text = extract_text(html)
    cache_path.write_text(text)
    return text


def fetch_multi_year_10k_texts(
    *,
    ticker: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    asof: dt.date | None = None,
    years: int = _DEFAULT_MULTI_YEAR_COUNT,
) -> list[tuple[str, str]]:
    """Return up to ``years`` of 10-K texts (newest-first), ≤ asof.

    ``[(filing_date, text), ...]``. Collects 10-K records from the recent
    submissions block and, ONLY when that is short of ``years``, walks the
    ``filings.files`` overflow shards. Each chosen filing's HTML is fetched
    directly via :func:`_fetch_and_cache_10k` — NOT delegated to
    ``fetch_10k_text``, which re-derives the filing via ``find_latest_10k``
    (recent-only) and would never see an overflow-sourced older year.

    For most issuers the recent block already holds ≥ ``years`` annual 10-Ks, so
    no overflow shard is read. Returns ``[]`` when the CIK is unresolvable.
    Reachable ONLY from the opt-in Buffett CLI — never the daily pipeline.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cik = _resolve_cik(ticker)
    if cik is None:
        logger.info("no CIK mapping for %s — multi-year 10-K unavailable", ticker)
        return []

    submissions = _fetch_submissions_json(cik)
    collected = find_10ks(submissions, asof=asof)
    if len(collected) < years:
        shards = submissions.get("filings", {}).get("files") or []
        for shard in shards:
            name = shard.get("name") if isinstance(shard, dict) else None
            if not name:
                continue
            try:
                shard_payload = _fetch_submissions_overflow(name)
            except Exception as exc:  # fail-soft: one unreadable shard is not fatal
                # ``continue`` not ``break``: a transient 403/500 on one shard
                # must not stop the walk — a later shard may still hold the
                # remaining years.
                logger.warning("submissions overflow %s fetch failed: %s", name, exc)
                continue
            collected.extend(find_10ks(shard_payload, asof=asof))
            if len(collected) >= years:
                break

    # Newest-first, de-duplicated on accession, truncated to ``years``.
    collected.sort(key=lambda r: r["filing_date"], reverse=True)
    seen: set[str] = set()
    deduped: list[dict] = []
    for rec in collected:
        if rec["accession"] in seen:
            continue
        seen.add(rec["accession"])
        deduped.append(rec)
    out: list[tuple[str, str]] = []
    for rec in deduped[:years]:
        out.append((rec["filing_date"], _fetch_and_cache_10k(ticker, cik, rec, cache_dir)))
    return out


def fetch_peer_10k_texts(
    *,
    ticker: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    asof: dt.date | None = None,
    max_peers: int = _DEFAULT_MAX_PEERS,
    min_cohort: int | None = None,
    peer_filter=None,
) -> list[tuple[str, str]]:
    """Return up to ``max_peers`` SIC/FF-peer latest-10K texts (fail-soft).

    ``[(peer_ticker, text), ...]``. Resolves the peer cohort via
    :func:`sic_index.iter_peers_fallback`, drops the subject ticker, and takes
    the first ``max_peers`` — a HARD cap on SEC request volume. Each peer's
    LATEST 10-K is fetched via :func:`fetch_10k_text` (correct here: we want each
    peer's most recent filing, not a multi-year history). A peer that fails to
    resolve / fetch is skipped, never raised.

    ``peer_filter`` is an optional ``Callable[[list[str]], list[str]]`` passed
    straight to ``iter_peers_fallback`` (e.g. a ``functools.partial`` binding the
    mcap/price floor). Default ``None`` keeps the peer loop free of any
    feature-fetcher dependency.

    Caveat: peer MEMBERSHIP is current-snapshot (today's SIC cohort), NOT
    point-in-time. The individual peer FILINGS are still ``asof``-filtered by
    ``fetch_10k_text``. Peer-selection quality (ordering, comparability) is left
    to the consumer; this is plumbing to make peer 10-K text retrievable.
    Reachable ONLY from the opt-in Buffett CLI — never the daily pipeline.
    """
    from alphalens_pipeline.data.fundamentals import sic_index

    if min_cohort is None:
        min_cohort = sic_index.DEFAULT_MIN_COHORT
    sic = sic_index.get_sic(ticker)
    peers, _ = sic_index.iter_peers_fallback(sic, min_cohort=min_cohort, peer_filter=peer_filter)
    subject = ticker.upper()
    candidates = [p for p in peers if p.upper() != subject][:max_peers]
    out: list[tuple[str, str]] = []
    for peer in candidates:
        try:
            text = fetch_10k_text(ticker=peer, cache_dir=cache_dir, asof=asof)
        except Exception as exc:  # fail-soft per peer
            logger.warning("peer 10-K fetch failed for %s: %s", peer, exc)
            continue
        if text is None:
            continue
        out.append((peer, text))
    return out


def has_theme_keywords_in_10k(
    *,
    ticker: str,
    keywords: Iterable[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    asof: dt.date | None = None,
    reason: dict | None = None,
) -> bool | None:
    """Verification gate: any ``keyword`` substring-present in ``ticker``'s 10-K?

    Tri-state: ``True`` (keyword hit), ``False`` (10-K fetched but no hit),
    ``None`` (CIK unresolvable or fetch failed — orchestrator records as
    ``gates_unknown``, NOT a false negative).

    ``reason`` (PR-4, OPTIONAL out-param): when supplied, filled with
    ``{threshold, actual, unit}`` -- threshold 1 keyword hit, actual = the count
    of distinct theme keywords matched in the filing. Purely observational; the
    return value is unchanged whether or not it is passed.
    """
    if reason is not None:
        reason.update({"threshold": 1, "actual": None, "unit": "keyword_hits"})
    try:
        text = fetch_10k_text(ticker=ticker, cache_dir=cache_dir, asof=asof)
    except Exception as exc:
        logger.warning("10-K fetch failed for %s: %s", ticker, exc)
        return None
    if text is None:
        return None
    hits = grep_keywords(text, keywords)
    if reason is not None:
        reason["actual"] = len(hits)
    return len(hits) > 0


__all__ = [
    "DEFAULT_CACHE_DIR",
    "extract_text",
    "fetch_10k_text",
    "fetch_multi_year_10k_texts",
    "fetch_peer_10k_texts",
    "find_10ks",
    "find_latest_10k",
    "grep_keywords",
    "has_theme_keywords_in_10k",
]

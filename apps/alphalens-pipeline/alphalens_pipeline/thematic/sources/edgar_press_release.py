"""EDGAR issuer press-release ingest adapter (8-K Exhibit 99.1).

Discovers every 8-K filed market-wide on a UTC date via the SEC daily form
index (``form.{YYYYMMDD}.idx``), filters to the in-universe CIK set, keeps only
filings whose 8-K items intersect the press-release item set, fetches the
Exhibit 99.1 narrative as the article body, and tags tickers from the filer
CIK (not title NER). Output cache lives at
``~/.alphalens/thematic_news/edgar_press_release/{YYYY-MM-DD}.parquet``.

Why the daily index instead of per-ticker submissions polling:
- COVERAGE: one ``.idx`` lists every 8-K filed that day, so we never miss an
  in-universe filer absent from a stale local roster.
- HTTP VOLUME: 1 index fetch + ~2 fetches per in-universe 8-K hit (tens/day)
  vs ~3500 submissions fetches per run. ~30-50x less HTTP under the per-IP
  10 req/s budget the VPS shares across edgar-detect + thematic.
- CACHE: a past-date ``.idx`` is immutable, so the per-day parquet cache
  absorbs every re-run.

All SEC HTTP goes through the canonical :class:`SecEdgarClient` (User-Agent +
10 req/s throttle + 429/5xx retry). No raw ``requests``/``urllib`` here.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pandas as pd
from defusedxml.ElementTree import fromstring as _defused_fromstring

from alphalens_pipeline.data.alt_data.sec_edgar_client import (
    SecEdgarClient,
    get_default_sec_client,
)
from alphalens_pipeline.edgar_detector.sources.eightk import extract_8k_items
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news" / "edgar_press_release"
SOURCE = "edgar_press_release"
ARCHIVES_BASE = "https://www.sec.gov"

# 8-K items that carry a real issuer press release worth thematic extraction.
PRESS_RELEASE_ITEMS = frozenset({"1.01", "2.01", "2.02", "7.01", "8.01"})
# FilingSummary ``<File doctype=...>`` values that hold an Exhibit 99.1 narrative.
_EX_991_DOCTYPES = frozenset({"EX-99.1", "EX-99"})
# Form-type column values we keep from the daily index (mirror of edgar.py's
# primary-doc set); the press-release ITEM gate further narrows these later.
_KEPT_FORM_TYPES = frozenset({"8-K", "8-K/A"})


# --- universe / CIK->ticker inverse map (mirror of CIKLoader, inverted) -----
@lru_cache(maxsize=1)
def _load_cik_to_ticker() -> dict[str, str]:
    """Build the CIK(10-zfill)->ticker map from the on-disk company_tickers.json.

    Reuses :class:`CIKLoader`'s cache file (ticker->cik) but inverts it.
    Presence of a CIK in this map means "in-universe" — the full SEC roster
    covers S&P 1500 + R2000 by construction.
    """
    from alphalens_pipeline.edgar_detector.sources.cik_loader import default_cik_cache_path

    path = default_cik_cache_path()
    payload = json.loads(path.read_text())
    out: dict[str, str] = {}
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker")
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            out[str(cik).zfill(10)] = str(ticker).upper()
    return out


# --- (1) discovery: daily form index -> list of 8-K rows --------------------
def fetch_form_index(*, date: dt.date, client: SecEdgarClient) -> str:
    """Fetch the SEC daily form index text for ``date``."""
    quarter = (date.month - 1) // 3 + 1
    url = (
        f"{ARCHIVES_BASE}/Archives/edgar/daily-index/"
        f"{date.year}/QTR{quarter}/form.{date:%Y%m%d}.idx"
    )
    return client.get_text(url)


def parse_form_index_8k(idx_text: str) -> list[dict]:
    """Parse the daily form index, returning the 8-K / 8-K/A rows.

    The ``.idx`` is fixed-width text: header lines, then a dashed-separator
    line, then one row per filing. We start parsing after the separator and
    keep rows whose Form Type column is in :data:`_KEPT_FORM_TYPES`. Each row
    becomes ``{form_type, cik_padded, accession, filing_date, base_dir}``.
    """
    rows: list[dict] = []
    lines = idx_text.splitlines()
    started = False
    for line in lines:
        if not started:
            if set(line.strip()) == {"-"} and line.strip():
                started = True
            continue
        if not line.strip():
            continue
        parsed = _parse_index_row(line)
        if parsed is not None:
            rows.append(parsed)
    return rows


def _parse_index_row(line: str) -> dict | None:
    """Parse one daily-index row; return None if it is not a kept 8-K row.

    Columns are whitespace-separated with the Company Name possibly containing
    spaces. We anchor on the Form Type (column 0), the File Name (last token),
    and the CIK / Date Filed which sit just before the File Name.
    """
    parts = line.split()
    if len(parts) < 4:
        return None
    form_type = parts[0]
    if form_type not in _KEPT_FORM_TYPES:
        return None
    file_name = parts[-1]
    date_filed = parts[-2]
    cik_raw = parts[-3]
    if not cik_raw.isdigit():
        return None
    cik_padded = cik_raw.zfill(10)
    accession = _accession_from_index_filename(file_name)
    if not accession:
        return None
    return {
        "form_type": form_type,
        "cik_padded": cik_padded,
        "accession": accession,
        "filing_date": date_filed,
        "base_dir": _base_dir_from_index_filename(file_name, cik_padded),
    }


def _accession_from_index_filename(file_name: str) -> str | None:
    """Extract the dashed accession number from an index File Name path.

    e.g. ``edgar/data/320193/0000320193-26-000050-index.htm``
    -> ``0000320193-26-000050``.
    """
    leaf = file_name.rsplit("/", 1)[-1]
    leaf = leaf.removesuffix("-index.htm").removesuffix(".txt")
    # Accession is the first three dash-separated groups (CIK-YY-SEQ).
    bits = leaf.split("-")
    if len(bits) < 3:
        return None
    return "-".join(bits[:3])


def _base_dir_from_index_filename(file_name: str, cik_padded: str) -> str:
    """Build the filing archive base dir from the index File Name.

    SEC archive layout: ``/Archives/edgar/data/{cik_no_zeros}/{acc_no_dashes}``.
    """
    accession = _accession_from_index_filename(file_name) or ""
    cik_no_zeros = str(int(cik_padded))
    acc_no_dashes = accession.replace("-", "")
    return f"{ARCHIVES_BASE}/Archives/edgar/data/{cik_no_zeros}/{acc_no_dashes}"


# --- (2) FilingSummary doctype pickers (defused XML, mirror of edgar.py) -----
def _pick_8k_primary_name(filing_summary_xml: str) -> str | None:
    """Pick the primary 8-K document filename from FilingSummary.xml."""
    try:
        root = _defused_fromstring(filing_summary_xml)
    except ParseError:
        return None
    for file_el in root.iter("File"):
        doctype = (file_el.get("doctype") or "").upper()
        if doctype in _KEPT_FORM_TYPES:
            original = file_el.get("original")
            if original:
                return original
            text = (file_el.text or "").strip()
            return text or None
    return None


def pick_ex_991_name(filing_summary_xml: str) -> str | None:
    """Pick the Exhibit 99.1 (or EX-99) document filename from FilingSummary.xml."""
    try:
        root = _defused_fromstring(filing_summary_xml)
    except ParseError:
        return None
    for file_el in root.iter("File"):
        doctype = (file_el.get("doctype") or "").upper()
        if doctype in _EX_991_DOCTYPES:
            original = file_el.get("original")
            if original:
                return original
            text = (file_el.text or "").strip()
            return text or None
    return None


# --- (3) per-hit enrichment: base_dir -> items + EX-99.1 body ----------------
def _safe_text(client: SecEdgarClient, url: str) -> str | None:
    """Best-effort text fetch; returns None on any client error (mirror of edgar.py)."""
    try:
        return client.get_text(url)
    except Exception as exc:
        # One bad document must not kill the day (mirror of edgar.py::_get).
        logger.warning("edgar press-release fetch failed (%s): %s", url, exc, exc_info=True)
        return None


def _strip_subsection(items: list[str]) -> list[str]:
    """Drop ``(a)``-style suffixes so item codes compare against the bare set."""
    return [item.split("(", 1)[0] for item in items]


def _enrich_filing(row: dict, *, client: SecEdgarClient) -> dict | None:
    """Resolve one daily-index hit to its items + EX-99.1 body, or None to skip."""
    base_dir = row["base_dir"]
    fs = client.get_text(f"{base_dir}/FilingSummary.xml")
    if not fs:
        return None
    primary = _pick_8k_primary_name(fs)
    items = extract_8k_items(_safe_text(client, f"{base_dir}/{primary}") or "") if primary else []
    if not (set(_strip_subsection(items)) & PRESS_RELEASE_ITEMS):
        return None
    ex_name = pick_ex_991_name(fs)
    if not ex_name:
        # No press-release exhibit -> not our signal.
        return None
    body = _safe_text(client, f"{base_dir}/{ex_name}") or ""
    return {
        "cik_padded": row["cik_padded"],
        "accession": row["accession"],
        "filing_date": row["filing_date"],
        "base_dir": base_dir,
        "items": items,
        "body": body,
    }


# --- (4) pure transform: enriched hits -> NEWS_COLUMNS frame -----------------
def _title_from_body(body: str) -> str:
    """First non-empty stripped text line from the EX-99.1 narrative."""
    stripped = body.replace("<p>", "\n").replace("</p>", "\n")
    text = re.sub(r"<[^>]+>", " ", stripped)
    for line in text.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return ""


def transform(
    hits: list[dict],
    *,
    cik_to_ticker: dict[str, str],
    date: dt.date,
) -> pd.DataFrame:
    """Normalise enriched 8-K press-release hits to the ``NEWS_COLUMNS`` schema."""
    del date  # filing_date on each hit is authoritative; kept for signature parity
    rows: list[dict] = []
    for hit in hits:
        ticker = cik_to_ticker.get(hit["cik_padded"])
        if not ticker:
            continue  # universe filter
        extra = {
            "accession": hit["accession"],
            "items": hit["items"],
            "cik": hit["cik_padded"],
            "exhibit": "99.1",
        }
        title = _title_from_body(hit["body"]) or f"{ticker} 8-K Item {','.join(hit['items'])}"
        rows.append(
            {
                "id": hit["accession"],  # SEC-stable, dashed form
                "source": SOURCE,
                "timestamp": pd.Timestamp(hit["filing_date"], tz="UTC"),
                "tickers": [ticker],  # from filer CIK, NOT title NER
                "title": title,
                "body": hit["body"] or "",
                "url": f"{hit['base_dir']}/{hit['accession']}-index.htm",
                "keywords": [],
                "extra": json.dumps(extra, ensure_ascii=False),
            }
        )

    if not rows:
        return empty_news_frame()

    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


# --- (5) cached orchestrator (signature matches rss/polygon convention) ------
def fetch_daily_news(
    *,
    date: dt.date,
    client: SecEdgarClient | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    force: bool = False,
) -> pd.DataFrame:
    """Fetch and cache one day's EDGAR press releases, returning the normalised frame.

    Read-through cache: returns the parquet immediately if it exists and
    ``force=False``. A daily-index fetch failure yields an empty frame (no
    raise) so the unified ingest's ``_safe_call`` degrades gracefully; a single
    bad per-filing fetch skips only that filing.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)

    sec = client or get_default_sec_client()
    cik_to_ticker = _load_cik_to_ticker()

    try:
        idx = fetch_form_index(date=date, client=sec)
    except Exception as exc:
        # Index failure must not raise — the unified ingest's _safe_call would
        # swallow it anyway, but caching an empty frame here avoids re-hitting
        # SEC on every retry within the same UTC day.
        logger.warning("edgar daily-index fetch failed for %s: %s", date, exc, exc_info=True)
        empty = empty_news_frame()
        empty.to_parquet(cache_path, index=False)
        return empty

    rows = parse_form_index_8k(idx)
    # Pre-filter to in-universe CIKs BEFORE any per-filing HTTP (cuts ~3500 -> tens).
    rows = [r for r in rows if r["cik_padded"] in cik_to_ticker]

    hits: list[dict] = []
    for row in rows:
        try:
            hit = _enrich_filing(row, client=sec)
        except Exception as exc:
            # One bad filing must not kill the day.
            logger.warning(
                "edgar 8-K enrich failed %s: %s", row.get("accession"), exc, exc_info=True
            )
            continue
        if hit:
            hits.append(hit)

    df = transform(hits, cik_to_ticker=cik_to_ticker, date=date)
    df.to_parquet(cache_path, index=False)
    return df

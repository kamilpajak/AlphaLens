"""EDGAR issuer press-release ingest adapter (8-K Exhibit 99.1).

Discovers every 8-K filed market-wide on a UTC date via the SEC daily form
index (``form.{YYYYMMDD}.idx``), filters to the in-universe CIK set, keeps only
filings whose 8-K items intersect the press-release item set, fetches the
Exhibit 99.1 narrative as the article body, and tags tickers from the filer
CIK (not title NER). Output cache lives at
``~/.alphalens/thematic_news/edgar_press_release/{YYYY-MM-DD}.parquet``.

Exhibit discovery reads the Document Format Files table of the filing's
``{accession}-index.htm`` page — the authoritative listing of every document's
exhibit Type. We deliberately do NOT use FilingSummary.xml: it lists only the
XBRL render files plus the primary 8-K and NEVER carries an ``EX-99.1`` doctype,
so an XML scan for one always returns nothing (issue #337). One index.htm fetch
yields both the primary 8-K (for item extraction) and the EX-99.1 (for the
body), replacing the previous two FilingSummary-derived fetches.

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

EX-99.1 rows are timestamped at the SEC acceptance instant — the moment the
filing became publicly visible on EDGAR — parsed from the ``Accepted`` field of
the same ``{accession}-index.htm`` already fetched for exhibit discovery and
converted ET->UTC (issue #391). No extra HTTP. When that field is absent or
unparseable we fall back to the daily-index ``Date Filed`` column, which is
date-only (00:00 UTC) and, for after-5:30pm-ET filings, rolls to the next
calendar day. Cross-source URL dedup still tie-breaks on ``_SOURCE_PRIORITY``
(this source is rank 0), not timestamp, so the acceptance instant changes only
the recency ORDERING (which previously sank every EX-99.1 row to 00:00 and out
of the news_ingest recency cap), not which row survives URL dedup.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from alphalens_pipeline.data.alt_data.sec_edgar_client import (
    SecEdgarClient,
    SecForbiddenError,
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
# index.htm Document Format Files ``Type`` values that hold an Exhibit 99.1
# narrative, in preference order (``EX-99.1`` wins over the bare ``EX-99``).
# EX-99.2 (a second press-release/presentation exhibit on multi-item 8-Ks) is
# deliberately excluded in v1: EX-99.1 is the overwhelming norm for the primary
# press release, and EX-99.2 is frequently a slide deck rather than narrative.
_EX_991_TYPES = ("EX-99.1", "EX-99")
# index.htm Document Format Files ``Type`` values for the primary 8-K document.
_PRIMARY_8K_TYPES = frozenset({"8-K", "8-K/A"})
# Form-type column values we keep from the daily index (mirror of edgar.py's
# primary-doc set); the press-release ITEM gate further narrows these later.
_KEPT_FORM_TYPES = frozenset({"8-K", "8-K/A"})
# iXBRL viewer prefix wrapping a document href in the index.htm table; the real
# document path follows the ``doc=`` query parameter.
_IXBRL_VIEWER_PREFIX = "/ix?doc="

# --- acceptance-datetime parse (issue #391) ---------------------------------
# The {accession}-index.htm header (already fetched for exhibit discovery)
# carries the SEC acceptance instant:
#   <div class="infoHead">Accepted</div>
#   <div class="info">2026-04-30 16:30:41</div>
# This is the moment the filing became publicly visible on EDGAR (when the
# market could first act on it), distinct from the daily-index "Date Filed"
# column, which is DATE-ONLY and rolls to the NEXT calendar day for
# after-5:30pm-ET filings. We anchor on the ``class="infoHead">Accepted</div>``
# label (scoping to the header block so a stray "Accepted</div>" elsewhere on
# the page can't false-match) and tolerate extra attributes / reordering on the
# following ``class="info"`` value cell (so an SEC markup tweak like
# ``<div class="info" id="...">`` doesn't silently break the parse). Captures
# the second-precision value (NO tz suffix in the markup).
_ACCEPTED_RE = re.compile(
    r'class="infoHead">\s*Accepted\s*</div>\s*'
    r'<div[^>]*class="info"[^>]*>\s*'
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)
_ACCEPTED_FORMAT = "%Y-%m-%d %H:%M:%S"
# EDGAR stamps acceptance in US Eastern (EST/EDT). A real tz database is
# REQUIRED (not a fixed -4/-5 offset) so the DST boundary converts correctly.
# Ambiguous (fall-back) / non-existent (spring-forward) wall-clock instants
# resolve with zoneinfo's default fold=0; both windows (01:00-03:00 ET) are
# outside EDGAR's filing hours, so this is a documented no-op, not a guarantee.
_EDGAR_TZ = ZoneInfo("America/New_York")


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
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        # A missing or truncated company_tickers.json (CIKLoader write race,
        # disk issue) must not raise through the whole thematic ingest. Treat
        # it as an empty universe — nothing matches this run. Each ingest run
        # is a fresh process (fresh lru_cache), so the next run recovers once
        # the file is whole.
        logger.warning("edgar CIK->ticker map load failed (%s): %s", path, exc)
        return {}
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


# --- (2) index.htm Document Format Files parser (Type -> doc basename) -------
# Why html.parser, not defusedxml: the filing index page is real-world HTML
# (unclosed <td>s, &nbsp; entities, <span> noise inside the document cell), not
# strict XML, so an XML parser would raise on the first malformed tag. The
# stdlib HTMLParser is tolerant and needs no third-party dependency.
_DOCUMENT_TABLE_SUMMARY = "Document Format Files"
# Column indices in the Document Format Files table: Seq, Description,
# Document(<a href>), Type, Size.
_DOC_CELL_INDEX = 2
_TYPE_CELL_INDEX = 3


class _DocumentTableParser(HTMLParser):
    """Walk the ``Document Format Files`` table → ``{Type -> doc basename}``.

    Each ``<tr>`` row carries five cells: Seq, Description, Document (with the
    document ``<a href>``), Type, Size. We capture the href from the Document
    cell only (index 2) — not the first href anywhere in the row, so a footnote
    link in the Description cell cannot hijack it — and on the closing ``</tr>``
    map the Type cell (index 3) to that href basename (with any ``/ix?doc=``
    iXBRL viewer prefix and directory path stripped). First occurrence of a Type
    wins.

    Parsing is scoped to the ``summary="Document Format Files"`` table so the
    sibling ``Data Files`` table (XBRL render files, identical column layout)
    and any third-party tables cannot contribute a colliding Type regardless of
    their DOM order.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.docs: dict[str, str] = {}
        self._in_target_table = False
        self._in_row = False
        self._cells: list[str] = []
        self._cell_text: list[str] = []
        self._row_href: str | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if dict(attrs).get("summary") == _DOCUMENT_TABLE_SUMMARY:
                self._in_target_table = True
            return
        if not self._in_target_table:
            return
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._cell_text = []
            self._row_href = None
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_text = []
        elif (
            tag == "a"
            and self._in_row
            and self._row_href is None
            and len(self._cells) == _DOC_CELL_INDEX  # only the Document cell
        ):
            for name, value in attrs:
                if name == "href" and value:
                    self._row_href = value
                    break

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_target_table:
            self._in_target_table = False
            self._in_row = False
            return
        if not self._in_target_table:
            return
        if tag in ("td", "th") and self._in_row:
            self._cells.append("".join(self._cell_text).strip())
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            self._finish_row()
            self._in_row = False

    def _finish_row(self) -> None:
        if self._row_href is None or len(self._cells) <= _TYPE_CELL_INDEX:
            return
        doc_type = self._cells[_TYPE_CELL_INDEX].upper()
        basename = _basename_from_href(self._row_href)
        if doc_type and basename and doc_type not in self.docs:
            self.docs[doc_type] = basename


def _basename_from_href(href: str) -> str:
    """Resolve a document ``<a href>`` to its bare filename.

    Strips a leading ``/ix?doc=`` iXBRL viewer prefix (which wraps the primary
    8-K) and any directory path, e.g.
    ``/ix?doc=/Archives/edgar/data/1/2/ef_8k.htm`` -> ``ef_8k.htm``.
    """
    if href.startswith(_IXBRL_VIEWER_PREFIX):
        href = href[len(_IXBRL_VIEWER_PREFIX) :]
    return href.rsplit("/", 1)[-1]


def parse_index_documents(index_html: str) -> dict[str, str]:
    """Parse a filing's ``{accession}-index.htm`` → ``{doc Type -> basename}``.

    Returns an empty map on any parse error (mirrors the source-wide
    degrade-gracefully contract).
    """
    parser = _DocumentTableParser()
    try:
        parser.feed(index_html)
    except Exception as exc:  # malformed markup must not raise through ingest
        logger.warning("edgar index.htm parse failed: %s", exc, exc_info=True)
        return {}
    return parser.docs


def _pick_from_docs(docs: dict[str, str], types) -> str | None:
    """Return the basename for the first matching Type (in preference order)."""
    for doc_type in types:
        name = docs.get(doc_type)
        if name:
            return name
    return None


def pick_ex_991_name(index_html: str) -> str | None:
    """Pick the Exhibit 99.1 (or bare EX-99) document basename from the index table.

    Takes the filing's ``{accession}-index.htm`` HTML and returns the basename
    whose document Type is ``EX-99.1``, falling back to the bare ``EX-99``.
    """
    return _pick_from_docs(parse_index_documents(index_html), _EX_991_TYPES)


# --- (3) per-hit enrichment: base_dir -> items + EX-99.1 body (1 index.htm) --
def _safe_text(client: SecEdgarClient, url: str) -> str | None:
    """Best-effort text fetch; returns None on a CLEAN miss (404, parse, empty).

    A transient SEC failure (403 traffic-threshold / UA-reject) is RE-RAISED so
    the caller can distinguish "document genuinely absent" from "we were rate-
    limited". The latter must NOT be silently swallowed into an empty body, or a
    403 storm caches empty-body rows and poisons the day (#379 / #382 / #383).
    """
    try:
        return client.get_text(url)
    except SecForbiddenError:
        raise  # transient under shared-IP load — propagate to the enrich guard
    except Exception as exc:
        # One bad document must not kill the day (mirror of edgar.py::_get).
        logger.warning("edgar press-release fetch failed (%s): %s", url, exc, exc_info=True)
        return None


def _strip_subsection(items: list[str]) -> list[str]:
    """Drop ``(a)``-style suffixes so item codes compare against the bare set."""
    return [item.split("(", 1)[0] for item in items]


def parse_accepted_utc(index_html: str) -> pd.Timestamp | None:
    """Parse the ``Accepted`` ET datetime from a filing's index.htm -> UTC Timestamp.

    The ``{accession}-index.htm`` header carries ``Accepted`` as
    ``YYYY-MM-DD HH:MM:SS`` in America/New_York (the instant the filing became
    publicly visible on EDGAR). We localise it ET->UTC, DST-correct via the tz
    database (EDT -4h in summer, EST -5h in winter), and return a tz-aware UTC
    ``pd.Timestamp``.

    Returns ``None`` when the field is absent or unparseable so the caller falls
    back to the daily-index date-only filing date (00:00 UTC). This NEVER raises
    and NEVER touches HTTP: by the time it runs the index.htm was already fetched
    successfully, so a parse miss can never be confused with the SEC-403
    re-raise path (#379 / #382 / #383) that ``_enrich_filing`` must surface. The
    anchor-miss path logs at DEBUG so fallback frequency stays greppable if the
    SEC ``formGrouping`` markup ever drifts (issue #391 RISK 1).
    """
    if not index_html:
        return None
    match = _ACCEPTED_RE.search(index_html)
    if not match:
        logger.debug("edgar acceptance-datetime anchor not found; falling back to filing_date")
        return None
    try:
        naive = dt.datetime.strptime(match.group(1), _ACCEPTED_FORMAT)
        return pd.Timestamp(naive.replace(tzinfo=_EDGAR_TZ)).tz_convert("UTC")
    except (ValueError, OverflowError) as exc:
        # Anchor matched but the value is not a usable datetime (month 13, an
        # out-of-bounds year — pandas' OutOfBoundsDatetime is a ValueError, so
        # it is covered here). Honour the "never raises" contract: degrade to the
        # date-only fallback rather than crash the day.
        logger.warning("edgar acceptance-datetime parse failed (%r): %s", match.group(1), exc)
        return None


def _enrich_filing(row: dict, *, client: SecEdgarClient) -> dict | None:
    """Resolve one daily-index hit to its items + EX-99.1 body, or None to skip.

    One ``{accession}-index.htm`` fetch yields the document-Type table that
    locates both the primary 8-K (for item extraction) and the EX-99.1 (for the
    body), so no separate FilingSummary.xml round-trip is needed.

    Raises ``SecForbiddenError`` if any of the per-filing fetches (index.htm /
    primary / EX-99.1) 403s under shared-IP load — the caller MUST classify this
    as a transient error so an all-403 day does not cache an empty/empty-body
    frame that poisons later runs (#382/#383). Do NOT wrap the body fetch in a
    blanket ``except Exception`` that would re-swallow it.
    """
    base_dir = row["base_dir"]
    index_html = client.get_text(f"{base_dir}/{row['accession']}-index.htm")
    if not index_html:
        return None
    docs = parse_index_documents(index_html)  # parse once, look up both Types
    primary = _pick_from_docs(docs, _PRIMARY_8K_TYPES)
    items = extract_8k_items(_safe_text(client, f"{base_dir}/{primary}") or "") if primary else []
    if not (set(_strip_subsection(items)) & PRESS_RELEASE_ITEMS):
        return None
    ex_name = _pick_from_docs(docs, _EX_991_TYPES)
    if not ex_name:
        # No press-release exhibit -> not our signal.
        return None
    body = _safe_text(client, f"{base_dir}/{ex_name}") or ""
    return {
        "cik_padded": row["cik_padded"],
        "accession": row["accession"],
        "filing_date": row["filing_date"],
        # Real publish instant from the already-fetched index.htm header (no
        # extra HTTP); None when absent/unparseable -> transform falls back to
        # the date-only filing_date timestamp (#391).
        "accepted_utc": parse_accepted_utc(index_html),
        "base_dir": base_dir,
        "items": items,
        "body": body,
    }


# --- (4) pure transform: enriched hits -> NEWS_COLUMNS frame -----------------
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|h[1-6]|li|tr)\s*/?>", re.IGNORECASE)


def _title_from_body(body: str) -> str:
    """First non-empty stripped text line from the EX-99.1 narrative.

    EX-99.1 exhibits are HTML with varied block structure (``<div>``,
    ``<h1>``-``<h6>``, ``<br>``, tables), not just ``<p>``. Convert every
    block-level boundary to a newline first so a headline wrapped in any of
    them becomes the first line, then strip the remaining inline tags.
    """
    stripped = _BLOCK_TAG_RE.sub("\n", body)
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
        accepted = hit.get("accepted_utc")
        # Acceptance instant is the real publish moment (issue #391). Fall back
        # to the daily-index date-only filing date (00:00 UTC) only on parse
        # miss. For after-close filings whose filing_date rolled to the next day,
        # this deliberately stamps the row on the prior UTC day (the moment the
        # market could act); the per-day parquet cache is keyed by the discovery
        # date, not the row timestamp, so this never mis-routes the cache.
        if accepted is None:
            # Per-row breadcrumb so the silent-revert-to-00:00 frequency stays
            # greppable if the SEC header markup ever drifts (#391 RISK 1).
            logger.debug(
                "edgar row %s: no parsed acceptance, using filing_date fallback",
                hit["accession"],
            )
            timestamp = pd.Timestamp(hit["filing_date"], tz="UTC")
        else:
            timestamp = accepted
        rows.append(
            {
                "id": hit["accession"],  # SEC-stable, dashed form
                "source": SOURCE,
                "timestamp": timestamp,
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
        # Index failure must not raise — the unified ingest's _safe_call
        # degrades it to an empty source. Crucially, do NOT persist an empty
        # parquet here: SEC index failures are overwhelmingly transient
        # (429/5xx backoff), and the production cadence runs 6x/day. Caching
        # empty would poison every later run that UTC day (they would read the
        # empty cache instead of retrying), silently dropping EDGAR for the day.
        logger.warning("edgar daily-index fetch failed for %s: %s", date, exc, exc_info=True)
        return empty_news_frame()

    rows = parse_form_index_8k(idx)
    # Pre-filter to in-universe CIKs BEFORE any per-filing HTTP (cuts ~3500 -> tens).
    rows = [r for r in rows if r["cik_padded"] in cik_to_ticker]

    hits: list[dict] = []
    transient_errors = 0  # 403 (traffic / UA-reject) — distinct from clean skips
    other_errors = 0
    for row in rows:
        try:
            hit = _enrich_filing(row, client=sec)
        except SecForbiddenError as exc:
            # Shared-IP traffic 403 (or UA-reject) on ANY of the per-filing
            # fetches (index.htm / primary / ex991). This is the #379 vector: an
            # empty result here is an artifact of rate-limiting, not a quiet day.
            # Count it so the poison guard below refuses to cache.
            transient_errors += 1
            logger.warning("edgar 8-K enrich 403 %s: %s", row.get("accession"), exc)
            continue
        except Exception as exc:
            # Genuinely bad filing (malformed, 404). One must not kill the day.
            other_errors += 1
            logger.warning(
                "edgar 8-K enrich failed %s: %s", row.get("accession"), exc, exc_info=True
            )
            continue
        if hit:
            hits.append(hit)

    df = transform(hits, cik_to_ticker=cik_to_ticker, date=date)
    # Cache-poison guard (#379 / #382 / #383). Skip the empty-parquet write ONLY
    # when the frame is empty AND at least one transient (403) error occurred —
    # that empty is an artifact of rate-limiting, and caching it would poison the
    # 5 later same-UTC-day runs (they read the empty cache instead of retrying).
    # A genuinely empty day (transient_errors == 0) is still cached: the daily
    # index is immutable, so re-fetching is guaranteed-empty wasted SEC budget. A
    # non-empty (incl. partial) frame is always cached: refusing to cache partials
    # would re-enrich every surviving filing on all 6 daily runs, multiplying the
    # per-IP load that causes the 403s.
    if df.empty and transient_errors > 0:
        logger.warning(
            "edgar press-release: %d transient (403) error(s), %d other error(s), "
            "0 hits for %s; skipping empty-parquet write so later runs retry",
            transient_errors,
            other_errors,
            date,
        )
        return df
    df.to_parquet(cache_path, index=False)
    return df

"""Canonical publisher-title enrichment for brief events.

GDELT's DOC API mangles titles — it strips em-dashes and apostrophes
(transliterates to ASCII), so a headline like ``... the outbreak — when could
they be ready?`` arrives as ``... the outbreak when could they be ready?`` and
reads as broken English. The dash cannot be reconstructed from GDELT's data;
the only correct fix is to take the publisher's own canonical title
(``og:title`` / ``<title>``) from the event URL.

This module fetches that title best-effort, with a URL-keyed cache and a
replacement guard, and is wired into Phase E brief generation (only the ~14
selected events are fetched, never the 200-item news cache). Any failure falls
back to the existing title, so the pipeline degrades to pre-change behaviour
when the network is down or enrichment is disabled. See
``docs/research/canonical_title_enrichment_design_2026_06_12.md``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from alphalens_pipeline.thematic import text_similarity

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "og_title_cache"
# Publisher titles are near-immutable; re-fetch only after a long staleness
# window so a once-captured title never drifts but a genuinely changed page can
# eventually refresh.
_CACHE_TTL_DAYS = 180
_TITLE_MAX_LEN = 200  # mirror catalyst_resolver._TITLE_MAX_LEN
_TIMEOUT_S = 12
_MIN_LEN = 12
_MAX_LEN = 300
_MIN_SHARED_TOKENS = 2
_USER_AGENT = "AlphaLens/1.0 (+research; thematic brief title enrichment)"

# Bot-challenge / error pages expose a generic ``og:title``. Never cache or use
# one of these — fall back to the source title and retry next run (the page may
# pass the challenge later). Matched case-insensitively as a substring.
_JUNK_SUBSTRINGS = (
    "just a moment",
    "are you a robot",
    "access denied",
    "attention required",
    "403 forbidden",
    "please enable",
    "bot detection",
    "captcha",
    "verifying you are human",
    "enable javascript",
)


def _default_fetcher(url: str) -> str:
    """Fetch a publisher page as text. Generic web GET — deliberately NOT routed
    through a vendor client (these hit arbitrary publisher domains, not a quota'd
    vendor)."""
    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    return resp.text


def _clean(title: str) -> str:
    """Decode entities, collapse whitespace, trim, truncate to the brief cap."""
    title = html.unescape(title)
    title = " ".join(title.split())
    if len(title) > _TITLE_MAX_LEN:
        title = title[: _TITLE_MAX_LEN - 1].rstrip() + "…"
    return title


def _extract_title(html_text: str) -> str | None:
    """Pull the publisher title from page HTML: og:title → twitter:title → <title>.

    Returns the cleaned title, or ``None`` when no usable title tag is present.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    # Collect every <meta> title-ish tag keyed by its property/name, then pick in
    # preference order. Iterating find_all sidesteps the bs4 ``attrs=`` typing.
    meta_titles: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        content = tag.get("content")
        if isinstance(key, str) and isinstance(content, str) and content.strip():
            meta_titles.setdefault(key, content)
    for key in ("og:title", "twitter:title"):
        if key in meta_titles:
            return _clean(meta_titles[key])
    if soup.title is not None:
        title_str = soup.title.string
        if title_str and title_str.strip():
            return _clean(title_str)
    return None


def _is_junk(title: str) -> bool:
    low = title.lower()
    return any(sub in low for sub in _JUNK_SUBSTRINGS)


def _cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.txt"


def _cache_is_fresh(path: Path, ttl_days: int) -> bool:
    try:
        age = dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return age <= dt.timedelta(days=ttl_days)


def fetch_og_title(
    url: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    fetcher=_default_fetcher,
    ttl_days: int = _CACHE_TTL_DAYS,
) -> str | None:
    """Return the publisher's canonical title for ``url``, or ``None``.

    Tri-state, mirroring ``verification/tenk_grep.fetch_10k_text``:

    - cache hit (fresh) → cached title
    - cache miss → fetch → extract → validate → cache → title
    - failure / junk / empty / out-of-length → ``None`` and **no cache write**

    Negative results are deliberately NOT cached: a transient fetch failure or a
    bot-challenge page must not poison the URL forever (lesson from the SEC-403
    cache-poisoning incident, PR #386). The call-specific replacement decision
    (token overlap with the source title) lives in :func:`canonical_title_for`,
    not here, so the cache stores a source-independent canonical title.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, url)
    if path.exists() and _cache_is_fresh(path, ttl_days):
        return path.read_text(encoding="utf-8")

    try:
        html_text = fetcher(url)
    except Exception as exc:  # network / timeout / HTTP error — best-effort
        logger.info("og:title fetch failed for %s: %s", url, exc)
        return None

    title = _extract_title(html_text)
    if not title or _is_junk(title) or not (_MIN_LEN <= len(title) <= _MAX_LEN):
        return None

    path.write_text(title, encoding="utf-8")
    return title


def canonical_title_for(
    url: str | None,
    *,
    fallback: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    fetcher=_default_fetcher,
) -> str:
    """Return the publisher's canonical title for ``url`` if it is a safe
    replacement for ``fallback``, else ``fallback``. Never raises.

    "Safe replacement" = the fetched title shares at least
    ``_MIN_SHARED_TOKENS`` content tokens (lowercased, de-stopworded, length-
    filtered via :func:`text_similarity.normalize_title`) with ``fallback``.
    This confirms the same article even when the publisher reworded the headline
    — a Jaccard>=0.6 check would wrongly reject a heavy reword.
    """
    if not url:
        return fallback
    try:
        og = fetch_og_title(url, cache_dir=cache_dir, fetcher=fetcher)
    except (
        Exception
    ) as exc:  # defensive — fetch_og_title already swallows, but never break the brief
        logger.info("canonical_title_for unexpected error for %s: %s", url, exc)
        return fallback
    if not og:
        return fallback
    norm_fallback = text_similarity.normalize_title(fallback)
    if not norm_fallback:
        # No usable source title to cross-check against (blank/short). The
        # og:title is the only title for this exact URL and already passed the
        # junk filter, so take it.
        return og
    shared = text_similarity.normalize_title(og) & norm_fallback
    if len(shared) >= _MIN_SHARED_TOKENS:
        return og
    return fallback

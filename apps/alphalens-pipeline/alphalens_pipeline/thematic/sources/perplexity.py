"""Perplexity "top-stories" news source (5th thematic source).

Fetches the day's most significant market-moving stories via the canonical
PerplexityClient and emits NEWS_COLUMNS rows with tickers=[] — the downstream
map-themes stage proposes beneficiaries from the extracted theme. Source
curation is left entirely to Perplexity (no domain filter). Flag-gated in
news_ingest (ALPHALENS_PERPLEXITY_SOURCE); see
docs/research/perplexity_news_source_design_2026_06_24.md.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path

import pandas as pd

from alphalens_pipeline.literature_scanner.perplexity_client import PerplexityClient
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news_perplexity"

_PROMPT = """\
As of {date}, list the most significant market-moving news stories affecting \
US-listed equities that day. Output ONLY a JSON object, nothing else:

{{"stories": [{{"headline": "<concise headline>", "summary": "<1-2 sentence neutral summary>", "url": "<one representative source URL>"}}]}}

Rules: 8-12 distinct stories (not the same event rephrased); concrete events only."""


def build_prompt(date_iso: str) -> str:
    return _PROMPT.format(date=date_iso)


def _stable_id(s: str) -> str:
    # Content-addressing; sha256 to satisfy Sonar S4790.
    return "perplexity:" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _extract_json(content: str) -> object:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON object found")
    obj, _end = json.JSONDecoder().raw_decode(text[min(starts) :])
    return obj


def parse_stories(content: str) -> list[dict]:
    try:
        data = _extract_json(content)
    except (ValueError, json.JSONDecodeError):
        logger.warning("perplexity source: response was not parseable JSON")
        return []
    stories = data.get("stories") if isinstance(data, dict) else data
    if not isinstance(stories, list):
        return []
    out: list[dict] = []
    for s in stories:
        if not isinstance(s, dict):
            continue
        headline = str(s.get("headline") or "").strip()
        url = str(s.get("url") or "").strip()
        if not headline or not url:
            continue
        out.append(
            {"headline": headline, "summary": str(s.get("summary") or "").strip(), "url": url}
        )
    return out


def _default_client() -> PerplexityClient:
    key = os.environ.get("PERPLEXITY_API_KEY")
    if not key:
        raise RuntimeError("PERPLEXITY_API_KEY not set")
    return PerplexityClient(api_key=key)


def fetch_daily_news(
    *,
    date: dt.date,
    client: PerplexityClient | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    force: bool = False,
) -> pd.DataFrame:
    """Fetch the day's Perplexity top-stories as NEWS_COLUMNS rows.

    Raw response cached per date; ``tickers`` is always ``[]`` so the
    downstream map-themes stage proposes beneficiaries from the theme.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.json"
    if cache_path.exists() and not force:
        raw = json.loads(cache_path.read_text())
        content, search_results = raw["content"], raw["search_results"]
    else:
        client = client or _default_client()
        after = (date - dt.timedelta(days=1)).strftime("%m/%d/%Y")
        before = (date + dt.timedelta(days=1)).strftime("%m/%d/%Y")
        result = client.ask_with_citations(
            build_prompt(date.isoformat()),
            search_context_size="high",
            search_after_date_filter=after,
            search_before_date_filter=before,
        )
        content, search_results = result.content, result.search_results
        cache_path.write_text(json.dumps({"content": content, "search_results": search_results}))

    stories = parse_stories(content)
    citation_urls = [str(r.get("url", "")) for r in search_results if isinstance(r, dict)]
    extra = json.dumps({"citation_count": len(citation_urls), "citations": citation_urls})
    ts = pd.Timestamp(date, tz="UTC")

    rows = [
        {
            "id": _stable_id(s["url"]),
            "source": "perplexity",
            "timestamp": ts,
            "tickers": [],
            "title": s["headline"],
            "body": s["summary"],
            "url": s["url"],
            "keywords": [],
            "extra": extra,
        }
        for s in stories
    ]
    if not rows:
        return empty_news_frame()
    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df

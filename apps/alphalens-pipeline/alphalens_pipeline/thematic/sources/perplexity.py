"""Perplexity "top-stories" news source (5th thematic source).

Fetches the day's most significant market-moving stories via the canonical
PerplexityClient and emits NEWS_COLUMNS rows with tickers=[] — the downstream
map-themes stage proposes beneficiaries from the extracted theme. Source
curation is left entirely to Perplexity (no domain filter). Flag-gated in
news_ingest (ALPHALENS_PERPLEXITY_SOURCE); see
docs/research/perplexity_news_source_design_2026_06_24.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

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
        headline = str(s.get("headline", "")).strip()
        url = str(s.get("url", "")).strip()
        if not headline or not url:
            continue
        out.append({"headline": headline, "summary": str(s.get("summary", "")).strip(), "url": url})
    return out

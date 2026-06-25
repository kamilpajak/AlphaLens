from __future__ import annotations

import json
import logging

from .models import DiscoverCandidate

logger = logging.getLogger(__name__)


def _extract_json(content: str) -> object:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not candidates:
        raise ValueError("no JSON object found")
    start = min(candidates)
    obj, _end = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _candidates_from_story(
    story: dict,
    citation_urls: list[str],
    citation_count: int,
) -> list[DiscoverCandidate]:
    """Build the candidate rows for one story's beneficiaries (skipping malformed)."""
    event_title = str(story.get("event_title", "")).strip()
    event_url = str(story.get("event_url", "")).strip()
    out: list[DiscoverCandidate] = []
    for b in story.get("beneficiaries") or []:
        if not isinstance(b, dict):
            continue
        ticker = str(b.get("ticker", "")).strip().upper()
        company = str(b.get("company", "")).strip()
        reason = str(b.get("reason", "")).strip()
        if not ticker or not company:
            continue
        out.append(
            DiscoverCandidate(
                ticker=ticker,
                company=company,
                theme=event_title,
                rationale=reason,
                citation_count=citation_count,
                citation_urls=citation_urls,
                source_event_title=event_title,
                source_event_url=event_url,
            )
        )
    return out


def parse_discover_response(content: str, search_results: list[dict]) -> list[DiscoverCandidate]:
    try:
        data = _extract_json(content)
    except ValueError:
        # json.JSONDecodeError is a subclass of ValueError, so this catches both.
        logger.warning("discover_lane: response was not parseable JSON")
        return []

    stories = data.get("stories") if isinstance(data, dict) else data
    if not isinstance(stories, list):
        return []

    citation_urls = [str(r.get("url", "")) for r in search_results if isinstance(r, dict)]
    citation_count = len(citation_urls)

    out: list[DiscoverCandidate] = []
    for story in stories:
        if isinstance(story, dict):
            out.extend(_candidates_from_story(story, citation_urls, citation_count))
    return out

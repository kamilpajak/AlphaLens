"""Gemini 2.5 Flash batch event extraction over the unified news parquet.

For each news row, one Gemini call returns a structured ``ThematicEvent`` JSON
object conforming to :data:`schema.EVENT_RESPONSE_SCHEMA`. Output is cached
per-day at ``~/.alphalens/thematic_events/{YYYY-MM-DD}.parquet`` and joined to
the source row via ``news_id``. Subsequent runs skip already-extracted IDs,
so partial-day runs (e.g. after a rate-limit pause) resume cleanly.

Cost envelope: ~200 items/day × Flash ~$0.0001/item ≈ $0.50/mo, well within
the §14 lock-7 $30-40/mo Gemini ceiling. Free tier (1500 req/day) absorbs
the entire batch.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import pandas as pd

from alphalens.thematic.extraction.schema import (
    EVENT_RESPONSE_SCHEMA,
    normalize_extraction,
    parse_extraction,
)

logger = logging.getLogger(__name__)

DEFAULT_NEWS_DIR = Path.home() / ".alphalens" / "thematic_news"
DEFAULT_EVENTS_DIR = Path.home() / ".alphalens" / "thematic_events"
DEFAULT_MODEL = "gemini-2.5-flash"

_PROMPT_TEMPLATE = """\
You are an analyst extracting structured events from financial news.

INPUT
-----
Source: {source}
Tickers tagged by feed: {tickers}
Title: {title}
Body: {body}

TASK
----
Return a JSON object with these fields:
- event_type: one of product_launch, m_and_a, regulatory, partnership, earnings, analyst, macro, other
- primary_entities: list of stock tickers (uppercase) most relevant to this news
- themes: free-form list of thematic keywords (e.g. ["quantum_computing", "AI_inference_hardware"])
- sentiment: positive | negative | neutral for the primary entity
- second_order_implications: list of 1-3 short sentences naming likely small/mid-cap downstream beneficiaries or losers, with one-clause rationale
- confidence: 0.0 to 1.0 reflecting how well-supported this extraction is

Be terse, ground every claim in the input, and skip speculation past second-order.
"""


def _load_genai_sdk():
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("google-genai SDK not installed. `uv add google-genai`.") from exc
    return genai, types


def _call_gemini(client, prompt: str, *, model: str, types_mod):
    """Single seam for tests to patch. Returns the raw SDK response."""
    return client.models.generate_content(
        model=model,
        contents=prompt,
        config=types_mod.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EVENT_RESPONSE_SCHEMA,
            temperature=0.0,
            max_output_tokens=2000,
        ),
    )


def build_prompt(news_row: dict | pd.Series) -> str:
    tickers = news_row.get("tickers") if isinstance(news_row, dict) else news_row["tickers"]
    tickers_str = ", ".join(tickers) if tickers is not None and len(tickers) else "(none)"
    return _PROMPT_TEMPLATE.format(
        source=news_row["source"],
        tickers=tickers_str,
        title=news_row["title"],
        body=(news_row.get("body") or "")[:2000]
        if isinstance(news_row, dict)
        else (news_row["body"] or "")[:2000],
    )


def extract_one(
    news_row: dict | pd.Series,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict | None:
    """Run Gemini Flash on a single news row; return normalised event dict or ``None``."""
    genai, types_mod = _load_genai_sdk()
    client = genai.Client(api_key=api_key)
    prompt = build_prompt(news_row)
    try:
        response = _call_gemini(client, prompt, model=model, types_mod=types_mod)
    except Exception as exc:
        logger.warning("Gemini extract failed: %s", exc, exc_info=True)
        return None
    raw = getattr(response, "text", "") or ""
    parsed = parse_extraction(raw)
    if parsed is None:
        logger.warning("Gemini returned unparseable JSON: %r", raw[:200])
        return None
    return normalize_extraction(parsed)


def _load_cached_events(events_path: Path) -> pd.DataFrame:
    if events_path.exists():
        return pd.read_parquet(events_path)
    return pd.DataFrame()


def extract_daily(
    *,
    date: dt.date,
    news_dir: Path = DEFAULT_NEWS_DIR,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> pd.DataFrame:
    """Extract events for one day's unified news parquet; cache results.

    Idempotent per ``news_id``: items already in the events parquet are kept
    untouched and not re-sent to Gemini.
    """
    api_key = api_key or os.environ.get("GOOGLE_API_KEY") or ""
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set — cannot call Gemini")

    news_path = news_dir / f"{date.isoformat()}.parquet"
    if not news_path.exists():
        raise FileNotFoundError(f"news parquet missing for {date}: {news_path}")

    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / f"{date.isoformat()}.parquet"

    news = pd.read_parquet(news_path)
    cached = _load_cached_events(events_path)
    already = set(cached["news_id"]) if not cached.empty else set()

    to_extract = news[~news["id"].isin(already)]
    logger.info(
        "extract_daily %s: %d total news, %d already cached, %d new",
        date,
        len(news),
        len(already),
        len(to_extract),
    )

    new_rows: list[dict] = []
    extracted_at = pd.Timestamp.now(tz="UTC")
    for _, row in to_extract.iterrows():
        event = extract_one(row, api_key=api_key, model=model)
        if event is None:
            continue
        new_rows.append(
            {
                "news_id": row["id"],
                **event,
                "model": model,
                "extracted_at": extracted_at,
            }
        )

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if cached.empty:
            combined = new_df
        else:
            combined = pd.concat([cached, new_df], ignore_index=True)
        combined.to_parquet(events_path, index=False)
    else:
        combined = cached

    # Stable order: by news arrival time via join back to news
    if not combined.empty:
        combined = combined.drop_duplicates(subset=["news_id"], keep="last").reset_index(drop=True)

    return combined


def _approx_cost_usd(n_items: int) -> float:
    """Rough Flash cost estimate at ~500 input + ~200 output tokens per item."""
    per_item = (500 * 0.075 + 200 * 0.30) / 1_000_000
    return n_items * per_item


__all__ = [
    "DEFAULT_EVENTS_DIR",
    "DEFAULT_MODEL",
    "DEFAULT_NEWS_DIR",
    "build_prompt",
    "extract_daily",
    "extract_one",
]

"""LLM event extraction schema.

Used both as the Gemini ``response_schema`` (JSON-Schema dict) and as the
runtime contract for downstream Layer 3 reasoning. Normalisation is deliberate:
LLMs occasionally drift on enum casing or surface unseen event_types, and we
quarantine those to ``other`` rather than letting them break the parquet
schema.
"""

from __future__ import annotations

import json

EVENT_TYPES: tuple[str, ...] = (
    # Corporate actions & ownership
    "m_and_a",
    "spinoff",
    "restructuring",
    "activist_position",
    # Earnings & guidance
    "earnings",
    "guidance",
    # Capital structure & financing
    "financing",
    "ipo",
    "secondary",
    "dividend",
    "buyback",
    "bankruptcy",
    # Governance & workforce
    "exec_change",
    "board_change",
    "strike",
    "layoffs",
    # Legal & regulatory
    "regulatory",
    "litigation",
    "settlement",
    "investigation",
    "recall",
    "breach",
    # Product, operations & commercial
    "product_launch",
    "product_retirement",
    "contract_award",
    "partnership",
    # Analyst & sentiment
    "analyst",
    "rating_change",
    "price_target",
    # Macro & policy
    "macro",
    "geopolitical",
    "central_bank",
    # Non-market-moving / informational (explicit noise branch — per Perplexity
    # research §5.3, standard practice in academic event extraction systems).
    # ``evergreen`` = perennial-relevance content (explainers, "What is X"
    # primers) — distinguished from time-sensitive catalysts.
    "opinion",
    "lifestyle",
    "listicle",
    "promo",
    "evergreen",
    "sponsored",
    # Catch-all (normalize_extraction coerces unrecognised values here).
    "other",
)

# Subset of EVENT_TYPES that downstream filters (catalyst_resolver) treat as
# non-catalyst noise. Single source of truth so the "what counts as noise"
# decision lives in one place.
NOISE_EVENT_TYPES: tuple[str, ...] = (
    "opinion",
    "lifestyle",
    "listicle",
    "promo",
    "evergreen",
    "sponsored",
)

SENTIMENTS: tuple[str, ...] = ("positive", "negative", "neutral")

EVENT_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
        "primary_entities": {
            "type": "array",
            "items": {"type": "string"},
        },
        "themes": {
            "type": "array",
            "items": {"type": "string"},
        },
        "sentiment": {"type": "string", "enum": list(SENTIMENTS)},
        "second_order_implications": {
            "type": "array",
            "items": {"type": "string"},
        },
        "confidence": {"type": "number"},
    },
    "required": ["event_type", "themes", "sentiment", "confidence"],
}


def parse_extraction(raw: str) -> dict | None:
    """Parse JSON from Gemini text. Falls back to greedy ``{...}`` extraction."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            return json.loads(raw[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            return None
    return None


def normalize_extraction(extraction: dict) -> dict:
    """Coerce LLM output to a canonical shape: lowercased enums, uppercased tickers, clamped confidence."""
    event_type = str(extraction.get("event_type", "other")).strip().lower()
    if event_type not in EVENT_TYPES:
        event_type = "other"

    sentiment = str(extraction.get("sentiment", "neutral")).strip().lower()
    if sentiment not in SENTIMENTS:
        sentiment = "neutral"

    primary_entities = [
        str(e).strip().upper() for e in (extraction.get("primary_entities") or []) if str(e).strip()
    ]
    themes = [str(t).strip() for t in (extraction.get("themes") or []) if str(t).strip()]
    implications = [
        str(s).strip()
        for s in (extraction.get("second_order_implications") or [])
        if str(s).strip()
    ]

    try:
        confidence = float(extraction.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "event_type": event_type,
        "primary_entities": primary_entities,
        "themes": themes,
        "sentiment": sentiment,
        "second_order_implications": implications,
        "confidence": confidence,
    }

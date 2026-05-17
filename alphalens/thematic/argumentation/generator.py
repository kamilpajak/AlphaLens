"""Gemini brief generator — single per-row LLM call with Pro/Flash routing.

Selects model per ``layer4_weighted_score``: ≥4 → ``gemini-3-pro-preview``;
≤3 (or missing) → ``gemini-2.5-flash``. Same response schema for both so
the orchestrator + renderer don't need to branch.

Returns the parsed brief dict with an added ``model_used`` field, or
``None`` on API failure / unparseable response. Mirrors the
``gemini_mapper.propose_candidates`` error-handling shape so the
orchestrator's per-row ``_safe`` wrapper can treat the two interchangeably.
"""

from __future__ import annotations

import logging

from alphalens.thematic.argumentation.prompts import build_flash_prompt, build_pro_prompt
from alphalens.thematic.argumentation.schema import BRIEF_RESPONSE_SCHEMA
from alphalens.thematic.extraction.schema import parse_extraction

logger = logging.getLogger(__name__)

PRO_MODEL = "gemini-3-pro-preview"
FLASH_MODEL = "gemini-2.5-flash"


def choose_model(*, weighted_score: int | float | None) -> str:
    """Pro for weighted_score ≥ 4, Flash otherwise (including None)."""
    if weighted_score is None:
        return FLASH_MODEL
    try:
        return PRO_MODEL if int(weighted_score) >= 4 else FLASH_MODEL
    except (TypeError, ValueError):
        return FLASH_MODEL


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
            response_schema=BRIEF_RESPONSE_SCHEMA,
            temperature=0.2,
            max_output_tokens=2000,
        ),
    )


def generate_brief(
    facts: dict,
    *,
    api_key: str | None = None,
    client_pro=None,
    client_flash=None,
    types_mod=None,
) -> dict | None:
    """Compose a single brief for one Phase D-scored candidate.

    Returns the brief dict (5 LLM-composed string fields + ``model_used``)
    or ``None`` on any failure. The orchestrator catches None per-row and
    emits a placeholder so the rest of the batch survives.
    """
    model = choose_model(weighted_score=facts.get("weighted_score"))
    prompt = build_pro_prompt(facts) if model == PRO_MODEL else build_flash_prompt(facts)

    # Hoisted clients must come paired with types_mod — partial hoisting
    # would silently discard the user's client and lazy-build a new one.
    if (client_pro is not None or client_flash is not None) and types_mod is None:
        raise ValueError("generate_brief: hoisted clients require types_mod (pass both or neither)")
    # Build a client if the caller didn't hoist one (test path / single-call use).
    if types_mod is None:
        genai, types_mod = _load_genai_sdk()
        if api_key is None:
            raise ValueError("generate_brief requires api_key or pre-built clients + types_mod")
        client_pro = client_pro or genai.Client(api_key=api_key)
        client_flash = client_flash or client_pro
    client = client_pro if model == PRO_MODEL else (client_flash or client_pro)
    if client is None:
        raise ValueError("missing client for model %s" % model)

    try:
        response = _call_gemini(client, prompt, model=model, types_mod=types_mod)
    except Exception as exc:
        logger.warning("brief generation failed for %s: %s", facts.get("ticker"), exc)
        return None

    raw = getattr(response, "text", "") or ""
    parsed = parse_extraction(raw)
    if parsed is None:
        logger.warning("brief response unparseable for %s: %r", facts.get("ticker"), raw[:200])
        return None

    # Defensive: ensure all 5 expected keys present; missing key → string "".
    for key in BRIEF_RESPONSE_SCHEMA["required"]:
        parsed.setdefault(key, "")
    parsed["model_used"] = model
    return parsed


__all__ = ["FLASH_MODEL", "PRO_MODEL", "choose_model", "generate_brief"]

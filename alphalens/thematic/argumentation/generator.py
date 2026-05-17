"""Gemini brief generator — single per-row LLM call with Pro/Flash routing.

Selects model per ``layer4_weighted_score``: ≥4 → ``gemini-3-pro-preview``;
≤3 (or missing) → ``gemini-2.5-flash``. Same response schema for both so
the orchestrator + renderer don't need to branch.

``generate_brief`` returns ``(brief | None, BriefErrorKind)`` so callers
can branch on the exact failure mode. ``generate_brief_with_retry`` wraps
it with the Perplexity-recommended retry policy (2026-05-17): on
``BriefErrorKind.TRUNCATED`` (Gemini ``finish_reason == MAX_TOKENS``)
retry once with double ``max_output_tokens`` and ``temperature=0``.
Other failure kinds (``MALFORMED_JSON`` / ``SAFETY`` / ``TRANSPORT``)
do not retry — they will not be helped by more tokens or different
temperature.
"""

from __future__ import annotations

import enum
import logging
from typing import Any

from alphalens.thematic.argumentation.prompts import build_flash_prompt, build_pro_prompt
from alphalens.thematic.argumentation.schema import BRIEF_RESPONSE_SCHEMA
from alphalens.thematic.extraction.schema import parse_extraction

logger = logging.getLogger(__name__)

PRO_MODEL = "gemini-3-pro-preview"
FLASH_MODEL = "gemini-2.5-flash"

_DEFAULT_MAX_OUTPUT_TOKENS = 2000
_DEFAULT_TEMPERATURE = 0.2
_RETRY_TEMPERATURE = 0.0  # greedy decode for stability on the retry


class BriefErrorKind(enum.Enum):
    """Classifies the outcome of a single brief-generation call.

    ``NONE`` means the brief parsed cleanly. The other kinds tell the
    retry wrapper whether retrying makes sense (only ``TRUNCATED`` does).
    """

    NONE = "none"
    TRUNCATED = "truncated"  # finish_reason == MAX_TOKENS
    MALFORMED_JSON = "malformed_json"  # finish_reason == STOP but parse failed
    SAFETY = "safety"  # finish_reason == SAFETY
    TRANSPORT = "transport"  # SDK raised before producing a response


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


def _call_gemini(
    client, prompt: str, *, model: str, types_mod, max_output_tokens: int, temperature: float
):
    """Single seam for tests to patch. Returns the raw SDK response."""
    return client.models.generate_content(
        model=model,
        contents=prompt,
        config=types_mod.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BRIEF_RESPONSE_SCHEMA,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ),
    )


def _classify_finish_reason(response: Any) -> BriefErrorKind | None:
    """Return TRUNCATED / SAFETY when the candidate's finish_reason matches.

    Returns None when the field is absent (test mocks) or indicates STOP.
    Tolerates both enum-shaped (e.g., ``genai.types.FinishReason.MAX_TOKENS``,
    where ``.name == "MAX_TOKENS"``) and string-shaped (``finish_reason ==
    "MAX_TOKENS"``) SDK variants.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    fr = getattr(candidates[0], "finish_reason", None)
    if fr is None:
        return None
    name = getattr(fr, "name", None) or str(fr)
    if name == "MAX_TOKENS":
        return BriefErrorKind.TRUNCATED
    if name == "SAFETY":
        return BriefErrorKind.SAFETY
    return None


def generate_brief(
    facts: dict,
    *,
    api_key: str | None = None,
    client_pro=None,
    client_flash=None,
    types_mod=None,
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
) -> tuple[dict | None, BriefErrorKind]:
    """Compose a single brief for one Phase D-scored candidate.

    Returns ``(brief_dict_with_model_used, BriefErrorKind.NONE)`` on
    success, or ``(None, kind)`` describing the failure mode.
    """
    model = choose_model(weighted_score=facts.get("weighted_score"))
    prompt = build_pro_prompt(facts) if model == PRO_MODEL else build_flash_prompt(facts)

    # Hoisted clients must come paired with types_mod — partial hoisting
    # would silently discard the user's client and lazy-build a new one.
    if (client_pro is not None or client_flash is not None) and types_mod is None:
        raise ValueError("generate_brief: hoisted clients require types_mod (pass both or neither)")
    if types_mod is None:
        genai, types_mod = _load_genai_sdk()
        if api_key is None:
            raise ValueError("generate_brief requires api_key or pre-built clients + types_mod")
        client_pro = client_pro or genai.Client(api_key=api_key)
        client_flash = client_flash or client_pro
    client = client_pro if model == PRO_MODEL else (client_flash or client_pro)
    if client is None:
        raise ValueError(f"missing client for model {model}")

    try:
        response = _call_gemini(
            client,
            prompt,
            model=model,
            types_mod=types_mod,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning("brief generation failed for %s: %s", facts.get("ticker"), exc)
        return None, BriefErrorKind.TRANSPORT

    # Classify finish_reason first — a TRUNCATED response will also fail
    # parse_extraction (JSON cut mid-string), but the truncation kind is
    # the load-bearing signal for the retry wrapper.
    finish_kind = _classify_finish_reason(response)
    if finish_kind is not None:
        logger.warning(
            "brief finish_reason=%s for %s (raw text first 200 chars: %r)",
            finish_kind.value,
            facts.get("ticker"),
            (getattr(response, "text", "") or "")[:200],
        )
        return None, finish_kind

    raw = getattr(response, "text", "") or ""
    parsed = parse_extraction(raw)
    if parsed is None:
        logger.warning("brief response unparseable for %s: %r", facts.get("ticker"), raw[:200])
        return None, BriefErrorKind.MALFORMED_JSON

    # Defensive: ensure all 5 expected keys present; missing key → string "".
    for key in BRIEF_RESPONSE_SCHEMA["required"]:
        parsed.setdefault(key, "")
    parsed["model_used"] = model
    return parsed, BriefErrorKind.NONE


def generate_brief_with_retry(
    facts: dict,
    *,
    api_key: str | None = None,
    client_pro=None,
    client_flash=None,
    types_mod=None,
    base_max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
) -> dict | None:
    """Generate a brief, retrying once on ``BriefErrorKind.TRUNCATED``.

    The retry doubles ``max_output_tokens`` and sets ``temperature=0`` so
    decoding is greedy/deterministic. Other failure kinds (MALFORMED_JSON,
    SAFETY, TRANSPORT) return None without retrying — extra tokens won't
    fix bad JSON, safety blocks, or network errors.

    Returns the brief dict (with ``model_used``) on success, ``None``
    otherwise. The orchestrator's graceful-degradation renderer then
    surfaces the deterministic facts even when this returns None.

    Example:
        >>> brief = generate_brief_with_retry(facts, api_key="sk-...", base_max_output_tokens=2000)
        >>> brief["tldr"] if brief else "(LLM failed)"
    """
    # Hoist SDK + client init ONCE so the retry path doesn't re-do it. When
    # the caller passed hoisted clients (orchestrator batch path) this is a
    # no-op; the ad-hoc path (api_key only) would otherwise pay two
    # genai.Client() handshakes per truncation incident.
    if types_mod is None and client_pro is None and client_flash is None and api_key is not None:
        genai, types_mod = _load_genai_sdk()
        client_pro = genai.Client(api_key=api_key)
        client_flash = client_pro

    brief, kind = generate_brief(
        facts,
        api_key=api_key,
        client_pro=client_pro,
        client_flash=client_flash,
        types_mod=types_mod,
        max_output_tokens=base_max_output_tokens,
        temperature=_DEFAULT_TEMPERATURE,
    )
    if kind == BriefErrorKind.NONE:
        return brief
    if kind != BriefErrorKind.TRUNCATED:
        return None

    retry_tokens = base_max_output_tokens * 2
    logger.info(
        "brief retry for %s: max_output_tokens %d -> %d, temperature=%.1f",
        facts.get("ticker"),
        base_max_output_tokens,
        retry_tokens,
        _RETRY_TEMPERATURE,
    )
    brief, kind = generate_brief(
        facts,
        api_key=api_key,
        client_pro=client_pro,
        client_flash=client_flash,
        types_mod=types_mod,
        max_output_tokens=retry_tokens,
        temperature=_RETRY_TEMPERATURE,
    )
    return brief if kind == BriefErrorKind.NONE else None


__all__ = [
    "FLASH_MODEL",
    "PRO_MODEL",
    "BriefErrorKind",
    "choose_model",
    "generate_brief",
    "generate_brief_with_retry",
]

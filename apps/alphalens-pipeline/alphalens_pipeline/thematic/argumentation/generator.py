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

import json_repair

from alphalens_pipeline.data.alt_data.gemini_client import GeminiClient, get_default_gemini_client
from alphalens_pipeline.thematic.argumentation.prompts import build_flash_prompt, build_pro_prompt
from alphalens_pipeline.thematic.argumentation.schema import BRIEF_RESPONSE_SCHEMA
from alphalens_pipeline.thematic.extraction.schema import parse_extraction

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


def _call_gemini(
    gemini_client: GeminiClient,
    prompt: str,
    *,
    model: str,
    max_output_tokens: int,
    temperature: float,
):
    """Single seam for tests to patch. Returns the raw SDK response."""
    return gemini_client.generate_content(
        model=model,
        contents=prompt,
        config=gemini_client.build_config(
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


def _resolve_gemini_client(
    *,
    model: str,
    api_key: str | None,
    gemini_client_pro: GeminiClient | None,
    gemini_client_flash: GeminiClient | None,
) -> GeminiClient:
    """Pick the right (pro vs flash) client, lazily building defaults.

    Client init lives in this helper so missing-SDK / missing-key failures
    can be caught by the per-brief try/except wrapper (TRANSPORT kind)
    rather than crashing the orchestrator loop.
    """
    if gemini_client_pro is None and gemini_client_flash is None:
        default = GeminiClient(api_key=api_key) if api_key else get_default_gemini_client()
        gemini_client_pro = default
        gemini_client_flash = default
    else:
        # Partial hoisting — fill in the other half with the supplied one.
        gemini_client_pro = gemini_client_pro or gemini_client_flash
        gemini_client_flash = gemini_client_flash or gemini_client_pro
    return gemini_client_pro if model == PRO_MODEL else gemini_client_flash


def generate_brief(
    facts: dict,
    *,
    api_key: str | None = None,
    gemini_client_pro: GeminiClient | None = None,
    gemini_client_flash: GeminiClient | None = None,
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
) -> tuple[dict | None, BriefErrorKind]:
    """Compose a single brief for one Phase D-scored candidate.

    Returns ``(brief_dict_with_model_used, BriefErrorKind.NONE)`` on
    success, or ``(None, kind)`` describing the failure mode.

    Pro and Flash models can be routed through the same or different
    :class:`GeminiClient` instances (the SDK uses one client for all
    models). Pass either ``gemini_client_pro`` / ``gemini_client_flash``
    (orchestrator batch path) OR ``api_key=`` (ad-hoc), otherwise the
    process-wide default client is used.
    """
    model = choose_model(weighted_score=facts.get("weighted_score"))
    prompt = build_pro_prompt(facts) if model == PRO_MODEL else build_flash_prompt(facts)

    try:
        client = _resolve_gemini_client(
            model=model,
            api_key=api_key,
            gemini_client_pro=gemini_client_pro,
            gemini_client_flash=gemini_client_flash,
        )
        response = _call_gemini(
            client,
            prompt,
            model=model,
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
        # finish_reason=STOP + parse failed → try json-repair (per
        # Perplexity 2026-05-17 §1.2). The model finished generating but
        # the JSON has small structural errors (missing comma, trailing
        # bracket, etc); json_repair often salvages exactly the kind of
        # output the schema expects. We do NOT apply repair to TRUNCATED
        # responses — those short-circuit upstream so the retry wrapper
        # can drive a fresh attempt with more tokens.
        parsed = _try_json_repair(raw, ticker=facts.get("ticker"))
        if parsed is None:
            logger.warning("brief response unparseable for %s: %r", facts.get("ticker"), raw[:200])
            return None, BriefErrorKind.MALFORMED_JSON

    # Defensive: ensure all 5 expected keys present; missing key → string "".
    for key in BRIEF_RESPONSE_SCHEMA["required"]:
        parsed.setdefault(key, "")
    parsed["model_used"] = model
    return parsed, BriefErrorKind.NONE


def _try_json_repair(raw: str, *, ticker: str | None = None) -> dict | None:
    """Attempt to salvage a malformed JSON brief via json-repair.

    Returns the parsed dict on success, None otherwise. Logs at INFO
    level when repair succeeds so the operator can monitor how often
    repair is needed (frequent repair = upstream prompt or schema
    issue worth investigating).

    Treats empty / content-less dicts as failure (zen review 2026-05-17
    M1): ``json_repair.loads('{ unparseable garbage')`` returns ``{}``,
    which is structurally a dict but has no substantive content; counting
    it as a "successful repair" would pollute the Pro/Flash counters and
    mislead the BriefErrorKind classifier. Require at least one schema-
    required key with non-empty string text to count as recovery.
    """
    try:
        repaired = json_repair.loads(raw)
    except Exception as exc:
        logger.debug("json_repair failed for %s: %s", ticker, exc)
        return None
    if not isinstance(repaired, dict):
        logger.debug("json_repair for %s returned non-dict: %r", ticker, type(repaired))
        return None
    has_substantive_field = any(
        isinstance(repaired.get(k), str) and repaired[k].strip()
        for k in BRIEF_RESPONSE_SCHEMA["required"]
    )
    if not has_substantive_field:
        logger.debug("json_repair for %s returned empty/contentless dict", ticker)
        return None
    logger.info("json_repair recovered brief for %s (%d keys)", ticker, len(repaired))
    return repaired


def generate_brief_with_retry(
    facts: dict,
    *,
    api_key: str | None = None,
    gemini_client_pro: GeminiClient | None = None,
    gemini_client_flash: GeminiClient | None = None,
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
    """
    # Resolve clients ONCE so the retry path doesn't re-do lazy-singleton
    # lookup. Cheap when the caller already hoisted (orchestrator batch
    # path); meaningful when called ad-hoc with just an api_key.
    if gemini_client_pro is None and gemini_client_flash is None:
        default = GeminiClient(api_key=api_key) if api_key else get_default_gemini_client()
        gemini_client_pro = default
        gemini_client_flash = default

    brief, kind = generate_brief(
        facts,
        gemini_client_pro=gemini_client_pro,
        gemini_client_flash=gemini_client_flash,
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
        gemini_client_pro=gemini_client_pro,
        gemini_client_flash=gemini_client_flash,
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

"""LLM brief generator — single per-row call with Pro/Flash routing.

Selects model per ``layer4_weighted_score``: ≥4 → ``deepseek/deepseek-v4-pro``;
≤3 (or missing) → ``deepseek/deepseek-v4-flash``. Same response schema for
both so the orchestrator + renderer don't need to branch.

``generate_brief`` returns ``(brief | None, BriefErrorKind)`` so callers
can branch on the exact failure mode. ``generate_brief_with_retry`` wraps
it with the Perplexity-recommended retry policy (2026-05-17): on
``BriefErrorKind.TRUNCATED`` (OpenRouter ``finish_reason == "length"``,
translated to ``"MAX_TOKENS"`` by the OpenRouter client wrapper) retry
once with double ``max_output_tokens`` and ``temperature=0``; on
``BriefErrorKind.EMPTY`` (finish_reason STOP/absent but the response body
was empty/whitespace-only — a transient no-content response) retry once
with the same token cap and ``temperature=0``. Other failure kinds
(``MALFORMED_JSON`` / ``SAFETY`` / ``TRANSPORT``) do not retry — they
will not be helped by more tokens or different temperature.
"""

from __future__ import annotations

import enum
import logging
import re
from typing import Any

import json_repair

from alphalens_pipeline.data.alt_data.openrouter_client import (
    OpenRouterClient,
    get_default_openrouter_client,
)
from alphalens_pipeline.thematic.argumentation.prompts import build_flash_prompt, build_pro_prompt
from alphalens_pipeline.thematic.argumentation.schema import BRIEF_RESPONSE_SCHEMA
from alphalens_pipeline.thematic.extraction.schema import parse_extraction

logger = logging.getLogger(__name__)

PRO_MODEL = "deepseek/deepseek-v4-pro"
FLASH_MODEL = "deepseek/deepseek-v4-flash"

_DEFAULT_MAX_OUTPUT_TOKENS = 2000
_DEFAULT_TEMPERATURE = 0.2
_RETRY_TEMPERATURE = 0.0  # greedy decode for stability on the retry

# CJK Unicode blocks: a brief is English prose, so ANY Han / Kana / Hangul
# character signals whole-language drift (DeepSeek v4 is Chinese-developed and
# nondeterministically writes the whole brief in Chinese when the prompt does
# not pin the output language — WK card 2026-06-12). Deliberately NOT a generic
# "non-ASCII" test: English briefs legitimately carry Greek math notation
# (α, ρ), the minus sign (−), and the multiplication sign (×), none of which
# fall in these blocks, so they must never trip the guard.
_CJK_RE = re.compile(
    "["
    "぀-ヿ"  # Hiragana + Katakana
    "㐀-䶿"  # CJK Extension A
    "一-鿿"  # CJK Unified Ideographs
    "가-힣"  # Hangul syllables
    "豈-﫿"  # CJK Compatibility Ideographs
    "＀-￯"  # Halfwidth + Fullwidth forms
    "]"
)


class BriefErrorKind(enum.Enum):
    """Classifies the outcome of a single brief-generation call.

    ``NONE`` means the brief parsed cleanly. The other kinds tell the
    retry wrapper whether retrying makes sense (only ``TRUNCATED`` does).
    """

    NONE = "none"
    TRUNCATED = "truncated"  # finish_reason == MAX_TOKENS
    EMPTY = "empty"  # finish_reason STOP/absent but response text empty/whitespace-only
    MALFORMED_JSON = "malformed_json"  # finish_reason == STOP, non-empty body, parse failed
    SAFETY = "safety"  # finish_reason == SAFETY
    TRANSPORT = "transport"  # SDK raised before producing a response
    LANGUAGE_DRIFT = "language_drift"  # parsed cleanly but the prose is CJK, not English


def _contains_cjk(parsed: dict) -> bool:
    """True when any of the brief's required string fields carries CJK text.

    A drifted brief is unreadable for the WhatsApp group, so even one drifted
    field rejects the whole response (the retry regenerates all fields).
    """
    return any(
        isinstance(parsed.get(key), str) and bool(_CJK_RE.search(parsed[key]))
        for key in BRIEF_RESPONSE_SCHEMA["required"]
    )


def choose_model(*, weighted_score: int | float | None) -> str:
    """Pro for weighted_score ≥ 4, Flash otherwise (including None)."""
    if weighted_score is None:
        return FLASH_MODEL
    try:
        return PRO_MODEL if int(weighted_score) >= 4 else FLASH_MODEL
    except (TypeError, ValueError):
        return FLASH_MODEL


def _call_llm(
    llm_client: OpenRouterClient,
    prompt: str,
    *,
    model: str,
    max_output_tokens: int,
    temperature: float,
):
    """Single seam for tests to patch. Returns the raw wrapped response."""
    return llm_client.generate_content(
        model=model,
        contents=prompt,
        config=llm_client.build_config(
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


def _resolve_llm_client(
    *,
    model: str,
    api_key: str | None,
    llm_client_pro: OpenRouterClient | None,
    llm_client_flash: OpenRouterClient | None,
) -> OpenRouterClient:
    """Pick the right (pro vs flash) client, lazily building defaults.

    Client init lives in this helper so missing-SDK / missing-key failures
    can be caught by the per-brief try/except wrapper (TRANSPORT kind)
    rather than crashing the orchestrator loop.
    """
    if llm_client_pro is None and llm_client_flash is None:
        default = OpenRouterClient(api_key=api_key) if api_key else get_default_openrouter_client()
        llm_client_pro = default
        llm_client_flash = default
    else:
        # Partial hoisting — fill in the other half with the supplied one.
        llm_client_pro = llm_client_pro or llm_client_flash
        llm_client_flash = llm_client_flash or llm_client_pro
    assert llm_client_pro is not None and llm_client_flash is not None
    return llm_client_pro if model == PRO_MODEL else llm_client_flash


def generate_brief(
    facts: dict,
    *,
    api_key: str | None = None,
    llm_client_pro: OpenRouterClient | None = None,
    llm_client_flash: OpenRouterClient | None = None,
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
) -> tuple[dict | None, BriefErrorKind]:
    """Compose a single brief for one Phase D-scored candidate.

    Returns ``(brief_dict_with_model_used, BriefErrorKind.NONE)`` on
    success, or ``(None, kind)`` describing the failure mode.

    Pro and Flash models can be routed through the same or different
    :class:`OpenRouterClient` instances (the SDK uses one client for all
    models). Pass either ``llm_client_pro`` / ``llm_client_flash``
    (orchestrator batch path) OR ``api_key=`` (ad-hoc), otherwise the
    process-wide default client is used.
    """
    model = choose_model(weighted_score=facts.get("weighted_score"))
    prompt = build_pro_prompt(facts) if model == PRO_MODEL else build_flash_prompt(facts)

    try:
        client = _resolve_llm_client(
            model=model,
            api_key=api_key,
            llm_client_pro=llm_client_pro,
            llm_client_flash=llm_client_flash,
        )
        response = _call_llm(
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
    if raw.strip() == "":
        # finish_reason was STOP/absent (not MAX_TOKENS, not SAFETY) but the
        # model returned no content at all. This is a transient no-content
        # response (PJT brief 2026-06-07 run 12:54 UTC: empty body + STOP,
        # while the 03:05 and 06:58 runs that day produced full briefs). It
        # is distinct from MALFORMED_JSON ("non-empty but unparseable") — an
        # empty string is "no content", not "bad content" — so json-repair
        # has nothing to salvage. Surface EMPTY so the retry wrapper drives a
        # fresh call rather than degrading to deterministic-only.
        logger.warning(
            "brief response empty (finish_reason STOP/absent) for %s", facts.get("ticker")
        )
        return None, BriefErrorKind.EMPTY

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

    # Language guard: DeepSeek v4 (Chinese-developed) nondeterministically writes
    # the whole brief in Chinese. Such a brief parses cleanly but is unreadable
    # for the WhatsApp group (WK card 2026-06-12). Surface LANGUAGE_DRIFT so the
    # retry wrapper drives a fresh greedy (temperature=0) call; the English
    # directive in the prompt makes that retry deterministically English.
    if _contains_cjk(parsed):
        logger.warning("brief language drift (CJK output) for %s", facts.get("ticker"))
        return None, BriefErrorKind.LANGUAGE_DRIFT

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
    llm_client_pro: OpenRouterClient | None = None,
    llm_client_flash: OpenRouterClient | None = None,
    base_max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
) -> dict | None:
    """Generate a brief, retrying once on ``TRUNCATED`` or ``EMPTY``.

    Retryable kinds:

    * ``BriefErrorKind.TRUNCATED`` — the retry doubles ``max_output_tokens``
      (the model ran out of room) and sets ``temperature=0`` so decoding is
      greedy/deterministic.
    * ``BriefErrorKind.EMPTY`` — a transient no-content response (empty /
      whitespace-only body with finish_reason STOP/absent). The recovery is
      a fresh call at ``temperature=0``; the token cap is left unchanged —
      doubling it does nothing for an empty response (it was never a
      truncation), so we keep the base cap.
    * ``BriefErrorKind.LANGUAGE_DRIFT`` — the brief parsed cleanly but the
      prose came back in Chinese (DeepSeek v4 is Chinese-developed and drifts
      when the language is not pinned). The recovery is a fresh greedy
      (``temperature=0``) call at the base cap; combined with the prompt's
      English directive the retry is deterministically English.

    Non-retryable kinds (``MALFORMED_JSON``, ``SAFETY``, ``TRANSPORT``)
    return None without retrying — extra tokens won't fix bad JSON, safety
    blocks, or network errors.

    Either way the retry runs at most once (no loop). Returns the brief
    dict (with ``model_used``) on success, ``None`` otherwise. The
    orchestrator's graceful-degradation renderer then surfaces the
    deterministic facts even when this returns None.
    """
    # Resolve clients ONCE so the retry path doesn't re-do lazy-singleton
    # lookup. Cheap when the caller already hoisted (orchestrator batch
    # path); meaningful when called ad-hoc with just an api_key.
    if llm_client_pro is None and llm_client_flash is None:
        default = OpenRouterClient(api_key=api_key) if api_key else get_default_openrouter_client()
        llm_client_pro = default
        llm_client_flash = default

    brief, kind = generate_brief(
        facts,
        llm_client_pro=llm_client_pro,
        llm_client_flash=llm_client_flash,
        max_output_tokens=base_max_output_tokens,
        temperature=_DEFAULT_TEMPERATURE,
    )
    if kind == BriefErrorKind.NONE:
        return brief
    if kind not in (
        BriefErrorKind.TRUNCATED,
        BriefErrorKind.EMPTY,
        BriefErrorKind.LANGUAGE_DRIFT,
    ):
        return None

    # Only TRUNCATED needs more room; EMPTY / LANGUAGE_DRIFT were not token-
    # exhaustion, so a fresh greedy call at the base cap is the right recovery.
    retry_tokens = (
        base_max_output_tokens * 2 if kind == BriefErrorKind.TRUNCATED else base_max_output_tokens
    )
    logger.info(
        "brief retry for %s (kind=%s): max_output_tokens %d -> %d, temperature=%.1f",
        facts.get("ticker"),
        kind.value,
        base_max_output_tokens,
        retry_tokens,
        _RETRY_TEMPERATURE,
    )
    brief, kind = generate_brief(
        facts,
        llm_client_pro=llm_client_pro,
        llm_client_flash=llm_client_flash,
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

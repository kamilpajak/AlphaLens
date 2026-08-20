"""LLM brief generator — single per-row call with Pro/Flash routing.

Selects model per ``layer4_weighted_score``: ≥4 → ``deepseek/deepseek-v4-pro``;
≤3 (or missing) → ``deepseek/deepseek-v4-flash``. Same response schema for
both so the orchestrator + renderer don't need to branch.

``generate_brief`` returns ``(brief | None, BriefErrorKind)`` so callers
can branch on the exact failure mode. ``generate_brief_with_retry`` wraps
it with the retry policy: on ``BriefErrorKind.TRUNCATED`` (OpenRouter
``finish_reason == "length"``, translated to ``"MAX_TOKENS"`` by the client
wrapper) ESCALATE ``max_output_tokens`` through a doubling ladder up to the
ceiling (8000 → 16000 → 32000) at ``temperature=0``, stopping at the first
success — the reasoning trace, not the ~300-token JSON, is what exhausts the
budget, so one fixed double is not enough (2026-07-28 XMTR incident). On
``BriefErrorKind.EMPTY`` (finish_reason STOP/absent but the response body
was empty/whitespace-only — a transient no-content response) or
``BriefErrorKind.EMPTY_CONTENT`` (valid JSON parsed but every required field
is blank — the MC/Moelis empty-card incident) retry once with the same token
cap and ``temperature=0``. Other failure kinds (``MALFORMED_JSON`` /
``SAFETY`` / ``TRANSPORT``) do not retry — they will not be helped by more
tokens or different temperature. The wrapper also returns
``(brief | None, BriefErrorKind)`` — ``NONE`` on success, otherwise the
LAST failing kind observed once the retry policy is exhausted.
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
from alphalens_pipeline.thematic.argumentation.support_guard import (
    check_support_language,
)
from alphalens_pipeline.thematic.extraction.schema import parse_extraction

logger = logging.getLogger(__name__)

PRO_MODEL = "deepseek/deepseek-v4-pro"
FLASH_MODEL = "deepseek/deepseek-v4-flash"

# Base output-token budget. DeepSeek v4 is a REASONING model: its thinking trace
# counts against max_tokens, so a small cap can be exhausted by reasoning before
# the ~300-token JSON closes -> finish_reason=length -> empty brief. Measured
# 2026-07-28: XMTR's brief emits only ~279 completion tokens but truncates at
# 2000/4000 and completes cleanly at 8000, so the base carries real reasoning
# headroom. OpenRouter bills tokens GENERATED, not the ceiling, so a generous cap
# is free on healthy briefs and only pays on the reasoning-heavy ones we want to
# succeed.
_DEFAULT_MAX_OUTPUT_TOKENS = 8000
# The truncation retry ladder doubles the cap up to this ceiling (8000 -> 16000
# -> 32000) before giving up, covering a rare heavier reasoning trace.
_MAX_OUTPUT_TOKENS_CEILING = 32000
_DEFAULT_TEMPERATURE = 0.2

# Key stamped on a brief the retry recovered, naming the FIRST draw's failing
# kind. Not part of BRIEF_RESPONSE_SCHEMA and never rendered — the four prose
# columns are projected by name.
FIRST_ATTEMPT_KIND_KEY = "first_attempt_error_kind"
_RETRY_TEMPERATURE = 0.0  # greedy decode for stability on the retry


def _truncation_retry_caps(base: int, ceiling: int) -> list[int]:
    """The escalating retry caps for a TRUNCATED brief: double ``base`` until the
    ceiling, which appears exactly once. ``[]`` when ``base >= ceiling`` (no room
    to escalate). E.g. ``(8000, 32000) -> [16000, 32000]``."""
    caps: list[int] = []
    cap = base
    while cap < ceiling:
        cap = min(cap * 2, ceiling)
        caps.append(cap)
    return caps


# CJK Unicode blocks: a brief is English prose, so ANY Han / Kana / Hangul
# character signals whole-language drift (DeepSeek v4 is Chinese-developed and
# nondeterministically writes the whole brief in Chinese when the prompt does
# not pin the output language — WK card 2026-06-12). Deliberately NOT a generic
# "non-ASCII" test: English briefs legitimately carry Greek math notation
# (α, ρ), the minus sign (−), and the multiplication sign (×), none of which
# fall in these blocks, so they must never trip the guard.
_CJK_RE = re.compile(
    "[\u3000-\u303f"  # CJK symbols & punctuation (ideographic comma/period/brackets)
    "\u3040-\u30ff"  # Hiragana + Katakana
    "\u3400-\u4dbf"  # CJK Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uac00-\ud7a3"  # Hangul syllables
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "\uff00-\uffef]"  # Halfwidth + Fullwidth forms
)


class BriefErrorKind(enum.Enum):
    """Classifies the outcome of a single brief-generation call.

    ``NONE`` means the brief parsed cleanly. The other kinds tell the
    retry wrapper whether retrying makes sense (only ``TRUNCATED`` does).
    """

    NONE = "none"
    TRUNCATED = "truncated"  # finish_reason == MAX_TOKENS
    EMPTY = "empty"  # finish_reason STOP/absent but response text empty/whitespace-only
    EMPTY_CONTENT = "empty_content"  # valid JSON parsed, but every required field is blank
    MALFORMED_JSON = "malformed_json"  # finish_reason == STOP, non-empty body, parse failed
    SAFETY = "safety"  # finish_reason == SAFETY
    TRANSPORT = "transport"  # SDK raised before producing a response
    LANGUAGE_DRIFT = "language_drift"  # parsed cleanly but the prose is CJK, not English
    # Parsed cleanly, but the prose asserts a benefit the channel record
    # cannot support. Same SHAPE as LANGUAGE_DRIFT — a contract violation in
    # otherwise-valid output — and handled the same way: ONE greedy re-roll,
    # then withhold the prose while the ROW still ships.
    UNSUPPORTED_BENEFIT_CLAIM = "unsupported_benefit_claim"


def _has_substantive_field(parsed: dict) -> bool:
    """True when at least one schema-required field carries non-whitespace text.

    The bar for "a brief has content". Used both to gate the primary parse path
    (an all-blank body is not a usable brief) and to validate a json-repair
    recovery. ``.strip()`` means a whitespace-only field does NOT count.
    """
    return any(
        isinstance(parsed.get(key), str) and parsed[key].strip() != ""
        for key in BRIEF_RESPONSE_SCHEMA["required"]
    )


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
    violation_sink: list | None = None,
) -> tuple[dict | None, BriefErrorKind]:
    """Compose a single brief for one Phase D-scored candidate.

    ``violation_sink``, when given, receives the support-guard violations of the
    LAST draw that violated. A withheld row returns no brief, so without this the
    count and spans of the text the operator would have been shown are lost —
    and those are exactly what the first-weeks manual read of the guard needs.

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

    # Defensive: ensure all 4 expected keys present; missing key → string "".
    for key in BRIEF_RESPONSE_SCHEMA["required"]:
        parsed.setdefault(key, "")

    # Empty-content guard: a valid JSON body whose required fields are ALL blank
    # (empty or whitespace-only) parses cleanly but is not a usable brief — the
    # card would render blank SUPPLY.CHAIN / BEAR.CASE / CATALYST.FAILURE.EXIT
    # sections (MC/Moelis incident, 2026-07-19 run: DeepSeek v4 Pro returned
    # `{"tldr":"", ...}`). This is distinct from EMPTY ("raw body empty") and
    # MALFORMED_JSON ("unparseable") — the body parsed, it just has no content.
    # Surface EMPTY_CONTENT so the retry wrapper drives a fresh call rather than
    # accepting a blank brief as success. The bar is "at least one substantive
    # field" (matching the json-repair recovery bar), NOT "all four non-empty",
    # so a terse-but-real brief is not needlessly retried.
    if not _has_substantive_field(parsed):
        logger.warning("brief parsed but every required field is blank for %s", facts.get("ticker"))
        return None, BriefErrorKind.EMPTY_CONTENT

    # Language guard: DeepSeek v4 (Chinese-developed) nondeterministically writes
    # the whole brief in Chinese. Such a brief parses cleanly but is unreadable
    # for the WhatsApp group (WK card 2026-06-12). Surface LANGUAGE_DRIFT so the
    # retry wrapper drives a fresh greedy (temperature=0) call; the English
    # directive in the prompt makes that retry deterministically English.
    if _contains_cjk(parsed):
        logger.warning("brief language drift (CJK output) for %s", facts.get("ticker"))
        return None, BriefErrorKind.LANGUAGE_DRIFT

    # Support-contract guard: the prose must not assert a benefit the channel
    # record cannot support. Same position and same shape as the CJK check —
    # parsed cleanly, but it violates a hard contract. INERT unless the record
    # is bottom-level, absent, or not grounded, so a well-grounded brief is
    # never touched.
    # An absent ``causal_support`` means the CALLER projected no record at all,
    # which is not the same as a record that failed: there is nothing for the
    # prose to contradict, so the guard stays inert. Production cannot take this
    # branch — ``orchestrator._row_to_facts`` always projects the key, using
    # ``no_record`` for an outage, and a test pins that.
    causal_support = str(facts.get("causal_support") or "")
    violations = (
        [
            v
            for v in check_support_language(
                parsed,
                causal_support=causal_support,
                grounding=str(facts.get("channel_grounding") or ""),
            )
            if v.suppressed_by is None
        ]
        if causal_support
        else []
    )
    if violations:
        if violation_sink is not None:
            violation_sink.clear()
            violation_sink.extend(violations)
        for violation in violations:
            logger.warning(
                "brief asserts an unsupported benefit for %s "
                "(causal_support=%s, grounding=%s, field=%s, phrase=%r): %s",
                facts.get("ticker"),
                facts.get("causal_support"),
                facts.get("channel_grounding"),
                violation.field,
                violation.matched_phrase,
                violation.span,
            )
        return None, BriefErrorKind.UNSUPPORTED_BENEFIT_CLAIM

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
    if not _has_substantive_field(repaired):
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
    max_output_tokens_ceiling: int = _MAX_OUTPUT_TOKENS_CEILING,
    violation_sink: list | None = None,
) -> tuple[dict | None, BriefErrorKind]:
    """Generate a brief, retrying on ``TRUNCATED`` / ``EMPTY`` / drift.

    Retryable kinds:

    * ``BriefErrorKind.TRUNCATED`` — escalate ``max_output_tokens`` through a
      doubling ladder up to ``max_output_tokens_ceiling`` (8000 -> 16000 ->
      32000), stopping at the first success, each retry at ``temperature=0`` for
      greedy/deterministic decoding. The reasoning trace, not the tiny JSON, is
      what exhausts the budget, so one fixed double is not enough (2026-07-28).
    * ``BriefErrorKind.EMPTY`` — a transient no-content response (empty /
      whitespace-only body with finish_reason STOP/absent). The recovery is
      a fresh call at ``temperature=0``; the token cap is left unchanged —
      doubling it does nothing for an empty response (it was never a
      truncation), so we keep the base cap.
    * ``BriefErrorKind.EMPTY_CONTENT`` — a valid JSON body that parsed cleanly
      but whose required fields are all blank (MC/Moelis incident 2026-07-19).
      Like ``EMPTY`` this is a transient no-content outcome, so the recovery is
      the same fresh ``temperature=0`` call at the base cap.
    * ``BriefErrorKind.LANGUAGE_DRIFT`` — the brief parsed cleanly but the
      prose came back in Chinese (DeepSeek v4 is Chinese-developed and drifts
      when the language is not pinned). The recovery is a fresh greedy
      (``temperature=0``) call at the base cap; combined with the prompt's
      English directive the retry is deterministically English.

    Non-retryable kinds (``MALFORMED_JSON``, ``SAFETY``, ``TRANSPORT``)
    fail immediately without retrying — extra tokens won't fix bad JSON,
    safety blocks, or network errors.

    Either way the single-retry kinds retry at most once (no loop). Returns
    ``(brief_dict_with_model_used, BriefErrorKind.NONE)`` on success, or
    ``(None, terminal_kind)`` on failure, where the terminal kind is the
    LAST failing kind observed: ``TRUNCATED`` after the token ladder is
    exhausted (or whatever kind the last rung failed with), the immediate
    kind for non-retryable failures, and the retry's failing kind for the
    single-retry kinds. The orchestrator's graceful-degradation renderer
    then surfaces the deterministic facts even when the brief is None.
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
        violation_sink=violation_sink,
    )
    if kind == BriefErrorKind.NONE:
        return brief, kind
    # Recorded on a brief that only survives BECAUSE of the retry, so the caller
    # can tell "clean on the first draw" from "repaired on the second" without a
    # second signature. Read by the orchestrator's guard telemetry; the four
    # prose columns are projected by name, so this extra key never ships.
    first_attempt_kind = kind
    if kind not in (
        BriefErrorKind.TRUNCATED,
        BriefErrorKind.EMPTY,
        BriefErrorKind.EMPTY_CONTENT,
        BriefErrorKind.LANGUAGE_DRIFT,
        BriefErrorKind.UNSUPPORTED_BENEFIT_CLAIM,
    ):
        return None, kind

    # TRUNCATED = the reasoning trace + JSON ran out of room -> escalate the cap
    # through the doubling ladder, stopping at the first success. EMPTY /
    # EMPTY_CONTENT / LANGUAGE_DRIFT / UNSUPPORTED_BENEFIT_CLAIM were NOT
    # token-exhaustion, so a single fresh greedy call at the base cap is the right
    # recovery (more tokens do nothing). The retry uses the SAME prompt — the
    # contract is already in it — so the model gets a clean greedy draw rather
    # than a post-hoc edit. We never edit the model's text: a Python-inserted
    # "may" would fabricate hedging the model never reasoned about and would
    # destroy the audit trail.
    if kind == BriefErrorKind.TRUNCATED:
        for retry_tokens in _truncation_retry_caps(
            base_max_output_tokens, max_output_tokens_ceiling
        ):
            logger.info(
                "brief retry for %s (kind=truncated): max_output_tokens -> %d, temperature=%.1f",
                facts.get("ticker"),
                retry_tokens,
                _RETRY_TEMPERATURE,
            )
            brief, kind = generate_brief(
                facts,
                llm_client_pro=llm_client_pro,
                llm_client_flash=llm_client_flash,
                max_output_tokens=retry_tokens,
                temperature=_RETRY_TEMPERATURE,
                violation_sink=violation_sink,
            )
            if kind == BriefErrorKind.NONE:
                return _stamp_first_attempt(brief, first_attempt_kind), kind
        # Ladder exhausted — surface the LAST failing kind observed (usually
        # TRUNCATED, but the final rung may have failed differently).
        return None, kind

    logger.info(
        "brief retry for %s (kind=%s): max_output_tokens %d (unchanged), temperature=%.1f",
        facts.get("ticker"),
        kind.value,
        base_max_output_tokens,
        _RETRY_TEMPERATURE,
    )
    brief, kind = generate_brief(
        facts,
        llm_client_pro=llm_client_pro,
        llm_client_flash=llm_client_flash,
        max_output_tokens=base_max_output_tokens,
        temperature=_RETRY_TEMPERATURE,
        violation_sink=violation_sink,
    )
    # On a failed retry the terminal kind is the RETRY's failing kind (it may
    # differ from the first attempt's kind).
    if kind != BriefErrorKind.NONE:
        return None, kind
    return _stamp_first_attempt(brief, first_attempt_kind), kind


def _stamp_first_attempt(brief: dict | None, kind: BriefErrorKind) -> dict | None:
    """Record which failure the FIRST draw hit on a brief the retry recovered."""
    if brief is not None:
        brief[FIRST_ATTEMPT_KIND_KEY] = kind.value
    return brief


__all__ = [
    "FIRST_ATTEMPT_KIND_KEY",
    "FLASH_MODEL",
    "PRO_MODEL",
    "BriefErrorKind",
    "choose_model",
    "generate_brief",
    "generate_brief_with_retry",
]

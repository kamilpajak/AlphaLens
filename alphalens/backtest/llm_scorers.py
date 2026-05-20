"""Reference LLM scorer implementations for historical_validation.

Available implementations:

1. `gemini_flash_tractability_scorer` — a single Gemini 2.5 Flash call,
   ~$0.01-0.03 per ticker. Asks the model whether a name is "analysis-tractable":
   coherent business model, reasonable size, not a zombie/fraud.

2. `rule_and_gemini_hybrid_scorer` — deterministic rule first; Gemini only when
   the rule returns 'uncertain'. Cheapest because most picks land on the rule.

Both return an `LLMVerdict` matching the interface in `historical_validation.py`.

**Look-ahead bias**: LLMs are trained up to a fixed cutoff. If you test on
dates beyond the cutoff (e.g. 2026 with a Gemini 2.5 Pro early-2025 cutoff),
the model effectively "knows" the post-event outcome. For rigorous validation,
use a 2022-2023 window (pre-cutoff for all mainstream models).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from datetime import date

from alphalens.data.alt_data.gemini_client import GeminiClient, get_default_gemini_client

from .historical_validation import LLMVerdict

logger = logging.getLogger(__name__)


_TRACTABILITY_PROMPT = """\
You are evaluating whether a stock ticker is suitable for deep LLM analysis
in a momentum-trading pipeline. The pipeline will spend compute analyzing
accepted names; rejected names get skipped.

A name is ACCEPT-worthy if it has:
- Coherent, well-known business model (not a shell, SPAC, or obscure penny stock)
- Sensible size (typically $500M - $15B market cap, small/mid)
- Recent trading activity (not halted or delisted)
- Available fundamental context (analyst coverage, SEC filings, news flow)

A name is REJECT-worthy if it has:
- Zombie / no-business / fraud indicators
- Micro-cap illiquid (< $100M or < $1M daily volume)
- Very recent IPO or SPAC with no trading history
- Heavily shorted with contentious thesis (better deferred)

**Ticker**: {ticker}
**As-of date**: {asof}
**Themes it was surfaced under**: {themes}
**Momentum rank**: {rank} (1 = highest in screener)
**Composite momentum score**: {score:.3f}

Respond with: verdict (one of accept/reject/uncertain), confidence (0.0-1.0),
and one-sentence reasoning explaining tractability.
"""


def _parse_gemini_response(raw: str) -> dict | None:
    """Parse JSON from Gemini text. Falls back to greedy `{...}` extraction on preamble."""
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


# Approximate Flash cost: input ~300 tokens @ $0.075/1M + output ~50 tokens @ $0.30/1M
_GEMINI_FLASH_APPROX_COST_USD = (300 * 0.075 + 50 * 0.30) / 1_000_000

_TRACTABILITY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject", "uncertain"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reasoning"],
}


def gemini_flash_tractability_scorer(
    ticker: str,
    asof: date,
    context: Mapping,
    model_name: str = "gemini-2.5-flash",
    api_key: str | None = None,
    gemini_client: GeminiClient | None = None,
) -> LLMVerdict:
    """Single Gemini Flash call ~$0.01-0.03 per ticker.

    Routes through the canonical
    :class:`alphalens.data.alt_data.gemini_client.GeminiClient`. Pass
    ``gemini_client=`` for tests; pass ``api_key=`` for ad-hoc one-off
    use; omit both to fall back to ``get_default_gemini_client()`` which
    reads ``GOOGLE_API_KEY`` once per process.

    `context` must include: rank, momentum_score, themes.
    """
    if gemini_client is None:
        gemini_client = GeminiClient(api_key=api_key) if api_key else get_default_gemini_client()

    prompt = _TRACTABILITY_PROMPT.format(
        ticker=ticker,
        asof=asof.isoformat(),
        themes=", ".join(context.get("themes") or []) or "none",
        rank=context.get("rank", 99),
        score=context.get("momentum_score", 0.0),
    )

    t0 = time.perf_counter()
    try:
        response = gemini_client.generate_content(
            model=model_name,
            contents=prompt,
            config=gemini_client.build_config(
                response_mime_type="application/json",
                response_schema=_TRACTABILITY_RESPONSE_SCHEMA,
                temperature=0.0,
                max_output_tokens=2000,
            ),
        )
    except Exception as exc:
        return LLMVerdict(
            verdict="uncertain",
            confidence=0.0,
            reasoning=f"Gemini API error: {exc}",
            latency_sec=time.perf_counter() - t0,
            cost_usd=0.0,
        )
    latency = time.perf_counter() - t0

    raw = response.text if response else ""
    parsed = _parse_gemini_response(raw)
    if parsed is None:
        return LLMVerdict(
            verdict="uncertain",
            confidence=0.0,
            reasoning=f"parse error | raw: {raw[:120]}",
            latency_sec=latency,
            cost_usd=0.0,
        )

    verdict = parsed.get("verdict", "uncertain")
    if verdict not in ("accept", "reject", "uncertain"):
        verdict = "uncertain"

    return LLMVerdict(
        verdict=verdict,  # type: ignore[arg-type]
        confidence=float(parsed.get("confidence", 0.0)),
        reasoning=str(parsed.get("reasoning", ""))[:280],
        latency_sec=latency,
        cost_usd=_GEMINI_FLASH_APPROX_COST_USD,
    )


def rule_and_gemini_hybrid_scorer(
    ticker: str,
    asof: date,
    context: Mapping,
) -> LLMVerdict:
    """Hybrid: rules first, Gemini only when the rule returns 'uncertain'.

    Cheap (most picks land on the deterministic rule); LLM fallback only for
    borderline cases.
    """
    from .historical_validation import rule_based_tractability_scorer

    rule_verdict = rule_based_tractability_scorer(ticker, asof, context)
    if rule_verdict.verdict != "uncertain":
        return rule_verdict

    # Fallback to Gemini
    try:
        return gemini_flash_tractability_scorer(ticker, asof, context)
    except RuntimeError:
        return rule_verdict

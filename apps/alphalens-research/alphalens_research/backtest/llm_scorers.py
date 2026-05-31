# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false
"""Reference LLM scorer implementations for historical_validation.

Available implementations:

1. `llm_tractability_scorer` — a single DeepSeek v4-flash call,
   ~$0.01-0.03 per ticker. Asks the model whether a name is "analysis-tractable":
   coherent business model, reasonable size, not a zombie/fraud.

2. `rule_and_llm_hybrid_scorer` — deterministic rule first; the LLM only when
   the rule returns 'uncertain'. Cheapest because most picks land on the rule.

Both return an `LLMVerdict` matching the interface in `historical_validation.py`.

**Look-ahead bias**: LLMs are trained up to a fixed cutoff. If you test on
dates beyond the model's training cutoff, the model effectively "knows" the
post-event outcome. For rigorous validation, use a 2022-2023 window
(pre-cutoff for all mainstream models).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from datetime import date
from typing import Any

from alphalens_pipeline.data.alt_data.openrouter_client import (
    OpenRouterClient,
    get_default_openrouter_client,
)

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


def _parse_llm_response(raw: str) -> dict[str, Any] | None:
    """Parse JSON from LLM text. Falls back to greedy `{...}` extraction on preamble."""
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


# Approximate Flash cost: DeepSeek v4-flash $0.10/M in + $0.20/M out
# (input ~300 tokens @ $0.10/1M + output ~50 tokens @ $0.20/1M)
_FLASH_APPROX_COST_USD = (300 * 0.10 + 50 * 0.20) / 1_000_000

_TRACTABILITY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject", "uncertain"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reasoning"],
}


def llm_tractability_scorer(
    ticker: str,
    asof: date,
    context: Mapping[str, Any],
    model_name: str = "deepseek/deepseek-v4-flash",
    api_key: str | None = None,
    llm_client: OpenRouterClient | None = None,
) -> LLMVerdict:
    """Single DeepSeek v4-flash call ~$0.01-0.03 per ticker.

    Routes through the canonical
    :class:`alphalens_pipeline.data.alt_data.openrouter_client.OpenRouterClient`. Pass
    ``llm_client=`` for tests; pass ``api_key=`` for ad-hoc one-off
    use; omit both to fall back to ``get_default_openrouter_client()`` which
    reads ``OPENROUTER_API_KEY`` once per process.

    `context` must include: rank, momentum_score, themes.
    """
    prompt = _TRACTABILITY_PROMPT.format(
        ticker=ticker,
        asof=asof.isoformat(),
        themes=", ".join(context.get("themes") or []) or "none",
        rank=context.get("rank", 99),
        score=context.get("momentum_score", 0.0),
    )

    t0 = time.perf_counter()
    try:
        # Client init inside try so missing-SDK / missing-key failures
        # degrade to LLMVerdict('uncertain') rather than crashing the
        # historical_validation loop (zen pre-merge HIGH 2026-05-20).
        if llm_client is None:
            llm_client = (
                OpenRouterClient(api_key=api_key) if api_key else get_default_openrouter_client()
            )
        response = llm_client.generate_content(
            model=model_name,
            contents=prompt,
            config=llm_client.build_config(
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
            reasoning=f"LLM API error: {exc}",
            latency_sec=time.perf_counter() - t0,
            cost_usd=0.0,
        )
    latency = time.perf_counter() - t0

    raw = response.text if response else ""
    parsed = _parse_llm_response(raw)
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
        cost_usd=_FLASH_APPROX_COST_USD,
    )


def rule_and_llm_hybrid_scorer(
    ticker: str,
    asof: date,
    context: Mapping[str, Any],
) -> LLMVerdict:
    """Hybrid: rules first, the LLM only when the rule returns 'uncertain'.

    Cheap (most picks land on the deterministic rule); LLM fallback only for
    borderline cases.
    """
    from .historical_validation import rule_based_tractability_scorer

    rule_verdict = rule_based_tractability_scorer(ticker, asof, context)
    if rule_verdict.verdict != "uncertain":
        return rule_verdict

    # Fallback to LLM
    try:
        return llm_tractability_scorer(ticker, asof, context)
    except RuntimeError:
        return rule_verdict

"""Reference LLM scorer implementations for historical_validation.

Two available implementations:

1. `gemini_flash_tractability_scorer` — a single Gemini 2.5 Flash call,
   ~$0.01-0.03 per ticker. Asks the model whether a name is "analysis-tractable":
   coherent business model, reasonable size, not a zombie/fraud.

2. `tradingagents_reduced_scorer` — a full TradingAgents run with
   `selected_analysts=["market", "news"]`. ~$0.50-1 per ticker. Production-parity;
   skips fundamentals (Alpha Vantage bottleneck) and social.

Both return an `LLMVerdict` matching the interface in `historical_validation.py`.

**Look-ahead bias**: LLMs are trained up to a fixed cutoff. If you test on
dates beyond the cutoff (e.g. 2026 with a Gemini 2.5 Pro early-2025 cutoff),
the model effectively "knows" the post-event outcome. For rigorous validation,
use a 2022-2023 window (pre-cutoff for all mainstream models).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from datetime import date

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


def _load_genai_sdk():
    """Import google-genai SDK; raise with actionable message if absent."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "google-genai SDK not installed. `uv add google-genai` or use an alternative scorer."
        ) from exc
    return genai, types


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
) -> LLMVerdict:
    """Single Gemini Flash call ~$0.01-0.03 per ticker.

    Uses the Google Gen AI SDK (google-genai). Raises if the API key is missing
    or the SDK is not installed.

    `context` must include: rank, momentum_score, themes.
    """
    api_key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set — cannot call Gemini")

    genai, types = _load_genai_sdk()
    prompt = _TRACTABILITY_PROMPT.format(
        ticker=ticker,
        asof=asof.isoformat(),
        themes=", ".join(context.get("themes") or []) or "none",
        rank=context.get("rank", 99),
        score=context.get("momentum_score", 0.0),
    )

    client = genai.Client(api_key=api_key)
    t0 = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
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


def tradingagents_reduced_scorer(
    ticker: str,
    asof: date,
    context: Mapping,
    selected_analysts: tuple[str, ...] = ("market", "news"),
) -> LLMVerdict:
    """Run TradingAgentsGraph with a reduced analyst set.

    Note: debate, risk management, and portfolio manager are HARDCODED in
    setup.py — they cannot be disabled through `selected_analysts`. Real cost
    is ~$0.50-1 per ticker on the Gemini paid tier, much more than a single
    Flash tractability call.

    Use this when you want a REALISTIC PRODUCTION EVAL (as if running Layer 3
    on the pick).
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    from alphalens.core.config_gemini import build_gemini_config

    config = build_gemini_config()
    graph = TradingAgentsGraph(
        selected_analysts=list(selected_analysts),
        debug=False,
        config=config,
    )

    t0 = time.perf_counter()
    try:
        _final_state, decision = graph.propagate(ticker, asof.isoformat())
    except Exception as exc:
        return LLMVerdict(
            verdict="uncertain",
            confidence=0.0,
            reasoning=f"TradingAgents error: {exc}",
            latency_sec=time.perf_counter() - t0,
            cost_usd=0.0,
        )
    latency = time.perf_counter() - t0

    decision_upper = str(decision).upper()
    if any(k in decision_upper for k in ("BUY", "OVERWEIGHT")):
        v = "accept"
    elif any(k in decision_upper for k in ("SELL", "UNDERWEIGHT")):
        v = "reject"
    else:
        v = "uncertain"

    # Rough cost estimate: ~10 LLM calls × ~1k tokens each × Gemini 2.5 Flash blended
    cost_approx = (
        10 * 1500 * (0.30 + 0.075) / 1_000_000
    )  # ~$0.006 actually lol; for Pro model it's ~$0.50+

    return LLMVerdict(
        verdict=v,  # type: ignore[arg-type]
        confidence=0.6,
        reasoning=f"TradingAgents verdict={decision_upper[:80]}",
        latency_sec=latency,
        cost_usd=cost_approx,
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

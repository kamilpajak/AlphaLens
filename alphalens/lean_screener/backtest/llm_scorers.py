"""Reference LLM scorer implementations dla historical_validation.

Dwie dostępne implementacje:

1. `gemini_flash_tractability_scorer` — pojedynczy Gemini 2.5 Flash call,
   ~$0.01-0.03 per ticker. Pyta model czy nazwa jest "analysis-tractable"
   — czyli czy ma coherent business model, sensowny size, nie zombie/fraud.

2. `tradingagents_reduced_scorer` — odpalenie pełnego TradingAgents
   z `selected_analysts=["market", "news"]`. ~$0.50-1 per ticker. Realistic
   production-parity; skip fundamentals (= Alpha Vantage bottleneck) + social.

Oba zwracają `LLMVerdict` zgodny z interfejsem w `historical_validation.py`.

**Look-ahead bias**: LLM trenowany jest na danych do określonego cutoff.
Jeśli testujesz na datach > cutoff (np. 2026 w Gemini 2.5 Pro cutoff early
2025), model efektywnie "wie" post-event. Dla rigorous validation — użyj
okna 2022-2023 (pre-cutoff dla wszystkich mainstream models).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from typing import Mapping

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


def gemini_flash_tractability_scorer(
    ticker: str,
    asof: date,
    context: Mapping,
    model_name: str = "gemini-2.5-flash",
    api_key: str | None = None,
) -> LLMVerdict:
    """Single Gemini Flash call ~$0.01-0.03 per ticker.

    Używa Google Gen AI SDK (google-genai). Fallback: raise jeśli brak API key
    lub SDK nie zainstalowane.

    `context` powinien mieć: rank, momentum_score, themes.
    """
    api_key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set — cannot call Gemini")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "google-genai SDK not installed. `uv add google-genai` or use "
            "an alternative scorer."
        ) from exc

    prompt = _TRACTABILITY_PROMPT.format(
        ticker=ticker,
        asof=asof.isoformat(),
        themes=", ".join(context.get("themes") or []) or "none",
        rank=context.get("rank", 99),
        score=context.get("momentum_score", 0.0),
    )

    # Enforced schema — Gemini musi zwrócić dokładnie tę strukturę
    response_schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["accept", "reject", "uncertain"]},
            "confidence": {"type": "number"},
            "reasoning": {"type": "string"},
        },
        "required": ["verdict", "confidence", "reasoning"],
    }

    client = genai.Client(api_key=api_key)
    t0 = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0.0,
                max_output_tokens=2000,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        latency = time.perf_counter() - t0
        return LLMVerdict(
            verdict="uncertain",
            confidence=0.0,
            reasoning=f"Gemini API error: {exc}",
            latency_sec=latency,
            cost_usd=0.0,
        )
    latency = time.perf_counter() - t0

    # Strategia parsowania: spróbuj raw, potem fallback ze znalezieniem pierwszego '{'
    raw = response.text if response else ""
    parsed = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Fallback: wyciągnij JSON z preambuły
        brace_start = raw.find("{")
        brace_end = raw.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                parsed = json.loads(raw[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

    if parsed is None:
        return LLMVerdict(
            verdict="uncertain",
            confidence=0.0,
            reasoning=f"parse error | raw: {raw[:120]}",
            latency_sec=latency,
            cost_usd=0.0,
        )

    verdict = parsed.get("verdict", "uncertain")
    confidence = float(parsed.get("confidence", 0.0))
    reasoning = str(parsed.get("reasoning", ""))[:280]

    if verdict not in ("accept", "reject", "uncertain"):
        verdict = "uncertain"

    # Approximate Flash cost: input ~300 tokens @ $0.075/1M + output ~50 tokens @ $0.30/1M
    cost_approx = (300 * 0.075 + 50 * 0.30) / 1_000_000

    return LLMVerdict(
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        reasoning=reasoning,
        latency_sec=latency,
        cost_usd=cost_approx,
    )


def tradingagents_reduced_scorer(
    ticker: str,
    asof: date,
    context: Mapping,
    selected_analysts: tuple[str, ...] = ("market", "news"),
) -> LLMVerdict:
    """Wywołuje TradingAgentsGraph z reduced analyst set.

    Uwaga: debate, risk management, portfolio manager są HARDCODED w setup.py —
    nie można ich wyłączyć przez `selected_analysts`. Realny koszt ~$0.50-1
    per ticker na Gemini paid tier, znacznie więcej niż Flash tractability call.

    Użyć gdy chcesz REALISTIC PRODUCTION EVAL (jakby odpalić Layer 3 na picku).
    """
    from alphalens.config_gemini import build_gemini_config
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = build_gemini_config()
    graph = TradingAgentsGraph(
        selected_analysts=list(selected_analysts),
        debug=False,
        config=config,
    )

    t0 = time.perf_counter()
    try:
        _final_state, decision = graph.propagate(ticker, asof.isoformat())
    except Exception as exc:  # noqa: BLE001
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
    cost_approx = 10 * 1500 * (0.30 + 0.075) / 1_000_000  # ~$0.006 actually lol; for Pro model it's ~$0.50+

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
    """Hybrid: rules first, Gemini tylko gdy rule → 'uncertain'.

    Tanie (większość picks lands na deterministic rule), LLM fallback dla
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

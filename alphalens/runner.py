"""TradingAgents runner: single place that instantiates the graph and invokes propagate.

Wraps the vendored TradingAgents call in an `AnalysisResult` with timing + metadata.
Kept deliberately thin so the vendored code stays untouched.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .candidates import AnalysisResult, Candidate
from .config_gemini import build_gemini_config

logger = logging.getLogger(__name__)


GraphFactory = Callable[[dict[str, Any]], Any]
ConfigBuilder = Callable[[], dict[str, Any]]


def _default_graph_factory(config: dict[str, Any]):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(debug=False, config=config)


def _format_themed(p: dict[str, Any]) -> str:
    score = p.get("momentum_score", "?")
    themes = ",".join(p.get("themes") or [])
    scorer = p.get("scorer", "momentum")
    return f"Triggered by themed screener (scorer={scorer}): score={score}, themes={themes}"


def _format_watchdog_sec(p: dict[str, Any]) -> str:
    return (
        f"Triggered by SEC filing: Form {p.get('form', '?')}, "
        f"accession {p.get('accession', '?')}"
    )


def _format_prescreener(p: dict[str, Any]) -> str:
    return (
        f"Triggered by prescreener: rank {p.get('rank', '?')}, "
        f"composite={p.get('composite_score', '?')}"
    )


# Keys are Candidate.source values. Themed pipeline emits both "momentum" and
# "early-stage" depending on the injected scorer — same formatter for both.
_FORMATTERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "momentum": _format_themed,
    "early-stage": _format_themed,
    "watchdog_sec": _format_watchdog_sec,
    "prescreener": _format_prescreener,
}


def build_trigger_context(candidate: "Candidate") -> str:
    """Per-source prose describing why this ticker reached Layer 3.

    Logged today; forwarded into the TradingAgents graph once an upstream
    injection hook lands (planned PR). Keep it short — intended for a system
    message prepended to the first analyst turn.
    """
    formatter = _FORMATTERS.get(candidate.source)
    if formatter is None:
        return ""
    return formatter(candidate.payload)


class TradingAgentsRunner:
    def __init__(
        self,
        config_builder: ConfigBuilder = build_gemini_config,
        graph_factory: GraphFactory = _default_graph_factory,
    ):
        self._config_builder = config_builder
        self._graph_factory = graph_factory

    def run(self, candidate: Candidate, *, candidate_id: int) -> AnalysisResult:
        config = self._config_builder()
        graph = self._graph_factory(config)
        date_str = datetime.now(timezone.utc).date().isoformat()

        logger.info(
            "ta_run start ticker=%s source=%s candidate_id=%s",
            candidate.ticker,
            candidate.source,
            candidate_id,
        )

        trigger_context = build_trigger_context(candidate)
        if trigger_context:
            # Logged only today. Upstream PR will forward this into the graph's
            # initial state so the first analyst turn sees it.
            logger.info(
                "trigger_context ticker=%s source=%s context=%r",
                candidate.ticker,
                candidate.source,
                trigger_context,
            )

        t0 = time.perf_counter()
        final_state, decision = graph.propagate(candidate.ticker, date_str)
        duration = time.perf_counter() - t0

        model_used = str(config.get("deep_think_llm", ""))
        result = AnalysisResult(
            candidate_id=candidate_id,
            ticker=candidate.ticker,
            source=candidate.source,
            rating=str(decision),
            duration_sec=duration,
            cost_usd=None,  # populated once token accounting lands
            model_used=model_used,
            completed_at=datetime.now(timezone.utc),
            final_state=final_state or {},
        )
        logger.info(
            "ta_run done ticker=%s rating=%s duration_s=%.2f",
            result.ticker,
            result.rating,
            result.duration_sec,
        )
        return result

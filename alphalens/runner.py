"""TradingAgents runner: single place that instantiates the graph and invokes propagate.

Wraps the vendored TradingAgents call in an `AnalysisResult` with timing + metadata.
Kept deliberately thin so the vendored code stays untouched.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Callable

from .candidates import AnalysisResult, Candidate
from .config_gemini import build_gemini_config

logger = logging.getLogger(__name__)


GraphFactory = Callable[..., Any]
ConfigBuilder = Callable[[], dict[str, Any]]


def _default_graph_factory(config: dict[str, Any], selected_analysts: list[str] | None = None):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    kwargs: dict[str, Any] = {"debug": False, "config": config}
    if selected_analysts is not None:
        kwargs["selected_analysts"] = list(selected_analysts)
    return TradingAgentsGraph(**kwargs)


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


def _format_lean(p: dict[str, Any]) -> str:
    return (
        f"Triggered by Lean screener (archived): rank {p.get('rank', '?')}, "
        f"score={p.get('score', '?')}"
    )


# Keys are Candidate.source values. Themed pipeline emits both "momentum" and
# "early-stage" depending on the injected scorer — same formatter for both.
_FORMATTERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "momentum": _format_themed,
    "early-stage": _format_themed,
    "watchdog_sec": _format_watchdog_sec,
    "prescreener": _format_prescreener,
    "lean": _format_lean,
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

    def run(
        self,
        candidate: Candidate,
        *,
        candidate_id: int,
        curr_date: date | None = None,
        selected_analysts: list[str] | None = None,
    ) -> AnalysisResult:
        """Run Layer 3 on a candidate.

        Args:
            candidate: the ticker and source metadata.
            candidate_id: row id in the queue (for tracing).
            curr_date: point-in-time replay date; default today. Drops news/social
                look-ahead risk for historical replays (though social agent's date
                handling is not fully PIT-safe — exclude it via selected_analysts
                for clean replay).
            selected_analysts: subset of ["market", "social", "news", "fundamentals"].
                None → upstream default (all four). Pass without "social" for
                PIT-clean replays on historical dates.
        """
        config = self._config_builder()
        # Only forward selected_analysts when explicitly provided — preserves
        # backward-compat with graph_factory callables that take just (config).
        if selected_analysts is not None:
            graph = self._graph_factory(config, selected_analysts=selected_analysts)
        else:
            graph = self._graph_factory(config)
        replay_date = curr_date or datetime.now(timezone.utc).date()
        date_str = replay_date.isoformat()

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

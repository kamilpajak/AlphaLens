"""Historical validation: czy LLM rejections correlate z subsequent underperformance.

Phase 0 audit per Perplexity recommendation (2026-04-19):
"Audit last 60 days: Of your top-5 daily candidates, what % led to actionable
insights? What % were 'can't find edge'?"

Ten moduł iteruje over historical top-N picks (z `BacktestReport` lub
`MomentumHistoryStore`), puszcza pluggable LLM scorer na każdy, porównuje:
- accept_rate: % picks gdzie LLM daje approve
- accept_hit_rate: mean forward return w accepted
- reject_hit_rate: mean forward return w rejected
- delta: accept_hit - reject_hit; >0 znaczy LLM dodaje value, ~0 znaczy noise

Scorer function może być:
- Custom lightweight Gemini call (~2-3 LLM calls, $0.01-0.05/analysis)
- TradingAgentsGraph z reduced analysts (~10 calls, $0.50-1/analysis)
- Cokolwiek innego co zwraca `LLMVerdict`
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Literal, Mapping

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

Verdict = Literal["accept", "reject", "uncertain"]


@dataclass(frozen=True)
class LLMVerdict:
    """Output of the pluggable scorer function for one (ticker, date) pair."""

    verdict: Verdict
    confidence: float        # [0, 1]; 1 = sure, 0 = pure noise
    reasoning: str = ""      # optional — for post-hoc audit
    latency_sec: float = 0.0
    cost_usd: float = 0.0    # approximate; -1 if unknown


ScorerFn = Callable[[str, date, Mapping], LLMVerdict]
"""
Signature: scorer(ticker, asof_date, context) -> LLMVerdict.

`context` is a dict with at minimum:
- `scorer_name`: str          # which Layer 2b scorer produced this pick
- `momentum_score`: float
- `themes`: list[str]
- `rank`: int                  # 1 = top
"""


@dataclass(frozen=True)
class PickRecord:
    """One historical pick + actual forward return."""

    asof_date: date
    ticker: str
    rank: int
    momentum_score: float
    themes: list[str]
    forward_return: float       # actual realized, e.g. 5-day


@dataclass
class ValidationResult:
    """Aggregate stats across all evaluated picks."""

    n_total: int
    n_accept: int
    n_reject: int
    n_uncertain: int
    accept_rate: float                        # n_accept / n_total
    accept_mean_return: float                 # mean forward_return among accepted
    reject_mean_return: float                 # mean forward_return among rejected
    delta_accept_minus_reject: float          # key metric: >0 = LLM helps
    accept_hit_rate: float                    # % of accepted with fwd_return > 0
    reject_hit_rate: float                    # % of rejected with fwd_return > 0
    accept_sharpe_proxy: float                # mean/std of accepted fwd returns
    reject_sharpe_proxy: float
    total_llm_cost_usd: float
    total_llm_latency_sec: float
    per_pick_evaluations: list[dict] = field(default_factory=list)  # audit trail


def evaluate_historical_picks(
    picks: Iterable[PickRecord],
    scorer_fn: ScorerFn,
    extra_context_fn: Callable[[PickRecord], Mapping] | None = None,
    progress_every: int = 20,
) -> ValidationResult:
    """Run the pluggable scorer over each historical pick, aggregate results.

    `scorer_fn` is called once per pick. Build it around the LLM of your
    choice (custom Gemini Flash call, TradingAgents with reduced analysts,
    rule-based fallback for testing). Must return `LLMVerdict`.

    `extra_context_fn` optionally enriches context beyond the basics from
    `PickRecord` (e.g., add SEC filing recency, analyst activity).
    """
    pick_list = list(picks)
    n = len(pick_list)
    if n == 0:
        return ValidationResult(
            n_total=0, n_accept=0, n_reject=0, n_uncertain=0,
            accept_rate=0.0, accept_mean_return=0.0, reject_mean_return=0.0,
            delta_accept_minus_reject=0.0, accept_hit_rate=0.0, reject_hit_rate=0.0,
            accept_sharpe_proxy=0.0, reject_sharpe_proxy=0.0,
            total_llm_cost_usd=0.0, total_llm_latency_sec=0.0,
        )

    rows: list[dict] = []
    total_cost = 0.0
    total_latency = 0.0

    for idx, pick in enumerate(pick_list):
        context = {
            "scorer_name": "layer2b_momentum",
            "momentum_score": pick.momentum_score,
            "themes": list(pick.themes),
            "rank": pick.rank,
        }
        if extra_context_fn is not None:
            extra = extra_context_fn(pick) or {}
            context.update(extra)

        t0 = time.perf_counter()
        try:
            verdict = scorer_fn(pick.ticker, pick.asof_date, context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scorer raised on %s @ %s: %s", pick.ticker, pick.asof_date, exc)
            verdict = LLMVerdict(verdict="uncertain", confidence=0.0, reasoning=f"error: {exc}")
        latency = time.perf_counter() - t0

        rows.append({
            "date": pick.asof_date.isoformat(),
            "ticker": pick.ticker,
            "rank": pick.rank,
            "momentum_score": pick.momentum_score,
            "themes": ",".join(pick.themes),
            "forward_return": pick.forward_return,
            "verdict": verdict.verdict,
            "confidence": verdict.confidence,
            "reasoning": verdict.reasoning,
            "llm_cost": verdict.cost_usd,
            "llm_latency_sec": verdict.latency_sec or latency,
        })
        total_cost += max(verdict.cost_usd, 0.0)
        total_latency += verdict.latency_sec or latency

        if (idx + 1) % progress_every == 0:
            logger.info("validation progress: %d/%d, cost so far $%.2f", idx + 1, n, total_cost)

    df = pd.DataFrame(rows)
    accepted = df[df["verdict"] == "accept"]
    rejected = df[df["verdict"] == "reject"]
    uncertain = df[df["verdict"] == "uncertain"]

    def _safe_mean(s: pd.Series) -> float:
        s = s.dropna()
        return float(s.mean()) if len(s) else 0.0

    def _safe_sharpe(s: pd.Series) -> float:
        s = s.dropna()
        if len(s) < 2:
            return 0.0
        std = float(s.std(ddof=1))
        if std < 1e-12:
            return 0.0
        return float(s.mean() / std) * np.sqrt(252)

    def _hit_rate(s: pd.Series) -> float:
        s = s.dropna()
        if len(s) == 0:
            return 0.0
        return float((s > 0).mean())

    return ValidationResult(
        n_total=n,
        n_accept=int(len(accepted)),
        n_reject=int(len(rejected)),
        n_uncertain=int(len(uncertain)),
        accept_rate=len(accepted) / n,
        accept_mean_return=_safe_mean(accepted["forward_return"]),
        reject_mean_return=_safe_mean(rejected["forward_return"]),
        delta_accept_minus_reject=(
            _safe_mean(accepted["forward_return"]) - _safe_mean(rejected["forward_return"])
        ),
        accept_hit_rate=_hit_rate(accepted["forward_return"]),
        reject_hit_rate=_hit_rate(rejected["forward_return"]),
        accept_sharpe_proxy=_safe_sharpe(accepted["forward_return"]),
        reject_sharpe_proxy=_safe_sharpe(rejected["forward_return"]),
        total_llm_cost_usd=total_cost,
        total_llm_latency_sec=total_latency,
        per_pick_evaluations=rows,
    )


def format_decision_matrix(result: ValidationResult) -> str:
    """Ludzko-czytelny raport z rekomendacją deploy / iterate / abandon."""
    lines = [
        f"=== Historical Validation Results ===",
        f"",
        f"Evaluated:        {result.n_total} picks",
        f"  accepted:       {result.n_accept} ({result.accept_rate * 100:.1f}%)",
        f"  rejected:       {result.n_reject}",
        f"  uncertain:      {result.n_uncertain}",
        f"",
        f"Forward returns:",
        f"  accepted mean:  {result.accept_mean_return * 100:+.3f}%",
        f"  rejected mean:  {result.reject_mean_return * 100:+.3f}%",
        f"  **delta**:      {result.delta_accept_minus_reject * 100:+.3f}% (accept - reject)",
        f"",
        f"Hit rates (fwd return > 0):",
        f"  accepted:       {result.accept_hit_rate * 100:.1f}%",
        f"  rejected:       {result.reject_hit_rate * 100:.1f}%",
        f"",
        f"Sharpe proxy (mean/std × √252):",
        f"  accepted:       {result.accept_sharpe_proxy:+.2f}",
        f"  rejected:       {result.reject_sharpe_proxy:+.2f}",
        f"",
        f"LLM cost:         ${result.total_llm_cost_usd:.2f}",
        f"LLM latency:      {result.total_llm_latency_sec:.1f} s "
        f"({result.total_llm_latency_sec / max(result.n_total, 1):.2f} s/pick)",
        f"",
        f"=== Decision ===",
    ]

    delta = result.delta_accept_minus_reject
    hit_delta = result.accept_hit_rate - result.reject_hit_rate

    if delta > 0.005 and hit_delta > 0.05:
        lines.append("**DEPLOY** — LLM reject-rate correlates with underperformance")
        lines.append(f"  (delta {delta * 100:+.2f}% AND hit-rate delta {hit_delta * 100:+.1f} p.p.)")
        lines.append("  → Integracja 3-tier adaptive architecture warta kosztów.")
    elif delta > 0.002 or hit_delta > 0.02:
        lines.append("**ITERATE** — marginal signal, wymaga większej sample lub lepszego promptu")
        lines.append(f"  (delta {delta * 100:+.2f}%, hit-rate delta {hit_delta * 100:+.1f} p.p.)")
        lines.append("  → Rozszerzyć sample do 90+ dni, przetestować różne scorer prompts.")
    else:
        lines.append("**SKIP** — LLM nie dodaje signal value vs rule-based alone")
        lines.append(f"  (delta {delta * 100:+.2f}%, hit-rate delta {hit_delta * 100:+.1f} p.p.)")
        lines.append("  → Status quo (rule-based screener + Layer 3 post-analysis) jest optimal")
        lines.append("    dla solo retail. Nie integrować pre-screening filter.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers to build PickRecord lists from existing sources
# ---------------------------------------------------------------------------


def picks_from_backtest_report(report) -> list[PickRecord]:
    """Ekstraktuj PickRecord z BacktestReport.daily_results.

    Używa `top_n_forward_returns` jako holding-period fwd return (5-day).
    """
    from alphalens.lean_screener.backtest.engine import BacktestReport  # late import

    if not isinstance(report, BacktestReport):
        raise TypeError(f"expected BacktestReport, got {type(report)}")

    out: list[PickRecord] = []
    for r in report.daily_results:
        for rank_idx, (ticker, score, fwd) in enumerate(
            zip(r.top_n_tickers, r.top_n_scores, r.top_n_forward_returns), start=1
        ):
            if fwd is None or (isinstance(fwd, float) and np.isnan(fwd)):
                continue
            out.append(PickRecord(
                asof_date=r.date.date(),
                ticker=ticker,
                rank=rank_idx,
                momentum_score=float(score),
                themes=[],  # scored_frames would carry themes; report doesn't
                forward_return=float(fwd),
            ))
    return out


def picks_from_history_store(
    store, days: int = 60, top_n: int = 5
) -> list[PickRecord]:
    """Ekstraktuj z MomentumHistoryStore (z produkcji Layer 2b).

    Musi byc `momentum_history.db` wypełniony przez daily runs. Forward return
    liczymy z późniejszych picks — jeśli ticker pojawił się w historii później
    też, używamy późniejszej ceny jako proxy. Fallback: None (skip).
    """
    from alphalens.momentum_screener.history_store import MomentumHistoryStore  # late

    if not isinstance(store, MomentumHistoryStore):
        raise TypeError(f"expected MomentumHistoryStore, got {type(store)}")

    timeline = store.picks_timeline(days=days)
    timeline = timeline[timeline["rank"] <= top_n]
    if timeline.empty:
        return []

    # Forward return z HistoryStore (OHLCV)
    from alphalens.lean_screener.backtest.history_store import HistoryStore
    from alphalens.lean_screener.config import DATA_DIR

    hs = HistoryStore(DATA_DIR)
    tickers = sorted(timeline["ticker"].unique())
    hs.load(tickers)

    out: list[PickRecord] = []
    for _, row in timeline.iterrows():
        asof = date.fromisoformat(str(row["run_date"]))
        fwd = hs.forward_return(row["ticker"], asof, horizon=5)
        if fwd is None:
            continue
        themes = [t for t in str(row["themes"]).split(",") if t]
        out.append(PickRecord(
            asof_date=asof,
            ticker=str(row["ticker"]),
            rank=int(row["rank"]),
            momentum_score=float(row["momentum_score"]),
            themes=themes,
            forward_return=fwd,
        ))
    return out


# ---------------------------------------------------------------------------
# Reference scorer implementations — pluggable baselines
# ---------------------------------------------------------------------------


def rule_based_tractability_scorer(
    ticker: str, asof: date, context: Mapping
) -> LLMVerdict:
    """Deterministic baseline — **żaden** LLM, pure rules.

    Accept criteria:
    - rank in top 2 OR momentum_score > 0.7
    - NOT single-theme concentration (jeśli w context są theme_weights)

    Używany jako floor — jeśli LLM nie pokonuje tego scoringu w delta,
    LLM nie dodaje value.
    """
    rank = context.get("rank", 99)
    score = context.get("momentum_score", 0.0)
    themes = context.get("themes") or []

    if rank <= 2 or score > 0.7:
        return LLMVerdict(
            verdict="accept",
            confidence=0.8,
            reasoning=f"rule: rank={rank}, score={score:.2f}",
            cost_usd=0.0,
        )
    if len(themes) == 0:
        return LLMVerdict(
            verdict="uncertain",
            confidence=0.5,
            reasoning="no theme classification",
            cost_usd=0.0,
        )
    return LLMVerdict(
        verdict="reject",
        confidence=0.6,
        reasoning=f"rule: rank={rank} > 2 AND score={score:.2f} < 0.7",
        cost_usd=0.0,
    )

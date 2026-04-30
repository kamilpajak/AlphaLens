"""Single-year pilot runner for GuruAgent v2.

For one evaluation year: (a) randomly sample ``sample_size`` tickers from the
S&P 500 PIT universe, (b) build financial context for each, (c) score via
``GuruScorer`` (LLM), (d) pick top-N by conviction, (e) simulate equal-weight
buy-and-hold for 252 trading days, (f) return outperformance vs benchmark.

The multi-year orchestrator (Phase 5, report.py) aggregates these single-year
results across 2018/2020/2022/2024.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pandas as pd

from alphalens.archive.guru.llm_scorer import ConvictionResult

logger = logging.getLogger(__name__)

CONTEXT_BUILDER_TYPE = Callable[..., dict | None]


def sample_tickers(universe: Sequence[str], *, size: int, seed: int) -> list[str]:
    if size > len(universe):
        raise ValueError(f"sample size {size} exceeds universe size {len(universe)}")
    rng = random.Random(seed)
    return rng.sample(list(universe), size)


@dataclass(frozen=True)
class SingleYearResult:
    year: int
    asof: pd.Timestamp
    picks: list[ConvictionResult]
    portfolio_return: float
    benchmark_return: float
    outperformance: float
    total_cost_usd: float
    n_scored: int
    n_skipped: int = 0
    skipped_tickers: tuple[str, ...] = field(default_factory=tuple)


def _compute_1yr_return(
    close: pd.Series, entry_date: pd.Timestamp, holding_days: int = 252
) -> float | None:
    idx = close.index
    future = idx[idx >= entry_date]
    if len(future) < holding_days + 1:
        return None
    entry = float(close.loc[future[0]])
    exit_ = float(close.loc[future[holding_days]])
    if entry == 0:
        return None
    return exit_ / entry - 1.0


def _load_price_series(price_store, ticker: str) -> pd.Series | None:
    """Return ``close`` series for ticker, or None if unavailable."""
    try:
        df = price_store.full(ticker)
    except KeyError:
        return None
    if df is None or df.empty:
        return None
    return df["close"]


def _score_one_ticker(
    *,
    ticker: str,
    asof: pd.Timestamp,
    price_store,
    scorer,
    context_builder: CONTEXT_BUILDER_TYPE,
) -> ConvictionResult | None:
    """Build context + invoke scorer for a single ticker. None on any data/scorer failure."""
    price_series = _load_price_series(price_store, ticker)
    if price_series is None:
        return None
    try:
        ctx = context_builder(ticker=ticker, asof=asof, price_series=price_series)
    except Exception as exc:
        logger.warning("context_builder failed for %s: %s", ticker, exc)
        return None
    if ctx is None:
        return None
    context_text = ctx if isinstance(ctx, str) else str(ctx)
    try:
        return scorer.score(ticker=ticker, asof=asof, context_text=context_text)
    except Exception as exc:
        logger.warning("Scorer failed for %s: %s", ticker, exc)
        return None


def _portfolio_return(picks: list[ConvictionResult], price_store, asof: pd.Timestamp) -> float:
    returns: list[float] = []
    for p in picks:
        try:
            price_df = price_store.full(p.ticker)
        except KeyError:
            continue
        ret = _compute_1yr_return(price_df["close"], asof)
        if ret is not None:
            returns.append(ret)
    return sum(returns) / len(returns) if returns else 0.0


def run_single_year(
    *,
    year: int,
    universe: Sequence[str],
    sample_size: int,
    top_n: int,
    seed: int,
    scorer,
    context_builder: CONTEXT_BUILDER_TYPE,
    price_store,
    benchmark: str = "SPY",
) -> SingleYearResult:
    asof = pd.Timestamp(f"{year}-01-01")
    sampled = sample_tickers(universe, size=sample_size, seed=seed + year)

    picks_all: list[ConvictionResult] = []
    skipped: list[str] = []
    total_cost = 0.0

    for ticker in sampled:
        result = _score_one_ticker(
            ticker=ticker,
            asof=asof,
            price_store=price_store,
            scorer=scorer,
            context_builder=context_builder,
        )
        if result is None:
            skipped.append(ticker)
            continue
        picks_all.append(result)
        total_cost += result.cost_usd

    if not picks_all:
        raise RuntimeError(f"No tickers could be scored for year {year}; check data sources")

    picks_all.sort(key=lambda r: r.conviction, reverse=True)
    picks = picks_all[:top_n]

    portfolio_return = _portfolio_return(picks, price_store, asof)
    bench_df = price_store.full(benchmark)
    benchmark_return = _compute_1yr_return(bench_df["close"], asof) or 0.0

    return SingleYearResult(
        year=year,
        asof=asof,
        picks=picks,
        portfolio_return=portfolio_return,
        benchmark_return=benchmark_return,
        outperformance=portfolio_return - benchmark_return,
        total_cost_usd=total_cost,
        n_scored=len(picks_all),
        n_skipped=len(skipped),
        skipped_tickers=tuple(skipped),
    )

"""Overlay backtest engine for Tactical Sector Rotation (Layer 2e).

Unlike the top-N ``BacktestEngine`` in ``alphalens.backtest.engine``, this one
holds a fixed ``core_weights`` allocation plus tactical tilts determined by a
``MacroRegimeScorer``. Positions are rebalanced every ``rebalance_stride`` bars
(default 63 = quarterly). Between rebalances, weights are held constant
(no intra-quarter drift — simplification, accurate enough for ETF-scale data).

Cost model: per-leg spread applied per rebalance as
``drag = sum_i |Δw_i| * spread_bps_i / 10_000`` subtracted from that day's
return. Matches Perplexity R12 guidance: ETF spreads (1–5 bps SPY/QQQ/IWM) are
much tighter than small-cap → a simple per-trade bps model is sufficient; no
Almgren-Chriss impact needed at our $1k-per-position scale.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from alphalens.archive.rotation.allocator import OverlayAllocator
from alphalens.backtest.history_store import HistoryStore
from alphalens.macro.scorer import MacroRegime
from alphalens.macro.signals import SignalSet


class MacroRegimeScorer(Protocol):
    def score(self, signals: Mapping[str, float]) -> MacroRegime: ...


@dataclass(frozen=True)
class RebalanceEvent:
    date: pd.Timestamp
    target_weights: Mapping[str, float]
    prev_weights: Mapping[str, float]
    turnover: float
    rule_firings: Mapping[str, bool]
    cost_bps: float  # drag applied on this rebalance (bps of portfolio)


@dataclass(frozen=True)
class OverlayBacktestResult:
    daily_returns_gross: pd.Series
    daily_returns_net: pd.Series
    benchmark_returns: pd.Series
    rebalances: list[RebalanceEvent] = field(default_factory=list)
    weights_history: pd.DataFrame = field(default_factory=pd.DataFrame)


class OverlayBacktestEngine:
    def __init__(
        self,
        *,
        store: HistoryStore,
        scorer: MacroRegimeScorer,
        allocator: OverlayAllocator,
        signals: SignalSet,
        etf_spread_bps: Mapping[str, float],
        benchmark: str = "SPY",
    ):
        self._store = store
        self._scorer = scorer
        self._allocator = allocator
        self._signals = signals
        self._spread_bps = {k: float(v) for k, v in etf_spread_bps.items()}
        self._benchmark = benchmark

    def run(
        self, *, start: pd.Timestamp, end: pd.Timestamp, rebalance_stride: int
    ) -> OverlayBacktestResult:
        if rebalance_stride <= 0:
            raise ValueError("rebalance_stride must be positive")

        calendar = HistoryStore.benchmark_calendar(self._store, self._benchmark, start, end)
        if len(calendar) < 2:
            raise ValueError(
                f"benchmark {self._benchmark} has insufficient bars in [{start}, {end}]"
            )

        tickers = list(self._allocator._core.keys())  # preserves insertion order
        closes = (
            pd.DataFrame({t: self._store.full(t)["close"] for t in tickers})
            .reindex(calendar)
            .ffill()
        )
        daily_rets = closes.pct_change().iloc[1:]  # first row dropped

        rebalance_idx = set(range(0, len(calendar), rebalance_stride))

        rebalances: list[RebalanceEvent] = []
        prev_weights: dict[str, float] = dict.fromkeys(tickers, 0.0)
        current_weights: dict[str, float] = dict.fromkeys(tickers, 0.0)
        weights_rows: list[dict[str, float]] = []
        gross: list[float] = []
        net: list[float] = []
        pending_cost_bps: float = 0.0

        for i, date in enumerate(calendar):
            if i in rebalance_idx:
                snap = self._signals.as_of(date)
                regime = self._scorer.score(snap)
                target = self._allocator.apply(regime)
                turnover = sum(abs(target[t] - prev_weights[t]) for t in tickers)
                cost_bps = self._rebalance_cost_bps(prev_weights, target)
                rebalances.append(
                    RebalanceEvent(
                        date=date,
                        target_weights=dict(target),
                        prev_weights=dict(prev_weights),
                        turnover=turnover,
                        rule_firings=dict(regime.flags),
                        cost_bps=cost_bps,
                    )
                )
                current_weights = dict(target)
                prev_weights = dict(target)
                # Cost is realised on the first return *after* the trade.
                pending_cost_bps += cost_bps

            weights_rows.append({"date": date, **current_weights})

            if i == 0:
                continue  # no prior bar → no daily return yet

            row_return = sum(current_weights[t] * daily_rets.iloc[i - 1][t] for t in tickers)
            gross.append(row_return)
            if pending_cost_bps > 0.0:
                drag = pending_cost_bps / 10_000.0
                net.append(row_return - drag)
                pending_cost_bps = 0.0
            else:
                net.append(row_return)

        return_idx = calendar[1:]
        gross_series = pd.Series(gross, index=return_idx, name="gross")
        net_series = pd.Series(net, index=return_idx, name="net")
        benchmark_series = daily_rets[self._benchmark].rename("benchmark")
        weights_df = pd.DataFrame(weights_rows).set_index("date")

        return OverlayBacktestResult(
            daily_returns_gross=gross_series,
            daily_returns_net=net_series,
            benchmark_returns=benchmark_series,
            rebalances=rebalances,
            weights_history=weights_df,
        )

    def _rebalance_cost_bps(self, prev: Mapping[str, float], target: Mapping[str, float]) -> float:
        total_bps = 0.0
        for t in target:
            dw = abs(target[t] - prev.get(t, 0.0))
            total_bps += dw * self._spread_bps.get(t, 0.0)
        return total_bps

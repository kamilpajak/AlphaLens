"""Backtest replay engine — daily cross-sectional scoring + forward-return tracking.

For each trading day `t` in the benchmark calendar:
  1. Truncate every ticker's history to `t` (point-in-time).
  2. Call the injected `scorer(histories, config)` with those truncated histories.
  3. For every scored ticker, compute the realized `holding_period`-day forward
     return (enter next trading day's close, exit N bars later).
  4. Snapshot top-N names, the portfolio return (weighted mean of top-N),
     the universe median return, and cross-sectional Rank IC.

The engine is scorer-agnostic. Callers supply both the scorer callable and
its config. Adapters for specific scorers (Layer 2b MomentumScorer, the
archived Lean `rank_universe`) live next to those scorers.

The returned `BacktestReport` carries the full per-date series so downstream
analysis (cost model, regime breakdown, FF3 regression) can operate on it
without re-running the simulation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Literal, Mapping

import pandas as pd

from .history_store import HistoryStore
from .metrics import rank_ic, turnover_pct
from .weighting import (
    WeightingScheme,
    compute_position_weights,
    weighted_return,
)

logger = logging.getLogger(__name__)


Scorer = Callable[[Mapping[str, pd.DataFrame], Mapping], pd.DataFrame]


TradeDirection = Literal["enter", "exit"]


@dataclass(frozen=True)
class Trade:
    """A rebalance-day trade: enter a new name or exit a dropped name.

    `notional` equals `|weight_delta| × portfolio_value`, always positive.
    `price` is the close on the scoring day — trades are assumed filled at
    the next open/close but the price here is just the most recent observable
    mark used for share counting in commission calculations.
    """

    date: pd.Timestamp
    ticker: str
    notional: float
    direction: TradeDirection
    price: float


@dataclass(frozen=True)
class DailyResult:
    """One-day snapshot of the backtest.

    `portfolio_return` is the **1-day forward return** of today's top-N picks —
    i.e. simulated "daily rebalance, 1-day hold" P&L. This is what Sharpe should
    be computed on (non-overlapping, independent daily observations).

    `ic` uses the longer `holding_period` forward returns cross-sectionally as
    the signal-quality metric. Overlapping is fine there because IC is a
    cross-sectional measure per day, not a time-series statistic.
    """

    date: pd.Timestamp
    scored_count: int
    top_n_tickers: list[str]
    top_n_scores: list[float]
    top_n_forward_returns: list[float]        # holding_period forward returns (IC-horizon, NaN OK)
    portfolio_return: float                    # 1-day forward return of top-N (Sharpe-ready)
    portfolio_return_holding: float            # holding_period forward return of top-N (signal diagnostic)
    universe_median_return: float              # 1-day median across scored set
    ic: float                                  # Rank IC over holding_period horizon
    top_n_weights: list[float] = field(default_factory=list)   # aligned with top_n_tickers
    trades: list[Trade] | None = None          # rebalance trades vs. previous day


@dataclass
class BacktestReport:
    scorer_config: dict
    holding_period: int
    top_n: int
    start: date
    end: date
    benchmark: str
    universe_ticker_count: int
    daily_results: list[DailyResult] = field(default_factory=list)
    scored_frames: dict[pd.Timestamp, pd.DataFrame] = field(default_factory=dict)
    portfolio_value: float = 100_000.0

    @property
    def portfolio_returns(self) -> pd.Series:
        """1-day forward returns of top-N (for Sharpe, non-overlapping)."""
        if not self.daily_results:
            return pd.Series(dtype=float)
        idx = [r.date for r in self.daily_results]
        vals = [r.portfolio_return for r in self.daily_results]
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="portfolio")

    @property
    def portfolio_returns_holding(self) -> pd.Series:
        """Holding-period forward returns of top-N (signal-quality diagnostic; overlaps)."""
        if not self.daily_results:
            return pd.Series(dtype=float)
        idx = [r.date for r in self.daily_results]
        vals = [r.portfolio_return_holding for r in self.daily_results]
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="portfolio_holding")

    @property
    def universe_median_returns(self) -> pd.Series:
        if not self.daily_results:
            return pd.Series(dtype=float)
        idx = [r.date for r in self.daily_results]
        vals = [r.universe_median_return for r in self.daily_results]
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="universe_median")

    @property
    def ic_series(self) -> pd.Series:
        if not self.daily_results:
            return pd.Series(dtype=float)
        idx = [r.date for r in self.daily_results]
        vals = [r.ic for r in self.daily_results]
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="ic")

    @property
    def turnover(self) -> float:
        return turnover_pct(r.top_n_tickers for r in self.daily_results)


class BacktestEngine:
    """Daily-rebalance backtest over a universe, using a pluggable scorer.

    The scorer must match the signature `scorer(histories, config) -> DataFrame`
    and return a frame with at minimum columns `ticker` and `score`. Any tickers
    it returns are treated as scored; top-N is selected by descending `score`.
    """

    MIN_BARS_REQUIRED = 220       # covers the longest lookback our scorers use (SMA200 + buffer)

    def __init__(
        self,
        history_store: HistoryStore,
        scorer: Scorer,
        scorer_config: Mapping,
        holding_period: int = 5,
        top_n: int = 30,
        benchmark: str = "SPY",
        screener_tickers: list[str] | None = None,
        retain_scored_frames: bool = False,
        weighting: WeightingScheme = "equal",
        portfolio_value: float = 100_000.0,
    ):
        self.store = history_store
        self._scorer = scorer
        self.scorer_config = dict(scorer_config)
        self.holding_period = int(holding_period)
        self.top_n = int(top_n)
        self.benchmark = benchmark
        self._screener_tickers = list(screener_tickers) if screener_tickers else []
        self.retain_scored_frames = bool(retain_scored_frames)
        self.weighting: WeightingScheme = weighting
        self.portfolio_value = float(portfolio_value)
        self._prev_weights: dict[str, float] = {}

    def run(self, start: date, end: date) -> BacktestReport:
        calendar = HistoryStore.benchmark_calendar(self.store, self.benchmark, start, end)
        if not calendar:
            raise RuntimeError(
                f"No trading days found for benchmark {self.benchmark!r} in [{start}, {end}]"
            )

        tickers = self._screener_tickers or [
            t for t in self.store.tickers() if t != self.benchmark.upper()
        ]

        report = BacktestReport(
            scorer_config=dict(self.scorer_config),
            holding_period=self.holding_period,
            top_n=self.top_n,
            start=start,
            end=end,
            benchmark=self.benchmark,
            universe_ticker_count=len(tickers),
            portfolio_value=self.portfolio_value,
        )

        # Reset state so multiple .run() calls on the same engine don't bleed
        # prior positions into a new simulation.
        self._prev_weights = {}

        logger.info(
            "backtest run: %s..%s benchmark=%s tickers=%d days=%d top_n=%d hold=%d",
            start, end, self.benchmark, len(tickers), len(calendar),
            self.top_n, self.holding_period,
        )

        for ts in calendar:
            day = ts.date()
            simulated = self._simulate_day(day, tickers)
            if simulated is None:
                continue
            snap, scored_frame = simulated
            report.daily_results.append(snap)
            if self.retain_scored_frames and scored_frame is not None:
                report.scored_frames[pd.Timestamp(day)] = scored_frame

        logger.info(
            "backtest done: %d daily snapshots out of %d trading days",
            len(report.daily_results), len(calendar),
        )
        return report

    # ------------------------------------------------------------------ internal

    def _build_trades(
        self,
        day: pd.Timestamp,
        current_weights: dict[str, float],
        prev_weights: dict[str, float],
        prices: dict[str, float],
        history_closes: dict[str, float],
    ) -> list[Trade]:
        trades: list[Trade] = []
        all_tickers = set(current_weights) | set(prev_weights)
        for ticker in sorted(all_tickers):
            cw = current_weights.get(ticker, 0.0)
            pw = prev_weights.get(ticker, 0.0)
            delta = cw - pw
            if abs(delta) < 1e-12:
                continue
            direction: TradeDirection = "enter" if delta > 0 else "exit"
            notional = abs(delta) * self.portfolio_value
            price = prices.get(ticker) or history_closes.get(ticker, 0.0)
            trades.append(
                Trade(
                    date=day,
                    ticker=ticker,
                    notional=notional,
                    direction=direction,
                    price=price,
                )
            )
        return trades

    def _simulate_day(
        self, day: date, tickers: list[str]
    ) -> tuple[DailyResult, pd.DataFrame | None] | None:
        histories: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            df = self.store.truncate_to(ticker, day)
            if len(df) < self.MIN_BARS_REQUIRED:
                continue
            histories[ticker] = df

        if not histories:
            return None

        scored = self._scorer(histories, self.scorer_config)
        if scored.empty:
            return None

        fwd_1d = []
        fwd_holding = []
        for ticker in scored["ticker"]:
            r1 = self.store.forward_return(ticker, day, 1)
            rh = self.store.forward_return(ticker, day, self.holding_period)
            fwd_1d.append(float("nan") if r1 is None else r1)
            fwd_holding.append(float("nan") if rh is None else rh)
        scored = scored.assign(fwd_1d=fwd_1d, fwd_holding=fwd_holding)

        valid_holding = scored.dropna(subset=["fwd_holding"])
        if valid_holding.empty:
            return None

        top_n = scored.sort_values("score", ascending=False).head(self.top_n)

        weights = compute_position_weights(len(top_n), self.weighting)
        fwd_1d_arr = top_n["fwd_1d"].to_numpy(dtype=float)
        fwd_h_arr = top_n["fwd_holding"].to_numpy(dtype=float)

        portfolio_ret_1d = weighted_return(fwd_1d_arr, weights)
        portfolio_ret_holding = weighted_return(fwd_h_arr, weights)
        universe_median_ret_1d = (
            float(scored["fwd_1d"].dropna().median()) if scored["fwd_1d"].notna().any() else 0.0
        )
        ic_value = rank_ic(
            valid_holding["score"].tolist(), valid_holding["fwd_holding"].tolist()
        )

        top_n_tickers = top_n["ticker"].tolist()
        weights_list = [float(w) for w in weights]
        current_weights = dict(zip(top_n_tickers, weights_list))
        trades = self._build_trades(
            day=pd.Timestamp(day),
            current_weights=current_weights,
            prices={t: float(p) for t, p in zip(top_n_tickers, top_n["fwd_1d"].tolist())},
            history_closes={t: _last_close(self.store, t, day) for t in top_n_tickers},
            prev_weights=self._prev_weights,
        )
        self._prev_weights = current_weights

        snap = DailyResult(
            date=pd.Timestamp(day),
            scored_count=int(len(valid_holding)),
            top_n_tickers=top_n_tickers,
            top_n_scores=[float(x) for x in top_n["score"].tolist()],
            top_n_forward_returns=[
                float(x) if not _is_nan(x) else float("nan")
                for x in top_n["fwd_holding"].tolist()
            ],
            portfolio_return=portfolio_ret_1d if not _is_nan(portfolio_ret_1d) else 0.0,
            portfolio_return_holding=(
                portfolio_ret_holding if not _is_nan(portfolio_ret_holding) else 0.0
            ),
            universe_median_return=universe_median_ret_1d,
            ic=ic_value,
            top_n_weights=weights_list,
            trades=trades,
        )

        scored_frame = (
            scored[["ticker", "score", "fwd_1d", "fwd_holding"]].copy()
            if self.retain_scored_frames
            else None
        )
        return snap, scored_frame


def _is_nan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return False


def _last_close(store: HistoryStore, ticker: str, asof: date) -> float:
    df = store.truncate_to(ticker, asof)
    if df.empty:
        return 0.0
    return float(df["close"].iloc[-1])

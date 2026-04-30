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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from alphalens.data.store.history import HistoryStore

from .metrics import rank_ic, turnover_pct
from .weighting import (
    WeightingScheme,
    compute_position_weights,
    weighted_return,
)

logger = logging.getLogger(__name__)


Scorer = Callable[[Mapping[str, pd.DataFrame], Mapping], pd.DataFrame]


@dataclass(frozen=True)
class RebalanceSnapshot:
    """One per-rebalance snapshot of the backtest — one entry per scorer
    invocation. With ``rebalance_stride=1`` this is daily; stride=5 weekly;
    stride=21 monthly.

    ``portfolio_return`` is the **1-period (=1 trading day) forward return** of
    the top-N picks selected at that rebalance. For stride>1 the strategy is
    effectively in-market only 1 day per rebalance period — use
    ``periods_per_year = 252 / stride`` when annualising Sharpe to reflect
    the rebalance cadence rather than the per-period observation horizon.

    ``ic`` uses the longer ``holding_period`` forward returns cross-sectionally
    as the signal-quality metric. Overlapping is fine there because IC is a
    cross-sectional measure per rebalance, not a time-series statistic.
    """

    date: pd.Timestamp
    scored_count: int
    top_n_tickers: list[str]
    top_n_scores: list[float]
    top_n_forward_returns: list[float]  # holding_period forward returns (IC-horizon, NaN OK)
    portfolio_return: float  # 1-day forward return of top-N (Sharpe-ready)
    portfolio_return_holding: float  # holding_period forward return of top-N (signal diagnostic)
    universe_median_return: float  # 1-day median across scored set
    ic: float  # Rank IC over holding_period horizon


@dataclass
class BacktestReport:
    scorer_config: dict
    holding_period: int
    top_n: int
    start: date
    end: date
    benchmark: str
    universe_ticker_count: int
    rebalance_results: list[RebalanceSnapshot] = field(default_factory=list)
    scored_frames: dict[pd.Timestamp, pd.DataFrame] = field(default_factory=dict)

    @property
    def portfolio_returns(self) -> pd.Series:
        """1-period (=1 trading day) forward returns of top-N, one entry per
        rebalance date. Non-overlapping. For stride>1 the series is sampled
        at the rebalance cadence; annualise Sharpe at ``252/stride``."""
        if not self.rebalance_results:
            return pd.Series(dtype=float)
        idx = [r.date for r in self.rebalance_results]
        vals = [r.portfolio_return for r in self.rebalance_results]
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="portfolio")

    @property
    def portfolio_returns_holding(self) -> pd.Series:
        """Holding-period forward returns of top-N (signal-quality diagnostic; overlaps)."""
        if not self.rebalance_results:
            return pd.Series(dtype=float)
        idx = [r.date for r in self.rebalance_results]
        vals = [r.portfolio_return_holding for r in self.rebalance_results]
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="portfolio_holding")

    @property
    def universe_median_returns(self) -> pd.Series:
        if not self.rebalance_results:
            return pd.Series(dtype=float)
        idx = [r.date for r in self.rebalance_results]
        vals = [r.universe_median_return for r in self.rebalance_results]
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="universe_median")

    @property
    def ic_series(self) -> pd.Series:
        if not self.rebalance_results:
            return pd.Series(dtype=float)
        idx = [r.date for r in self.rebalance_results]
        vals = [r.ic for r in self.rebalance_results]
        return pd.Series(vals, index=pd.DatetimeIndex(idx), name="ic")

    @property
    def turnover(self) -> float:
        return turnover_pct(r.top_n_tickers for r in self.rebalance_results)


class BacktestEngine:
    """Backtest over a universe with configurable rebalance stride, using a
    pluggable scorer.

    The scorer must match the signature `scorer(histories, config) -> DataFrame`
    and return a frame with at minimum columns `ticker` and `score`. Any tickers
    it returns are treated as scored; top-N is selected by descending `score`.
    """

    # Fallback warmup bars when a scorer doesn't declare its own MIN_BARS_REQUIRED
    # attribute. The scorer's declaration is authoritative — e.g. EarlyStageScorer
    # declares 252 for Jegadeesh 11-1. Never mutate this at runtime — override via
    # scorer attr.
    MIN_BARS_REQUIRED = 220

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
        rebalance_stride: int = 1,
        phase_offset: int = 0,
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
        # Sample every Nth trading day — a stride of 5 gives weekly rebalance,
        # 21 gives monthly. Needed for Layer 2d insider scorer backtests where
        # per-day EDGAR fetches would push daily-rebalance runtime past 24h
        # on 12y × 1400-ticker sweeps.
        self.rebalance_stride = max(1, int(rebalance_stride))
        # Which 1-in-stride trading day to sample as the rebalance. Default 0 =
        # start at calendar[0]. Necessary to avoid phase-aliasing bias when
        # comparing subsamples across a longer window — see
        # docs/research/methodology_audit_2026_04_29.md and
        # tests/test_backtest_engine_stride.py::TestPhaseOffset.
        if not 0 <= int(phase_offset) < self.rebalance_stride:
            raise ValueError(
                f"phase_offset must satisfy 0 <= offset < rebalance_stride "
                f"({self.rebalance_stride}); got {phase_offset}"
            )
        self.phase_offset = int(phase_offset)
        # Scorer's declared requirement is authoritative (it knows its own
        # indicator lookbacks). Class attr is only a fallback when the scorer
        # doesn't declare one.
        self._min_bars = int(getattr(scorer, "MIN_BARS_REQUIRED", type(self).MIN_BARS_REQUIRED))

    def run(self, start: date, end: date) -> BacktestReport:
        calendar = HistoryStore.benchmark_calendar(self.store, self.benchmark, start, end)
        if not calendar:
            raise RuntimeError(
                f"No trading days found for benchmark {self.benchmark!r} in [{start}, {end}]"
            )
        if self.rebalance_stride > 1 or self.phase_offset > 0:
            calendar = calendar[self.phase_offset :: self.rebalance_stride]

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
        )

        logger.info(
            "backtest run: %s..%s benchmark=%s tickers=%d days=%d top_n=%d hold=%d",
            start,
            end,
            self.benchmark,
            len(tickers),
            len(calendar),
            self.top_n,
            self.holding_period,
        )

        total_days = len(calendar)
        # Log every 5% of days so large sweeps emit ~20 progress lines total.
        progress_stride = max(1, total_days // 20)

        for idx, ts in enumerate(calendar):
            day = ts.date()
            simulated = self._simulate_rebalance(day, tickers)
            if simulated is None:
                continue
            snap, scored_frame = simulated
            report.rebalance_results.append(snap)
            if self.retain_scored_frames and scored_frame is not None:
                report.scored_frames[pd.Timestamp(day)] = scored_frame
            if (idx + 1) % progress_stride == 0 or idx == total_days - 1:
                logger.info(
                    "backtest progress: %d/%d days (%.0f%%) — latest snap %s scored=%d",
                    idx + 1,
                    total_days,
                    100 * (idx + 1) / total_days,
                    day,
                    snap.scored_count,
                )

        logger.info(
            "backtest done: %d rebalance snapshots out of %d trading days",
            len(report.rebalance_results),
            len(calendar),
        )
        return report

    # ------------------------------------------------------------------ internal

    def _build_histories(self, day: date, tickers: list[str]) -> dict[str, pd.DataFrame]:
        histories: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            df = self.store.truncate_to(ticker, day)
            if len(df) >= self._min_bars:
                histories[ticker] = df
        return histories

    def _attach_forward_returns(self, scored: pd.DataFrame, day: date) -> pd.DataFrame:
        fwd_1d: list[float] = []
        fwd_holding: list[float] = []
        for ticker in scored["ticker"]:
            r1 = self.store.forward_return(ticker, day, 1)
            rh = self.store.forward_return(ticker, day, self.holding_period)
            fwd_1d.append(float("nan") if r1 is None else r1)
            fwd_holding.append(float("nan") if rh is None else rh)
        return scored.assign(fwd_1d=fwd_1d, fwd_holding=fwd_holding)

    def _build_rebalance_snapshot(
        self, day: date, scored: pd.DataFrame, valid_holding: pd.DataFrame
    ) -> RebalanceSnapshot:
        top_n = scored.sort_values("score", ascending=False).head(self.top_n)
        weights = compute_position_weights(len(top_n), self.weighting)
        portfolio_ret_1d = weighted_return(top_n["fwd_1d"].to_numpy(dtype=float), weights)
        portfolio_ret_holding = weighted_return(top_n["fwd_holding"].to_numpy(dtype=float), weights)
        universe_median_ret_1d = (
            float(scored["fwd_1d"].dropna().median()) if scored["fwd_1d"].notna().any() else 0.0
        )
        ic_value = rank_ic(valid_holding["score"].tolist(), valid_holding["fwd_holding"].tolist())
        return RebalanceSnapshot(
            date=pd.Timestamp(day),
            scored_count=len(valid_holding),
            top_n_tickers=top_n["ticker"].tolist(),
            top_n_scores=[float(x) for x in top_n["score"].tolist()],
            top_n_forward_returns=[
                float(x) if not _is_nan(x) else float("nan") for x in top_n["fwd_holding"].tolist()
            ],
            portfolio_return=portfolio_ret_1d if not _is_nan(portfolio_ret_1d) else 0.0,
            portfolio_return_holding=(
                portfolio_ret_holding if not _is_nan(portfolio_ret_holding) else 0.0
            ),
            universe_median_return=universe_median_ret_1d,
            ic=ic_value,
        )

    def _simulate_rebalance(
        self, day: date, tickers: list[str]
    ) -> tuple[RebalanceSnapshot, pd.DataFrame | None] | None:
        histories = self._build_histories(day, tickers)
        if not histories:
            return None

        scored = self._scorer(histories, self.scorer_config)
        if scored.empty:
            return None

        scored = self._attach_forward_returns(scored, day)
        valid_holding = scored.dropna(subset=["fwd_holding"])
        if valid_holding.empty:
            return None

        snap = self._build_rebalance_snapshot(day, scored, valid_holding)
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

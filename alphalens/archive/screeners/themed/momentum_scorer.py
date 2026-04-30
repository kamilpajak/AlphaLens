"""Score stocks on momentum metrics: near-52w-high, 20d change, volume surge,
relative strength vs benchmark, RSI, ADX, MACD histogram."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import pandas as pd
from stockstats import wrap

from alphalens.data.fundamentals.gate import fundamental_gate_score

from .config import THEMED_DEFAULTS

logger = logging.getLogger(__name__)

METRIC_COLS = (
    "near_high_score",
    "pct_20d_score",
    "volume_surge_score",
    "rel_strength_score",
    "rsi_score",
    "adx_score",
    "macd_score",
)


class MomentumScorer:
    # near_high is 52-week (252 bars) + relative-strength benchmark window.
    # BacktestEngine honours this via getattr to enforce warmup before scoring.
    MIN_BARS_REQUIRED = 252

    def __init__(self, config: dict | None = None):
        self.config = config or THEMED_DEFAULTS

    def score_all(
        self,
        tickers: list[str],
        prices: dict[str, pd.DataFrame],
        benchmark_ticker: str | None = None,
        fundamentals: Mapping[str, Mapping] | None = None,
    ) -> pd.DataFrame:
        """Score a list of tickers. `fundamentals` is optional: when provided,
        its per-ticker feature dict is multiplied into the composite via the
        fundamental gate (see issue #14). When None, gate is 1.0 (no-op).
        """
        benchmark = prices.get(benchmark_ticker) if benchmark_ticker else None
        rows = [
            self._score_one(
                t,
                prices.get(t),
                benchmark,
                (fundamentals or {}).get(t, {}),
            )
            for t in tickers
        ]
        return pd.DataFrame(rows)

    def _score_one(
        self,
        ticker: str,
        df: pd.DataFrame | None,
        benchmark: pd.DataFrame | None,
        ticker_fundamentals: Mapping,
    ) -> dict:
        if df is None or df.empty:
            return self._zero_row(ticker)
        try:
            near_high = self._near_high_score(df, self.config["near_high_pct"])
            pct_20d = self._pct_20d_score(df)
            vol_surge = self._volume_surge_score(df, self.config["volume_surge_min"])
            rel_strength = self._rel_strength_score(df, benchmark)

            rsi, adx, macd_hist = self._indicators(df)
            rsi_s = self._rsi_score(rsi, self.config["rsi_low"], self.config["rsi_high"])
            adx_s = self._adx_score(adx, self.config["adx_min"])
            macd_s = self._macd_score(macd_hist)

            technical_composite = (
                near_high * self.config["weight_near_high"]
                + pct_20d * self.config["weight_pct_20d"]
                + vol_surge * self.config["weight_volume_surge"]
                + rel_strength * self.config["weight_rel_strength"]
                + rsi_s * self.config["weight_rsi"]
                + adx_s * self.config["weight_adx"]
                + macd_s * self.config["weight_macd"]
            )

            gate = fundamental_gate_score(ticker_fundamentals or {}, self.config)
            composite = technical_composite * gate

            return {
                "ticker": ticker,
                "near_high_score": near_high,
                "pct_20d_score": pct_20d,
                "volume_surge_score": vol_surge,
                "rel_strength_score": rel_strength,
                "rsi_score": rsi_s,
                "adx_score": adx_s,
                "macd_score": macd_s,
                "fundamental_gate": gate,
                "momentum_score": composite,
            }
        except Exception:
            logger.warning("Scoring failed for %s", ticker, exc_info=True)
            return self._zero_row(ticker)

    @staticmethod
    def _zero_row(ticker: str) -> dict:
        row = {"ticker": ticker, "momentum_score": 0.0, "fundamental_gate": 1.0}
        for col in METRIC_COLS:
            row[col] = 0.0
        return row

    # Metric primitives (staticmethods so tests can hit them directly) -------

    @staticmethod
    def _near_high_score(df: pd.DataFrame, pct: float = 0.15) -> float:
        """Distance from 52w high. Within `pct` -> 1.0, linearly tapers to 0 at -50%."""
        window = df.tail(252) if len(df) >= 252 else df
        high_52w = float(window["High"].max())
        if high_52w <= 0:
            return 0.0
        close = float(df["Close"].iloc[-1])
        distance = (high_52w - close) / high_52w  # 0 = at high, 1 = 100% below
        if distance <= pct:
            return 1.0
        # Linear taper: at pct -> 1.0, at 0.5 -> 0.0
        tail = max(0.0, 1.0 - (distance - pct) / (0.5 - pct))
        return min(1.0, tail)

    @staticmethod
    def _pct_20d_score(df: pd.DataFrame) -> float:
        """20d pct change. 0% -> 0.5, +20% -> 1.0, -20% -> 0.0 (clamped)."""
        if len(df) < 21:
            return 0.5
        close_now = float(df["Close"].iloc[-1])
        close_prev = float(df["Close"].iloc[-21])
        if close_prev <= 0:
            return 0.5
        pct = (close_now - close_prev) / close_prev
        return max(0.0, min(1.0, 0.5 + pct * 2.5))

    @staticmethod
    def _volume_surge_score(df: pd.DataFrame, threshold: float = 2.0) -> float:
        """Today's volume / 50d avg. `threshold`x -> 1.0, 1x -> 0.5."""
        if len(df) < 51:
            return 0.5
        today = float(df["Volume"].iloc[-1])
        avg_50 = float(df["Volume"].iloc[-51:-1].mean())
        if avg_50 <= 0:
            return 0.5
        ratio = today / avg_50
        # 1x -> 0.5, threshold -> 1.0, linearly
        if ratio <= 1.0:
            return max(0.0, 0.5 * ratio)
        span = max(threshold - 1.0, 0.1)
        return max(0.0, min(1.0, 0.5 + 0.5 * (ratio - 1.0) / span))

    @staticmethod
    def _rel_strength_score(stock: pd.DataFrame, benchmark: pd.DataFrame | None) -> float:
        """Stock's 3mo return vs benchmark's 3mo return."""
        if benchmark is None or benchmark.empty:
            return 0.5
        if len(stock) < 63 or len(benchmark) < 63:
            return 0.5
        stock_ret = float(stock["Close"].iloc[-1]) / float(stock["Close"].iloc[-63]) - 1
        bench_ret = float(benchmark["Close"].iloc[-1]) / float(benchmark["Close"].iloc[-63]) - 1
        diff = stock_ret - bench_ret
        # diff of +20% -> 1.0, 0% -> 0.5, -20% -> 0.0
        return max(0.0, min(1.0, 0.5 + diff * 2.5))

    @staticmethod
    def _rsi_score(rsi: float | None, low: float = 50, high: float = 75) -> float:
        """RSI in [low, high] -> 1.0. Outside -> linear taper."""
        if rsi is None:
            return 0.5
        if low <= rsi <= high:
            return 1.0
        if rsi < low:
            return max(0.0, rsi / low)
        return max(0.0, (100 - rsi) / (100 - high))

    @staticmethod
    def _adx_score(adx: float | None, threshold: float = 25) -> float:
        if adx is None:
            return 0.5
        if adx >= threshold:
            return 1.0
        return max(0.0, adx / threshold)

    @staticmethod
    def _macd_score(macd_hist: float | None) -> float:
        if macd_hist is None:
            return 0.5
        return 0.75 if macd_hist > 0 else 0.25

    @staticmethod
    def _indicators(
        df: pd.DataFrame,
    ) -> tuple[float | None, float | None, float | None]:
        """Extract RSI(14), ADX(14), MACD histogram from the frame."""
        try:
            ss = wrap(df[["Open", "High", "Low", "Close", "Volume"]].copy())
            return (
                _safe_last(ss, "rsi_14"),
                _safe_last(ss, "dx_14_ema"),
                _safe_last(ss, "macdh"),
            )
        except Exception:
            return None, None, None


def _safe_last(ss: pd.DataFrame, col: str) -> float | None:
    try:
        series = ss[col]
        valid = series.dropna()
        return float(valid.iloc[-1]) if not valid.empty else None
    except Exception:
        return None

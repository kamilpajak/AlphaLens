"""EarlyStageScorer — catches stocks at base → Stage 2 transition,
not after extended rallies.

Seven metrics per Perplexity research (CAN SLIM / Minervini VCP / Jegadeesh 11-1):
  1. base_breakout       — (close - SMA_50) / (SMA_50 - SMA_200) ∈ [0.05, 0.25]
  2. acceleration        — d²P/dt² > 0 AND > median(d²P/dt², 60d)
  3. vcp                 — BB_width < p30(BB_width, 90d) AND close > SMA_20
  4. rsi_emergence       — RSI ∈ [45, 65]; penalty if >70
  5. adx_building        — ADX ∈ [20, 35] AND ΔADX_5d > +2
  6. volume_accumulation — vol20/vol60 ∈ [1.1, 1.5]; penalty if >2.0
  7. jegadeesh_11_1      — (close[t-21] - close[t-252])/close[t-252] > 0, trend live

Same `score_all(tickers, prices, benchmark_ticker)` API as MomentumScorer so
it's pluggable via the BacktestEngine scorer_adapter pattern.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import numpy as np
import pandas as pd
from stockstats import wrap

from ...fundamentals.gate import fundamental_gate_score

logger = logging.getLogger(__name__)

EARLY_STAGE_DEFAULTS = {
    # Weights per Perplexity recommendation (sum to 1.00)
    "weight_base_breakout": 0.20,
    "weight_acceleration": 0.15,
    "weight_vcp": 0.15,
    "weight_rsi_emergence": 0.15,
    "weight_adx_building": 0.15,
    "weight_volume_accumulation": 0.10,
    "weight_jegadeesh_11_1": 0.10,
    # Thresholds
    "base_breakout_min": 0.05,
    "base_breakout_max": 0.25,
    "rsi_emergence_low": 45.0,
    "rsi_emergence_high": 65.0,
    "rsi_overbought": 70.0,
    "adx_building_min": 20.0,
    "adx_building_max": 35.0,
    "adx_delta_min": 2.0,
    "volume_accum_min": 1.1,
    "volume_accum_max": 1.5,
    "volume_climactic_max": 2.0,
    "vcp_width_percentile": 30.0,
    "vcp_lookback_days": 90,
    "acceleration_lookback": 60,
    # Guardrails — inherit from MomentumScorer defaults (can override from outside)
    "min_market_cap": 300_000_000,
    "min_avg_volume": 1_000_000,
    "min_price": 2.0,
    "benchmark": "SPY",
    "top_n": 5,
    # Fundamental soft-guardrail (issue #14). Defaults disabled — opt-in.
    "fundamental_gate_enabled": False,
    "cash_runway_months_hard_reject": 3,
    "cash_runway_months_penalty_full": 12,
    "ps_ceiling_preprofit_penalty_full": 100,
    "consecutive_neg_ocf_penalty": 4,
    "fundamental_gate_floor": 0.3,
}

METRIC_COLS = (
    "base_breakout_score",
    "acceleration_score",
    "vcp_score",
    "rsi_emergence_score",
    "adx_building_score",
    "volume_accumulation_score",
    "jegadeesh_11_1_score",
)


class EarlyStageScorer:
    # Jegadeesh 11-1 requires exactly 252 bars (t-252 price lookback).
    # BacktestEngine honours this via getattr to enforce warmup before scoring.
    MIN_BARS_REQUIRED = 252

    def __init__(self, config: dict | None = None):
        self.config = config or dict(EARLY_STAGE_DEFAULTS)

    def score_all(
        self,
        tickers: list[str],
        prices: dict[str, pd.DataFrame],
        benchmark_ticker: str | None = None,
        fundamentals: Mapping[str, Mapping] | None = None,
    ) -> pd.DataFrame:
        # benchmark_ticker accepted for interface parity with MomentumScorer.score_all
        _ = benchmark_ticker
        rows = [self._score_one(t, prices.get(t), (fundamentals or {}).get(t, {})) for t in tickers]
        return pd.DataFrame(rows)

    def _score_one(
        self,
        ticker: str,
        df: pd.DataFrame | None,
        ticker_fundamentals: Mapping,
    ) -> dict:
        if df is None or df.empty:
            return self._zero_row(ticker)
        try:
            base = self._base_breakout_score(
                df,
                self.config["base_breakout_min"],
                self.config["base_breakout_max"],
            )
            accel = self._acceleration_score(df, self.config["acceleration_lookback"])
            vcp = self._vcp_score(
                df,
                self.config["vcp_width_percentile"],
                self.config["vcp_lookback_days"],
            )
            rsi_val, adx_cur, adx_5d = self._indicators(df)
            rsi_e = self._rsi_emergence_score(
                rsi_val,
                self.config["rsi_emergence_low"],
                self.config["rsi_emergence_high"],
                self.config["rsi_overbought"],
            )
            adx_b = self._adx_building_score(
                adx_cur,
                adx_5d,
                self.config["adx_building_min"],
                self.config["adx_building_max"],
                self.config["adx_delta_min"],
            )
            vol_a = self._volume_accumulation_score(
                df,
                self.config["volume_accum_min"],
                self.config["volume_accum_max"],
                self.config["volume_climactic_max"],
            )
            jeg = self._jegadeesh_11_1_score(df)

            technical_composite = (
                base * self.config["weight_base_breakout"]
                + accel * self.config["weight_acceleration"]
                + vcp * self.config["weight_vcp"]
                + rsi_e * self.config["weight_rsi_emergence"]
                + adx_b * self.config["weight_adx_building"]
                + vol_a * self.config["weight_volume_accumulation"]
                + jeg * self.config["weight_jegadeesh_11_1"]
            )

            gate = fundamental_gate_score(ticker_fundamentals or {}, self.config)
            composite = technical_composite * gate

            return {
                "ticker": ticker,
                "base_breakout_score": base,
                "acceleration_score": accel,
                "vcp_score": vcp,
                "rsi_emergence_score": rsi_e,
                "adx_building_score": adx_b,
                "volume_accumulation_score": vol_a,
                "jegadeesh_11_1_score": jeg,
                "fundamental_gate": gate,
                "early_stage_score": composite,
            }
        except Exception:
            logger.warning("EarlyStage scoring failed for %s", ticker, exc_info=True)
            return self._zero_row(ticker)

    @staticmethod
    def _zero_row(ticker: str) -> dict:
        row = {"ticker": ticker, "early_stage_score": 0.0, "fundamental_gate": 1.0}
        for col in METRIC_COLS:
            row[col] = 0.0
        return row

    # --- Metric primitives (staticmethods so tests hit them directly) --------

    @staticmethod
    def _base_breakout_score(
        df: pd.DataFrame,
        lo: float = 0.05,
        hi: float = 0.25,
    ) -> float:
        """(close - SMA_50) / (SMA_50 - SMA_200) ∈ [lo, hi] → 1.0, else 0.

        Needs 200 bars for SMA_200. If SMA_50 == SMA_200 (flat stock), return 0
        (divide-by-zero guard).
        """
        if len(df) < 200:
            return 0.0
        close = float(df["Close"].iloc[-1])
        sma_50 = float(df["Close"].tail(50).mean())
        sma_200 = float(df["Close"].tail(200).mean())
        denom = sma_50 - sma_200
        if abs(denom) < 1e-9:
            return 0.0
        ratio = (close - sma_50) / denom
        return 1.0 if lo <= ratio <= hi else 0.0

    @staticmethod
    def _acceleration_score(df: pd.DataFrame, lookback: int = 60) -> float:
        """d²P/dt² > 0 AND > median over last `lookback` days → 1.0, else 0.

        Second derivative = consecutive daily return differences.
        """
        if len(df) < lookback + 2:
            return 0.0
        close = df["Close"].astype(float)
        first_diff = close.diff()
        second_diff = first_diff.diff()
        current = second_diff.iloc[-1]
        if not np.isfinite(current) or current <= 0:
            return 0.0
        recent = second_diff.tail(lookback).dropna()
        if recent.empty:
            return 0.0
        return 1.0 if current > float(recent.median()) else 0.0

    @staticmethod
    def _vcp_score(
        df: pd.DataFrame,
        percentile: float = 30.0,
        lookback: int = 90,
    ) -> float:
        """BB_width < p30(BB_width, `lookback`d) AND close > SMA_20 → 1.0.

        BB_width = 2 * rolling_std(close, 20).
        """
        if len(df) < max(lookback, 20) + 1:
            return 0.0
        close = df["Close"].astype(float)
        bb_width = 2.0 * close.rolling(window=20).std()
        recent_bb = bb_width.tail(lookback).dropna()
        if recent_bb.empty:
            return 0.0
        threshold = float(np.percentile(recent_bb.values, percentile))
        current_bb = float(bb_width.iloc[-1])
        sma_20 = float(close.tail(20).mean())
        current_close = float(close.iloc[-1])
        if current_bb < threshold and current_close > sma_20:
            return 1.0
        return 0.0

    @staticmethod
    def _rsi_emergence_score(
        rsi: float | None,
        low: float = 45.0,
        high: float = 65.0,
        overbought: float = 70.0,
    ) -> float:
        """RSI ∈ [low, high] → 1.0; RSI > overbought OR < low → 0; None → 0.5."""
        if rsi is None:
            return 0.5
        if low <= rsi <= high:
            return 1.0
        return 0.0

    @staticmethod
    def _adx_building_score(
        adx_current: float | None,
        adx_5d_ago: float | None,
        lo: float = 20.0,
        hi: float = 35.0,
        delta_min: float = 2.0,
    ) -> float:
        """ADX ∈ [lo, hi] AND (adx_current - adx_5d_ago) > delta_min → 1.0."""
        if adx_current is None or adx_5d_ago is None:
            return 0.5
        if not (lo <= adx_current <= hi):
            return 0.0
        if (adx_current - adx_5d_ago) <= delta_min:
            return 0.0
        return 1.0

    @staticmethod
    def _volume_accumulation_score(
        df: pd.DataFrame,
        lo: float = 1.1,
        hi: float = 1.5,
        climactic: float = 2.0,
    ) -> float:
        """vol_20d / vol_60d ∈ [lo, hi] → 1.0; >= climactic → 0 (distribution)."""
        if len(df) < 60:
            return 0.0
        vol = df["Volume"].astype(float)
        vol_20 = float(vol.tail(20).mean())
        vol_60 = float(vol.tail(60).mean())
        if vol_60 <= 0:
            return 0.0
        ratio = vol_20 / vol_60
        if ratio >= climactic:
            return 0.0
        if lo <= ratio <= hi:
            return 1.0
        return 0.0

    @staticmethod
    def _jegadeesh_11_1_score(df: pd.DataFrame) -> float:
        """(close[t-21] - close[t-252]) / close[t-252] > 0 AND trend live
        (close[t] > close[t-20]) → 1.0.

        Skips the last month to avoid short-term reversal (Jegadeesh 1990 /
        Asness-Moskowitz-Pedersen 2013 convention).
        """
        if len(df) < 252:
            return 0.0
        close = df["Close"].astype(float)
        p_t_252 = float(close.iloc[-252])
        p_t_21 = float(close.iloc[-21])
        if p_t_252 <= 0:
            return 0.0
        mom_11_1 = (p_t_21 - p_t_252) / p_t_252
        if mom_11_1 <= 0:
            return 0.0
        # Trend live: current > 20 days ago
        if float(close.iloc[-1]) <= float(close.iloc[-21]):
            return 0.0
        return 1.0

    @staticmethod
    def _indicators(
        df: pd.DataFrame,
    ) -> tuple[float | None, float | None, float | None]:
        """Return (RSI_14_current, ADX_14_current, ADX_14_5d_ago). stockstats-based."""
        try:
            ss = wrap(df[["Open", "High", "Low", "Close", "Volume"]].copy())
            rsi = _safe_last(ss, "rsi_14")
            adx_series = ss["dx_14_ema"].dropna()
            adx_cur = float(adx_series.iloc[-1]) if len(adx_series) >= 1 else None
            adx_5d = float(adx_series.iloc[-6]) if len(adx_series) >= 6 else None
            return rsi, adx_cur, adx_5d
        except Exception:
            return None, None, None


def _safe_last(ss: pd.DataFrame, col: str) -> float | None:
    try:
        series = ss[col].dropna()
        return float(series.iloc[-1]) if not series.empty else None
    except Exception:
        return None

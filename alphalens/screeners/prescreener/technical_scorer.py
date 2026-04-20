"""Score stocks on technical indicators (RSI, trend, ADX, MACD)."""

from __future__ import annotations

import logging

import pandas as pd
from stockstats import wrap

from .config import PRESCREENER_DEFAULTS

logger = logging.getLogger(__name__)


class TechnicalScorer:
    def __init__(self, config: dict | None = None):
        self.config = config or PRESCREENER_DEFAULTS

    def score_all(
        self,
        price_data: dict[str, pd.DataFrame],
        tickers: list[str],
        curr_date: str,
    ) -> pd.DataFrame:
        """Score all tickers on technical criteria.

        Args:
            price_data: {ticker: OHLCV DataFrame} with DatetimeIndex
            tickers: list of ticker symbols
            curr_date: reference date string

        Returns:
            DataFrame with columns: ticker, rsi_score, trend_score,
            adx_score, technical_score
        """
        rows = []
        for ticker in tickers:
            df = price_data.get(ticker)
            if df is None or df.empty:
                rows.append(self._neutral_row(ticker))
                continue
            rows.append(self._score_single(ticker, df))
        return pd.DataFrame(rows)

    def _score_single(self, ticker: str, df: pd.DataFrame) -> dict:
        try:
            ss = wrap(df[["Open", "High", "Low", "Close", "Volume"]].copy())
            last = df.iloc[-1]
            close = float(last["Close"])

            rsi = self._safe_indicator(ss, "rsi_14")
            ema50 = self._safe_indicator(ss, "close_50_ema")
            sma200 = self._safe_indicator(ss, "close_200_sma")
            adx = self._safe_indicator(ss, "dx_14_ema")  # ADX(14) in stockstats
            macd_hist = self._safe_indicator(ss, "macdh")

            rsi_s = self._rsi_score(rsi, self.config["rsi_low"], self.config["rsi_high"])
            trend_ema = self._trend_score(close, ema50)
            trend_sma = self._trend_score(close, sma200)
            trend_s = trend_ema * 0.5 + trend_sma * 0.5
            adx_s = self._adx_score(adx, self.config["adx_min"])
            macd_s = 0.7 if macd_hist is not None and macd_hist > 0 else 0.3 if macd_hist is not None else 0.5

            technical_score = (
                rsi_s * 0.25
                + trend_s * 0.35
                + adx_s * 0.20
                + macd_s * 0.20
            )

            return {
                "ticker": ticker,
                "rsi_score": rsi_s,
                "trend_score": trend_s,
                "adx_score": adx_s,
                "technical_score": technical_score,
            }
        except Exception:
            logger.warning("Technical scoring failed for %s", ticker, exc_info=True)
            return self._neutral_row(ticker)

    @staticmethod
    def _safe_indicator(ss: pd.DataFrame, col: str) -> float | None:
        """Extract latest value of a stockstats indicator, or None."""
        try:
            series = ss[col]
            val = series.dropna().iloc[-1] if not series.dropna().empty else None
            return float(val) if val is not None else None
        except Exception:
            return None

    @staticmethod
    def _neutral_row(ticker: str) -> dict:
        return {
            "ticker": ticker,
            "rsi_score": 0.5,
            "trend_score": 0.5,
            "adx_score": 0.5,
            "technical_score": 0.5,
        }

    @staticmethod
    def _rsi_score(rsi: float | None, low: float = 30, high: float = 70) -> float:
        """RSI in [low, high] -> 1.0. Outside -> linear taper to 0."""
        if rsi is None:
            return 0.5
        if low <= rsi <= high:
            return 1.0
        if rsi < low:
            return max(0.0, rsi / low)
        return max(0.0, (100 - rsi) / (100 - high))

    @staticmethod
    def _trend_score(price: float | None, ma: float | None) -> float:
        """Price above MA -> high score. % distance determines magnitude."""
        if price is None or ma is None or ma == 0:
            return 0.5
        pct = (price - ma) / ma
        return max(0.0, min(1.0, 0.5 + pct * 2.5))

    @staticmethod
    def _adx_score(adx: float | None, threshold: float = 25) -> float:
        """ADX above threshold -> 1.0. Below -> proportional."""
        if adx is None:
            return 0.5
        if adx >= threshold:
            return 1.0
        return max(0.0, adx / threshold)

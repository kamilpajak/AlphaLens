"""Score stocks on volume and liquidity metrics."""

from __future__ import annotations

import logging

import pandas as pd

from .config import PRESCREENER_DEFAULTS

logger = logging.getLogger(__name__)


class VolumeScorer:
    def __init__(self, config: dict | None = None):
        self.config = config or PRESCREENER_DEFAULTS

    def score_all(
        self,
        price_data: dict[str, pd.DataFrame],
        fundamentals: dict[str, dict],
        tickers: list[str],
        curr_date: str,
    ) -> pd.DataFrame:
        rows = []
        for ticker in tickers:
            df = price_data.get(ticker)
            info = fundamentals.get(ticker, {})
            rows.append(self._score_single(ticker, df, info))
        return pd.DataFrame(rows)

    def _score_single(self, ticker: str, df: pd.DataFrame | None, info: dict) -> dict:
        avg_vol = info.get("averageVolume")
        mkt_cap = info.get("marketCap")

        avg_20d = None
        avg_50d = None
        if df is not None and len(df) >= 20:
            avg_20d = float(df["Volume"].tail(20).mean())
            if len(df) >= 50:
                avg_50d = float(df["Volume"].tail(50).mean())
            if avg_vol is None:
                avg_vol = avg_20d

        vol_s = self._avg_volume_score(avg_vol, self.config["min_avg_volume"])
        cap_s = self._market_cap_score(mkt_cap, self.config["min_market_cap"])
        trend_s = self._volume_trend_score(avg_20d, avg_50d)

        composite = vol_s * 0.40 + cap_s * 0.30 + trend_s * 0.30
        return {
            "ticker": ticker,
            "volume_score": composite,
        }

    @staticmethod
    def _avg_volume_score(avg_volume: float | None, threshold: float = 500_000) -> float:
        if avg_volume is None:
            return 0.5
        if avg_volume <= 0:
            return 0.0
        return max(0.0, min(1.0, avg_volume / (2 * threshold)))

    @staticmethod
    def _market_cap_score(mkt_cap: float | None, floor: float = 2e9) -> float:
        if mkt_cap is None:
            return 0.5
        if mkt_cap <= 0:
            return 0.0
        return max(0.0, min(1.0, mkt_cap / (2 * floor)))

    @staticmethod
    def _volume_trend_score(avg_20d: float | None, avg_50d: float | None) -> float:
        if avg_20d is None or avg_50d is None or avg_50d == 0:
            return 0.5
        ratio = avg_20d / avg_50d
        return max(0.0, min(1.0, 0.5 + (ratio - 1.0) * 2.0))

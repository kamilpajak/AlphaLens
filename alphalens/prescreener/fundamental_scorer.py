"""Score stocks on fundamental metrics (P/E, PEG, ROE, Debt, Growth)."""

from __future__ import annotations

import pandas as pd

from .config import PRESCREENER_DEFAULTS


class FundamentalScorer:
    def __init__(self, config: dict | None = None):
        self.config = config or PRESCREENER_DEFAULTS

    def score_all(self, fundamentals: dict[str, dict]) -> pd.DataFrame:
        """Score all tickers on fundamental criteria.

        Args:
            fundamentals: {ticker: {field: value}} from yfinance .info

        Returns:
            DataFrame with columns: ticker, pe_score, peg_score, roe_score,
            debt_score, growth_score, fundamental_score
        """
        rows = []
        for ticker, info in fundamentals.items():
            scores = self._score_single(info)
            scores["ticker"] = ticker
            rows.append(scores)

        df = pd.DataFrame(rows)
        df["fundamental_score"] = (
            df["pe_score"] * 0.25
            + df["peg_score"] * 0.20
            + df["roe_score"] * 0.25
            + df["debt_score"] * 0.15
            + df["growth_score"] * 0.15
        )
        return df

    def _score_single(self, info: dict) -> dict:
        pe = info.get("trailingPE") or info.get("forwardPE")
        peg = info.get("pegRatio")
        roe = info.get("returnOnEquity")
        earnings_growth = info.get("earningsGrowth")

        total_debt = info.get("totalDebt")
        ebitda = info.get("ebitda")
        if total_debt and ebitda and ebitda > 0:
            debt_ebitda = total_debt / ebitda
        else:
            debt_ebitda = None

        return {
            "pe_score": self._pe_score(pe, self.config["pe_max"]),
            "peg_score": self._peg_score(peg, self.config["peg_max"]),
            "roe_score": self._roe_score(roe, self.config["roe_min"]),
            "debt_score": self._debt_ebitda_score(debt_ebitda, self.config["debt_ebitda_max"]),
            "growth_score": self._growth_score(earnings_growth, self.config["eps_growth_min"]),
        }

    @staticmethod
    def _pe_score(pe: float | None, threshold: float = 25.0) -> float:
        """Lower P/E is better. score = max(0, 1 - pe / (2 * threshold))."""
        if pe is None:
            return 0.5
        if pe <= 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - pe / (2 * threshold)))

    @staticmethod
    def _peg_score(peg: float | None, threshold: float = 1.5) -> float:
        """Lower PEG is better."""
        if peg is None:
            return 0.5
        if peg <= 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - peg / (2 * threshold)))

    @staticmethod
    def _roe_score(roe: float | None, threshold: float = 0.12) -> float:
        """Higher ROE is better. score = min(1, roe / (2 * threshold))."""
        if roe is None:
            return 0.5
        if roe <= 0:
            return 0.0
        return max(0.0, min(1.0, roe / (2 * threshold)))

    @staticmethod
    def _debt_ebitda_score(debt_ebitda: float | None, threshold: float = 3.0) -> float:
        """Lower Debt/EBITDA is better."""
        if debt_ebitda is None:
            return 0.5
        if debt_ebitda <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - debt_ebitda / (2 * threshold)))

    @staticmethod
    def _growth_score(growth: float | None, threshold: float = 0.10) -> float:
        """Higher earnings growth is better."""
        if growth is None:
            return 0.5
        if growth <= 0:
            return 0.0
        return max(0.0, min(1.0, growth / (2 * threshold)))

"""Weighted aggregation of technical, fundamental, and volume scores."""

from __future__ import annotations

import pandas as pd

from .config import PRESCREENER_DEFAULTS


class CompositeRanker:
    def __init__(self, config: dict | None = None):
        self.config = config or PRESCREENER_DEFAULTS

    def rank(
        self,
        technical_scores: pd.DataFrame,
        fundamental_scores: pd.DataFrame,
        volume_scores: pd.DataFrame,
    ) -> pd.DataFrame:
        """Merge and weight all scores, return ranked DataFrame."""
        w_fund = self.config["weight_fundamental"]
        w_tech = self.config["weight_technical"]
        w_vol = self.config["weight_volume"]

        merged = (
            technical_scores[["ticker", "technical_score"]]
            .merge(
                fundamental_scores[["ticker", "fundamental_score"]],
                on="ticker",
                how="left",
            )
            .merge(
                volume_scores[["ticker", "volume_score"]],
                on="ticker",
                how="left",
            )
        )

        merged["fundamental_score"] = merged["fundamental_score"].fillna(0.5)
        merged["volume_score"] = merged["volume_score"].fillna(0.5)

        merged["composite_score"] = (
            merged["fundamental_score"] * w_fund
            + merged["technical_score"] * w_tech
            + merged["volume_score"] * w_vol
        )

        merged = merged.sort_values("composite_score", ascending=False).reset_index(drop=True)
        merged["rank"] = merged.index + 1
        return merged

    def top_n(self, ranked: pd.DataFrame, n: int | None = None) -> pd.DataFrame:
        """Return top N candidates."""
        if n is None:
            n = self.config["top_n"]
        return ranked.head(n)

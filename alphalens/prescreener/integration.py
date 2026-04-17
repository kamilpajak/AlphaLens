"""Pipeline orchestrator: pre-screen → optional TradingAgents deep analysis."""

from __future__ import annotations

import logging

import pandas as pd

from tradingagents.graph.trading_graph import TradingAgentsGraph

from .composite_ranker import CompositeRanker
from .config import PRESCREENER_DEFAULTS
from .data_fetcher import BatchDataFetcher
from .fundamental_scorer import FundamentalScorer
from .technical_scorer import TechnicalScorer
from .universe import get_sp500_tickers
from .volume_scorer import VolumeScorer

logger = logging.getLogger(__name__)


class PrescreenerPipeline:
    def __init__(
        self,
        curr_date: str,
        tickers: list[str] | None = None,
        config: dict | None = None,
        ta_config: dict | None = None,
    ):
        self.curr_date = curr_date
        self.tickers = tickers or get_sp500_tickers()
        self.config = config or PRESCREENER_DEFAULTS
        self.ta_config = ta_config

    def screen(self) -> pd.DataFrame:
        """Run the full screening pipeline. Returns ranked DataFrame."""
        logger.info("Screening %d tickers for %s", len(self.tickers), self.curr_date)

        fetcher = BatchDataFetcher(self.tickers, self.curr_date, self.config)

        logger.info("Fetching prices...")
        prices = fetcher.fetch_prices()

        logger.info("Fetching fundamentals...")
        fundamentals = fetcher.fetch_fundamentals()

        available_tickers = sorted(set(prices.keys()) | set(fundamentals.keys()))

        logger.info("Scoring %d tickers...", len(available_tickers))
        tech_scores = TechnicalScorer(self.config).score_all(prices, available_tickers, self.curr_date)
        fund_scores = FundamentalScorer(self.config).score_all(fundamentals)
        vol_scores = VolumeScorer(self.config).score_all(prices, fundamentals, available_tickers, self.curr_date)

        ranker = CompositeRanker(self.config)
        ranked = ranker.rank(tech_scores, fund_scores, vol_scores)

        logger.info("Top 5: %s", ranked.head(5)[["ticker", "composite_score"]].to_string(index=False))
        return ranked

    def screen_and_analyze(self, top_n: int = 5) -> list[tuple[str, str]]:
        """Screen, then run TradingAgents on top N candidates.

        Returns [(ticker, decision), ...].
        """
        ranked = self.screen()
        candidates = CompositeRanker(self.config).top_n(ranked, n=top_n)

        if self.ta_config is None:
            raise ValueError("ta_config required for screen_and_analyze()")

        ta = TradingAgentsGraph(debug=False, config=self.ta_config)
        results = []

        for _, row in candidates.iterrows():
            ticker = row["ticker"]
            logger.info("Deep analysis: %s (rank #%d, score %.3f)", ticker, int(row["rank"]), row["composite_score"])
            try:
                _, decision = ta.propagate(ticker, self.curr_date)
                results.append((ticker, decision))
            except Exception:
                logger.error("TradingAgents failed for %s", ticker, exc_info=True)
                results.append((ticker, "ERROR"))

        return results

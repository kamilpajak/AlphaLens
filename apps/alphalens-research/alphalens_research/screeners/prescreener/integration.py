"""Pipeline orchestrator: pre-screen S&P 500. Hands off to Layer 3 through the shared queue."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd
from alphalens_pipeline.core.candidates import Candidate

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
    ):
        self.curr_date = curr_date
        self.tickers = tickers or get_sp500_tickers()
        self.config = config or PRESCREENER_DEFAULTS

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
        tech_scores = TechnicalScorer(self.config).score_all(prices, available_tickers)
        fund_scores = FundamentalScorer(self.config).score_all(fundamentals)
        vol_scores = VolumeScorer(self.config).score_all(prices, fundamentals, available_tickers)

        ranker = CompositeRanker(self.config)
        ranked = ranker.rank(tech_scores, fund_scores, vol_scores)

        logger.info(
            "Top 5: %s",
            ranked.head(5)[["ticker", "composite_score"]].to_string(index=False),
        )
        return ranked

    def to_candidates(self, df: pd.DataFrame) -> list[Candidate]:
        if df.empty:
            return []
        now = datetime.now(UTC)
        return [
            Candidate.from_screener(
                ticker=row["ticker"],
                source="prescreener",
                priority=20,
                payload={
                    "rank": int(row["rank"]),
                    "composite_score": float(row["composite_score"]),
                    "technical_score": float(row.get("technical_score", 0.0)),
                    "fundamental_score": float(row.get("fundamental_score", 0.0)),
                    "volume_score": float(row.get("volume_score", 0.0)),
                },
                discriminator=self.curr_date,
                detected_at=now,
            )
            for _, row in df.iterrows()
        ]

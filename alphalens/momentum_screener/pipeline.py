"""Momentum screener pipeline: universe -> fetch -> guardrails -> score -> top N."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from ..candidates import Candidate
from ..prescreener.data_fetcher import BatchDataFetcher
from .config import MOMENTUM_DEFAULTS
from .guardrails import Guardrails
from .momentum_scorer import MomentumScorer
from .universe import flatten_universe, load_universe

logger = logging.getLogger(__name__)


class MomentumPipeline:
    def __init__(self, config: dict | None = None):
        self.config = config or MOMENTUM_DEFAULTS

    def run(self, curr_date: str, top_n: int | None = None) -> pd.DataFrame:
        n = top_n if top_n is not None else self.config["top_n"]
        benchmark = self.config["benchmark"]

        themes = load_universe()
        membership = flatten_universe(themes)
        tickers = list(membership.keys())

        if not tickers:
            return pd.DataFrame(columns=["ticker", "momentum_score", "themes"])

        fetch_list = sorted(set(tickers) | {benchmark})
        fetcher = BatchDataFetcher(fetch_list, curr_date, self.config)
        prices = fetcher.fetch_prices()
        fundamentals = fetcher.fetch_fundamentals()

        guardrails = Guardrails(self.config, asof=pd.Timestamp(curr_date))
        kept, rejected = guardrails.filter(tickers, prices, fundamentals)
        logger.info(
            "Guardrails: kept %d, rejected %d out of %d",
            len(kept),
            len(rejected),
            len(tickers),
        )

        if not kept:
            return pd.DataFrame(columns=["ticker", "momentum_score", "themes"])

        scorer = MomentumScorer(self.config)
        scores = scorer.score_all(kept, prices, benchmark_ticker=benchmark)
        scores["themes"] = scores["ticker"].map(membership)

        ranked = scores.sort_values("momentum_score", ascending=False).reset_index(drop=True)
        return ranked.head(n)

    def to_candidates(
        self, df: pd.DataFrame, weighting: str = "linear"
    ) -> list[Candidate]:
        """Emit `Candidate` rows with per-position `weight` in payload.

        `weighting` controls suggested position sizing (nie TradingAgents decision —
        to jest sygnał dla ewentualnego downstream rebalansu albo eksternalnego
        sizingu). Domyślnie `"linear"` bo weighting-sweep pokazał że linear top-5
        daje +7% Sharpe i +27% Calmar vs equal weights. Schematy: `equal`, `linear`,
        `conviction` (zobacz `alphalens/backtest/weighting.py`).
        """
        if df.empty:
            return []
        # Lokalny import — backtest'owy moduł nie jest dependency produkcji;
        # pipeline działa niezmiennie gdy plik zniknie (fallback do equal).
        try:
            from alphalens.backtest.weighting import compute_position_weights
            weights = compute_position_weights(len(df), weighting).tolist()
        except (ImportError, ValueError):
            weights = [1.0 / len(df)] * len(df)

        now = datetime.now(timezone.utc)
        discriminator = now.date().isoformat()
        # df jest już posortowane descending by momentum_score w run().
        return [
            Candidate.from_screener(
                ticker=row["ticker"],
                source="momentum",
                priority=10,
                payload={
                    "momentum_score": float(row["momentum_score"]),
                    "themes": list(row.get("themes") or []),
                    "weight": float(weights[idx]),
                    "weighting_scheme": weighting,
                },
                discriminator=discriminator,
                detected_at=now,
            )
            for idx, (_, row) in enumerate(df.iterrows())
        ]

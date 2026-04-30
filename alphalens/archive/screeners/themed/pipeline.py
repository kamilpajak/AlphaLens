"""Themed screener pipeline: curated YAML universe → fetch → guardrails → score → top N.

Scorer is pluggable (MomentumScorer default, EarlyStageScorer alternative) — the
pipeline's invariant is the themed universe loader, not the scoring math.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd

from alphalens.core.candidates import Candidate
from alphalens.screeners.prescreener.data_fetcher import BatchDataFetcher

from .config import THEMED_DEFAULTS
from .guardrails import Guardrails
from .momentum_scorer import MomentumScorer
from .universe import flatten_universe, load_universe

logger = logging.getLogger(__name__)


class ThemedPipeline:
    def __init__(
        self,
        config: dict | None = None,
        scorer=None,
        source_name: str = "momentum",
    ):
        self.config = config or THEMED_DEFAULTS
        self.scorer = scorer or MomentumScorer(self.config)
        self.source_name = source_name

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

        # Merge Alpha Vantage fundamentals into the per-ticker info dict when
        # the soft gate is enabled. Cache handles the 90-day TTL so we do at
        # most ~113 API calls per quarter, not per daily run.
        if self.config.get("fundamental_gate_enabled", False):
            from alphalens.fundamentals.cache import FundamentalsCache
            from alphalens.fundamentals.fetcher import extract_features, fetch_ticker_bundle

            cache = FundamentalsCache()
            for ticker in tickers:
                try:
                    av_features = cache.get_or_fetch(
                        ticker,
                        lambda tk: extract_features(fetch_ticker_bundle(tk)),
                    )
                except Exception as exc:
                    logger.warning("Fundamental fetch failed for %s: %s", ticker, exc)
                    continue
                fundamentals.setdefault(ticker, {}).update(av_features)

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

        scores = self.scorer.score_all(
            kept,
            prices,
            benchmark_ticker=benchmark,
            fundamentals=fundamentals,
        )
        # Normalise scorer-specific column to canonical "momentum_score" so all
        # downstream code (history_store, to_candidates, reporter) keeps working
        # regardless of which scorer was injected.
        for alt in ("early_stage_score",):
            if alt in scores.columns and "momentum_score" not in scores.columns:
                scores = scores.rename(columns={alt: "momentum_score"})
        scores["themes"] = scores["ticker"].map(membership)

        ranked = scores.sort_values("momentum_score", ascending=False).reset_index(drop=True)
        return ranked.head(n)

    def to_candidates(self, df: pd.DataFrame, weighting: str = "linear") -> list[Candidate]:
        """Emit `Candidate` rows with per-position `weight` in payload.

        `weighting` controls the suggested position sizing (not a TradingAgents
        decision — it's a signal for any downstream rebalance or external
        sizing). Default is `"linear"`: the weighting sweep showed that
        linear top-5 yields +7% Sharpe and +27% Calmar vs equal weights.
        Schemes: `equal`, `linear`, `conviction` (see
        `alphalens/backtest/weighting.py`).
        """
        if df.empty:
            return []
        # Local import — the backtest module is not a production dependency;
        # the pipeline keeps working even if the file is missing (fallback to equal).
        try:
            from alphalens.backtest.weighting import compute_position_weights

            weights = compute_position_weights(len(df), weighting).tolist()
        except (ImportError, ValueError):
            weights = [1.0 / len(df)] * len(df)

        now = datetime.now(UTC)
        discriminator = now.date().isoformat()
        # df is already sorted descending by momentum_score in run().
        return [
            Candidate.from_screener(
                ticker=row["ticker"],
                source=self.source_name,
                priority=10,
                payload={
                    "momentum_score": float(row["momentum_score"]),
                    "themes": list(row.get("themes") or []),
                    "weight": float(weights[idx]),
                    "weighting_scheme": weighting,
                    "scorer": self.source_name,
                },
                discriminator=discriminator,
                detected_at=now,
            )
            for idx, (_, row) in enumerate(df.iterrows())
        ]

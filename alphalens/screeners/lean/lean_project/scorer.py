"""MVP1 rule-based scorer: guardrails + per-ticker features + cross-sectional rank.

Pure pandas/numpy — no Lean/AlgorithmImports. Consumed by main.py inside Lean
and by host-side tests verbatim.

Input contract: a dict {ticker: pd.DataFrame} where each DataFrame has columns
`open, high, low, close, volume` indexed by ascending date. The scorer produces
a ranked DataFrame suitable for JSON dumping.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    # Host-side: loaded as `alphalens.screeners.lean.lean_project.scorer` — relative import works.
    from . import features as feat
except ImportError:
    # Inside Lean Docker: main.py imports scorer directly with /Project on sys.path,
    # so the module has no parent package. Fall back to absolute import.
    import features as feat  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_METRIC_KEYS = (
    "roc20",
    "roc60",
    "volume_surprise",
    "trend_strength",
    "breakout",
    "near_high",
)


@dataclass(frozen=True)
class TickerMetrics:
    ticker: str
    roc5: float
    roc20: float
    roc60: float
    sma20: float
    sma50: float
    sma200: float
    volume_surprise: float
    trend_strength: float
    breakout: bool
    near_high: float  # 1 - distance_to_high
    last_close: float
    avg_dollar_volume: float


def guardrails_pass(
    history: pd.DataFrame,
    min_price: float,
    max_price: float,
    min_avg_dollar_volume: float,
    dollar_volume_window: int = 20,
) -> bool:
    """Price in range + enough liquidity. Rejects thin or extreme-priced names."""
    if history.empty or len(history) < dollar_volume_window:
        return False
    last_close = float(history["close"].iloc[-1])
    if math.isnan(last_close) or last_close < min_price or last_close > max_price:
        return False
    adv = feat.dollar_volume_average(history["close"], history["volume"], dollar_volume_window)
    if math.isnan(adv) or adv < min_avg_dollar_volume:
        return False
    return True


def compute_metrics(ticker: str, history: pd.DataFrame, config: Mapping) -> TickerMetrics:
    close = history["close"]
    volume = history["volume"]

    roc5 = feat.rate_of_change(close, config["roc_short"])
    roc20 = feat.rate_of_change(close, config["roc_medium"])
    roc60 = feat.rate_of_change(close, config["roc_long"])
    sma20 = feat.sma(close, config["sma_short"])
    sma50 = feat.sma(close, config["sma_medium"])
    sma200 = feat.sma(close, config["sma_long"])
    volsurge = feat.volume_surprise(volume, config["volume_window"])
    trend = feat.trend_strength(
        close, config["sma_short"], config["sma_medium"], config["sma_long"]
    )
    is_breakout = feat.breakout(
        close,
        volume,
        config["breakout_window"],
        config["breakout_volume_multiple"],
    )
    near_high = 1.0 - feat.distance_to_high(close, config["near_high_window"])
    adv = feat.dollar_volume_average(close, volume, config["volume_window"])

    return TickerMetrics(
        ticker=ticker,
        roc5=roc5,
        roc20=roc20,
        roc60=roc60,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        volume_surprise=volsurge,
        trend_strength=trend,
        breakout=is_breakout,
        near_high=near_high,
        last_close=float(close.iloc[-1]),
        avg_dollar_volume=adv,
    )


def _clip01(x: float) -> float:
    if math.isnan(x):
        return 0.0
    return max(0.0, min(1.0, x))


def _score_row(row: pd.Series, config: Mapping) -> float:
    """Combine z-scored metrics with configured weights into final [0, ~]."""
    # Z-scored features (already clipped/normalized upstream to [0, 1])
    return (
        config["weight_roc20"] * _clip01(row["roc20_norm"])
        + config["weight_roc60"] * _clip01(row["roc60_norm"])
        + config["weight_volume_surprise"] * _clip01(row["volume_surprise_norm"])
        + config["weight_trend_strength"] * _clip01(row["trend_strength"])
        + config["weight_breakout"] * (1.0 if row["breakout"] else 0.0)
        + config["weight_near_high"] * _clip01(row["near_high"])
    )


def _normalize(values: pd.Series) -> pd.Series:
    """Map to [0, 1] via z-score then sigmoid-ish clip. NaN → 0."""
    z = feat.z_score(values.tolist())
    # compress via tanh-like: (z / 2 + 0.5) capped
    return pd.Series(np.clip(z / 2.0 + 0.5, 0.0, 1.0), index=values.index)


def rank_universe(histories: Mapping[str, pd.DataFrame], config: Mapping) -> pd.DataFrame:
    """Score every ticker whose history passes guardrails and rank cross-sectionally.

    Returns DataFrame with columns:
        ticker, score, roc5, roc20, roc60, volume_surprise, trend_strength,
        breakout, near_high, last_close, avg_dollar_volume, rank
    Sorted by `score` descending, with `rank` starting at 1.
    """
    rows: list[TickerMetrics] = []
    rejected = 0
    for ticker, history in histories.items():
        if not guardrails_pass(
            history,
            min_price=config["min_price"],
            max_price=config["max_price"],
            min_avg_dollar_volume=config["min_avg_dollar_volume"],
            dollar_volume_window=config["volume_window"],
        ):
            rejected += 1
            continue
        try:
            rows.append(compute_metrics(ticker, history, config))
        except ValueError as exc:
            logger.debug("skip %s: %s", ticker, exc)
            rejected += 1

    if not rows:
        return pd.DataFrame(
            columns=[
                "ticker",
                "score",
                "roc5",
                "roc20",
                "roc60",
                "volume_surprise",
                "trend_strength",
                "breakout",
                "near_high",
                "last_close",
                "avg_dollar_volume",
                "rank",
            ]
        )

    df = pd.DataFrame([m.__dict__ for m in rows])
    df["roc20_norm"] = _normalize(df["roc20"])
    df["roc60_norm"] = _normalize(df["roc60"])
    df["volume_surprise_norm"] = _normalize(df["volume_surprise"])
    df["score"] = df.apply(lambda r: _score_row(r, config), axis=1)
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1
    logger.info("rank_universe: scored=%d rejected=%d", len(df), rejected)
    return df

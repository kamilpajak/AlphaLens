"""Layer 4 orchestrator — composes the 4 signal modules per candidate.

Reads a Phase C ``thematic_candidates`` DataFrame, resolves each candidate's
SimFin industry peers once, runs the 4 signal scorers, then composes a
weighted ``layer4_weighted_score`` (insider 2× weight; others 1×; clipped to
1-5 per the locked plan §C5 doctrine).

Per-industry caching: a single :class:`_PerIndustryCache` instance computes
peer lists + memoizes the SimFin feature dicts so the same industry's peers
are not re-fetched across candidates that share an industry.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from alphalens.thematic.screening import (
    fcff_signal,
    insider_signal,
    sector_peers,
    technicals_signal,
    valuation_signal,
)

logger = logging.getLogger(__name__)


# ---- Per-signal "positive" rules ----------------------------------------


def insider_is_positive(*, score_usd: float | None) -> bool:
    """Positive when net opportunistic buy clears the verification threshold."""
    if score_usd is None:
        return False
    return score_usd > 50_000.0


def fcff_is_positive(*, sector_percentile: float | None) -> bool:
    """Positive when FCFF yield is at or above the sector median."""
    if sector_percentile is None:
        return False
    return sector_percentile >= 50.0


def valuation_is_positive(*, composite_percentile: float | None) -> bool:
    """Positive when the cheaper-is-better composite is at or above sector median."""
    if composite_percentile is None:
        return False
    return composite_percentile > 50.0


def technicals_are_positive(*, rsi: float | None, ma_distance_pct: float | None) -> bool:
    """Positive when RSI is in [30, 70] AND price is within ±15% of MA50.

    Cheap heuristic: extreme RSI (overbought/oversold) and large MA50
    distance suggest the move has already exhausted, so skip. Operator can
    revisit these thresholds after observing real candidates.
    """
    if rsi is None or ma_distance_pct is None:
        return False
    return 30.0 <= rsi <= 70.0 and -15.0 <= ma_distance_pct <= 15.0


_DEFAULT_SIGNAL_SHAPES = {
    "insider": {"score_usd": None, "sector_percentile": None},
    "fcff": {"yield_pct": None, "sector_percentile": None},
    "valuation": {
        "pe": None,
        "ps": None,
        "ev_rev": None,
        "fcf_margin": None,
        "composite_sector_percentile": None,
    },
    "technicals": {
        "rsi": None,
        "ma50_distance_pct": None,
        "atr_pct": None,
        "volume_zscore": None,
        "summary": "no data",
    },
}


def _safe_signal(name: str, fn, **kwargs):
    """Run a signal scorer; on exception log + return the default shape.

    Mirrors the Phase C orchestrator ``_safe`` pattern: a single ticker's
    data anomaly (corrupt Form-4 partition, SimFin parse error, yfinance
    schema drift) must not abort the entire batch. Caller sees the
    "missing-data" dict for that signal and continues with the others.
    """
    try:
        return fn(**kwargs)
    except Exception as exc:
        logger.warning(
            "signal %s raised for ticker=%s: %s", name, kwargs.get("ticker"), exc, exc_info=True
        )
        return dict(_DEFAULT_SIGNAL_SHAPES[name])


def compose_weighted_score(
    *,
    insider_positive: bool,
    fcff_positive: bool,
    valuation_positive: bool,
    technicals_positive: bool,
) -> int:
    """Combine boolean signals into a 1-5 confidence score.

    Weight rule (locked plan §C5): insider 2× others 1×. Floored at 1 so
    "no information" candidates still emit a value.
    """
    raw = (
        2 * int(insider_positive)
        + int(fcff_positive)
        + int(valuation_positive)
        + int(technicals_positive)
    )
    return max(1, min(5, raw))


# ---- Data-fetcher factories (kept thin so tests can swap them) ----------


def _build_feature_fetcher(tickers: list[str] | None = None) -> Callable[[str, date], dict | None]:
    """Build a SimFin ``ev_fcff_features_as_of`` lookup.

    ``tickers`` is forwarded to :meth:`SimFinFundamentalsStore.preload` so
    the store loads the bulk CSVs once for the full universe Phase D will
    query. Callers can pass an empty list — preload still loads the data
    (the ticker list only drives coverage validation, not fetching).

    On preload failure (RuntimeError from SimFin's <50% coverage abort, or
    any other exception) returns a stub fetcher that always returns None,
    so Layer 4 still emits structured "all-signals-missing" rows instead
    of failing the whole batch.
    """
    from alphalens.data.store.simfin import SimFinFundamentalsStore

    try:
        store = SimFinFundamentalsStore(with_prices=True)
        store.preload(tickers or [])
    except Exception as exc:
        logger.warning("SimFin preload aborted, valuation/fcff signals will be missing: %s", exc)
        return lambda ticker, asof: None

    cache: dict[tuple[str, date], dict | None] = {}

    def fetcher(ticker: str, asof: date) -> dict | None:
        key = (ticker.upper(), asof)
        if key in cache:
            return cache[key]
        features = store.ev_fcff_features_as_of(ticker, asof)
        cache[key] = features
        return features

    return fetcher


_THEMATIC_OHLCV_CACHE = Path.home() / ".alphalens" / "thematic_ohlcv"


def _build_ohlcv_loader() -> Callable[[str, date], pd.DataFrame]:
    """Build an OHLCV loader with disk + in-process cache.

    Disk cache lives at ``~/.alphalens/thematic_ohlcv/{TICKER}.parquet``.
    First miss triggers a live ``yfinance.Ticker.history`` fetch (180d
    lookback) and persists the result. Subsequent runs reuse the parquet
    until the operator clears the cache (no TTL — Phase D's 180d window
    is robust to ~1-day staleness; clear for a fresh fetch).
    """
    import yfinance as yf

    cache_dir = _THEMATIC_OHLCV_CACHE
    cache_dir.mkdir(parents=True, exist_ok=True)
    mem_cache: dict[str, pd.DataFrame] = {}

    def loader(ticker: str, asof: date) -> pd.DataFrame:
        upper = ticker.upper()
        if upper not in mem_cache:
            path = cache_dir / f"{upper}.parquet"
            df: pd.DataFrame
            if path.exists():
                try:
                    df = pd.read_parquet(path)
                except Exception as exc:
                    logger.warning("ohlcv cache read failed for %s: %s", upper, exc)
                    df = pd.DataFrame()
            else:
                try:
                    start = pd.Timestamp(asof) - pd.Timedelta(days=180)
                    end = pd.Timestamp(asof) + pd.Timedelta(days=1)
                    df = yf.Ticker(upper).history(start=start, end=end, auto_adjust=False)
                    df.columns = [c.lower() for c in df.columns]
                    if df.index.tz is not None:
                        df.index = df.index.tz_localize(None)
                    df = df[["open", "high", "low", "close", "volume"]]
                    if not df.empty:
                        df.to_parquet(path)
                except Exception as exc:
                    logger.warning("yfinance fetch failed for %s: %s", upper, exc)
                    df = pd.DataFrame()
            mem_cache[upper] = df
        df = mem_cache[upper]
        if df.empty:
            return df
        return df[df.index <= pd.Timestamp(asof)]

    return loader


# ---- Main entry point ---------------------------------------------------


def score_candidates(candidates: pd.DataFrame, *, asof: dt.date) -> pd.DataFrame:
    """Enrich the Phase C candidates frame with Layer 4 columns.

    Does not drop or reorder rows; appends 18 new columns (industry trio +
    4 signals × 3-5 fields each + weighted score). All numerical columns are
    nullable; string ``technicals_summary_str`` always renders.
    """
    if candidates.empty:
        return candidates.copy()

    # Collect candidate tickers + each industry's peer set so SimFin preload
    # validates coverage across the full universe Phase D will query.
    peer_cache: dict[int, list[str]] = {}
    universe: set[str] = set()
    for _, cand in candidates.iterrows():
        tkr = str(cand["ticker"]).upper()
        universe.add(tkr)
        ind = sector_peers.get_industry_id(tkr)
        if ind is not None:
            peer_cache.setdefault(ind, sector_peers.iter_industry_peers(ind))
            universe.update(peer_cache[ind])

    feature_fetcher = _build_feature_fetcher(sorted(universe))
    ohlcv_loader = _build_ohlcv_loader()

    rows: list[dict] = []
    for _, cand in candidates.iterrows():
        ticker = str(cand["ticker"]).upper()
        industry_id = sector_peers.get_industry_id(ticker)
        if industry_id is None:
            industry_name: str | None = None
            sector_name: str | None = None
            peers: list[str] = []
        else:
            industry_name, sector_name = sector_peers.industry_label(industry_id)
            if industry_id not in peer_cache:
                peer_cache[industry_id] = sector_peers.iter_industry_peers(industry_id)
            peers = peer_cache[industry_id]

        ins = _safe_signal(
            "insider", insider_signal.score_insider, ticker=ticker, asof=asof, peers=peers
        )
        fcff = _safe_signal(
            "fcff",
            fcff_signal.score_fcff,
            ticker=ticker,
            asof=asof,
            peers=peers,
            feature_fetcher=feature_fetcher,
        )
        val = _safe_signal(
            "valuation",
            valuation_signal.score_valuation,
            ticker=ticker,
            asof=asof,
            peers=peers,
            feature_fetcher=feature_fetcher,
        )
        tech = _safe_signal(
            "technicals",
            technicals_signal.score_technicals,
            ticker=ticker,
            asof=asof,
            loader=ohlcv_loader,
        )

        weighted = compose_weighted_score(
            insider_positive=insider_is_positive(score_usd=ins["score_usd"]),
            fcff_positive=fcff_is_positive(sector_percentile=fcff["sector_percentile"]),
            valuation_positive=valuation_is_positive(
                composite_percentile=val["composite_sector_percentile"]
            ),
            technicals_positive=technicals_are_positive(
                rsi=tech["rsi"], ma_distance_pct=tech["ma50_distance_pct"]
            ),
        )

        rows.append(
            {
                "ticker": ticker,
                "industry_id": industry_id if industry_id is not None else np.nan,
                "industry_name": industry_name,
                "sector_name": sector_name,
                "insider_score_usd": ins["score_usd"],
                "insider_score_sector_percentile": ins["sector_percentile"],
                "fcff_yield_pct": fcff["yield_pct"],
                "fcff_yield_sector_percentile": fcff["sector_percentile"],
                "valuation_pe": val["pe"],
                "valuation_ps": val["ps"],
                "valuation_ev_rev": val["ev_rev"],
                "valuation_fcf_margin": val["fcf_margin"],
                "valuation_composite_sector_percentile": val["composite_sector_percentile"],
                "technical_rsi": tech["rsi"],
                "technical_ma50_distance_pct": tech["ma50_distance_pct"],
                "technical_atr_pct": tech["atr_pct"],
                "technical_volume_zscore": tech["volume_zscore"],
                "technicals_summary_str": tech["summary"],
                "layer4_weighted_score": weighted,
            }
        )
    enrichment = pd.DataFrame(rows)
    # Merge on ticker to preserve original order + Phase C columns.
    merged = candidates.copy().reset_index(drop=True)
    merged["ticker"] = merged["ticker"].astype(str).str.upper()
    return merged.merge(enrichment, on="ticker", how="left")


__all__ = [
    "compose_weighted_score",
    "fcff_is_positive",
    "insider_is_positive",
    "score_candidates",
    "technicals_are_positive",
    "valuation_is_positive",
]

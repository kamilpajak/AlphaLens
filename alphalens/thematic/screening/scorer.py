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
    """Build a SimFin ``ev_fcff_features_as_of``-style lookup with NI added.

    ``tickers`` is forwarded to :meth:`SimFinFundamentalsStore.preload` so
    the store loads the bulk CSVs once for the full universe Phase D will
    query. Callers can pass an empty list — preload still loads the data
    (the ticker list only drives coverage validation, not fetching).
    """
    from alphalens.data.store.simfin import SimFinFundamentalsStore

    store = SimFinFundamentalsStore(with_prices=True)
    store.preload(tickers or [])
    cache: dict[tuple[str, date], dict | None] = {}

    def fetcher(ticker: str, asof: date) -> dict | None:
        key = (ticker.upper(), asof)
        if key in cache:
            return cache[key]
        ev_fcff = store.ev_fcff_features_as_of(ticker, asof)
        base = store.features_as_of(ticker, asof) or {}
        if ev_fcff is None:
            cache[key] = None
            return None
        # Merge net_income_ttm from the features_as_of side so valuation_signal
        # can compute P/E without re-pulling SimFin a third time.
        merged = dict(ev_fcff)
        merged["net_income_ttm"] = base.get("net_income_ttm")
        cache[key] = merged
        return merged

    return fetcher


def _build_ohlcv_loader() -> Callable[[str, date], pd.DataFrame]:
    """Build an on-demand OHLCV loader (yfinance live, 180d lookback).

    Phase D candidate batches are small (~5-20 tickers/day) so direct
    ``yf.Ticker(t).history`` calls are cheap. Cache is in-process per call;
    a future backfill workflow can pre-populate
    ``alphalens.data.alt_data.yfinance_cache`` if Phase D becomes batch-heavy.
    """
    import yfinance as yf

    cache: dict[str, pd.DataFrame] = {}

    def loader(ticker: str, asof: date) -> pd.DataFrame:
        if ticker not in cache:
            try:
                start = pd.Timestamp(asof) - pd.Timedelta(days=180)
                end = pd.Timestamp(asof) + pd.Timedelta(days=1)
                df = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
                df.columns = [c.lower() for c in df.columns]
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                cache[ticker] = df[["open", "high", "low", "close", "volume"]]
            except Exception as exc:
                logger.warning("yfinance fetch failed for %s: %s", ticker, exc)
                cache[ticker] = pd.DataFrame()
        df = cache[ticker]
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

        ins = insider_signal.score_insider(ticker=ticker, asof=asof, peers=peers)
        fcff = fcff_signal.score_fcff(
            ticker=ticker, asof=asof, peers=peers, feature_fetcher=feature_fetcher
        )
        val = valuation_signal.score_valuation(
            ticker=ticker, asof=asof, peers=peers, feature_fetcher=feature_fetcher
        )
        tech = technicals_signal.score_technicals(ticker=ticker, asof=asof, loader=ohlcv_loader)

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

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
    catalyst_signals,
    fcff_signal,
    insider_signal,
    magic_formula,
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
    """Positive when the cheaper-is-better composite is at or above sector median.

    Retained for the secondary sector-percentile transparency line in the
    brief renderer. The Magic Formula cohort rank is now the primary
    valuation gate consumed by ``compose_weighted_score``.
    """
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
        "financials_publish_date": None,
        "financials_age_days": None,
    },
    "technicals": {
        "rsi": None,
        "ma50_distance_pct": None,
        "atr_pct": None,
        "volume_zscore": None,
        "pct_off_52w_high": None,
        "pct_off_52w_low": None,
        "ma200_distance_pct": None,
        "ma200_slope_pct_per_day": None,
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
    magic_formula_top_quartile: bool,
    deep_drawdown_reversal: bool,
    technicals_positive: bool,
    catalyst_strength: float,
) -> int:
    """Combine boolean signals + catalyst strength into a 1-5 confidence score.

    Components:
    - insider 2× (Cohen-Malloy paradigm #11 doctrine)
    - fcff 1×, technicals 1×
    - value/reversal slot 1×: fires on EITHER Magic Formula top-quartile
      (mature value pick) OR deep-drawdown-reversal (thematic momentum
      pick) — two different alpha drivers, same slot
    - catalyst_floor 0-2: strong catalyst (≥0.70) lifts cohort by 2;
      moderate (≥0.25) by 1; weak by 0

    Result clipped to [1, 5].
    """
    val_or_reversal = int(magic_formula_top_quartile or deep_drawdown_reversal)
    raw = (
        2 * int(insider_positive)
        + int(fcff_positive)
        + val_or_reversal
        + int(technicals_positive)
        + catalyst_signals.catalyst_floor(catalyst_strength)
    )
    return max(1, min(5, raw))


# ---- Data-fetcher factories (kept thin so tests can swap them) ----------


def _build_feature_fetcher(tickers: list[str] | None = None) -> Callable[[str, date], dict | None]:
    """Build an EDGAR-backed ``ev_fcff_features_as_of`` lookup.

    ``tickers`` is forwarded to :meth:`EdgarFundamentalsStore.preload` so
    the store on-demand-fetches any missing CIK companyfacts from SEC
    (throttled to 10 req/s; first cold-cache run adds ~12 s per 100 missing
    tickers, subsequent runs are free).

    On preload failure (network outage, SEC 5xx) the store keeps any locally
    cached parquets and just logs the failed CIKs; this fetcher then returns
    the cached features per ticker (or None when no cache + no live fetch).
    """
    from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

    try:
        store = EdgarFundamentalsStore(with_prices=True)
        store.preload(tickers or [])
    except Exception as exc:
        logger.warning("EDGAR preload aborted, valuation/fcff signals will be missing: %s", exc)
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
            # Cache filename includes asof so cross-asof reruns don't silently
            # reuse a stale parquet whose tail predates the new evaluation
            # date (zen review 2026-05-17 HIGH finding).
            path = cache_dir / f"{upper}_{asof.isoformat()}.parquet"
            df: pd.DataFrame
            if path.exists():
                try:
                    df = pd.read_parquet(path)
                except Exception as exc:
                    logger.warning("ohlcv cache read failed for %s: %s", upper, exc)
                    df = pd.DataFrame()
            else:
                try:
                    start = pd.Timestamp(asof) - pd.Timedelta(days=400)
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

    # Per-theme catalyst lookup cache. ``find_trigger_event`` returns the
    # latest theme-tagged event (with full extraction metadata) so multiple
    # candidates sharing a theme reuse a single resolution.
    from alphalens.thematic.mapping import catalyst_resolver

    catalyst_cache: dict[str, dict | None] = {}

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

        # Magic Formula inputs — derived from the (cached) SimFin features
        # dict. ``feature_fetcher`` memoises per (ticker, asof) so this call
        # is free after the val/fcff signal scorers above.
        features = feature_fetcher(ticker, asof) or {}
        market_cap = _market_cap_from_features(features)
        mf_ev_ebitda = magic_formula.compute_ev_ebitda(
            features, market_cap=market_cap if market_cap is not None else float("nan")
        )
        mf_roic = magic_formula.compute_roic(features)
        mf_roe = magic_formula.compute_roe(features)
        mf_health = magic_formula.passes_health_gate(features)

        # Catalyst lookup — cached per theme. Returns extended event dict
        # (url, title, published_at, event_type, confidence, second_order_
        # implications). None when no theme-tagged event survives noise filter.
        theme = str(cand.get("theme", ""))
        if theme not in catalyst_cache:
            try:
                catalyst_cache[theme] = catalyst_resolver.find_trigger_event(theme=theme, asof=asof)
            except Exception as exc:
                logger.warning("catalyst lookup failed for theme=%r: %s", theme, exc)
                catalyst_cache[theme] = None
        catalyst_event = catalyst_cache[theme]
        cs_val = catalyst_signals.compute_catalyst_strength(catalyst_event)

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
                "valuation_ev_ebitda": mf_ev_ebitda,
                "valuation_fcf_margin": val["fcf_margin"],
                "valuation_composite_sector_percentile": val["composite_sector_percentile"],
                "valuation_financials_publish_date": val.get("financials_publish_date"),
                "valuation_financials_age_days": val.get("financials_age_days"),
                "roic_pct": mf_roic,
                "roe_pct": mf_roe,
                "magic_formula_health_pass": mf_health,
                "technical_rsi": tech["rsi"],
                "technical_ma50_distance_pct": tech["ma50_distance_pct"],
                "technical_atr_pct": tech["atr_pct"],
                "technical_volume_zscore": tech["volume_zscore"],
                "technical_pct_off_52w_high": tech.get("pct_off_52w_high"),
                "technical_pct_off_52w_low": tech.get("pct_off_52w_low"),
                "technical_ma200_distance_pct": tech.get("ma200_distance_pct"),
                "technical_ma200_slope_pct_per_day": tech.get("ma200_slope_pct_per_day"),
                "technicals_summary_str": tech["summary"],
                "catalyst_strength": cs_val,
                "catalyst_event_type": (catalyst_event or {}).get("event_type"),
                "catalyst_confidence": (catalyst_event or {}).get("confidence"),
                # Stashed for the reversal detector (source_event_url is
                # in candidates.parquet, merged AFTER this frame). Dropped
                # before the public DataFrame is built.
                "_catalyst_url": (catalyst_event or {}).get("url"),
                # Signal positives stashed for the post-loop weighted-score
                # composition; dropped before the public DataFrame is built.
                "_insider_positive": insider_is_positive(score_usd=ins["score_usd"]),
                "_fcff_positive": fcff_is_positive(sector_percentile=fcff["sector_percentile"]),
                "_technicals_positive": technicals_are_positive(
                    rsi=tech["rsi"], ma_distance_pct=tech["ma50_distance_pct"]
                ),
            }
        )
    enrichment = pd.DataFrame(rows)

    # Cohort-relative Magic Formula rank — operates on the survivor basket
    # rather than sector peers. ``valuation_pe`` here is the SimFin-derived
    # P/E from valuation_signal (NOT the Magic-Formula variant) to preserve
    # the existing column semantic; both compute the same way (market_cap /
    # net_income_ttm) so reuse is safe.
    enrichment["magic_formula_rank"] = magic_formula.compute_cohort_rank(enrichment)
    cohort_n = int(enrichment["magic_formula_health_pass"].sum())
    enrichment["magic_formula_cohort_n"] = cohort_n

    # Deep-drawdown-reversal is per-candidate (computed once columns are
    # final). The detector reads ``source_event_url`` so we temporarily
    # mirror the stashed ``_catalyst_url`` into that column for the apply,
    # then drop the temp. (candidates.parquet's own source_event_url
    # arrives via the final merge below — we don't want to collide.)
    enrichment["source_event_url"] = enrichment["_catalyst_url"]
    enrichment["deep_drawdown_reversal"] = enrichment.apply(
        catalyst_signals.is_deep_drawdown_reversal, axis=1
    )
    enrichment = enrichment.drop(columns=["source_event_url", "_catalyst_url"])

    enrichment["layer4_weighted_score"] = [
        compose_weighted_score(
            insider_positive=row["_insider_positive"],
            fcff_positive=row["_fcff_positive"],
            magic_formula_top_quartile=magic_formula.is_top_quartile(
                rank=row["magic_formula_rank"], cohort_n=cohort_n
            ),
            deep_drawdown_reversal=bool(row["deep_drawdown_reversal"]),
            technicals_positive=row["_technicals_positive"],
            catalyst_strength=float(row["catalyst_strength"]),
        )
        for _, row in enrichment.iterrows()
    ]
    enrichment = enrichment.drop(
        columns=["_insider_positive", "_fcff_positive", "_technicals_positive"]
    )

    # Merge on ticker to preserve original order + Phase C columns.
    merged = candidates.copy().reset_index(drop=True)
    merged["ticker"] = merged["ticker"].astype(str).str.upper()
    return merged.merge(enrichment, on="ticker", how="left")


def _market_cap_from_features(features: dict) -> float | None:
    """Compute market cap from SimFin price × shares. None if either missing
    or NaN/non-positive. NaN guard matters here: ``float(nan) <= 0`` is
    False, so without the isnan check NaN would silently pass through.
    """
    import math

    price = features.get("price")
    shares = features.get("shares_outstanding")
    if price is None or shares is None:
        return None
    try:
        p = float(price)
        s = float(shares)
    except (TypeError, ValueError):
        return None
    if math.isnan(p) or math.isnan(s) or p <= 0 or s <= 0:
        return None
    return p * s


__all__ = [
    "compose_weighted_score",
    "fcff_is_positive",
    "insider_is_positive",
    "score_candidates",
    "technicals_are_positive",
    "valuation_is_positive",
]

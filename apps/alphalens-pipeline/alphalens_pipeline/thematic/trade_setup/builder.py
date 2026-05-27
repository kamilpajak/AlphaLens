"""Orchestrate a deterministic Trade Setup from cached daily OHLCV.

Pipeline order (design memo §3): detect levels -> disaster stop -> entry
tiers -> equal-risk sizing -> blended entry -> stop floor revalidation ->
TP tranches -> assemble. The stop is computed BEFORE the tiers so the
ladder's min-stop-distance filter can run; the -25% floor is applied AFTER
sizing and the tiers are then re-validated against the (possibly raised)
stop.

All numbers here are deterministic and authoritative (cached OHLCV). The
LLM never produces a number — it only narrates ``(level, tag)`` pairs.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

import pandas as pd

from alphalens_pipeline.thematic.screening.technicals_signal import _compute_atr_pct
from alphalens_pipeline.thematic.trade_setup import ladder, levels, sizing
from alphalens_pipeline.thematic.trade_setup.model import (
    SCHEMA_VERSION,
    STATUS_OK,
    EntryTier,
    TpTranche,
    TradeSetup,
)

logger = logging.getLogger(__name__)

_MIN_BARS = 30
_SWING_THRESHOLD_MULT = 2.5
_STOP_ATR_BUFFER = 1.0  # stop = deepest support - 1*ATR
_SHALLOW_PULLBACK_MULT = 0.5  # nearest fallback entry = close - 0.5*ATR
_DEEP_FALLBACK_MULT = 2.0  # deep fallback entry = close - 2.0*ATR
_DISASTER_FLOOR_FRAC = 0.75  # stop >= blended_entry * 0.75 (i.e. >= -25%)
_DEFAULT_RISK_BUDGET_PCT = 1.0
_DEFAULT_ORDER_TTL_DAYS = 10


def _sma(close: pd.Series, period: int) -> float | None:
    if len(close) < period:
        return None
    val = close.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


def _jitter_stop(close: float, stop: float, atr: float) -> float:
    """Nudge the stop off exact round ATR multiples (anti stop-hunt, §7.5).

    If (close - stop)/ATR lands within 0.05 of a half-integer multiple
    (1.0, 1.5, 2.0, ...), push the stop down by 0.07*ATR. Deterministic.
    """
    if atr <= 0:
        return stop
    k = (close - stop) / atr
    nearest_half = round(k * 2) / 2
    if abs(k - nearest_half) <= 0.05:
        return stop - 0.07 * atr
    return stop


def _entry_candidates(
    close: float,
    atr: float,
    supports: list[float],
    sma50: float | None,
    sma200: float | None,
) -> list[tuple[float, str]]:
    """Assemble (price, tag) entry candidates strictly below close.

    Real swing-low zones first, then moving averages (only if below close —
    in a downtrend an MA above price is resistance, not support), then ATR
    volatility fallbacks so a name with no clean structure still yields a
    sane ladder.
    """
    candidates: list[tuple[float, str]] = [(s, "swing-low") for s in supports if s < close]
    if sma50 is not None and sma50 < close:
        candidates.append((sma50, "50-day MA"))
    if sma200 is not None and sma200 < close:
        candidates.append((sma200, "200-day MA"))
    candidates.append((close - _SHALLOW_PULLBACK_MULT * atr, "shallow pullback"))
    candidates.append((close - _DEEP_FALLBACK_MULT * atr, "volatility support"))
    return candidates


def _assemble_tiers(
    close: float,
    atr: float,
    candidates: list[tuple[float, str]],
    stop: float,
    risk_distribution: list[float] | None,
) -> tuple[list[EntryTier], float]:
    """Build tiers + equal-risk allocations -> (tiers, blended_entry)."""
    picked = ladder.build_entry_tiers(close, atr, candidates, stop)
    if not picked:
        return [], 0.0
    prices = [p for p, _ in picked]
    weights = risk_distribution[: len(prices)] if risk_distribution else None
    allocs = sizing.equal_risk_allocations(prices, stop, weights=weights)
    blended = sizing.blended_entry(prices, allocs)
    tiers = [
        EntryTier(limit=p, alloc_pct=a, atr_distance=(close - p) / atr, tag=tag)
        for (p, tag), a in zip(picked, allocs, strict=True)
    ]
    return tiers, blended


def build_trade_setup_from_frame(
    ohlcv: pd.DataFrame,
    *,
    risk_budget_pct: float = _DEFAULT_RISK_BUDGET_PCT,
    order_ttl_days: int = _DEFAULT_ORDER_TTL_DAYS,
    risk_distribution: list[float] | None = None,
) -> TradeSetup:
    """Compute a TradeSetup from an OHLCV frame (lowercase yfinance schema)."""
    if ohlcv is None or ohlcv.empty or "close" not in ohlcv.columns or len(ohlcv) < _MIN_BARS:
        return TradeSetup.no_structure(asof_close=0.0, atr=0.0, order_ttl_days=order_ttl_days)

    close_series = ohlcv["close"].astype(float)
    close = float(close_series.iloc[-1])
    atr_pct = _compute_atr_pct(ohlcv)
    if close <= 0 or atr_pct is None or atr_pct <= 0:
        return TradeSetup.no_structure(asof_close=close, atr=0.0, order_ttl_days=order_ttl_days)
    atr = atr_pct / 100.0 * close

    highs = ohlcv["high"].astype(float).tolist()
    lows = ohlcv["low"].astype(float).tolist()
    pivots = levels.detect_swing_points(highs, lows, threshold=_SWING_THRESHOLD_MULT * atr)
    supports, resistances = levels.support_resistance(close, pivots, atr)

    candidates = _entry_candidates(
        close, atr, supports, _sma(close_series, 50), _sma(close_series, 200)
    )
    deepest = min(p for p, _ in candidates)
    stop = _jitter_stop(close, deepest - _STOP_ATR_BUFFER * atr, atr)

    tiers, blended = _assemble_tiers(close, atr, candidates, stop, risk_distribution)
    if not tiers:
        return TradeSetup.no_structure(asof_close=close, atr=atr, order_ttl_days=order_ttl_days)

    # -25% disaster floor: never risk more than 25% from the blended entry.
    # If the structural stop is deeper, raise it and re-validate tiers (a
    # raised stop can push the deepest tier inside the min-stop-distance).
    floor = blended * _DISASTER_FLOOR_FRAC
    if stop < floor:
        stop = floor
        tiers, blended = _assemble_tiers(close, atr, candidates, stop, risk_distribution)
        if not tiers:
            return TradeSetup.no_structure(asof_close=close, atr=atr, order_ttl_days=order_ttl_days)

    prices = [t.limit for t in tiers]
    weights = risk_distribution[: len(prices)] if risk_distribution else None
    size = sizing.suggested_size_pct(prices, stop, risk_budget_pct, weights=weights)

    tp_raw = ladder.build_tp_tranches(close, atr, resistances, blended, stop)
    # Equal tranche weighting by default (no edge claim; keep it honest/simple).
    tp_pct = 100.0 / len(tp_raw) if tp_raw else 0.0
    tp_tranches = tuple(
        TpTranche(target=target, tranche_pct=tp_pct, r_multiple=r, tag=tag)
        for target, r, tag in tp_raw
    )

    return TradeSetup(
        schema_version=SCHEMA_VERSION,
        status=STATUS_OK,
        asof_close=close,
        atr=atr,
        disaster_stop=stop,
        suggested_size_pct=size,
        order_ttl_days=order_ttl_days,
        entry_tiers=tuple(tiers),
        tp_tranches=tp_tranches,
    )


def build_trade_setup(
    *,
    ticker: str,
    asof: dt.date,
    loader: Callable[[str, dt.date], pd.DataFrame],
    risk_budget_pct: float = _DEFAULT_RISK_BUDGET_PCT,
    order_ttl_days: int = _DEFAULT_ORDER_TTL_DAYS,
    risk_distribution: list[float] | None = None,
) -> TradeSetup:
    """Top-level: load OHLCV via ``loader`` (same cache as Layer 4) and build.

    A failed/empty load degrades to ``NO_STRUCTURE`` rather than raising —
    one bad ticker must not abort the brief batch.
    """
    try:
        ohlcv = loader(ticker, asof)
    except Exception as exc:
        logger.warning("trade_setup loader failed for %s: %s", ticker, exc)
        ohlcv = pd.DataFrame()
    return build_trade_setup_from_frame(
        ohlcv,
        risk_budget_pct=risk_budget_pct,
        order_ttl_days=order_ttl_days,
        risk_distribution=risk_distribution,
    )


__all__ = ["build_trade_setup", "build_trade_setup_from_frame"]

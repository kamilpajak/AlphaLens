"""Index-level market-state regime classifier (PR-1) — display-only, log-now.

Composes the PR-0 primitives into a discrete 4-state trend×volatility label
(+ ``unknown``) for a single index (SPY), per the design memo
(docs/research/market_state_signal_design_2026_07_05.md §1.3). The label is a
HEURISTIC, not an estimated regime; every threshold is FROZEN a-priori and
UNVALIDATED (usable history is <1y, so there is no honest in-sample fit — memo
§2). Nothing here feeds selection or ordering: it is a context label, held out
of the brief sort exactly like the expert panel (the PR-6 sort allowlist).

Design (memo §1.3):
- Trend axis {up, down, neutral}: price vs SMA200, SMA50 vs SMA200, SMA50 slope,
  with a ``DIST_FLAT_BAND`` deadband → neutral.
- Vol axis {low, high}: OR of a realized proxy (ATR% ≥ its own trailing quantile)
  and an implied proxy (VIX ≥ 25). The OR is a single pre-committed a-priori
  choice (crypto-origin), logged in the config version.
- neutral trend folds to up/down by ``sign(dist200)``; the (trend, vol) grid maps
  to the four named states. Any missing/insufficient input → ``unknown`` (a
  first-class token, never silently mapped to a real state).

``MARKET_STATE_CONFIG_VERSION`` is the sole poolability key for the label + all
telemetry; the deferred forward study partitions rows by it, never pools across
versions. The two-axis raw drivers are stamped alongside the bucket so the study
correlates the CONTINUOUS drivers, never only the label (the disagreement.py
discipline). This module is the pure classifier; the store/FRED I/O wrapper and
the broadcast ``enrich`` stamp are added in a later step of this PR.
"""

from __future__ import annotations

import math

import pandas as pd

from alphalens_pipeline.market.primitives import (
    atr_pct,
    bollinger_keltner_squeeze,
    normalized_slope,
    rolling_quantile_rank,
    sma,
)

# --- Frozen hyperparameters (memo §2.2 manifest). All UNVALIDATED literal priors;
# --- any change ⇒ bump MARKET_STATE_CONFIG_VERSION so old rows are not pooled.
SMA_FAST = 50
SMA_SLOW = 200
SLOPE_WIN = 20
SLOPE_EPS = 0.0
DIST_FLAT_BAND = 0.02
ATR_WIN = 14
ATR_QUANTILE_LOOKBACK = 252
ATR_HIGH_Q = 0.70
VIX_HIGH = 25.0
BB_WIN = 20
BB_K = 2.0
KC_WIN = 20
KC_MULT = 1.5

MARKET_STATE_CONFIG_VERSION = "mstate-v1-spy-sma50x200-atrq70-vix15_25-UNVALIDATED"

# The columns this signal stamps onto every (broadcast) row. ``market_state`` is
# the label; the rest are the raw continuous drivers + the poolability key.
MARKET_STATE_COLUMNS: tuple[str, ...] = (
    "market_state",
    "market_state_atr_pct",
    "market_state_atr_pct_q",
    "market_state_dist200",
    "market_state_vix",
    "market_state_vix_decile",
    "market_state_squeeze_on",
    "market_state_config_version",
)

_UNKNOWN = "unknown"

# (trend, vol) → named state (after the neutral fold).
_STATE_MAP = {
    ("up", "low"): "bull_quiet",
    ("up", "high"): "bull_volatile",
    ("down", "high"): "bear_volatile",
    ("down", "low"): "bear_quiet",
}


def _last(series: pd.Series) -> float:
    """Last value as a float, or NaN when the series is empty."""
    if len(series) == 0:
        return float("nan")
    return float(series.iloc[-1])


def classify_state(
    *,
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vix: pd.Series,
) -> dict[str, object]:
    """Classify the market state at the LAST bar of the trailing input series.

    ``close``/``high``/``low`` are the trailing index (SPY) daily bars ending at
    the as-of date; ``vix`` is the trailing VIX series ending at the same date.
    Returns the label under ``market_state`` plus the raw driver telemetry. Any
    missing/insufficient input yields ``market_state == 'unknown'`` with NaN
    telemetry where a driver could not be computed. Pure — no I/O.
    """
    telemetry: dict[str, object] = {
        "market_state_atr_pct": float("nan"),
        "market_state_atr_pct_q": float("nan"),
        "market_state_dist200": float("nan"),
        "market_state_vix": float("nan"),
        "market_state_vix_decile": float("nan"),
        "market_state_squeeze_on": None,
    }

    have_bars = len(close) > 0
    if have_bars:
        sma_fast = sma(close, SMA_FAST)
        sma_slow = sma(close, SMA_SLOW)
        slope_series = normalized_slope(sma_fast, window=SLOPE_WIN)
        atr_pct_series = atr_pct(high, low, close, window=ATR_WIN)
        atr_pct_q_series = rolling_quantile_rank(atr_pct_series, lookback=ATR_QUANTILE_LOOKBACK)
        squeeze_series = bollinger_keltner_squeeze(
            close, high, low, bb_window=BB_WIN, bb_k=BB_K, kc_window=KC_WIN, kc_mult=KC_MULT
        )

        c = _last(close)
        sma50_now = _last(sma_fast)
        sma200_now = _last(sma_slow)
        slope_now = _last(slope_series)
        atr_pct_now = _last(atr_pct_series)
        atr_pct_q_now = _last(atr_pct_q_series)
        dist200 = (
            (c - sma200_now) / sma200_now
            if math.isfinite(sma200_now) and sma200_now != 0.0
            else float("nan")
        )

        telemetry["market_state_atr_pct"] = atr_pct_now
        telemetry["market_state_atr_pct_q"] = atr_pct_q_now
        telemetry["market_state_dist200"] = dist200
        telemetry["market_state_squeeze_on"] = bool(squeeze_series.iloc[-1])
    else:
        c = sma50_now = sma200_now = slope_now = atr_pct_q_now = dist200 = float("nan")

    vix_now = _last(vix)
    if len(vix) > 0:
        telemetry["market_state_vix"] = vix_now
        telemetry["market_state_vix_decile"] = _last(
            rolling_quantile_rank(vix, lookback=ATR_QUANTILE_LOOKBACK)
        )

    # Any decision input missing/insufficient → unknown (first-class token).
    decision_inputs = (c, sma50_now, sma200_now, slope_now, dist200, atr_pct_q_now, vix_now)
    if not all(math.isfinite(x) for x in decision_inputs):
        return {"market_state": _UNKNOWN, **telemetry}

    vol = "high" if (atr_pct_q_now >= ATR_HIGH_Q or vix_now >= VIX_HIGH) else "low"

    if abs(dist200) <= DIST_FLAT_BAND:
        trend = "neutral"
    elif c > sma200_now and sma50_now > sma200_now and slope_now > SLOPE_EPS:
        trend = "up"
    elif c < sma200_now and sma50_now < sma200_now and slope_now < -SLOPE_EPS:
        trend = "down"
    else:
        trend = "neutral"

    if trend == "neutral":
        trend = "up" if dist200 >= 0 else "down"

    return {"market_state": _STATE_MAP[(trend, vol)], **telemetry}


__all__ = [
    "MARKET_STATE_COLUMNS",
    "MARKET_STATE_CONFIG_VERSION",
    "classify_state",
]

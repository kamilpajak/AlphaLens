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

import datetime as dt
import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.data.rs_history import DEFAULT_RS_HISTORY_ROOT
from alphalens_pipeline.market.primitives import (
    atr_pct,
    bollinger_keltner_squeeze,
    normalized_slope,
    rolling_quantile_rank,
    sma,
)

logger = logging.getLogger(__name__)

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


def _unknown_result() -> dict[str, Any]:
    """The fully-unknown classification — used when inputs are missing or I/O fails."""
    return {
        "market_state": _UNKNOWN,
        "market_state_atr_pct": float("nan"),
        "market_state_atr_pct_q": float("nan"),
        "market_state_dist200": float("nan"),
        "market_state_vix": float("nan"),
        "market_state_vix_decile": float("nan"),
        "market_state_squeeze_on": None,
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
) -> dict[str, Any]:
    """Classify the market state at the LAST bar of the trailing input series.

    ``close``/``high``/``low`` are the trailing index (SPY) daily bars ending at
    the as-of date; ``vix`` is the trailing VIX series ending at the same date.
    The two series' last values are read INDEPENDENTLY (``.iloc[-1]`` on each),
    so the SPY session and the VIX print may fall on slightly different calendar
    dates — acceptable for a daily regime label; the caller (:func:`classify`)
    is responsible for the ``<= asof`` PIT constraint on both.
    Returns the label under ``market_state`` plus the raw driver telemetry. Any
    missing/insufficient input yields ``market_state == 'unknown'`` with NaN
    telemetry where a driver could not be computed. Pure — no I/O.
    """
    telemetry: dict[str, Any] = {
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


# --- I/O wrapper + broadcast enrich (mirrors disagreement.enrich) --------------

INDEX_TICKER = "SPY"
VIX_SERIES_ID = "VIXCLS"
# Trailing sessions to load: the ATR% quantile (252) is the binding window; the
# margin absorbs holidays/gaps. Fewer on disk → classify_state returns 'unknown'.
_HISTORY_SESSIONS = 300

# Explicit empty-frame dtypes (memo §3.1): object for the two string columns,
# float64 for the drivers, nullable boolean for the squeeze flag. Missing
# conventions: ``'unknown'`` label / ``NaN`` driver floats / ``pd.NA`` squeeze.
_EMPTY_COLUMN_DTYPES: dict[str, str] = {
    "market_state": "object",
    "market_state_atr_pct": "float64",
    "market_state_atr_pct_q": "float64",
    "market_state_dist200": "float64",
    "market_state_vix": "float64",
    "market_state_vix_decile": "float64",
    "market_state_squeeze_on": "boolean",
    "market_state_config_version": "object",
}


def _session_dates_on_or_before(grouped_root: Path, asof: dt.date, limit: int) -> list[dt.date]:
    """The newest ``limit`` stored session dates on or before ``asof`` (PIT)."""
    if not grouped_root.is_dir():
        return []
    cutoff = asof.isoformat()
    stems = sorted(
        p.stem for p in grouped_root.glob("*.parquet") if len(p.stem) == 10 and p.stem <= cutoff
    )
    dates: list[dt.date] = []
    for stem in stems[-limit:]:
        try:
            dates.append(dt.date.fromisoformat(stem))
        except ValueError:
            continue
    return dates


def _load_index_ohlc(
    grouped_root: Path, ticker: str, asof: dt.date, sessions: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Assemble the trailing (close, high, low) series for ``ticker`` from the
    whole-market grouped-daily store — disk-only, PIT, split-adjusted."""
    tkr = ticker.upper()
    index: list[pd.Timestamp] = []
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for d in _session_dates_on_or_before(grouped_root, asof, sessions):
        path = grouped_root / f"{d.isoformat()}.parquet"
        try:
            df = pd.read_parquet(path, columns=["T", "h", "l", "c"])
        except (OSError, ValueError):
            continue
        sub = df[df["T"].astype(str).str.upper() == tkr]
        if sub.empty:
            continue
        row = sub.iloc[-1]
        try:
            c, h, low_v = float(row["c"]), float(row["h"]), float(row["l"])
        except (TypeError, ValueError):
            continue
        index.append(pd.Timestamp(d))
        closes.append(c)
        highs.append(h)
        lows.append(low_v)
    return (
        pd.Series(closes, index=index, dtype=float),
        pd.Series(highs, index=index, dtype=float),
        pd.Series(lows, index=index, dtype=float),
    )


def classify(
    asof: dt.date,
    *,
    grouped_root: Path | str | None = None,
    fred_client=None,
    ticker: str = INDEX_TICKER,
    sessions: int = _HISTORY_SESSIONS,
) -> dict[str, Any]:
    """Load the trailing index bars + VIX and classify the state at ``asof``.

    Disk-only for the index bars (grouped store); VIX via the canonical FRED
    client (injected in tests, ``FREDClient.from_env()`` in production). No raw
    HTTP — the store is local parquet and FRED routes through its one client.
    """
    root = Path(grouped_root) if grouped_root is not None else DEFAULT_RS_HISTORY_ROOT
    close, high, low = _load_index_ohlc(root, ticker, asof, sessions)
    if fred_client is None:
        from alphalens_pipeline.data.macro.fred_client import FREDClient

        fred_client = FREDClient.from_env()
    vix = fred_client.fetch_series(VIX_SERIES_ID)
    vix = vix[vix.index <= pd.Timestamp(asof)]  # PIT: never a future VIX print
    return classify_state(close=close, high=high, low=low, vix=vix)


def enrich(
    frame: pd.DataFrame,
    *,
    asof: dt.date,
    grouped_root: Path | str | None = None,
    fred_client=None,
    ticker: str = INDEX_TICKER,
    sessions: int = _HISTORY_SESSIONS,
) -> pd.DataFrame:
    """Return ``frame`` with the index-level market_state columns broadcast onto
    every row (the ``disagreement.enrich`` idiom).

    Market state is computed ONCE for ``asof`` and stamped identically on every
    candidate row; ``market_state_config_version`` is stamped unconditionally.
    With no rows, all columns are still added (zero length, stable dtypes) for a
    stable parquet schema.
    """
    out = frame.copy()
    if len(out) == 0:
        for col, dtype in _EMPTY_COLUMN_DTYPES.items():
            out[col] = pd.Series([], dtype=dtype)
        return out

    try:
        result = classify(
            asof,
            grouped_root=grouped_root,
            fred_client=fred_client,
            ticker=ticker,
            sessions=sessions,
        )
    except Exception:  # best-effort context enrichment
        # A store read / FRED fetch hiccup degrades to 'unknown' rather than
        # aborting the score stage (the buffett/oneil fail-soft precedent).
        logger.warning(
            "market_state: classify failed for %s — stamping 'unknown'",
            asof,
            exc_info=True,
        )
        result = _unknown_result()

    # Broadcast each column with its DECLARED dtype so a populated frame matches
    # the empty-frame schema exactly — in particular the nullable boolean squeeze
    # (a fail-soft ``None`` becomes ``<NA>``, not an object column).
    n = len(out)
    for col, dtype in _EMPTY_COLUMN_DTYPES.items():
        if col == "market_state_config_version":
            out[col] = MARKET_STATE_CONFIG_VERSION
        else:
            out[col] = pd.Series([result[col]] * n, index=out.index, dtype=dtype)
    return out


__all__ = [
    "MARKET_STATE_COLUMNS",
    "MARKET_STATE_CONFIG_VERSION",
    "classify",
    "classify_state",
    "enrich",
]

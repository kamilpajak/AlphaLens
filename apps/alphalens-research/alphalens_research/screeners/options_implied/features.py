"""Phase A — PIT-correct feature joiner for v7 options-implied screener.

Pre-registered as `v7_smd_options_implied_2026_05_02` per
`docs/research/preregistration/params_v7_smd_options_implied_2026_05_02.json`.

Reads from the immutable iVolatility smd cache (`~/.alphalens/ivolatility_smd/`)
and produces a 7-column feature frame per (ticker, asof):
- 4 options features extracted verbatim from vendor smd row:
  * ivp30                — 1y rolling percentile of ivx30 (PIT-validated 0.9990)
  * ivx30                — absolute 30d implied vol level
  * ivx180_minus_ivx30   — term-structure slope
  * ivx30_over_hv20      — vol risk premium ratio
- 3 equity controls computed locally from smd's daily `close` series:
  * reversal_1m  = −1 × (close[asof] / close[asof−21bd] − 1)
  * momentum_6m  =      close[asof−21bd] / close[asof−126bd] − 1   (Jegadeesh skip-month)
  * rv_30d       = annualised stdev log-returns over last 30 trading days

Universe construction (per pre-reg `universe_construction`):
- Dynamic optionable filter: smd `optVol > 0 OR (oi_call + oi_put) > 0` on t-1
- Min ADV (20-day median dollar volume) ≥ $2M
- Min underlying close ≥ $1
- Drop OTC pink (exchange ∈ {PINK, PINX, ...})
- Multi-row cross-listed tickers: keep US primary exchange row only

ETL anomaly bounds (drop rows): per pre-reg `etl_anomaly_bounds`.

Phase A gates (per pre-reg `phase_a_gates`):
- Coverage ≥ 70% non-NaN feature rows / (universe × asofs)
- Max pairwise |corr| < 0.85; if breached, deterministic drop hierarchy applies
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# Canonical feature ordering (LOCKED — must mirror pre-registration JSON's
# concatenation of feature_stack_options + feature_stack_equity_controls).
OPTIONS_FEATURES: tuple[str, ...] = (
    "ivp30",
    "ivx30",
    "ivx180_minus_ivx30",
    "ivx30_over_hv20",
)

EQUITY_CONTROLS: tuple[str, ...] = (
    "reversal_1m",
    "momentum_6m",
    "rv_30d",
)

FEATURE_NAMES: tuple[str, ...] = OPTIONS_FEATURES + EQUITY_CONTROLS


# ETL anomaly bounds — mirrors pre-reg `etl_anomaly_bounds`.
ETL_ANOMALY_BOUNDS = {
    "ivx30_max": 3.0,
    "ivx30_min": 0.05,
    "term_spread_abs_max": 1.5,
    "iv_hv_ratio_max": 10.0,
    "iv_hv_ratio_min": 0.1,
    "stock_price_min": 1.0,
}


# US primary exchanges. Cross-listed tickers (e.g. CTT NYSE+TSX) keep the US
# row only. PINK / OTC explicitly EXCLUDED for v7 (pre-reg drops OTC pink).
US_PRIMARY_EXCHANGES = frozenset(
    {
        "NYSE",
        "NASDAQ",
        "NASDAQ Stocks Exchange",
        "AMEX",
        "ARCA",
        "BATS",
        "XNAS",
        "XNYS",
        "XASE",
        "BATO",
    }
)


# Multicollinearity remediation hierarchy: pre-committed drop preferences for
# every plausible vol-cluster pair. Rationale: among vol-related features
# IVX30 (implied vol level) is the least-derived; downstream features are
# transforms (ratio, rank, spread) or proxies (realized vol). When two vol
# features co-vary above the gate, drop the more-derived one and keep the
# level. If a pair OUTSIDE the vol cluster trips (e.g. reversal_1m vs
# momentum_6m), that's a design surprise — function raises for caller review.
_MULTICOLLINEARITY_DROP_HIERARCHY: dict[frozenset[str], str] = {
    # IVX30 vs derivatives → drop derivative
    frozenset(["ivx30", "ivx30_over_hv20"]): "ivx30_over_hv20",
    frozenset(["ivp30", "ivx30"]): "ivp30",
    frozenset(["ivx30", "ivx180_minus_ivx30"]): "ivx180_minus_ivx30",
    frozenset(["ivx30", "rv_30d"]): "rv_30d",
    # IVP30 vs other derivatives → drop the further-derived one (IVP30 is
    # rank-vendor primary; ratio/spread/rv are all transforms of levels).
    frozenset(["ivp30", "ivx30_over_hv20"]): "ivx30_over_hv20",
    frozenset(["ivp30", "ivx180_minus_ivx30"]): "ivx180_minus_ivx30",
    frozenset(["ivp30", "rv_30d"]): "rv_30d",
    # term spread vs other derivatives → drop the lesser-grounded one
    frozenset(["ivx180_minus_ivx30", "ivx30_over_hv20"]): "ivx30_over_hv20",
    frozenset(["ivx180_minus_ivx30", "rv_30d"]): "rv_30d",
    # IV/HV ratio vs RV → drop RV (RV is half of the ratio's denominator;
    # ratio retains forward-vs-realized comparison while RV alone doesn't).
    frozenset(["ivx30_over_hv20", "rv_30d"]): "rv_30d",
}


# ---------------------------------------------------------------------------
# Internal helpers


_NUMERIC_COLUMNS = (
    "ivx30",
    "ivp30",
    "ivx180",
    "hv20",
    "close",
    "open",
    "high",
    "low",
    "stockVolume",
    "optVol",
    "openInterestCall",
    "openInterestPut",
)


def _coerce_numeric_columns(history: pd.DataFrame) -> pd.DataFrame:
    """Coerce known numeric vendor columns to float64. Vendor smd occasionally
    emits mixed-dtype columns (e.g. integer strings) that break `>` and `to_numpy(dtype=float)`."""
    for col in _NUMERIC_COLUMNS:
        if col in history.columns and history[col].dtype == object:
            history[col] = pd.to_numeric(history[col], errors="coerce")
    return history


def _filter_us_primary(history: pd.DataFrame) -> pd.DataFrame:
    """Keep rows whose `exchange` is a US primary exchange. Also coerces
    known-numeric vendor columns from object → float (cache passthrough
    sometimes preserves vendor's mixed-dtype strings)."""
    if "exchange" not in history.columns:
        return _coerce_numeric_columns(history.copy())
    mask = history["exchange"].astype(str).isin(US_PRIMARY_EXCHANGES)
    out = history.loc[mask].copy()
    return _coerce_numeric_columns(out)


def _slice_pit(history: pd.DataFrame, asof: str) -> pd.DataFrame:
    """Rows with `tradeDate <= asof` AND `ivp30` populated, sorted ascending.

    Vendor smd returns 7 daily rows per week (Sat/Sun carry-forward Friday's
    OHLCV but have NaN `ivp30`). Filtering on `ivp30` not-NaN drops those
    non-trading-day rows so `_last_row_or_none` picks the last actual
    trading bar at or before asof. Sporadic vendor gaps (rare; Mon
    holidays) also fall out via this filter.
    """
    if history.empty or "tradeDate" not in history.columns:
        return history
    asof_ts = pd.Timestamp(asof)
    sliced = history.loc[pd.to_datetime(history["tradeDate"]) <= asof_ts]
    if "ivp30" in sliced.columns:
        sliced = sliced.loc[sliced["ivp30"].notna()]
    return sliced.sort_values("tradeDate").reset_index(drop=True)


def _last_row_or_none(sliced: pd.DataFrame) -> pd.Series | None:
    if sliced.empty:
        return None
    return sliced.iloc[-1]


def _compute_options_features(row: pd.Series) -> dict[str, float] | None:
    """Map a single smd row to the 4 options features. Returns None if the
    row's ivx30/ivp30/ivx180/hv20 are missing (caller drops the row).

    Unit conversion: vendor smd returns IV/HV in PERCENT (e.g. 26.41 means
    26.41% = 0.2641 decimal vol). Pre-reg `etl_anomaly_bounds` are authored
    in decimal units. Divide vendor IV/HV by 100 here so downstream code
    operates on a single canonical unit. `ivp30` stays as 0-100 percentile
    per vendor convention (no scale conversion needed for Lasso).
    """
    ivx30 = row.get("ivx30")
    ivp30 = row.get("ivp30")
    ivx180 = row.get("ivx180")
    hv20 = row.get("hv20")
    if pd.isna(ivx30) or pd.isna(ivp30) or pd.isna(ivx180) or pd.isna(hv20):
        return None
    if hv20 == 0:
        return None
    ivx30_d = float(ivx30) / 100.0
    ivx180_d = float(ivx180) / 100.0
    hv20_d = float(hv20) / 100.0
    return {
        "ivp30": float(ivp30),
        "ivx30": ivx30_d,
        "ivx180_minus_ivx30": ivx180_d - ivx30_d,
        # Ratio is dimensionless; computed on decimal-form values
        "ivx30_over_hv20": ivx30_d / hv20_d,
    }


def _compute_equity_controls(sliced: pd.DataFrame) -> dict[str, float] | None:
    """Compute reversal_1m, momentum_6m, rv_30d from the last `close` series.

    Returns None if `sliced` doesn't have enough history (≥126 trading days).
    """
    closes = sliced["close"].to_numpy(dtype=float)
    n = len(closes)
    if n < 127:  # need close[t-126] for momentum_6m
        return None
    if not np.all(np.isfinite(closes[-127:])) or closes[-1] <= 0:
        return None

    close_now = closes[-1]
    close_21 = closes[-22]  # close[asof-21 bdays]
    close_126 = closes[-127]
    if close_21 <= 0 or close_126 <= 0:
        return None

    reversal_1m = -((close_now / close_21) - 1.0)
    momentum_6m = (close_21 / close_126) - 1.0

    # rv_30d: annualized stdev log-returns over last 30 trading days.
    # n >= 127 already guaranteed above, so n >= 31 is guaranteed too.
    log_rets = np.diff(np.log(closes[-31:]))
    if len(log_rets) < 30 or not np.all(np.isfinite(log_rets)):
        return None
    rv_30d = float(np.std(log_rets, ddof=1)) * np.sqrt(252.0)

    return {
        "reversal_1m": reversal_1m,
        "momentum_6m": momentum_6m,
        "rv_30d": rv_30d,
    }


def _check_optionable(prev_row: pd.Series) -> bool:
    """Pre-reg dynamic optionable filter on t-1: optVol > 0 OR (oi_c + oi_p) > 0.

    Vendor smd occasionally returns numeric fields as strings on certain
    rows (mixed-dtype columns). Coerce to float defensively.
    """

    def _to_float(v) -> float:
        if v is None:
            return 0.0
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if pd.isna(f) else f

    opt_vol = _to_float(prev_row.get("optVol", 0))
    oi_call = _to_float(prev_row.get("openInterestCall", 0))
    oi_put = _to_float(prev_row.get("openInterestPut", 0))
    return (opt_vol > 0) or ((oi_call + oi_put) > 0)


def _check_adv(sliced: pd.DataFrame, min_dollar: float = 2_000_000.0) -> bool:
    """20-day median dollar volume >= min_dollar.

    Uses the last 20 rows of (close × stockVolume); requires ≥20 rows.
    """
    if len(sliced) < 20 or "close" not in sliced.columns or "stockVolume" not in sliced.columns:
        return False
    tail = sliced.tail(20)
    closes = tail["close"].to_numpy(dtype=float)
    vols = tail["stockVolume"].to_numpy(dtype=float)
    dollar = closes * vols
    if not np.any(np.isfinite(dollar)):
        return False
    return float(np.nanmedian(dollar)) >= min_dollar


def _violates_etl_bounds(features: dict[str, float], close_now: float) -> bool:
    ivx30 = features["ivx30"]
    ratio = features["ivx30_over_hv20"]
    return (
        ivx30 > ETL_ANOMALY_BOUNDS["ivx30_max"]
        or ivx30 < ETL_ANOMALY_BOUNDS["ivx30_min"]
        or abs(features["ivx180_minus_ivx30"]) > ETL_ANOMALY_BOUNDS["term_spread_abs_max"]
        or ratio > ETL_ANOMALY_BOUNDS["iv_hv_ratio_max"]
        or ratio < ETL_ANOMALY_BOUNDS["iv_hv_ratio_min"]
        or close_now < ETL_ANOMALY_BOUNDS["stock_price_min"]
    )


# ---------------------------------------------------------------------------
# Main builder


def _build_row_for_asof(
    *,
    history: pd.DataFrame,
    asof: str,
    ticker: str,
    adv_min_dollar: float,
) -> dict | None:
    sliced = _slice_pit(history, asof)
    row = _last_row_or_none(sliced)
    if row is None or len(sliced) < 2:
        return None
    if not _check_optionable(sliced.iloc[-2]):
        return None
    close_now = row.get("close")
    if pd.isna(close_now) or close_now < ETL_ANOMALY_BOUNDS["stock_price_min"]:
        return None
    if not _check_adv(sliced, adv_min_dollar):
        return None
    opts = _compute_options_features(row)
    if opts is None or _violates_etl_bounds(opts, float(close_now)):
        return None
    controls = _compute_equity_controls(sliced)
    if controls is None:
        return None
    return {"asof": asof, "ticker": ticker.upper(), **opts, **controls}


def build_feature_frame(
    *,
    smd_loader: Callable[[str], pd.DataFrame | None],
    universe: Sequence[str],
    asof_dates: Sequence[str],
    adv_min_dollar: float = 2_000_000.0,
) -> pd.DataFrame:
    """PIT-correct feature joiner across the iVol smd cache.

    Parameters
    ----------
    smd_loader
        Callable mapping `ticker -> DataFrame | None` (cached vendor smd).
        See `alphalens_pipeline.data.alt_data.ivolatility_smd_cache.load_cached_smd`.
    universe
        Tickers to consider. Cross-listed entries handled via US-primary filter.
    asof_dates
        Rebalance dates (as ISO strings or pd.Timestamp-coercible).
    adv_min_dollar
        20-day median dollar-volume floor (default $2M per pre-reg).

    Returns
    -------
    pd.DataFrame
        Long-format frame with columns `[asof, ticker, *FEATURE_NAMES]`. One
        row per (asof, ticker) that passes universe + ETL filters.
    """
    asof_strs = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in asof_dates]

    rows: list[dict] = []
    for ticker in universe:
        history = smd_loader(ticker)
        if history is None or history.empty:
            continue
        history = _filter_us_primary(history)
        if history.empty:
            continue
        for asof in asof_strs:
            row = _build_row_for_asof(
                history=history,
                asof=asof,
                ticker=ticker,
                adv_min_dollar=adv_min_dollar,
            )
            if row is not None:
                rows.append(row)

    if not rows:
        return pd.DataFrame(columns=["asof", "ticker", *FEATURE_NAMES])

    out = pd.DataFrame(rows)
    return out.reindex(columns=["asof", "ticker", *FEATURE_NAMES])


# ---------------------------------------------------------------------------
# Phase A gates


def validate_phase_a_gates(
    frame: pd.DataFrame,
    *,
    coverage_min: float = 0.70,
    corr_max: float = 0.85,
) -> dict:
    """Apply pre-reg Phase A gates: coverage ≥ coverage_min and max pairwise
    |corr| < corr_max across FEATURE_NAMES.

    Returns
    -------
    dict
        {
          "coverage_pct": float,
          "coverage_pass": bool,
          "max_abs_corr": float,
          "offending_pair": tuple[str, str] | None,
          "multicollinearity_pass": bool,
        }
    """
    if frame.empty:
        return {
            "coverage_pct": 0.0,
            "coverage_pass": False,
            "max_abs_corr": float("nan"),
            "offending_pair": None,
            "multicollinearity_pass": False,
        }

    feat_cols = [c for c in FEATURE_NAMES if c in frame.columns]
    feat_block = frame[feat_cols]

    # Coverage = fraction of cells (non-NaN) across the feature block
    total_cells = feat_block.size
    non_nan = int(feat_block.notna().sum().sum())
    coverage_pct = non_nan / total_cells if total_cells else 0.0

    # Pairwise correlation
    corr = feat_block.corr().abs()
    np.fill_diagonal(corr.values, 0.0)
    if corr.empty:
        max_abs_corr = float("nan")
        offending_pair: tuple[str, str] | None = None
    else:
        max_abs_corr = float(corr.values.max())
        if max_abs_corr >= corr_max:
            # Argmax of upper triangle
            stacked = corr.where(np.triu(np.ones(corr.shape, dtype=bool), k=1)).stack()
            pair = stacked.idxmax()
            offending_pair = (pair[0], pair[1])
        else:
            offending_pair = None

    return {
        "coverage_pct": coverage_pct,
        "coverage_pass": coverage_pct >= coverage_min,
        "max_abs_corr": max_abs_corr,
        "offending_pair": offending_pair,
        "multicollinearity_pass": max_abs_corr < corr_max,
    }


def multicollinearity_drop_recommendation(*, offending_pair: tuple[str, str]) -> str:
    """Pre-committed deterministic drop hierarchy. Raises ValueError on a
    pair outside the hierarchy — design surprise, not auto-remediable.
    """
    key = frozenset(offending_pair)
    if key in _MULTICOLLINEARITY_DROP_HIERARCHY:
        return _MULTICOLLINEARITY_DROP_HIERARCHY[key]
    raise ValueError(
        f"No drop recommendation for pair {sorted(offending_pair)!r}. "
        "Investigate manually — pre-committed hierarchy covers only the "
        f"options-feature pairs anticipated in pre-reg: "
        f"{sorted({tuple(sorted(k)) for k in _MULTICOLLINEARITY_DROP_HIERARCHY})!r}."
    )

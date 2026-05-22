"""Phase A — PIT-correct feature joiner for the multi_source_two_stage screener.

Pre-registered as `multi_source_two_stage_2026_04_30` per
`docs/research/preregistration/params_multi_source_two_stage_2026_04_30.json`.

Builds a 21-column feature frame per (ticker, asof) by joining five PIT-correct
sources: insider Form 4 cluster parquet (post-F4 fix), per-ticker OHLCV via
HistoryStore.truncate_to, Fama-French/Carhart factors (for rolling β +
idiosyncratic vol), FRED daily macro (VIX, term spread), and derived
cross-sectional ranks + 4 pre-specified interactions.

PIT contract:
- All features read strictly `<= asof` (no forward bars).
- Insider scorer must be constructed with `delisting_events` to apply F4
  fire-sale exclusion (180d before delisting).
- Rolling-window features (β, idiov, returns, vol) read up to and including
  `close[asof]`. Engine convention enters trades at next-day close
  (HistoryStore.forward_return), so reading close[asof] is consistent.
- Cross-sectional ranks compute over the asof slice only; tickers with NaN
  inputs are excluded from rank denominator (effective ≥1y history filter
  on `ret_252d` / `rolling_beta_mkt_252d` / `near_52w_high_distance`).
- VIX-quartile thresholds frozen on train period; applied to entire frame
  (train + holdout). Phase A computes thresholds via `train_quartile_thresholds`.

Phase A scope: feature joiner only. Lasso fitting / nested CV / holdout reveal
live in Phase B (separate module + experiment script).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np
import pandas as pd

from alphalens_research.data.store.history import HistoryStore
from alphalens_research.screeners.insider_activity.parquet_scorer import ParquetInsiderScorer

logger = logging.getLogger(__name__)


# Canonical 21-feature ordering. MUST match `params_frozen.feature_whitelist`
# in `docs/research/preregistration/params_multi_source_two_stage_2026_04_30.json`
# exactly. Locked by `tests/test_multi_source_two_stage_features.py::test_feature_names_match_preregistration`.
FEATURE_NAMES: tuple[str, ...] = (
    "insider_log_count",
    "insider_log_dollar",
    "insider_cluster_window_days",
    "vix_level",
    "vix_change_20d",
    "term_spread_10y_3m",
    "ret_20d",
    "ret_60d",
    "ret_252d",
    "vol_realized_20d",
    "near_52w_high_distance",
    "dollar_volume_z_20d",
    "rolling_beta_mkt_252d",
    "idiosyncratic_vol_residual_60d",
    "rank_momentum_60d",
    "rank_lowvol_20d",
    "rank_dollar_volume_size",
    "interaction_insider_x_vix_high",
    "interaction_mom20_x_vol_regime",
    "interaction_mom20_x_size_quintile",
    "interaction_insider_x_mom20",
)

REGIME_LABELS = ("Q1_calm", "Q2", "Q3", "Q4_stress")


# ---------------------------------------------------------------------------
# Helpers


def _last_value_on_or_before(series: pd.Series, asof: date) -> float | None:
    """Last available value with index <= asof. Used for daily macro / FF
    series that may have holiday gaps.
    """
    asof_ts = pd.Timestamp(asof)
    sliced = series.loc[:asof_ts]
    if sliced.empty:
        return None
    val = sliced.iloc[-1]
    return None if pd.isna(val) else float(val)


def _value_n_bdays_before(series: pd.Series, asof: date, n_bdays: int) -> float | None:
    """Last available value with index <= (asof shifted back n business days).
    Tolerates holiday gaps by stepping the date back to a real index entry.
    """
    sliced = series.loc[: pd.Timestamp(asof)]
    if len(sliced) <= n_bdays:
        return None
    val = sliced.iloc[-(n_bdays + 1)]
    return None if pd.isna(val) else float(val)


def _truncate_close_volume(
    history_store: HistoryStore, ticker: str, asof: date
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return (closes, volumes) arrays up to and including asof, or None if empty."""
    df = history_store.truncate_to(ticker, asof)
    if df.empty:
        return None
    closes = df["close"].to_numpy(dtype=float)
    volumes = df["volume"].to_numpy(dtype=float)
    if not np.isfinite(closes).all() or (closes <= 0).any():
        return None
    return closes, volumes


def _ret_lookback(closes: np.ndarray, lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    base = closes[-(lookback + 1)]
    if base <= 0:
        return None
    return float(closes[-1] / base - 1.0)


def _realised_vol_log(closes: np.ndarray, window: int) -> float | None:
    """Std of daily log returns over the last `window` trading days."""
    if len(closes) <= window:
        return None
    rets = np.diff(np.log(closes[-(window + 1) :]))
    if len(rets) < window // 2 or np.any(~np.isfinite(rets)):
        return None
    return float(np.std(rets, ddof=1))


def _near_52w_high_distance(closes: np.ndarray, window: int = 252) -> float | None:
    """(close[asof] - rolling_max_252d) / rolling_max_252d. Always <= 0."""
    if len(closes) < window:
        return None
    high = float(np.max(closes[-window:]))
    if high <= 0:
        return None
    return float(closes[-1] / high - 1.0)


def _dollar_volume_z(closes: np.ndarray, volumes: np.ndarray, window: int = 20) -> float | None:
    """Z-score of today's dollar volume relative to last `window` days."""
    if len(closes) < window + 1:
        return None
    dollar_vol = closes[-(window + 1) :] * volumes[-(window + 1) :]
    history = dollar_vol[:-1]
    today = dollar_vol[-1]
    mean = float(np.mean(history))
    std = float(np.std(history, ddof=1))
    if std <= 0 or not np.isfinite(std):
        return None
    return float((today - mean) / std)


def _ticker_excess_return_series(
    history_store: HistoryStore,
    ticker: str,
    asof: date,
    rf_series: pd.Series,
) -> pd.Series | None:
    """Daily ticker excess returns aligned to RF. Index = trading days strictly
    before or equal to `asof` (the daily-return at index t requires close[t-1]
    and close[t], so the earliest valid date is the second bar).
    """
    df = history_store.truncate_to(ticker, asof)
    if df.empty or len(df) < 2:
        return None
    closes = df["close"]
    daily_ret = closes.pct_change().dropna()
    aligned = daily_ret.to_frame("ret").join(rf_series.rename("rf"), how="inner")
    if aligned.empty:
        return None
    return (aligned["ret"] - aligned["rf"]).rename(ticker)


def _rolling_beta_and_idiov(
    excess_ret: pd.Series,
    mkt_excess: pd.Series,
    asof: date,
    beta_window: int = 252,
    idiov_window: int = 60,
) -> tuple[float | None, float | None]:
    """OLS β of ticker excess returns on Mkt-RF over last `beta_window` days
    ending at `asof`. Idiosyncratic vol = std of last `idiov_window` regression
    residuals.
    """
    asof_ts = pd.Timestamp(asof)
    aligned = pd.concat(
        [excess_ret.loc[:asof_ts].rename("y"), mkt_excess.loc[:asof_ts].rename("x")],
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < beta_window:
        return None, None
    window = aligned.iloc[-beta_window:]
    x = window["x"].to_numpy(dtype=float)
    y = window["y"].to_numpy(dtype=float)
    x_var = float(np.var(x, ddof=1))
    if x_var <= 0:
        return None, None
    beta = float(np.cov(y, x, ddof=1)[0, 1] / x_var)
    alpha = float(np.mean(y) - beta * np.mean(x))
    residuals = y - alpha - beta * x
    if len(residuals) < idiov_window:
        return beta, None
    idiov = float(np.std(residuals[-idiov_window:], ddof=1))
    return beta, (idiov if np.isfinite(idiov) and idiov > 0 else None)


def train_quartile_thresholds(series: pd.Series, train_end: date) -> tuple[float, float, float]:
    """Quartile breakpoints (25%, 50%, 75%) of `series` over the train period
    `[start_of_series, train_end]`. Frozen and applied to entire frame.
    """
    train_end_ts = pd.Timestamp(train_end)
    train_slice = series.loc[:train_end_ts].dropna()
    if train_slice.empty:
        raise ValueError(f"Cannot fit VIX quartiles: series empty up to train_end={train_end}")
    q25, q50, q75 = train_slice.quantile([0.25, 0.50, 0.75]).tolist()
    return float(q25), float(q50), float(q75)


def assign_regime(vix_value: float | None, thresholds: tuple[float, float, float]) -> str:
    """Assign one of REGIME_LABELS based on VIX value vs frozen quartile cuts."""
    if vix_value is None or not np.isfinite(vix_value):
        return "Q2"  # neutral fallback for missing VIX
    q25, q50, q75 = thresholds
    if vix_value <= q25:
        return "Q1_calm"
    if vix_value <= q50:
        return "Q2"
    if vix_value <= q75:
        return "Q3"
    return "Q4_stress"


def _cross_sectional_rank(values: pd.Series) -> pd.Series:
    """Percentile rank (0..1) within the slice. NaN inputs propagate to NaN
    outputs (excluded from rank denominator). Single-element fallback = 0.5.
    """
    if len(values.dropna()) <= 1:
        return values.where(values.isna(), 0.5)
    return values.rank(pct=True, na_option="keep")


def _vix_high_indicator(vix_value: float | None, vix_history: pd.Series, asof: date) -> float:
    """Binary 1 if VIX > rolling-252d 75th percentile at asof, else 0.
    Rolling pct uses VIX history strictly <= asof.
    """
    if vix_value is None or not np.isfinite(vix_value):
        return 0.0
    asof_ts = pd.Timestamp(asof)
    history = vix_history.loc[:asof_ts].dropna().tail(252)
    if len(history) < 60:
        return 0.0
    p75 = float(history.quantile(0.75))
    return 1.0 if vix_value > p75 else 0.0


# ---------------------------------------------------------------------------
# Per-ticker feature row


def _compute_per_ticker_features(
    ticker: str,
    asof: date,
    *,
    history_store: HistoryStore,
    insider_scorer: ParquetInsiderScorer,
    mkt_excess: pd.Series,
    rf_series: pd.Series,
) -> dict | None:
    """Compute the subset of features that depend only on (ticker, asof) — no
    cross-sectional info. Returns a dict keyed by feature name, or None if the
    ticker has insufficient history for any non-optional feature.

    Insider features default to 0.0 when scorer returns None (matches "no
    signal" semantic per InsiderScorer contract). All other features may be
    NaN; downstream cross-sectional ranking handles it.
    """
    cv = _truncate_close_volume(history_store, ticker, asof)
    if cv is None:
        return None
    closes, volumes = cv

    # OHLCV-derived
    ret_20d = _ret_lookback(closes, 20)
    ret_60d = _ret_lookback(closes, 60)
    ret_252d = _ret_lookback(closes, 252)
    vol_20d = _realised_vol_log(closes, 20)
    near_high = _near_52w_high_distance(closes, 252)
    dv_z = _dollar_volume_z(closes, volumes, 20)

    # Rolling β + idiov vs Mkt-RF
    excess_ret = _ticker_excess_return_series(history_store, ticker, asof, rf_series)
    if excess_ret is not None:
        beta, idiov = _rolling_beta_and_idiov(excess_ret, mkt_excess, asof)
    else:
        beta, idiov = None, None

    # Insider features — default 0 on None (no signal), per scorer contract
    insider_feat = insider_scorer.features_as_of(ticker, asof)
    if insider_feat is None:
        insider_log_count = 0.0
        insider_log_dollar = 0.0
        insider_window = 0.0
    else:
        insider_log_count = float(np.log1p(insider_feat["insider_count"]))
        insider_log_dollar = float(np.log1p(max(insider_feat["aggregate_dollar"], 0.0)))
        insider_window = float(insider_feat["cluster_window_days"])

    return {
        "asof": asof,
        "ticker": ticker.upper(),
        "insider_log_count": insider_log_count,
        "insider_log_dollar": insider_log_dollar,
        "insider_cluster_window_days": insider_window,
        "ret_20d": _nan_if_none(ret_20d),
        "ret_60d": _nan_if_none(ret_60d),
        "ret_252d": _nan_if_none(ret_252d),
        "vol_realized_20d": _nan_if_none(vol_20d),
        "near_52w_high_distance": _nan_if_none(near_high),
        "dollar_volume_z_20d": _nan_if_none(dv_z),
        "rolling_beta_mkt_252d": _nan_if_none(beta),
        "idiosyncratic_vol_residual_60d": _nan_if_none(idiov),
    }


def _nan_if_none(v: float | None) -> float:
    return float("nan") if v is None else float(v)


def _vix_change_20d(vix_level: float | None, vix_series: pd.Series, asof: date) -> float:
    vix_20bd_ago = _value_n_bdays_before(vix_series, asof, 20)
    if vix_level is None or vix_20bd_ago is None or vix_20bd_ago == 0:
        return float("nan")
    return vix_level / vix_20bd_ago - 1.0


def _term_spread(dgs10: pd.Series, dgs3mo: pd.Series, asof: date) -> float:
    dgs10_at = _last_value_on_or_before(dgs10, asof)
    dgs3mo_at = _last_value_on_or_before(dgs3mo, asof)
    if dgs10_at is None or dgs3mo_at is None:
        return float("nan")
    return dgs10_at - dgs3mo_at


# ---------------------------------------------------------------------------
# Main builder


def build_feature_frame(
    *,
    history_store: HistoryStore,
    insider_scorer: ParquetInsiderScorer,
    carhart_factors: pd.DataFrame,
    fred_series: Mapping[str, pd.Series],
    universe: Sequence[str],
    asof_dates: Sequence[date],
    train_end: date,
    benchmark: str = "SPY",
) -> pd.DataFrame:
    """PIT-correct 21-feature joiner across 5 sources.

    Parameters
    ----------
    history_store
        OHLCV via PIT-correct truncate_to. MUST contain delisted tickers
        per O2 invariant (`tests/test_pit_universe_delisted_inclusion.py`).
    insider_scorer
        ParquetInsiderScorer constructed with delisting_events to apply F4
        fire-sale exclusion (`tests/test_pit_insider_delisting_exclusion.py`).
    carhart_factors
        Daily DataFrame with columns at minimum `Mkt-RF`, `RF` in decimal returns.
        Output of `alphalens_research.data.factors.load_carhart_daily()`.
    fred_series
        dict with `VIXCLS`, `DGS10`, `DGS3MO` daily Series. Caller composes
        from FREDClient.fetch().
    universe
        PIT-correct ticker list (yfinance + survivorship).
    asof_dates
        Rebalance dates to compute features for.
    train_end
        Inclusive last train-period date. Used to freeze VIX-quartile thresholds.
    benchmark
        Excluded from per-ticker feature loop.

    Returns
    -------
    pd.DataFrame
        Long-format frame with columns
        `[asof, ticker, *FEATURE_NAMES, regime]`.
        One row per (asof, ticker) pair where the per-ticker feature compute
        succeeded.
    """
    if "VIXCLS" not in fred_series:
        raise KeyError("fred_series must include VIXCLS")
    if "DGS10" not in fred_series or "DGS3MO" not in fred_series:
        raise KeyError("fred_series must include DGS10 and DGS3MO")
    if "Mkt-RF" not in carhart_factors.columns or "RF" not in carhart_factors.columns:
        raise KeyError("carhart_factors must include 'Mkt-RF' and 'RF'")

    vix_series = fred_series["VIXCLS"]
    dgs10 = fred_series["DGS10"]
    dgs3mo = fred_series["DGS3MO"]

    # Freeze VIX quartile thresholds on train period
    vix_thresholds = train_quartile_thresholds(vix_series, train_end)

    # Pre-extract Mkt-RF and RF series (decimal returns, daily index)
    mkt_excess = carhart_factors["Mkt-RF"]
    rf_series = carhart_factors["RF"]

    benchmark_up = benchmark.upper()
    universe_up = [t.upper() for t in universe if t.upper() != benchmark_up]

    def _build_asof_block(asof: date) -> pd.DataFrame | None:
        rows = [
            row
            for ticker in universe_up
            if (
                row := _compute_per_ticker_features(
                    ticker,
                    asof,
                    history_store=history_store,
                    insider_scorer=insider_scorer,
                    mkt_excess=mkt_excess,
                    rf_series=rf_series,
                )
            )
            is not None
        ]
        if not rows:
            return None
        df_asof = pd.DataFrame(rows)

        vix_level = _last_value_on_or_before(vix_series, asof)
        df_asof["vix_level"] = float("nan") if vix_level is None else vix_level
        df_asof["vix_change_20d"] = _vix_change_20d(vix_level, vix_series, asof)
        df_asof["term_spread_10y_3m"] = _term_spread(dgs10, dgs3mo, asof)

        df_asof["rank_momentum_60d"] = _cross_sectional_rank(df_asof["ret_60d"])
        df_asof["rank_lowvol_20d"] = _cross_sectional_rank(-df_asof["vol_realized_20d"])
        df_asof["rank_dollar_volume_size"] = _cross_sectional_rank(df_asof["dollar_volume_z_20d"])

        vix_high = _vix_high_indicator(vix_level, vix_series, asof)
        df_asof["interaction_insider_x_vix_high"] = df_asof["insider_log_count"] * vix_high
        df_asof["interaction_mom20_x_vol_regime"] = df_asof["ret_20d"] * df_asof["vol_realized_20d"]
        df_asof["interaction_mom20_x_size_quintile"] = (
            df_asof["ret_20d"] * df_asof["rank_dollar_volume_size"]
        )
        df_asof["interaction_insider_x_mom20"] = df_asof["insider_log_count"] * df_asof["ret_20d"]

        df_asof["regime"] = assign_regime(vix_level, vix_thresholds)
        return df_asof

    all_rows: list[pd.DataFrame] = []
    for asof in asof_dates:
        block = _build_asof_block(asof)
        if block is not None:
            all_rows.append(block)

    if not all_rows:
        return _empty_frame()

    out = pd.concat(all_rows, ignore_index=True)

    # Reorder columns: asof, ticker, then FEATURE_NAMES, then regime
    cols = ["asof", "ticker", *FEATURE_NAMES, "regime"]
    return out.reindex(columns=cols)


def _empty_frame() -> pd.DataFrame:
    cols = ["asof", "ticker", *FEATURE_NAMES, "regime"]
    return pd.DataFrame(columns=cols)

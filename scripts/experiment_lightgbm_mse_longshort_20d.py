"""Phase B v1 experiment driver — nonlinear_alt_data_v1_lightgbm_mse_2026_05_01.

Pre-registered as `nonlinear_alt_data_v1_lightgbm_mse_2026_05_01` in NEW class
`nonlinear_alt_data_search_2026_05_01`. In-class n=1 -> threshold |t|>=1.96
BUT v1 uses PROGRAM-LEVEL n=8 -> |t|>=2.74 as the actionable PASS gate.
Pre-reg: `docs/research/preregistration/params_nonlinear_alt_data_v1_lightgbm_mse_2026_05_01.json`.

Pivot from CLOSED alt_data_screener class (4/4 FAIL on linear-Lasso). v8
(LightGBM quantile alpha=0.5, max_depth=3, threshold 1.96) was REJECTED post
adversarial review. v9 (this driver) is the synthesis of zen + perplexity
recommendations:
- objective='regression' (MSE; real hessian; magnitude-aware)
- max_depth=5, min_child_samples=500 (less aggressive than v8's 3/2000)
- threshold |t|>=2.74 program-level (not fresh-class 1.96)

Same v4 selection rule (long top-decile EW MINUS short bottom-decile EW with
SI<=15% HTB filter), same 10 features, same train/holdout, same costs/HAC.

DIAGNOSTIC ONLY: capital deploy off-table regardless of holdout verdict.

10-feature whitelist (UNCHANGED from v4):
  earnings_sue_naive_4q_decayed, earnings_pead_5d_post_decayed,
  earnings_recency_days, short_interest_pct_float_change_60d,
  rank_short_interest_pct_float, log1p_days_to_cover, insider_log_count,
  insider_log_dollar, rank_realized_downside_skew_60d, filing_density_4q.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

from alphalens.archive.screeners.insider.parquet_scorer import ParquetInsiderScorer
from alphalens.attribution.cost_model import RealisticCostModel
from alphalens.attribution.factor_analysis import run_regression
from alphalens.backtest.metrics import max_drawdown, sharpe, sharpe_autocorr_adjusted
from alphalens.data.alt_data.polygon_short_interest import PolygonShortInterestClient
from alphalens.data.alt_data.shares_outstanding import (
    latest_shares_as_of,
    parse_company_facts,
)
from alphalens.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import load_carhart_daily
from alphalens.data.fundamentals.sue import FosterSUEStore
from alphalens.data.store.history import HistoryStore
from alphalens.data.store.survivorship_pit import load_delisting_events
from alphalens.screeners.alt_data.features import (
    FEATURE_NAMES as ALT_DATA_FEATURE_NAMES,
)
from alphalens.screeners.alt_data.features import (
    build_feature_frame,
)
from alphalens.screeners.multi_source_two_stage import (
    EMBARGO_DAYS_DEFAULT,
    N_FOLDS_DEFAULT,
    aligned_train_targets,
    build_target_frame,
    fit_lightgbm_mse_global,
    predict_scores_lightgbm,
    split_train_holdout,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
PRICES_DIR = Path.home() / ".alphalens" / "prices"
INSIDER_PARQUET = Path.home() / ".alphalens" / "insider_form4.parquet"
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
SURVIVORSHIP_YAML = (
    REPO_ROOT
    / "alphalens"
    / "archive"
    / "screeners"
    / "lean"
    / "lean_project"
    / "delisted_universe.yaml"
)
COMPANYFACTS_DIR = Path.home() / ".alphalens" / "companyfacts"
TICKER_CIK_MAP_PATH = REPO_ROOT / "alphalens" / "data" / "alt_data" / "data" / "ticker_cik_map.yaml"
POLYGON_SI_CACHE = Path.home() / ".alphalens" / "polygon_short_interest"

CARHART_COLS = ["Mkt-RF", "SMB", "HML", "Mom"]
HOLDING_PERIOD = 20
HAC_MAXLAGS = 5

# Program-level Bonferroni n=8 cumulative across classes on this holdout
# -> |t| >= 2.74 per pre-reg honest threshold (NOT fresh-class 1.96).
PASS_T = 2.74
PASS_SHARPE = 0.5
PASS_ALPHA_ANN = 0.03
PASS_MAX_DD = -0.35
MID_T = 1.5
MID_SHARPE = 0.3
PASS_IR = 1.0
MID_IR = 0.5
PASS_HOLDOUT_IC = 0.0
# Descriptive (not-gate) market-neutrality threshold per pre-reg
DESCRIPTIVE_BETA_MAX = 0.20

# Locked SI HTB filter on short leg (zen Objection #2 mitigation).
SHORT_LEG_SI_MAX = 0.15
# Borrow fee scenario (separate accounting; reported, NOT subtracted from headline).
BORROW_FEE_ANN = 0.015


# ---------------------------------------------------------------------------
# Data loaders (identical to v3 driver)


def _load_pit_union(start: date, end: date) -> list[str]:
    union: set[str] = set()
    for path in sorted(PIT_DIR.glob("*.yaml")):
        try:
            snap_date = date.fromisoformat(path.stem + "-01")
        except ValueError:
            continue
        if not (start.replace(day=1) <= snap_date <= end):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for ticker in data.get("tickers", []):
            union.add(ticker)
    return sorted(union)


def _build_calendar(
    store: HistoryStore, benchmark: str, start: date, end: date, stride: int, phase_offset: int
) -> list[date]:
    full = HistoryStore.benchmark_calendar(store, benchmark, start, end)
    if not full:
        return []
    if not 0 <= phase_offset < stride:
        raise ValueError(f"phase_offset must satisfy 0 <= offset < {stride}; got {phase_offset}")
    sliced = full[phase_offset::stride]
    return [ts.date() for ts in sliced]


def _adv_at_asof(
    history_store: HistoryStore, ticker: str, asof: date, window: int = 20
) -> float | None:
    df = history_store.truncate_to(ticker, asof)
    if len(df) < window:
        return None
    closes = df["close"].tail(window).to_numpy(dtype=float)
    volumes = df["volume"].tail(window).to_numpy(dtype=float)
    dollar_vol = closes * volumes
    if not (dollar_vol > 0).any():
        return None
    return float(np.median(dollar_vol[dollar_vol > 0]))


def _apply_adv_filter(
    feature_frame: pd.DataFrame, history_store: HistoryStore, adv_min_usd: float
) -> pd.DataFrame:
    if feature_frame.empty:
        return feature_frame
    keep_mask = []
    for row in feature_frame.itertuples(index=False):
        adv = _adv_at_asof(history_store, row.ticker, row.asof, window=20)
        keep_mask.append(adv is not None and adv >= adv_min_usd)
    return feature_frame.loc[keep_mask].reset_index(drop=True)


def _make_filings_lookup(companyfacts_dir: Path, ticker_cik_map: TickerCikMap):
    cache: dict[str, list[date]] = {}

    def _lookup(ticker: str, asof: date) -> list[date]:
        if ticker not in cache:
            cik = ticker_cik_map.lookup(ticker)
            if cik is None:
                cache[ticker] = []
                return []
            path = companyfacts_dir / f"{cik}.json"
            if not path.exists():
                cache[ticker] = []
                return []
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                cache[ticker] = []
                return []
            seen: set[tuple[str, str]] = set()
            gaap = payload.get("facts", {}).get("us-gaap", {}) or {}
            for concept in gaap.values():
                for unit_entries in (concept.get("units") or {}).values():
                    for raw in unit_entries:
                        form = (raw.get("form") or "").upper()
                        filed = raw.get("filed") or ""
                        if form.startswith(("10-Q", "10-K")) and filed:
                            seen.add((form, filed))
            dates = sorted({date.fromisoformat(f) for _, f in seen})
            cache[ticker] = dates
        return [d for d in cache[ticker] if d <= asof]

    return _lookup


def _make_shares_lookup(companyfacts_dir: Path, ticker_cik_map: TickerCikMap):
    facts_cache: dict[str, list] = {}

    def _lookup(ticker: str, asof: date) -> int | None:
        if ticker not in facts_cache:
            cik = ticker_cik_map.lookup(ticker)
            if cik is None:
                facts_cache[ticker] = []
                return None
            path = companyfacts_dir / f"{cik}.json"
            if not path.exists():
                facts_cache[ticker] = []
                return None
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                facts_cache[ticker] = []
                return None
            facts_cache[ticker] = parse_company_facts(payload, cik=cik)
        return latest_shares_as_of(facts_cache[ticker], asof)

    return _lookup


# ---------------------------------------------------------------------------
# v4 long/short decile portfolio builder


def _holdout_longshort_decile_returns_20d(
    holdout_features: pd.DataFrame,
    scores: pd.Series,
    history_store: HistoryStore,
    *,
    benchmark: str,
    polygon_si_client,
    shares_lookup,
    holding_period: int = HOLDING_PERIOD,
    si_max_short: float = SHORT_LEG_SI_MAX,
) -> tuple[
    pd.Series,
    pd.Series,
    list[list[str]],
    list[list[str]],
    pd.Series,
    pd.Series,
    list[int],
    list[int],
]:
    """Build market-neutral long/short decile portfolio per pre-reg v4.

    Long-eligible universe = full post-ADV cross-section.
    Short-eligible universe = post-ADV cross-section AND raw
        SI %% float <= si_max_short at asof (recomputed via polygon_si_client +
        shares_lookup since features.py drops the raw column after ranking).
    Decile size on each leg = floor(n_eligible / 10).
    Asof skipped if either leg's decile size < 3.

    Returns:
        ls_returns: per-asof L/S spread = long_mean - short_mean
        bench_returns: benchmark 20d-forward returns aligned by asof
        long_lists, short_lists: per-asof selected tickers (for turnover)
        long_returns, short_returns: per-asof leg means (for diagnostics)
        long_sizes, short_sizes: per-asof decile sizes
    """
    holdout_features = holdout_features.assign(_score=scores)
    holdout_features = holdout_features.dropna(subset=["_score"])
    asof_dates = sorted(holdout_features["asof"].unique())

    ls_h: list[float] = []
    long_h: list[float] = []
    short_h: list[float] = []
    bench_h: list[float] = []
    long_lists: list[list[str]] = []
    short_lists: list[list[str]] = []
    long_sizes: list[int] = []
    short_sizes: list[int] = []
    asof_idx_out: list[date] = []

    for asof in asof_dates:
        slice_df = holdout_features.loc[holdout_features["asof"] == asof]
        if slice_df.empty:
            continue

        n_long_eligible = len(slice_df)
        long_decile = n_long_eligible // 10
        if long_decile < 3:
            continue

        # Recompute raw SI% per ticker at asof for short-eligibility filter.
        si_pct_raw: list[float] = []
        for tk in slice_df["ticker"]:
            rec = polygon_si_client.features_as_of(tk, asof)
            if rec is None:
                si_pct_raw.append(float("nan"))
                continue
            shares = shares_lookup(tk, rec.settlement_date)
            if shares is None or shares <= 0:
                si_pct_raw.append(float("nan"))
                continue
            si_pct_raw.append(rec.short_interest / shares)
        si_arr = np.asarray(si_pct_raw, dtype=float)
        # NaN SI is treated as INELIGIBLE for short leg (cannot verify HTB risk).
        short_eligible_mask = np.isfinite(si_arr) & (si_arr <= si_max_short)
        short_eligible = slice_df.loc[short_eligible_mask]
        n_short_eligible = len(short_eligible)
        short_decile = n_short_eligible // 10
        if short_decile < 3:
            continue

        long_tickers = (
            slice_df.sort_values("_score", ascending=False).head(long_decile)["ticker"].tolist()
        )
        short_tickers = (
            short_eligible.sort_values("_score", ascending=True)
            .head(short_decile)["ticker"]
            .tolist()
        )

        long_rets = []
        for tk in long_tickers:
            r = history_store.forward_return(tk, asof, holding_period)
            long_rets.append(np.nan if r is None else r)
        short_rets = []
        for tk in short_tickers:
            r = history_store.forward_return(tk, asof, holding_period)
            short_rets.append(np.nan if r is None else r)
        long_arr = np.asarray(long_rets, dtype=float)
        short_arr = np.asarray(short_rets, dtype=float)
        if np.all(np.isnan(long_arr)) or np.all(np.isnan(short_arr)):
            continue

        long_mean = float(np.nanmean(long_arr))
        short_mean = float(np.nanmean(short_arr))
        ls_h.append(long_mean - short_mean)
        long_h.append(long_mean)
        short_h.append(short_mean)

        bench_r = history_store.forward_return(benchmark, asof, holding_period)
        bench_h.append(float("nan") if bench_r is None else bench_r)

        long_lists.append(long_tickers)
        short_lists.append(short_tickers)
        long_sizes.append(long_decile)
        short_sizes.append(short_decile)
        asof_idx_out.append(asof)

    asof_index = pd.DatetimeIndex([pd.Timestamp(d) for d in asof_idx_out])
    ls_returns = pd.Series(ls_h, index=asof_index, dtype=float, name="ls_return_20d")
    bench_returns = pd.Series(bench_h, index=asof_index, dtype=float, name="benchmark_return_20d")
    long_returns = pd.Series(long_h, index=asof_index, dtype=float, name="long_return_20d")
    short_returns = pd.Series(short_h, index=asof_index, dtype=float, name="short_return_20d")
    return (
        ls_returns,
        bench_returns,
        long_lists,
        short_lists,
        long_returns,
        short_returns,
        long_sizes,
        short_sizes,
    )


def _aggregate_carhart_20d(
    carhart: pd.DataFrame,
    asof_dates: Sequence[pd.Timestamp],
    holding_period: int = HOLDING_PERIOD,
) -> pd.DataFrame:
    aggregated_rows: list[dict] = []
    out_index: list[pd.Timestamp] = []
    for asof in asof_dates:
        future = carhart.loc[carhart.index > pd.Timestamp(asof)]
        if len(future) < holding_period:
            continue
        window = future.iloc[:holding_period]
        if window.isna().any().any():
            continue
        row = window.sum().to_dict()
        aggregated_rows.append(row)
        out_index.append(pd.Timestamp(asof))
    if not aggregated_rows:
        return pd.DataFrame(columns=carhart.columns)
    return pd.DataFrame(aggregated_rows, index=pd.DatetimeIndex(out_index))


def _turnover_per_rebal(top_lists: Sequence[Sequence[str]]) -> float:
    if len(top_lists) < 2:
        return 0.0
    snapshots = [frozenset(t) for t in top_lists]
    turns = []
    for prev, nxt in zip(snapshots[:-1], snapshots[1:], strict=False):
        size = max(len(prev), 1)
        turns.append(len(prev - nxt) / size)
    return float(np.mean(turns)) if turns else 0.0


def _assess_longshort(
    ls_returns: pd.Series,
    long_returns: pd.Series,
    short_returns: pd.Series,
    bench_returns: pd.Series,
    carhart: pd.DataFrame,
    rebalance_stride: int,
    cost_half_spread_bps: float,
    long_lists: Sequence[Sequence[str]],
    short_lists: Sequence[Sequence[str]],
) -> dict:
    rets = ls_returns.dropna()
    if rets.empty:
        return {"n": 0}
    rebalances_per_year = 252 / max(1, rebalance_stride)

    sharpe_gross = sharpe_autocorr_adjusted(
        rets.tolist(),
        periods_per_year=int(rebalances_per_year),
        max_lag=5,
    )
    sharpe_gross_naive = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))

    turn_long = _turnover_per_rebal(long_lists)
    turn_short = _turnover_per_rebal(short_lists)
    turn_total = turn_long + turn_short

    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_long_bps = cost_model.primary_period_drag_bps(cost_half_spread_bps, turn_long)
    drag_short_bps = cost_model.primary_period_drag_bps(cost_half_spread_bps, turn_short)
    drag_per_rebal_bps = drag_long_bps + drag_short_bps
    drag_per_rebal = drag_per_rebal_bps / 10_000.0
    drag_ann = drag_per_rebal * rebalances_per_year

    rets_net = rets - drag_per_rebal
    sharpe_net = sharpe_autocorr_adjusted(
        rets_net.tolist(),
        periods_per_year=int(rebalances_per_year),
        max_lag=5,
    )

    carhart_20d = _aggregate_carhart_20d(carhart, rets.index, holding_period=HOLDING_PERIOD)
    if len(carhart_20d) < 20:
        logger.warning(
            "20d-aggregated Carhart only %d rows; HAC regression underpowered",
            len(carhart_20d),
        )
    # L/S spread is already a relative excess (long minus short); no RF subtraction.
    res4 = run_regression(
        rets,
        carhart_20d[[*CARHART_COLS, "RF"]],
        CARHART_COLS,
        hac_maxlags=HAC_MAXLAGS,
        subtract_rf=False,
    )

    bench_aligned = bench_returns.reindex(rets.index).dropna()
    excess_per_rebal = (rets.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann = (
        float(excess_per_rebal * rebalances_per_year)
        if not np.isnan(excess_per_rebal)
        else float("nan")
    )

    cum = (1 + rets_net.fillna(0)).cumprod()
    mdd = float(max_drawdown(cum.tolist()))

    long_clean = long_returns.dropna()
    short_clean = short_returns.dropna()
    long_ann = float(long_clean.mean() * rebalances_per_year) if len(long_clean) else float("nan")
    short_ann = (
        float(short_clean.mean() * rebalances_per_year) if len(short_clean) else float("nan")
    )

    beta_mkt = float(res4.betas.get("Mkt-RF", float("nan")))

    return {
        "n": len(rets),
        "sharpe_gross": float(sharpe_gross),
        "sharpe_gross_naive": float(sharpe_gross_naive),
        "sharpe_net": float(sharpe_net),
        "alpha_gross_4f": float(res4.alpha_annualized),
        "t_4f": float(res4.alpha_tstat),
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "excess_vs_bench_ann": excess_ann,
        "excess_vs_bench_net": excess_ann - drag_ann,
        "max_drawdown": mdd,
        "cost_drag_ann": drag_ann,
        "borrow_fee_drag_ann": BORROW_FEE_ANN,
        "alpha_net_after_borrow": float(res4.alpha_annualized) - drag_ann - BORROW_FEE_ANN,
        "turnover_per_rebal_long": turn_long,
        "turnover_per_rebal_short": turn_short,
        "turnover_per_rebal_total": turn_total,
        "long_ann_return": long_ann,
        "short_ann_return": short_ann,
        "decile_size_long_mean": float(np.mean([len(s) for s in long_lists]))
        if long_lists
        else 0.0,
        "decile_size_short_mean": float(np.mean([len(s) for s in short_lists]))
        if short_lists
        else 0.0,
        "n_rebalances": len(long_lists),
        "carhart_aggregated_n": len(carhart_20d),
        "beta_mkt_carhart4f": beta_mkt,
        "beta_neutrality_ok": (np.isfinite(beta_mkt) and abs(beta_mkt) <= DESCRIPTIVE_BETA_MAX),
    }


def _compute_holdout_rank_ic(
    holdout_features: pd.DataFrame,
    scores: pd.Series,
    history_store: HistoryStore,
    *,
    holding_period: int = HOLDING_PERIOD,
) -> tuple[float, list[float]]:
    """Per-asof Spearman rank correlation between predictions and forward returns
    across the FULL post-ADV cross-section (NOT decile-restricted)."""
    holdout_features = holdout_features.assign(_score=scores)
    holdout_features = holdout_features.dropna(subset=["_score"])
    asof_dates = sorted(holdout_features["asof"].unique())
    per_asof_ic: list[float] = []
    for asof in asof_dates:
        slice_df = holdout_features.loc[holdout_features["asof"] == asof]
        if len(slice_df) < 3:
            continue
        rets = []
        for tk in slice_df["ticker"]:
            r = history_store.forward_return(tk, asof, holding_period)
            rets.append(np.nan if r is None else r)
        rets_arr = np.asarray(rets, dtype=float)
        scores_arr = slice_df["_score"].to_numpy(dtype=float)
        valid = np.isfinite(rets_arr) & np.isfinite(scores_arr)
        if valid.sum() < 3:
            continue
        if np.std(scores_arr[valid]) == 0 or np.std(rets_arr[valid]) == 0:
            continue
        from scipy.stats import spearmanr

        ic, _p = spearmanr(scores_arr[valid], rets_arr[valid])
        if np.isfinite(ic):
            per_asof_ic.append(float(ic))
    mean_ic = float(np.mean(per_asof_ic)) if per_asof_ic else float("nan")
    return mean_ic, per_asof_ic


def _compute_in_cv_ir_lgbm(
    fit,
    train_X: pd.DataFrame,
    train_y: pd.Series,
) -> float:
    """In-CV information ratio for LightGBM fit: mean(fold rank-IC) / std(fold rank-IC).

    Re-runs the same 3-fold expanding-window CV used in fit_lightgbm_mse_global.
    For each fold, refit LightGBM with the chosen n_estimators on train pos,
    predict val pos, compute per-asof Spearman rank-IC of preds vs raw target,
    average within fold; then compute mean/std across folds.
    """
    from lightgbm import LGBMRegressor
    from scipy.stats import spearmanr

    from alphalens.screeners.multi_source_two_stage.model import (
        _expanding_splits_with_embargo,
    )

    asof = train_X["asof"]
    splits = _expanding_splits_with_embargo(asof, n_folds=3, embargo_days=60)
    if not splits:
        return float("nan")

    feature_names = list(fit.feature_names)
    n_estimators = max(1, int(fit.chosen_alpha))
    base_kwargs = {
        "objective": "regression",
        "max_depth": 5,
        "num_leaves": 32,
        "min_child_samples": 500,
        "learning_rate": 0.05,
        "n_estimators": n_estimators,
        "reg_alpha": 0.1,
        "reg_lambda": 0.1,
        "feature_fraction": 1.0,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "random_state": 42,
        "verbose": -1,
        "n_jobs": -1,
    }

    fold_ics: list[float] = []
    for train_idx, val_idx in splits:
        train_pos = train_X.index.get_indexer(train_idx)
        val_pos = train_X.index.get_indexer(val_idx)
        X_full = train_X[feature_names].to_numpy(dtype=float)
        nan_mask = np.isnan(X_full)
        if nan_mask.any():
            med_b = np.broadcast_to(fit.feature_medians, X_full.shape).copy()
            X_full[nan_mask] = med_b[nan_mask]
        y_train = train_y.to_numpy(dtype=float)[train_pos]
        y_val = train_y.to_numpy(dtype=float)[val_pos]
        model = LGBMRegressor(**base_kwargs)
        model.fit(X_full[train_pos], y_train)
        preds = model.predict(X_full[val_pos])

        val_asof = asof.iloc[val_pos].values
        ics_in_fold: list[float] = []
        for asof_val in pd.unique(val_asof):
            mask = val_asof == asof_val
            if mask.sum() < 3:
                continue
            if np.std(preds[mask]) == 0 or np.std(y_val[mask]) == 0:
                continue
            ic, _p = spearmanr(preds[mask], y_val[mask])
            if np.isfinite(ic):
                ics_in_fold.append(float(ic))
        if ics_in_fold:
            fold_ics.append(float(np.mean(ics_in_fold)))
    if len(fold_ics) < 2:
        return float("nan")
    mean_ic = float(np.mean(fold_ics))
    std_ic = float(np.std(fold_ics, ddof=1))
    if std_ic <= 0:
        return float("nan")
    return mean_ic / std_ic


def _verdict_for(
    stats: Mapping,
    n_nonzero_coefs: int,
    *,
    in_cv_ir: float = float("nan"),
    holdout_mean_ic: float = float("nan"),
) -> str:
    """Pre-reg success_criteria translation. n=4 -> t>=2.50 for PASS,
    PLUS gates: in-CV IR >= 1.0 (mid 0.5), holdout mean rank-IC > 0."""
    t = stats.get("t_4f", 0.0)
    sh = stats.get("sharpe_net", 0.0)
    a = stats.get("alpha_gross_4f", 0.0)
    mdd = stats.get("max_drawdown", 0.0)

    if n_nonzero_coefs == 0:
        return "FAIL (zero-coef structural artifact)"
    if not np.isfinite(holdout_mean_ic) or holdout_mean_ic <= PASS_HOLDOUT_IC:
        return f"FAIL (holdout mean rank-IC <= 0; got {holdout_mean_ic:.4f})"
    if not np.isfinite(in_cv_ir) or in_cv_ir < MID_IR:
        return f"FAIL (in-CV IR < {MID_IR}; got {in_cv_ir:.3f})"
    if (
        t >= PASS_T
        and sh >= PASS_SHARPE
        and a >= PASS_ALPHA_ANN
        and mdd >= PASS_MAX_DD
        and in_cv_ir >= PASS_IR
    ):
        return f"PASS-DIAGNOSTIC (provisional — burnt-holdout caveat applies; t>={PASS_T:.2f}, IR>={PASS_IR}; capital deploy still off-table per pre-reg)"
    if t >= MID_T and sh >= MID_SHARPE and in_cv_ir >= MID_IR:
        return "MID (refine and re-pre-register before deploy)"
    return "FAIL (log to ledger)"


# ---------------------------------------------------------------------------
# Sub-period regime stratification (descriptive post-hoc)


def _regime_stratify(
    ls_returns: pd.Series,
    long_returns: pd.Series,
    short_returns: pd.Series,
    *,
    rebalance_stride: int,
) -> dict:
    """Sub-period decile alpha by year (2024 / 2025 / 2026) per pre-reg
    regime_stratification_postdoc clause. Descriptive — does NOT gate verdict.
    Per perplexity rec [4] regime-stratified analysis."""
    rebalances_per_year = 252 / max(1, rebalance_stride)
    out: dict = {}
    for label, start, end in [
        ("2024_partial", "2024-04-30", "2024-12-31"),
        ("2025_full", "2025-01-01", "2025-12-31"),
        ("2026_partial", "2026-01-01", "2026-04-30"),
    ]:
        mask = (ls_returns.index >= pd.Timestamp(start)) & (ls_returns.index <= pd.Timestamp(end))
        sub_ls = ls_returns.loc[mask].dropna()
        sub_long = long_returns.loc[mask].dropna()
        sub_short = short_returns.loc[mask].dropna()
        out[label] = {
            "n_rebal": len(sub_ls),
            "ls_ann": float(sub_ls.mean() * rebalances_per_year) if len(sub_ls) else float("nan"),
            "long_ann": float(sub_long.mean() * rebalances_per_year)
            if len(sub_long)
            else float("nan"),
            "short_ann": float(sub_short.mean() * rebalances_per_year)
            if len(sub_short)
            else float("nan"),
        }
    return out


# ---------------------------------------------------------------------------
# CLI


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--holding", type=int, default=HOLDING_PERIOD)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--adv-min-usd", type=float, default=5_000_000.0)
    ap.add_argument("--cost-half-spread-bps", type=float, default=10.0)
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument("--train-start", type=date.fromisoformat, default=date(2018, 1, 1))
    ap.add_argument("--holdout-start", type=date.fromisoformat, default=date(2024, 4, 30))
    ap.add_argument("--holdout-end", type=date.fromisoformat, default=date(2026, 4, 30))
    ap.add_argument(
        "--si-max-short",
        type=float,
        default=SHORT_LEG_SI_MAX,
        help="Locked at 0.15 by pre-reg; flag exists for tests/debug only.",
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke mode: 5 tickers x 12 asofs to verify end-to-end plumbing",
    )
    ap.add_argument(
        "--smoke-tickers",
        nargs="+",
        default=["AAPL", "MSFT", "NVDA", "TSLA", "META"],
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/nonlinear_alt_data_v1_phase_b.md"),
    )
    ap.add_argument(
        "--audit-out",
        type=Path,
        default=Path("docs/research/nonlinear_alt_data_v1_audit.json"),
    )
    return ap


def _emit_result_line(
    label: str, stats: Mapping, *, half_spread_bps: float, adv_min_usd: float
) -> None:
    if stats.get("n", 0) == 0:
        return
    logger.info(
        "%s | ADV>=$%.0fM cost=%.0fbps | n=%d Lsize=%.0f Ssize=%.0f turn L=%.1f%%/S=%.1f%% | "
        "Sh gross=%.2f net=%.2f | LS excess gross=%.2f%%/y net=%.2f%%/y | a 4F=%.1f%% t=%.2f β=%.2f",
        label,
        adv_min_usd / 1e6,
        half_spread_bps,
        stats["n"],
        stats["decile_size_long_mean"],
        stats["decile_size_short_mean"],
        stats["turnover_per_rebal_long"] * 100,
        stats["turnover_per_rebal_short"] * 100,
        stats["sharpe_gross"],
        stats["sharpe_net"],
        stats["excess_vs_bench_ann"] * 100,
        stats["excess_vs_bench_net"] * 100,
        stats["alpha_gross_4f"] * 100,
        stats["t_4f"],
        stats.get("beta_mkt_carhart4f", float("nan")),
    )


def main() -> int:
    load_dotenv()
    ap = _build_parser()
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    train_end = args.holdout_start - timedelta(days=1)
    full_start = args.train_start
    full_end = args.holdout_end

    if args.smoke:
        universe = list(args.smoke_tickers)
        logger.info("SMOKE mode: universe = %s", universe)
    else:
        universe = _load_pit_union(full_start, full_end)
        logger.info("PIT universe size %s..%s = %d", full_start, full_end, len(universe))

    histories = load_cached_histories([*universe, args.benchmark], PRICES_DIR)
    history_store = HistoryStore(histories)
    logger.info("history store has %d tickers", len(history_store.tickers()))

    delisting_events = load_delisting_events(
        parquet_path=SURVIVORSHIP_PARQUET if SURVIVORSHIP_PARQUET.exists() else None,
        yaml_path=SURVIVORSHIP_YAML if SURVIVORSHIP_YAML.exists() else None,
    )
    insider_scorer = ParquetInsiderScorer(INSIDER_PARQUET, delisting_events=delisting_events)

    ff_start = full_start - timedelta(days=400)
    carhart = load_carhart_daily(start=ff_start, end=full_end)
    logger.info(
        "carhart %d rows %s..%s",
        len(carhart),
        carhart.index.min().date() if len(carhart) else None,
        carhart.index.max().date() if len(carhart) else None,
    )

    cik_map = TickerCikMap.load(TICKER_CIK_MAP_PATH)
    sue_store = FosterSUEStore(companyfacts_dir=COMPANYFACTS_DIR, ticker_cik_map=cik_map)
    polygon_si = PolygonShortInterestClient.from_env(cache_dir=POLYGON_SI_CACHE)
    shares_lookup = _make_shares_lookup(COMPANYFACTS_DIR, cik_map)
    filings_lookup = _make_filings_lookup(COMPANYFACTS_DIR, cik_map)

    asof_dates = _build_calendar(
        history_store,
        args.benchmark,
        full_start,
        full_end,
        args.rebalance_stride,
        args.phase_offset,
    )
    if not asof_dates:
        logger.error("empty benchmark calendar")
        return 1
    if args.smoke:
        asof_dates = asof_dates[:: max(1, len(asof_dates) // 12)][:12]
    logger.info(
        "calendar: %d asof dates (stride=%d, phase=%d) %s..%s",
        len(asof_dates),
        args.rebalance_stride,
        args.phase_offset,
        asof_dates[0],
        asof_dates[-1],
    )

    feat_universe = [t for t in universe if t.upper() != args.benchmark.upper()]
    features = build_feature_frame(
        history_store=history_store,
        insider_scorer=insider_scorer,
        sue_store=sue_store,
        polygon_si_client=polygon_si,
        shares_lookup=shares_lookup,
        filings_lookup=filings_lookup,
        universe=feat_universe,
        asof_dates=asof_dates,
        benchmark=args.benchmark,
    )
    logger.info("feature frame raw shape: %s", features.shape)

    features = _apply_adv_filter(features, history_store, args.adv_min_usd)
    logger.info("feature frame post-ADV (>=$%.0fM): %s", args.adv_min_usd / 1e6, features.shape)
    if features.empty:
        logger.error("feature frame empty after ADV filter — abort")
        return 1

    targets = build_target_frame(
        features,
        history_store=history_store,
        rf_series=carhart["RF"],
        holding_period=args.holding,
    )
    logger.info(
        "target frame (holding=%dd): %d rows, %d non-NaN",
        args.holding,
        len(targets),
        int(targets["target"].notna().sum()),
    )

    feat_train, feat_holdout = split_train_holdout(features, args.holdout_start)
    targ_train, _ = split_train_holdout(targets, args.holdout_start)
    logger.info(
        "split — train: %d feature rows, %d target rows; holdout: %d feature rows",
        len(feat_train),
        len(targ_train),
        len(feat_holdout),
    )
    if feat_train.empty or feat_holdout.empty:
        logger.error("train or holdout split empty — check date boundaries")
        return 1

    train_X, train_y = aligned_train_targets(feat_train, targ_train)
    logger.info("train aligned: %d rows after NaN-target drop", len(train_X))
    if train_X.empty:
        logger.error("no train observations with non-NaN target — abort")
        return 1

    fit = fit_lightgbm_mse_global(
        train_X,
        train_y,
        n_folds=N_FOLDS_DEFAULT,
        embargo_days=EMBARGO_DAYS_DEFAULT,
        feature_names=ALT_DATA_FEATURE_NAMES,
    )
    if fit is None:
        logger.error("fit_lightgbm_mse_global returned None — abort")
        return 1
    logger.info(
        "lgbm-mse global fit: n_train=%d, n_estimators chosen=%.0f, "
        "nonzero importance feats=%d/%d, CV mean MSE=%.4g",
        fit.n_train_obs,
        fit.chosen_alpha,
        fit.n_nonzero_coefs,
        len(fit.feature_names),
        fit.cv_mean_mse,
    )
    importances = fit.model.feature_importances_
    feat_imp = sorted(
        [(fit.feature_names[i], int(importances[i])) for i in range(len(importances))],
        key=lambda t: -t[1],
    )
    logger.info("feature importances (split count): %s", feat_imp)

    scores = predict_scores_lightgbm(fit, feat_holdout)
    n_scored = int(scores.notna().sum())
    logger.info("holdout scored rows: %d / %d", n_scored, len(feat_holdout))

    (
        ls_returns,
        bench_returns,
        long_lists,
        short_lists,
        long_returns,
        short_returns,
        long_sizes,
        short_sizes,
    ) = _holdout_longshort_decile_returns_20d(
        feat_holdout,
        scores,
        history_store,
        benchmark=args.benchmark,
        polygon_si_client=polygon_si,
        shares_lookup=shares_lookup,
        holding_period=args.holding,
        si_max_short=args.si_max_short,
    )
    logger.info(
        "holdout L/S rebalances: %d (mean Lsize=%.1f Ssize=%.1f)",
        len(ls_returns),
        float(np.mean(long_sizes)) if long_sizes else 0.0,
        float(np.mean(short_sizes)) if short_sizes else 0.0,
    )
    if ls_returns.empty:
        logger.error("no holdout L/S rebalances — abort")
        return 1

    label = f"HOLDOUT L/S {args.holdout_start.year}-{args.holdout_end.year}"
    stats = _assess_longshort(
        ls_returns,
        long_returns,
        short_returns,
        bench_returns,
        carhart,
        args.rebalance_stride,
        args.cost_half_spread_bps,
        long_lists,
        short_lists,
    )
    _emit_result_line(
        label,
        stats,
        half_spread_bps=args.cost_half_spread_bps,
        adv_min_usd=args.adv_min_usd,
    )

    in_cv_ir = _compute_in_cv_ir_lgbm(fit, train_X, train_y)
    holdout_mean_ic, holdout_per_asof_ic = _compute_holdout_rank_ic(
        feat_holdout,
        scores,
        history_store,
        holding_period=args.holding,
    )
    logger.info(
        "v9 gates: in_cv_ir=%.3f (pass>=%.2f, mid>=%.2f) | holdout_mean_rank_ic=%.4f (pass>0) | n_holdout_asofs_with_ic=%d",
        in_cv_ir,
        PASS_IR,
        MID_IR,
        holdout_mean_ic,
        len(holdout_per_asof_ic),
    )
    stats["in_cv_ir"] = in_cv_ir
    stats["holdout_mean_rank_ic"] = holdout_mean_ic
    stats["holdout_n_asofs_with_ic"] = len(holdout_per_asof_ic)

    regime_stratification = _regime_stratify(
        ls_returns,
        long_returns,
        short_returns,
        rebalance_stride=args.rebalance_stride,
    )
    stats["regime_stratification"] = regime_stratification

    verdict = _verdict_for(
        stats,
        fit.n_nonzero_coefs,
        in_cv_ir=in_cv_ir,
        holdout_mean_ic=holdout_mean_ic,
    )
    logger.info(
        "verdict: %s | t=%.2f Sh_net=%.2f a_4F=%.1f%% MaxDD=%.1f%% nonzero=%d/10 IR=%.2f hold-IC=%.4f β=%.2f",
        verdict,
        stats.get("t_4f", float("nan")),
        stats.get("sharpe_net", float("nan")),
        stats.get("alpha_gross_4f", float("nan")) * 100,
        stats.get("max_drawdown", float("nan")) * 100,
        fit.n_nonzero_coefs,
        in_cv_ir,
        holdout_mean_ic,
        stats.get("beta_mkt_carhart4f", float("nan")),
    )
    for label_r, sub in regime_stratification.items():
        logger.info(
            "regime %s: n=%d L/S=%.1f%%/y long=%.1f%%/y short=%.1f%%/y",
            label_r,
            sub["n_rebal"],
            sub["ls_ann"] * 100,
            sub["long_ann"] * 100,
            sub["short_ann"] * 100,
        )

    if not args.smoke:
        _write_report(
            args.out,
            args,
            stats,
            fit,
            verdict,
            len(feat_train),
            len(train_X),
            len(feat_holdout),
            n_scored,
        )
        _write_audit(args.audit_out, args, stats, fit, verdict)
        logger.info("wrote -> %s", args.out)
        logger.info("audit -> %s", args.audit_out)
    return 0


def _write_audit(
    out_path: Path,
    args: argparse.Namespace,
    stats: Mapping,
    fit,
    verdict: str,
) -> None:
    nonzero = [
        (fit.feature_names[i], int(fit.model.feature_importances_[i]))
        for i in range(len(fit.feature_names))
        if int(fit.model.feature_importances_[i]) > 0
    ]
    audit = {
        "id": "nonlinear_alt_data_v1_lightgbm_mse_2026_05_01",
        "class": "nonlinear_alt_data_search_2026_05_01",
        "verdict": verdict,
        "headline_metrics": {k: v for k, v in stats.items() if k != "regime_stratification"},
        "regime_stratification": stats.get("regime_stratification", {}),
        "feature_importances": [{"feature": n, "split_count": c} for n, c in nonzero],
        "fit": {
            "n_train_obs": fit.n_train_obs,
            "n_estimators_chosen": fit.chosen_alpha,
            "n_nonzero_importance_feats": fit.n_nonzero_coefs,
            "cv_mean_mse": fit.cv_mean_mse,
            "feature_names": list(fit.feature_names),
        },
        "params": {
            "holdout_start": str(args.holdout_start),
            "holdout_end": str(args.holdout_end),
            "rebalance_stride": args.rebalance_stride,
            "holding": args.holding,
            "adv_min_usd": args.adv_min_usd,
            "cost_half_spread_bps": args.cost_half_spread_bps,
            "si_max_short": args.si_max_short,
            "phase_offset": args.phase_offset,
        },
    }
    out_path.write_text(json.dumps(audit, indent=2, default=str))


def _write_report(
    out_path: Path,
    args: argparse.Namespace,
    stats: Mapping,
    fit,
    verdict: str,
    n_feat_train: int,
    n_train_aligned: int,
    n_feat_holdout: int,
    n_scored: int,
) -> None:
    nonzero = sorted(
        [
            (fit.feature_names[i], int(fit.model.feature_importances_[i]))
            for i in range(len(fit.feature_names))
            if int(fit.model.feature_importances_[i]) > 0
        ],
        key=lambda t: -t[1],
    )
    coefs_md = (
        "\n".join(f"  - `{n}` = {c} splits" for n, c in nonzero)
        if nonzero
        else "  - (no features used by trees)"
    )

    regime = stats.get("regime_stratification", {})
    regime_lines = []
    for label_r in ["2024_partial", "2025_full", "2026_partial"]:
        sub = regime.get(label_r, {})
        regime_lines.append(
            f"| {label_r} | {sub.get('n_rebal', 0)} | "
            f"{sub.get('ls_ann', float('nan')) * 100:+.2f}% | "
            f"{sub.get('long_ann', float('nan')) * 100:+.2f}% | "
            f"{sub.get('short_ann', float('nan')) * 100:+.2f}% |"
        )

    lines = [
        "# nonlinear_alt_data_v1 Phase B — holdout reveal (LightGBM MSE, decile L/S)",
        "",
        "**Pre-registration:** `nonlinear_alt_data_v1_lightgbm_mse_2026_05_01` "
        "(NEW class `nonlinear_alt_data_search_2026_05_01`; in-class n=1 fresh "
        "|t|>=1.96, but PASS gate is PROGRAM-LEVEL n=8 -> |t|>=2.74 across all "
        "classes on this holdout)",
        "**Burnt-holdout caveat:** v1 is DIAGNOSTIC ONLY. Capital deploy "
        "off-table regardless of verdict.",
        f"**Phase offset:** {args.phase_offset} (stride {args.rebalance_stride}, "
        f"holding {args.holding}d, overlap 4-tranche)",
        f"**Train:** {args.train_start} -> {args.holdout_start - timedelta(days=1)}",
        f"**Holdout:** {args.holdout_start} -> {args.holdout_end}",
        f"**ADV floor:** ${args.adv_min_usd / 1e6:.0f}M, 20d median",
        f"**Cost:** half-spread {args.cost_half_spread_bps:.0f} bps + 5 bps adverse selection per leg",
        f"**Short-leg HTB filter:** SI %% float <= {args.si_max_short:.0%} (zen Objection #2 mitigation)",
        f"**Borrow fee scenario:** {BORROW_FEE_ANN:.1%} annualized on short notional (separate accounting)",
        f"**HAC maxlags:** {HAC_MAXLAGS} (overlap correction)",
        "**Sharpe:** Lo (2002) variance-ratio adjusted",
        "",
        f"## Verdict: {verdict}",
        "",
        "## Headline metrics (holdout)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| n rebalances (overlapping 20d) | {stats.get('n', 0)} |",
        f"| mean decile size (long) | {stats.get('decile_size_long_mean', 0):.1f} |",
        f"| mean decile size (short) | {stats.get('decile_size_short_mean', 0):.1f} |",
        f"| turnover/rebal long | {stats.get('turnover_per_rebal_long', 0) * 100:.1f}% |",
        f"| turnover/rebal short | {stats.get('turnover_per_rebal_short', 0) * 100:.1f}% |",
        f"| Sharpe (gross, Lo-adj) | {stats.get('sharpe_gross', 0):.2f} |",
        f"| Sharpe (gross, naive) | {stats.get('sharpe_gross_naive', 0):.2f} |",
        f"| Sharpe (net, Lo-adj) | {stats.get('sharpe_net', 0):.2f} |",
        f"| alpha (gross, 4F) annualised | {stats.get('alpha_gross_4f', 0) * 100:+.2f}% |",
        f"| alpha (net, 4F) annualised | {stats.get('alpha_net_4f', 0) * 100:+.2f}% |",
        f"| alpha t-stat (HAC, maxlags={HAC_MAXLAGS}) | {stats.get('t_4f', 0):+.2f} |",
        f"| **Mkt-RF beta (Carhart 4F)** | {stats.get('beta_mkt_carhart4f', float('nan')):+.3f} |",
        f"| beta-neutrality OK (\\|β\\| ≤ 0.20) | {stats.get('beta_neutrality_ok', False)} |",
        f"| Excess vs SPY (gross) ann | {stats.get('excess_vs_bench_ann', 0) * 100:+.2f}% |",
        f"| Excess vs SPY (net) ann | {stats.get('excess_vs_bench_net', 0) * 100:+.2f}% |",
        f"| long leg ann return | {stats.get('long_ann_return', 0) * 100:+.2f}% |",
        f"| short leg ann return | {stats.get('short_ann_return', 0) * 100:+.2f}% |",
        f"| Max drawdown (net cum L/S) | {stats.get('max_drawdown', 0) * 100:+.2f}% |",
        f"| Cost drag annualised (60bps round-trip) | {stats.get('cost_drag_ann', 0) * 100:.2f}% |",
        f"| Borrow fee drag (separate) | {stats.get('borrow_fee_drag_ann', 0) * 100:.2f}% |",
        f"| alpha net after borrow scenario | {stats.get('alpha_net_after_borrow', 0) * 100:+.2f}% |",
        f"| Carhart aggregated n | {stats.get('carhart_aggregated_n', 0)} |",
        f"| in-CV IR (mean fold IC / std) | {stats.get('in_cv_ir', float('nan')):.3f} |",
        f"| holdout mean per-asof rank-IC | {stats.get('holdout_mean_rank_ic', float('nan')):+.4f} |",
        f"| holdout asofs with valid IC | {stats.get('holdout_n_asofs_with_ic', 0)} |",
        "",
        "## Regime stratification (descriptive — NOT a verdict gate)",
        "",
        "| Sub-period | n rebal | L/S ann | Long ann | Short ann |",
        "| --- | ---: | ---: | ---: | ---: |",
        *regime_lines,
        "",
        "## Train/holdout sizes",
        "",
        f"- feature rows in train pool: {n_feat_train}",
        f"- train rows after target NaN-drop: {n_train_aligned}",
        f"- feature rows in holdout: {n_feat_holdout}",
        f"- holdout rows scored: {n_scored}",
        "",
        "## LightGBM MSE fit (raw 20d-forward target, magnitude-aware)",
        "",
        f"- n_train: {fit.n_train_obs}",
        f"- n_estimators chosen (CV early stopping): {fit.chosen_alpha:.0f}",
        f"- features used by trees: {fit.n_nonzero_coefs} / {len(fit.feature_names)}",
        f"- CV mean MSE: {fit.cv_mean_mse:.4g}",
        "",
        "### Feature importances (split count)",
        "",
        coefs_md,
        "",
        "## Comparison to prior cross-class results on this holdout",
        "",
        "Prior CLOSED class `alt_data_screener_search_2026_04_30` (4/4 FAIL):",
        "- v1 (FINRA daily flow): ABANDONED (infra block)",
        "- v2 (Polygon SI, raw return target, top-30): alpha-t=+0.05, FAIL (0/10 coefs)",
        "- v3 (rank target, top-30): alpha-t=-4.32, FAIL (2/10 coefs, +0.0260 holdout rank-IC)",
        "- v4 (rank target, decile L/S + SI<=15%): alpha-t=-2.57, FAIL (short leg squeezed)",
        "",
        "v9 fresh-class pivot: model class linear-Lasso -> tree-boosting LightGBM with "
        "MSE objective. Same selection rule as v4 (settled). Tests whether nonlinear "
        "magnitude-aware modeling extracts additional signal.",
    ]
    out_path.write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())

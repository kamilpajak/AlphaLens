"""Phase B v6a-revised driver — alt_data_screener_v6a_revised_2026_05_01.

Pre-registered as `alt_data_screener_v6a_revised_2026_05_01` in class
`alt_data_screener_search_2026_04_30` (n=6 in-class but threshold uses
ROMANO-WOLF m≈50 → |t| ≥ 3.5 PRIMARY, |t| ≥ 4.0 STRETCH per HARKing
mitigation).
Pre-reg: `docs/research/preregistration/params_alt_data_screener_v6a_revised_2026_05_01.json`.

EXPLORATORY DIAGNOSTIC. Original v6a (mega-cap exclusion, SPY benchmark,
Bonferroni n=12) REJECTED by zen + perplexity adversarial review
2026-05-01 PM as mathematically self-defeating. Five mitigations adopted:
1. MDY benchmark (cap-matched, NOT SPY)
2. top-10% mkt-cap percentile filter (exogenous, NOT arbitrary count)
3. Lasso retrained on non-mega-cap train pool only
4. threshold |t|≥3.5 (Romano-Wolf m≈50)
5. EXPLORATORY framing — PASS requires fresh-OOS confirmation

THREE variables change vs v5:
v5: full universe selection, full universe Lasso fit, SPY benchmark.
v6a-revised: drop top-10% mkt-cap from selection AND train pool, refit
Lasso on subset, MDY benchmark.

Capital deploy OFF-TABLE per burnt-holdout policy regardless of verdict.

10-feature whitelist (UNCHANGED from v3 / v4 / v5):
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
    LAMBDA_GRID_POINTS_DEFAULT,
    N_FOLDS_DEFAULT,
    aligned_train_targets,
    build_target_frame,
    fit_global,
    predict_scores_global,
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

# Romano-Wolf m≈50 → primary 3.5, stretch 4.0. NOT simple Bonferroni.
PASS_T = 3.5
STRETCH_T = 4.0
PASS_SHARPE = 0.5
PASS_ALPHA_ANN = 0.03
PASS_MAX_DD = -0.25
MID_T = 1.5
MID_SHARPE = 0.3
PASS_IR = 0.5
MID_IR = 0.3
PASS_HOLDOUT_IC = 0.0
DESCRIPTIVE_BETA_MAX = 0.5

# Top-10% mkt-cap exclusion (exogenous percentile, locked pre-run).
MKT_CAP_TOP_PERCENTILE_DROP = 0.10


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
# v6a-revised: mkt-cap-at-asof helpers


def _close_at_asof(history_store: HistoryStore, ticker: str, asof: date) -> float | None:
    """Last close <= asof. None if no bars."""
    df = history_store.truncate_to(ticker, asof)
    if df.empty:
        return None
    px = float(df.iloc[-1]["close"])
    return px if px > 0.0 else None


def _compute_mkt_cap_filter(
    feature_frame: pd.DataFrame,
    history_store: HistoryStore,
    shares_lookup,
    top_percentile_drop: float = MKT_CAP_TOP_PERCENTILE_DROP,
) -> pd.DataFrame:
    """Add mkt_cap_at_asof column AND _is_mega bool flag (top-N% by mkt-cap per asof).

    NaN mkt_cap rows are RETAINED (treated as non-mega: absence-of-evidence).
    """
    rows = feature_frame[["ticker", "asof"]].copy()
    rows["asof"] = pd.to_datetime(rows["asof"]).dt.date
    mkt_caps: list[float] = []
    for tk, asof in zip(rows["ticker"], rows["asof"], strict=True):
        shares = shares_lookup(tk, asof)
        if shares is None or shares <= 0:
            mkt_caps.append(float("nan"))
            continue
        close = _close_at_asof(history_store, tk, asof)
        if close is None:
            mkt_caps.append(float("nan"))
            continue
        mkt_caps.append(float(shares) * close)
    rows["mkt_cap_at_asof"] = mkt_caps

    is_mega = pd.Series(False, index=rows.index)
    for asof_val, asof_idx in rows.groupby("asof").groups.items():
        sub = rows.loc[asof_idx, "mkt_cap_at_asof"]
        finite_mask = sub.notna()
        if finite_mask.sum() == 0:
            continue
        threshold = sub[finite_mask].quantile(1.0 - top_percentile_drop)
        # mark only rows whose mkt_cap >= threshold AND finite
        for ridx in asof_idx:
            cap = sub.loc[ridx]
            if pd.notna(cap) and cap >= threshold:
                is_mega.loc[ridx] = True

    out = feature_frame.copy()
    out["mkt_cap_at_asof"] = rows["mkt_cap_at_asof"]
    out["_is_mega"] = is_mega
    return out


# ---------------------------------------------------------------------------
# v6a-revised long-only decile (subset universe, MDY benchmark)


def _holdout_long_only_decile_returns_20d(
    holdout_features: pd.DataFrame,
    scores: pd.Series,
    history_store: HistoryStore,
    *,
    benchmark: str,
    holding_period: int = HOLDING_PERIOD,
) -> tuple[
    pd.Series,
    pd.Series,
    list[list[str]],
    pd.Series,
    list[int],
]:
    """Build long-only top-decile portfolio per pre-reg v5.

    Long-eligible universe = full post-ADV cross-section.
    Decile size = floor(n_eligible / 10).
    Asof skipped if long decile size < 3.

    Returns:
        long_returns: per-asof long-leg mean 20d-forward return
        bench_returns: benchmark 20d-forward returns aligned by asof
        long_lists: per-asof selected tickers (for turnover)
        excess_returns: per-asof (long - bench) excess
        long_sizes: per-asof decile sizes
    """
    holdout_features = holdout_features.assign(_score=scores)
    holdout_features = holdout_features.dropna(subset=["_score"])
    asof_dates = sorted(holdout_features["asof"].unique())

    long_h: list[float] = []
    bench_h: list[float] = []
    excess_h: list[float] = []
    long_lists: list[list[str]] = []
    long_sizes: list[int] = []
    asof_idx_out: list[date] = []

    for asof in asof_dates:
        slice_df = holdout_features.loc[holdout_features["asof"] == asof]
        if slice_df.empty:
            continue

        n_long_eligible = len(slice_df)
        long_decile = n_long_eligible // 10
        if long_decile < 3:
            continue

        long_tickers = (
            slice_df.sort_values("_score", ascending=False).head(long_decile)["ticker"].tolist()
        )

        long_rets = []
        for tk in long_tickers:
            r = history_store.forward_return(tk, asof, holding_period)
            long_rets.append(np.nan if r is None else r)
        long_arr = np.asarray(long_rets, dtype=float)
        if np.all(np.isnan(long_arr)):
            continue

        long_mean = float(np.nanmean(long_arr))

        bench_r = history_store.forward_return(benchmark, asof, holding_period)
        if bench_r is None or not np.isfinite(bench_r):
            continue

        long_h.append(long_mean)
        bench_h.append(float(bench_r))
        excess_h.append(long_mean - float(bench_r))

        long_lists.append(long_tickers)
        long_sizes.append(long_decile)
        asof_idx_out.append(asof)

    asof_index = pd.DatetimeIndex([pd.Timestamp(d) for d in asof_idx_out])
    long_returns = pd.Series(long_h, index=asof_index, dtype=float, name="long_return_20d")
    bench_returns = pd.Series(bench_h, index=asof_index, dtype=float, name="benchmark_return_20d")
    excess_returns = pd.Series(excess_h, index=asof_index, dtype=float, name="excess_return_20d")
    return (
        long_returns,
        bench_returns,
        long_lists,
        excess_returns,
        long_sizes,
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


def _assess_long_only(
    long_returns: pd.Series,
    bench_returns: pd.Series,
    excess_returns: pd.Series,
    carhart: pd.DataFrame,
    rebalance_stride: int,
    cost_half_spread_bps: float,
    long_lists: Sequence[Sequence[str]],
) -> dict:
    """Long-only metrics. Primary return series = excess (long - SPY).
    Single-leg cost (long only); no borrow fee."""
    rets = excess_returns.dropna()
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

    # Pre-reg locked: 10bp half-spread + 5bp adverse = 15bp/leg → 30bps RT (single leg).
    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_long_bps = cost_model.primary_period_drag_bps(cost_half_spread_bps, turn_long)
    # Long-only: single-leg drag (no short leg added). Half of v4's two-leg cost.
    drag_per_rebal_bps = drag_long_bps
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
    # Excess (long - SPY) is already relative; no RF subtraction.
    res4 = run_regression(
        rets,
        carhart_20d[[*CARHART_COLS, "RF"]],
        CARHART_COLS,
        hac_maxlags=HAC_MAXLAGS,
        subtract_rf=False,
    )

    excess_ann = float(rets.mean() * rebalances_per_year)

    cum = (1 + rets_net.fillna(0)).cumprod()
    mdd = float(max_drawdown(cum.tolist()))

    long_clean = long_returns.dropna()
    bench_clean = bench_returns.dropna()
    long_ann = float(long_clean.mean() * rebalances_per_year) if len(long_clean) else float("nan")
    bench_ann = (
        float(bench_clean.mean() * rebalances_per_year) if len(bench_clean) else float("nan")
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
        "turnover_per_rebal_long": turn_long,
        "long_ann_return": long_ann,
        "bench_ann_return": bench_ann,
        "decile_size_long_mean": float(np.mean([len(s) for s in long_lists]))
        if long_lists
        else 0.0,
        "n_rebalances": len(long_lists),
        "carhart_aggregated_n": len(carhart_20d),
        "beta_mkt_carhart4f": beta_mkt,
        "beta_descriptive_ok": (np.isfinite(beta_mkt) and abs(beta_mkt) <= DESCRIPTIVE_BETA_MAX),
        "stretch_threshold_passed": float(res4.alpha_tstat) >= STRETCH_T,
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


def _compute_in_cv_ir(
    fit,
    train_X: pd.DataFrame,
    train_y_ranked: pd.Series,
) -> float:
    """In-CV information ratio: mean(fold rank-IC) / std(fold rank-IC).
    Identical to v3 driver implementation."""
    from scipy.stats import spearmanr
    from sklearn.linear_model import Lasso
    from sklearn.preprocessing import StandardScaler

    from alphalens.screeners.multi_source_two_stage.model import (
        _expanding_splits_with_embargo,
    )

    asof = train_X["asof"]
    splits = _expanding_splits_with_embargo(asof, n_folds=3, embargo_days=60)
    if not splits:
        return float("nan")

    feature_names = list(fit.feature_names)
    fold_ics: list[float] = []
    for train_idx, val_idx in splits:
        train_pos = train_X.index.get_indexer(train_idx)
        val_pos = train_X.index.get_indexer(val_idx)
        X_full = train_X[feature_names].to_numpy(dtype=float)
        nan_mask = np.isnan(X_full)
        if nan_mask.any():
            med_b = np.broadcast_to(fit.feature_medians, X_full.shape).copy()
            X_full[nan_mask] = med_b[nan_mask]
        local_scaler = StandardScaler().fit(X_full[train_pos])
        Xs_train_local = local_scaler.transform(X_full[train_pos])
        Xs_val_local = local_scaler.transform(X_full[val_pos])
        y_train = train_y_ranked.to_numpy(dtype=float)[train_pos]
        y_val = train_y_ranked.to_numpy(dtype=float)[val_pos]
        model = Lasso(alpha=fit.chosen_alpha, max_iter=10_000, random_state=42, selection="cyclic")
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            model.fit(Xs_train_local, y_train)
            preds = model.predict(Xs_val_local)
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
        if t >= STRETCH_T:
            return (
                "PASS-DIAGNOSTIC-STRETCH (primary 2.81 + Romano-Wolf stretch 3.2 cleared; "
                "capital deploy STILL off-table per burnt-holdout caveat — fresh-OOS "
                "replication mandatory before any escalation)"
            )
        return (
            "PASS-DIAGNOSTIC-PRIMARY (program-level 2.81 cleared but stretch 3.2 not; "
            "directional positive consistent with primary gate, NOT robust to conservative "
            "multiplicity; capital deploy off-table)"
        )
    if t >= MID_T and sh >= MID_SHARPE and in_cv_ir >= MID_IR:
        return "MID (refine and re-pre-register before deploy)"
    return "FAIL (log to ledger)"


# ---------------------------------------------------------------------------
# Sub-period regime stratification (descriptive post-hoc)


def _regime_stratify(
    excess_returns: pd.Series,
    long_returns: pd.Series,
    bench_returns: pd.Series,
    *,
    rebalance_stride: int,
) -> dict:
    """Sub-period decile alpha by year (2024 / 2025 / 2026). Reports excess
    (long - SPY), long absolute, and benchmark absolute for each window.
    Descriptive — does NOT gate verdict."""
    rebalances_per_year = 252 / max(1, rebalance_stride)
    out: dict = {}
    for label, start, end in [
        ("2024_partial", "2024-04-30", "2024-12-31"),
        ("2025_full", "2025-01-01", "2025-12-31"),
        ("2026_partial", "2026-01-01", "2026-04-30"),
    ]:
        mask = (excess_returns.index >= pd.Timestamp(start)) & (
            excess_returns.index <= pd.Timestamp(end)
        )
        sub_ex = excess_returns.loc[mask].dropna()
        sub_long = long_returns.loc[mask].dropna()
        sub_bench = bench_returns.loc[mask].dropna()
        out[label] = {
            "n_rebal": len(sub_ex),
            "excess_ann": float(sub_ex.mean() * rebalances_per_year)
            if len(sub_ex)
            else float("nan"),
            "long_ann": float(sub_long.mean() * rebalances_per_year)
            if len(sub_long)
            else float("nan"),
            "bench_ann": float(sub_bench.mean() * rebalances_per_year)
            if len(sub_bench)
            else float("nan"),
        }
    return out


# ---------------------------------------------------------------------------
# CLI


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--holding", type=int, default=HOLDING_PERIOD)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument(
        "--benchmark",
        default="MDY",
        help="Cap-matched mid-cap benchmark for v6a-revised (locked at MDY per pre-reg).",
    )
    ap.add_argument("--adv-min-usd", type=float, default=5_000_000.0)
    ap.add_argument("--cost-half-spread-bps", type=float, default=10.0)
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument("--train-start", type=date.fromisoformat, default=date(2018, 1, 1))
    ap.add_argument("--holdout-start", type=date.fromisoformat, default=date(2024, 4, 30))
    ap.add_argument("--holdout-end", type=date.fromisoformat, default=date(2026, 4, 30))
    ap.add_argument(
        "--force-rebuild-features",
        action="store_true",
        help="Ignore feature parquet cache and rebuild from scratch.",
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
        default=Path("docs/research/alt_data_screener_v6a_revised_phase_b.md"),
    )
    ap.add_argument(
        "--audit-out",
        type=Path,
        default=Path("docs/research/alt_data_screener_v6a_revised_audit.json"),
    )
    return ap


def _emit_result_line(
    label: str, stats: Mapping, *, half_spread_bps: float, adv_min_usd: float
) -> None:
    if stats.get("n", 0) == 0:
        return
    logger.info(
        "%s | ADV>=$%.0fM cost=%.0fbps | n=%d Lsize=%.0f turn=%.1f%% | "
        "Sh gross=%.2f net=%.2f | excess vs SPY gross=%.2f%%/y net=%.2f%%/y | "
        "a 4F=%.1f%% t=%.2f (primary>=%.2f stretch>=%.2f) β=%.2f",
        label,
        adv_min_usd / 1e6,
        half_spread_bps,
        stats["n"],
        stats["decile_size_long_mean"],
        stats["turnover_per_rebal_long"] * 100,
        stats["sharpe_gross"],
        stats["sharpe_net"],
        stats["excess_vs_bench_ann"] * 100,
        stats["excess_vs_bench_net"] * 100,
        stats["alpha_gross_4f"] * 100,
        stats["t_4f"],
        PASS_T,
        STRETCH_T,
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

    # v6a-revised: load both primary (MDY) and descriptive (SPY) benchmarks.
    histories = load_cached_histories([*universe, args.benchmark, "SPY"], PRICES_DIR)
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

    cache_dir = Path.home() / ".alphalens" / "feature_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Cache key keyed on INPUT params only (universe set + window + stride/holding +
    # features version). NOT on realized calendar — yfinance refresh adds bars day-to-day,
    # which would needlessly invalidate the cache between sessions.
    # NB: benchmark NOT in cache key — features are benchmark-agnostic
    # (benchmark only used to exclude from universe; SPY/MDY both outside PIT universe).
    cache_key_payload = (
        ",".join(sorted(feat_universe))
        + f"|train_start={args.train_start.isoformat()}"
        + f"|holdout_start={args.holdout_start.isoformat()}"
        + f"|holdout_end={args.holdout_end.isoformat()}"
        + f"|stride={args.rebalance_stride}"
        + f"|holding={args.holding}"
        + "|features_v=alt_data_v3_v4_v5_10feat"
    )
    import hashlib

    cache_key = hashlib.sha256(cache_key_payload.encode()).hexdigest()[:16]
    cache_path = cache_dir / f"alt_data_features_{cache_key}.parquet"
    logger.info("feature cache key=%s path=%s", cache_key, cache_path)

    if cache_path.exists() and not args.force_rebuild_features:
        features = pd.read_parquet(cache_path)
        logger.info("feature frame LOADED from cache (skipped 50min build): %s", features.shape)
    else:
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
        logger.info("feature frame BUILT: %s; writing to cache %s", features.shape, cache_path)
        features.to_parquet(cache_path, index=False)

    logger.info("feature frame raw shape: %s", features.shape)

    features = _apply_adv_filter(features, history_store, args.adv_min_usd)
    logger.info("feature frame post-ADV (>=$%.0fM): %s", args.adv_min_usd / 1e6, features.shape)
    if features.empty:
        logger.error("feature frame empty after ADV filter — abort")
        return 1

    # v6a-revised: compute mkt-cap-at-asof + flag top-10% per asof, drop them.
    logger.info("computing mkt-cap-at-asof for %d rows ...", len(features))
    features = _compute_mkt_cap_filter(
        features, history_store, shares_lookup, top_percentile_drop=MKT_CAP_TOP_PERCENTILE_DROP
    )
    n_before = len(features)
    n_mega = int(features["_is_mega"].sum())
    features = features.loc[~features["_is_mega"]].drop(columns=["_is_mega", "mkt_cap_at_asof"])
    logger.info(
        "v6a-revised mkt-cap filter: dropped %d top-10%% rows (%.1f%%); pool %d → %d",
        n_mega,
        100 * n_mega / max(n_before, 1),
        n_before,
        len(features),
    )
    if features.empty:
        logger.error("feature frame empty after mkt-cap filter — abort")
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

    fit = fit_global(
        train_X,
        train_y,
        n_folds=N_FOLDS_DEFAULT,
        embargo_days=EMBARGO_DAYS_DEFAULT,
        lambda_grid_points=LAMBDA_GRID_POINTS_DEFAULT,
        feature_names=ALT_DATA_FEATURE_NAMES,
        target_transform="rank",
    )
    if fit is None:
        logger.error("fit_global returned None — abort")
        return 1
    logger.info(
        "global fit (rank-target 20d): n_train=%d, lambda chosen=%.4g, nonzero coefs=%d/%d, CV mean MSE=%.4g",
        fit.n_train_obs,
        fit.chosen_alpha,
        fit.n_nonzero_coefs,
        len(fit.feature_names),
        fit.cv_mean_mse,
    )
    coefs = fit.model.coef_
    nonzero_idx = [i for i, c in enumerate(coefs) if abs(c) > 1e-12]
    nonzero_features = [(fit.feature_names[i], float(coefs[i])) for i in nonzero_idx]
    logger.info("nonzero coefs: %s", nonzero_features)

    scores = predict_scores_global(fit, feat_holdout)
    n_scored = int(scores.notna().sum())
    logger.info("holdout scored rows: %d / %d", n_scored, len(feat_holdout))

    (
        long_returns,
        bench_returns,
        long_lists,
        excess_returns,
        long_sizes,
    ) = _holdout_long_only_decile_returns_20d(
        feat_holdout,
        scores,
        history_store,
        benchmark=args.benchmark,
        holding_period=args.holding,
    )
    logger.info(
        "holdout long-only rebalances: %d (mean Lsize=%.1f)",
        len(excess_returns),
        float(np.mean(long_sizes)) if long_sizes else 0.0,
    )
    if excess_returns.empty:
        logger.error("no holdout long-only rebalances — abort")
        return 1

    label = f"HOLDOUT LONG-ONLY {args.holdout_start.year}-{args.holdout_end.year}"
    stats = _assess_long_only(
        long_returns,
        bench_returns,
        excess_returns,
        carhart,
        args.rebalance_stride,
        args.cost_half_spread_bps,
        long_lists,
    )
    _emit_result_line(
        label,
        stats,
        half_spread_bps=args.cost_half_spread_bps,
        adv_min_usd=args.adv_min_usd,
    )

    from alphalens.screeners.multi_source_two_stage.target import (
        rank_transform_per_asof,
    )

    train_y_ranked = rank_transform_per_asof(train_y, train_X["asof"])
    in_cv_ir = _compute_in_cv_ir(fit, train_X, train_y_ranked)
    holdout_mean_ic, holdout_per_asof_ic = _compute_holdout_rank_ic(
        feat_holdout,
        scores,
        history_store,
        holding_period=args.holding,
    )
    logger.info(
        "v5 gates: in_cv_ir=%.3f (pass>=%.2f, mid>=%.2f) | holdout_mean_rank_ic=%.4f (pass>0) | n_holdout_asofs_with_ic=%d",
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
        excess_returns,
        long_returns,
        bench_returns,
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
            "regime %s: n=%d excess=%.1f%%/y long=%.1f%%/y bench=%.1f%%/y",
            label_r,
            sub["n_rebal"],
            sub["excess_ann"] * 100,
            sub["long_ann"] * 100,
            sub["bench_ann"] * 100,
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
        (fit.feature_names[i], float(fit.model.coef_[i]))
        for i in range(len(fit.feature_names))
        if abs(fit.model.coef_[i]) > 1e-12
    ]
    audit = {
        "id": "alt_data_screener_v6a_revised_2026_05_01",
        "class": "alt_data_screener_search_2026_04_30",
        "verdict": verdict,
        "headline_metrics": {k: v for k, v in stats.items() if k != "regime_stratification"},
        "regime_stratification": stats.get("regime_stratification", {}),
        "nonzero_coefs": [{"feature": n, "coef": c} for n, c in nonzero],
        "fit": {
            "n_train_obs": fit.n_train_obs,
            "chosen_alpha": fit.chosen_alpha,
            "n_nonzero_coefs": fit.n_nonzero_coefs,
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
            "phase_offset": args.phase_offset,
            "primary_threshold_t": PASS_T,
            "stretch_threshold_t": STRETCH_T,
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
    nonzero = [
        (fit.feature_names[i], float(fit.model.coef_[i]))
        for i in range(len(fit.feature_names))
        if abs(fit.model.coef_[i]) > 1e-12
    ]
    coefs_md = (
        "\n".join(f"  - `{n}` = {c:+.4f}" for n, c in nonzero) if nonzero else "  - (all zero)"
    )

    regime = stats.get("regime_stratification", {})
    regime_lines = []
    for label_r in ["2024_partial", "2025_full", "2026_partial"]:
        sub = regime.get(label_r, {})
        regime_lines.append(
            f"| {label_r} | {sub.get('n_rebal', 0)} | "
            f"{sub.get('excess_ann', float('nan')) * 100:+.2f}% | "
            f"{sub.get('long_ann', float('nan')) * 100:+.2f}% | "
            f"{sub.get('bench_ann', float('nan')) * 100:+.2f}% |"
        )

    lines = [
        "# alt_data_screener_v5_long_only Phase B — holdout reveal (long-only top-decile vs SPY)",
        "",
        "**Pre-registration:** `alt_data_screener_v6a_revised_2026_05_01` "
        "(class `alt_data_screener_search_2026_04_30`, n=5 in-class but program-level "
        f"n=10 -> primary |t|>={PASS_T:.2f}, stretch |t|>={STRETCH_T:.2f})",
        "**Pivot provenance:** auto-pivot from v10 (analyst_alt_data_v10) Phase A gate 2 "
        "yfinance survivorship FAIL (delisted/active event-rate ratio=0.003, z=620).",
        "**HARKing flag (explicit):** Path β was post-hoc designed against observed v4 "
        "long-leg performance (+20.6%/y); per Kerr 1998 + Simmons et al. 2011 this is "
        "classical hypothesis-mining. Pivot trigger from v10 was pre-registered objective; "
        "Path β design itself is HARKing-confounded. Capital deploy OFF-TABLE regardless "
        "of verdict; fresh-OOS replication mandatory before any escalation.",
        f"**Phase offset:** {args.phase_offset} (stride {args.rebalance_stride}, "
        f"holding {args.holding}d, overlap 4-tranche)",
        f"**Train:** {args.train_start} -> {args.holdout_start - timedelta(days=1)}",
        f"**Holdout:** {args.holdout_start} -> {args.holdout_end}",
        f"**ADV floor:** ${args.adv_min_usd / 1e6:.0f}M, 20d median",
        f"**Cost:** half-spread {args.cost_half_spread_bps:.0f} bps + 5 bps adverse "
        "selection (single-leg long only, ~30bps round-trip)",
        f"**HAC maxlags:** {HAC_MAXLAGS} (overlap correction)",
        "**Sharpe:** Lo (2002) variance-ratio adjusted",
        f"**Primary metric:** Carhart-4F alpha t-stat on (long_only_return - SPY_return) "
        f"series; PASS at |t| >= {PASS_T:.2f} primary, |t| >= {STRETCH_T:.2f} stretch.",
        "",
        f"## Verdict: {verdict}",
        "",
        "## Headline metrics (holdout)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| n rebalances (overlapping 20d) | {stats.get('n', 0)} |",
        f"| mean decile size (long) | {stats.get('decile_size_long_mean', 0):.1f} |",
        f"| turnover/rebal long | {stats.get('turnover_per_rebal_long', 0) * 100:.1f}% |",
        f"| Sharpe (gross, Lo-adj) | {stats.get('sharpe_gross', 0):.2f} |",
        f"| Sharpe (gross, naive) | {stats.get('sharpe_gross_naive', 0):.2f} |",
        f"| Sharpe (net, Lo-adj) | {stats.get('sharpe_net', 0):.2f} |",
        f"| alpha (gross, 4F) annualised | {stats.get('alpha_gross_4f', 0) * 100:+.2f}% |",
        f"| alpha (net, 4F) annualised | {stats.get('alpha_net_4f', 0) * 100:+.2f}% |",
        f"| alpha t-stat (HAC, maxlags={HAC_MAXLAGS}) | {stats.get('t_4f', 0):+.2f} |",
        f"| primary threshold passed (|t|>={PASS_T:.2f}) | {stats.get('t_4f', 0) >= PASS_T} |",
        f"| stretch threshold passed (|t|>={STRETCH_T:.2f}) | "
        f"{stats.get('stretch_threshold_passed', False)} |",
        f"| **Mkt-RF beta (Carhart 4F)** | {stats.get('beta_mkt_carhart4f', float('nan')):+.3f} |",
        f"| beta in descriptive bound (|β| ≤ {DESCRIPTIVE_BETA_MAX:.1f}) | "
        f"{stats.get('beta_descriptive_ok', False)} |",
        f"| Excess vs SPY (gross) ann | {stats.get('excess_vs_bench_ann', 0) * 100:+.2f}% |",
        f"| Excess vs SPY (net) ann | {stats.get('excess_vs_bench_net', 0) * 100:+.2f}% |",
        f"| long leg ann return | {stats.get('long_ann_return', 0) * 100:+.2f}% |",
        f"| benchmark ann return | {stats.get('bench_ann_return', 0) * 100:+.2f}% |",
        f"| Max drawdown (net cum excess) | {stats.get('max_drawdown', 0) * 100:+.2f}% |",
        f"| Cost drag annualised (~30bps RT, single leg) | "
        f"{stats.get('cost_drag_ann', 0) * 100:.2f}% |",
        f"| Carhart aggregated n | {stats.get('carhart_aggregated_n', 0)} |",
        f"| in-CV IR (mean fold IC / std) | {stats.get('in_cv_ir', float('nan')):.3f} |",
        f"| holdout mean per-asof rank-IC | {stats.get('holdout_mean_rank_ic', float('nan')):+.4f} |",
        f"| holdout asofs with valid IC | {stats.get('holdout_n_asofs_with_ic', 0)} |",
        "",
        "## Regime stratification (descriptive — NOT a verdict gate)",
        "",
        "| Sub-period | n rebal | Excess ann | Long ann | Bench ann |",
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
        "## Global Lasso fit (rank-target, 20d-forward)",
        "",
        f"- n_train: {fit.n_train_obs}",
        f"- lambda chosen: {fit.chosen_alpha:.4g}",
        f"- nonzero coefs: {fit.n_nonzero_coefs} / {len(fit.feature_names)}",
        f"- CV mean MSE: {fit.cv_mean_mse:.4g}",
        "",
        "### Nonzero coefficients",
        "",
        coefs_md,
        "",
        "## Comparison to prior in-class results",
        "",
        "Prior in-class (`alt_data_screener_search_2026_04_30`):",
        "- v1 (FINRA daily flow): ABANDONED (infra block)",
        "- v2 (Polygon SI, raw return target, top-30): alpha-t=+0.05, FAIL (0/10 coefs)",
        "- v3 (rank target, top-30): alpha-t=-4.32, FAIL (2/10 coefs, +0.0260 holdout rank-IC)",
        "- v4 (rank target, decile L/S, SI<=15%): alpha-t=-2.57, FAIL (2/10 coefs, "
        "short leg returned +108.5%/y in squeeze regime crushing the spread)",
        "",
        "v5 in-class continuation: ONLY selection rule changed (decile L/S -> "
        "long top-decile only vs SPY benchmark).",
    ]
    out_path.write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())

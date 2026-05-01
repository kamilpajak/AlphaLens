"""Phase B v5 experiment driver — alt_data_screener_v3_2026_05_01.

Pre-registered as `alt_data_screener_v3_2026_05_01` in class
`alt_data_screener_search_2026_04_30` (n=3 -> Bonferroni |t| >= 2.39).
Pre-reg: `docs/research/preregistration/params_alt_data_screener_v3_2026_05_01.json`.

Same-class ablation of v4 v2 FAIL (0/10 nonzero coefs). ONE variable changes
vs v4 v2: target is rank-transformed per-asof (percentile_rank - 0.5) BEFORE
Lasso fitting. Rationale: zen+perplexity adversarial review identified target
shape (heavy-tailed return distribution) as candidate bottleneck; rank
transform compresses to bounded [-0.5, +0.5] target where Lasso L1 + MSE is
mathematically equivalent to Spearman rank-IC maximization.

NEW pre-reg gates per adversarial review:
- in-CV IR (mean fold rank-IC / std fold rank-IC) >= 1.0
- holdout mean per-asof rank-IC > 0

10-feature whitelist (UNCHANGED from v4 v2):
  earnings_sue_naive_4q_decayed, earnings_pead_5d_post_decayed,
  earnings_recency_days, short_interest_pct_float_change_60d,
  rank_short_interest_pct_float, log1p_days_to_cover, insider_log_count,
  insider_log_dollar, rank_realized_downside_skew_60d, filing_density_4q.

All other architecture identical to v4 v2 (settled variables).
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

# Bonferroni n=3 (v1 abandoned + v2 completed-FAIL + v3 active) per ledger threshold
PASS_T = 2.39
PASS_SHARPE = 0.5
PASS_ALPHA_ANN = 0.03
PASS_MAX_DD = -0.35
MID_T = 1.5
MID_SHARPE = 0.3
# NEW gates per zen + perplexity adversarial review on v5 design
PASS_IR = 1.0  # in-CV mean fold rank-IC / std fold rank-IC
MID_IR = 0.5
PASS_HOLDOUT_IC = 0.0  # holdout mean per-asof rank-IC must be strictly > 0


# ---------------------------------------------------------------------------
# Data loaders


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


# ---------------------------------------------------------------------------
# v4-specific store factories


def _make_filings_lookup(companyfacts_dir: Path, ticker_cik_map: TickerCikMap):
    """Return a callable (ticker, asof) -> list[date] of distinct 10-Q/10-K filings."""
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
            # Collect distinct (form, filed) tuples across all us-gaap concepts;
            # keep only periodic filings (10-K, 10-Q and their amendments).
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
    """Return (ticker, asof) -> int | None shares outstanding."""
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
# Helpers reused from v3 driver shape (with v4-specific holding/HAC kept)


def _holdout_portfolio_returns_20d(
    holdout_features: pd.DataFrame,
    scores: pd.Series,
    history_store: HistoryStore,
    *,
    benchmark: str,
    top_n: int,
    holding_period: int = HOLDING_PERIOD,
) -> tuple[pd.Series, pd.Series, list[list[str]]]:
    holdout_features = holdout_features.assign(_score=scores)
    holdout_features = holdout_features.dropna(subset=["_score"])
    asof_dates = sorted(holdout_features["asof"].unique())
    port_h: list[float] = []
    bench_h: list[float] = []
    top_lists: list[list[str]] = []
    asof_idx_out: list[date] = []

    for asof in asof_dates:
        slice_df = holdout_features.loc[holdout_features["asof"] == asof]
        if slice_df.empty:
            continue
        ranked = slice_df.sort_values("_score", ascending=False).head(top_n)
        tickers = ranked["ticker"].tolist()
        if not tickers:
            continue

        rets_h = []
        for tk in tickers:
            r = history_store.forward_return(tk, asof, holding_period)
            rets_h.append(np.nan if r is None else r)
        rets_h_arr = np.array(rets_h, dtype=float)
        if np.all(np.isnan(rets_h_arr)):
            continue
        port_h.append(float(np.nanmean(rets_h_arr)))

        bench_r = history_store.forward_return(benchmark, asof, holding_period)
        bench_h.append(float("nan") if bench_r is None else bench_r)
        top_lists.append(tickers)
        asof_idx_out.append(asof)

    asof_index = pd.DatetimeIndex([pd.Timestamp(d) for d in asof_idx_out])
    port_returns = pd.Series(port_h, index=asof_index, dtype=float, name="portfolio_return_20d")
    bench_returns = pd.Series(bench_h, index=asof_index, dtype=float, name="benchmark_return_20d")
    return port_returns, bench_returns, top_lists


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


def _assess(
    portfolio_returns_20d: pd.Series,
    bench_returns_20d: pd.Series,
    carhart: pd.DataFrame,
    rebalance_stride: int,
    cost_half_spread_bps: float,
    top_lists: Sequence[Sequence[str]],
) -> dict:
    rets = portfolio_returns_20d.dropna()
    if rets.empty:
        return {"n": 0}
    rebalances_per_year = 252 / max(1, rebalance_stride)

    sharpe_gross = sharpe_autocorr_adjusted(
        rets.tolist(),
        periods_per_year=int(rebalances_per_year),
        max_lag=5,
    )
    sharpe_gross_naive = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))

    avg_turnover = _turnover_per_rebal(top_lists)
    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_per_rebal_bps = cost_model.primary_period_drag_bps(cost_half_spread_bps, avg_turnover)
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
    res4 = run_regression(
        rets,
        carhart_20d[[*CARHART_COLS, "RF"]],
        CARHART_COLS,
        hac_maxlags=HAC_MAXLAGS,
    )

    bench_aligned = bench_returns_20d.reindex(rets.index).dropna()
    excess_per_rebal = (rets.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann = (
        float(excess_per_rebal * rebalances_per_year)
        if not np.isnan(excess_per_rebal)
        else float("nan")
    )

    cum = (1 + rets_net.fillna(0)).cumprod()
    mdd = float(max_drawdown(cum.tolist()))

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
        "turnover_per_rebal": avg_turnover,
        "mean_top_n": float(np.mean([len(s) for s in top_lists])) if top_lists else 0.0,
        "n_rebalances": len(top_lists),
        "carhart_aggregated_n": len(carhart_20d),
    }


def _compute_holdout_rank_ic(
    holdout_features: pd.DataFrame,
    scores: pd.Series,
    history_store: HistoryStore,
    *,
    holding_period: int = HOLDING_PERIOD,
    rf_series: pd.Series | None = None,
) -> tuple[float, list[float]]:
    """Per-asof Spearman rank correlation between predictions and forward returns.

    Returns (mean across asofs, list of per-asof IC values). Asof slices with
    constant predictions or constant targets return NaN (excluded from mean).
    Used for the holdout-IC pre-reg gate per v5.
    """
    holdout_features = holdout_features.assign(_score=scores)
    holdout_features = holdout_features.dropna(subset=["_score"])
    asof_dates = sorted(holdout_features["asof"].unique())
    per_asof_ic: list[float] = []
    for asof in asof_dates:
        slice_df = holdout_features.loc[holdout_features["asof"] == asof]
        if len(slice_df) < 3:
            continue
        # Forward 20d return for each ticker
        rets = []
        for tk in slice_df["ticker"]:
            r = history_store.forward_return(tk, asof, holding_period)
            rets.append(np.nan if r is None else r)
        rets_arr = np.asarray(rets, dtype=float)
        scores_arr = slice_df["_score"].to_numpy(dtype=float)
        valid = np.isfinite(rets_arr) & np.isfinite(scores_arr)
        if valid.sum() < 3:
            continue
        # Constant-prediction / constant-target slices yield undefined Spearman
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

    Recomputes per-fold rank-IC at the chosen lambda by re-running the same
    expanding-window CV split machinery used during fit. Returns NaN when fewer
    than 2 valid folds (cannot compute std).
    """
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
        # Impute NaNs with feature_medians from the fit
        nan_mask = np.isnan(X_full)
        if nan_mask.any():
            med_b = np.broadcast_to(fit.feature_medians, X_full.shape).copy()
            X_full[nan_mask] = med_b[nan_mask]
        Xs = fit.scaler.transform(X_full)
        # Refit Lasso at chosen alpha on this fold's train (mirrors fit_global flow)
        local_scaler = StandardScaler().fit(X_full[train_pos])
        Xs_train_local = local_scaler.transform(X_full[train_pos])
        Xs_val_local = local_scaler.transform(X_full[val_pos])
        y_train = train_y_ranked.to_numpy(dtype=float)[train_pos]
        y_val = train_y_ranked.to_numpy(dtype=float)[val_pos]
        model = Lasso(alpha=fit.chosen_alpha, max_iter=10_000, random_state=42, selection="cyclic")
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            model.fit(Xs_train_local, y_train)
            preds = model.predict(Xs_val_local)
        # Per-asof rank-IC across the val rows of this fold
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
    """Pre-reg success_criteria translation. n=3 -> t>=2.39 for PASS,
    PLUS new gates: in-CV IR >= 1.0 (mid 0.5), holdout mean rank-IC > 0."""
    t = stats.get("t_4f", 0.0)
    sh = stats.get("sharpe_net", 0.0)
    a = stats.get("alpha_gross_4f", 0.0)
    mdd = stats.get("max_drawdown", 0.0)

    if n_nonzero_coefs == 0:
        return "FAIL (zero-coef structural artifact — see prior class v2-v3 finding)"
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
        return f"PASS (provisional — pending multi-phase audit; t>={PASS_T:.2f}, IR>={PASS_IR})"
    if t >= MID_T and sh >= MID_SHARPE and in_cv_ir >= MID_IR:
        return "MID (refine and re-pre-register before deploy)"
    return "FAIL (log to ledger)"


# ---------------------------------------------------------------------------
# CLI


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--top-n", type=int, default=30)
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
        "--smoke",
        action="store_true",
        help="Smoke mode: 5 tickers x 12 asofs to verify end-to-end plumbing",
    )
    ap.add_argument(
        "--smoke-tickers",
        nargs="+",
        default=["AAPL", "MSFT", "NVDA", "TSLA", "META"],
        help="Smoke universe (default: 5 mega-cap names with rich data)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/alt_data_screener_v3_phase_b.md"),
    )
    return ap


def _emit_result_line(
    label: str, stats: Mapping, *, half_spread_bps: float, adv_min_usd: float
) -> None:
    if stats.get("n", 0) == 0:
        return
    logger.info(
        "%s | ADV>=$%.0fM cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
        "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | a 4F=%.1f%% t=%.2f",
        label,
        adv_min_usd / 1e6,
        half_spread_bps,
        stats["n"],
        stats["mean_top_n"],
        stats["turnover_per_rebal"] * 100,
        stats["sharpe_gross"],
        stats["sharpe_net"],
        stats["excess_vs_bench_ann"] * 100,
        stats["excess_vs_bench_net"] * 100,
        stats["alpha_gross_4f"] * 100,
        stats["t_4f"],
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

    # v4 v2 stores
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
        # Smoke: 12 asofs spread across train+holdout for a quick sanity run
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
        "global fit (20d target): n_train=%d, lambda chosen=%.4g, nonzero coefs=%d/%d, CV mean MSE=%.4g",
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

    port_rets_20d, bench_rets_20d, top_lists = _holdout_portfolio_returns_20d(
        feat_holdout,
        scores,
        history_store,
        benchmark=args.benchmark,
        top_n=args.top_n,
        holding_period=args.holding,
    )
    logger.info("holdout rebalances (overlapping 20d-tranches): %d", len(port_rets_20d))
    if port_rets_20d.empty:
        logger.error("no holdout rebalances — abort")
        return 1

    label = f"HOLDOUT {args.holdout_start.year}-{args.holdout_end.year}"
    stats = _assess(
        port_rets_20d,
        bench_rets_20d,
        carhart,
        args.rebalance_stride,
        args.cost_half_spread_bps,
        top_lists,
    )
    _emit_result_line(
        label,
        stats,
        half_spread_bps=args.cost_half_spread_bps,
        adv_min_usd=args.adv_min_usd,
    )

    # NEW v5 gates: in-CV IR + holdout mean rank-IC.
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

    verdict = _verdict_for(
        stats,
        fit.n_nonzero_coefs,
        in_cv_ir=in_cv_ir,
        holdout_mean_ic=holdout_mean_ic,
    )
    logger.info(
        "verdict: %s | t=%.2f Sh_net=%.2f a_4F=%.1f%% MaxDD=%.1f%% nonzero=%d/10 IR=%.2f hold-IC=%.4f",
        verdict,
        stats.get("t_4f", float("nan")),
        stats.get("sharpe_net", float("nan")),
        stats.get("alpha_gross_4f", float("nan")) * 100,
        stats.get("max_drawdown", float("nan")) * 100,
        fit.n_nonzero_coefs,
        in_cv_ir,
        holdout_mean_ic,
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
        logger.info("wrote -> %s", args.out)
    return 0


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

    lines = [
        "# alt_data_screener_v3 Phase B — holdout reveal (rank-target ablation)",
        "",
        "**Pre-registration:** `alt_data_screener_v3_2026_05_01` "
        "(class `alt_data_screener_search_2026_04_30`, n=3 -> Bonferroni |t|>=2.39)",
        f"**Phase offset:** {args.phase_offset} (stride {args.rebalance_stride}, "
        f"holding {args.holding}d, overlap 4-tranche)",
        f"**Train:** {args.train_start} -> {args.holdout_start - timedelta(days=1)}",
        f"**Holdout:** {args.holdout_start} -> {args.holdout_end}",
        f"**ADV floor:** ${args.adv_min_usd / 1e6:.0f}M, 20d median",
        f"**Cost:** half-spread {args.cost_half_spread_bps:.0f} bps + 5 bps adverse selection",
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
        f"| mean top-N | {stats.get('mean_top_n', 0):.1f} |",
        f"| turnover / rebal | {stats.get('turnover_per_rebal', 0) * 100:.1f}% |",
        f"| Sharpe (gross, Lo-adj) | {stats.get('sharpe_gross', 0):.2f} |",
        f"| Sharpe (gross, naive) | {stats.get('sharpe_gross_naive', 0):.2f} |",
        f"| Sharpe (net, Lo-adj) | {stats.get('sharpe_net', 0):.2f} |",
        f"| alpha (gross, 4F) annualised | {stats.get('alpha_gross_4f', 0) * 100:+.2f}% |",
        f"| alpha (net, 4F) annualised | {stats.get('alpha_net_4f', 0) * 100:+.2f}% |",
        f"| alpha t-stat (HAC, maxlags={HAC_MAXLAGS}) | {stats.get('t_4f', 0):+.2f} |",
        f"| Excess vs SPY (gross) ann | {stats.get('excess_vs_bench_ann', 0) * 100:+.2f}% |",
        f"| Excess vs SPY (net) ann | {stats.get('excess_vs_bench_net', 0) * 100:+.2f}% |",
        f"| Max drawdown (net cum) | {stats.get('max_drawdown', 0) * 100:+.2f}% |",
        f"| Cost drag annualised | {stats.get('cost_drag_ann', 0) * 100:.2f}% |",
        f"| Carhart aggregated n | {stats.get('carhart_aggregated_n', 0)} |",
        f"| **NEW: in-CV IR (mean fold IC / std)** | {stats.get('in_cv_ir', float('nan')):.3f} |",
        f"| **NEW: holdout mean per-asof rank-IC** | {stats.get('holdout_mean_rank_ic', float('nan')):+.4f} |",
        f"| holdout asofs with valid IC | {stats.get('holdout_n_asofs_with_ic', 0)} |",
        "",
        "## Train/holdout sizes",
        "",
        f"- feature rows in train pool: {n_feat_train}",
        f"- train rows after target NaN-drop: {n_train_aligned}",
        f"- feature rows in holdout: {n_feat_holdout}",
        f"- holdout rows scored: {n_scored}",
        "",
        "## Global Lasso fit (20d-forward target)",
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
        "## Comparison to prior in-class + cross-class results",
        "",
        "Prior cross-class (`multi_source_two_stage_search_2026_04_30`):",
        "- v1 (4-regime, 5d): mean phase alpha-t=+0.65, 4.9pp dispersion, FAIL",
        "- v2 (global, 5d): mean phase alpha-t=+0.55, FAIL — 0/21 coefs",
        "- v3 (global, 20d): alpha-t=+1.32, FAIL — 0/21 coefs",
        "",
        "Prior in-class (`alt_data_screener_search_2026_04_30`):",
        "- v1 (FINRA daily flow): ABANDONED (infra block)",
        "- v2 (Polygon SI, raw return target): alpha-t=+0.05, FAIL — 0/10 coefs",
        "",
        "v3 in-class ablation: ONLY target rank-transform changed vs v2.",
    ]
    out_path.write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())

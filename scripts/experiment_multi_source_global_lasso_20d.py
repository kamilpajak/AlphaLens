"""Phase B v3 experiment driver — multi_source_global_lasso_20d (Exp 3).

Pre-registered as `multi_source_global_lasso_20d_2026_04_30` in class
`multi_source_two_stage_search_2026_04_30` (n=3 → Bonferroni |t| ≥ 2.39).
Pre-reg: `docs/research/preregistration/params_multi_source_global_lasso_20d_2026_04_30.json`.

Single-variable ablation FROM v2 (multi_source_global_lasso_2026_04_30).
ONLY target horizon changes: 5d → 20d. Architecture stays Global Lasso.

Design decisions per zen + Perplexity adversarial review:
- Stride STAYS at 5d → overlapping 4-tranche portfolios so each tranche
  holds 20d while preserving n≈99 holdout observations (vs n≈25 if we
  switched stride to 20).
- Each portfolio_returns observation is the 20d-forward return of the
  top-30 picked at that asof. Adjacent observations (5d apart) share
  15/20 = 75% of their holding window → MA(3-4) autocorrelation by
  construction.
- Carhart attribution uses 20d-AGGREGATED daily factor returns (sum of
  daily factor returns over the same 20d window) at each asof — keeps
  LHS and RHS at the same time scale.
- HAC maxlags explicitly set to 5 (covers MA(3-4) plus a buffer).
- Sharpe uses Lo (2002) variance-ratio adjustment via
  `sharpe_autocorr_adjusted`, which corrects for the autocorrelation
  introduced by overlap.

Architecture-pipeline IDENTICAL to v2 except for the four points above.
"""

from __future__ import annotations

import argparse
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
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import load_carhart_daily
from alphalens.data.macro.fred_client import FREDClient
from alphalens.data.store.history import HistoryStore
from alphalens.data.store.survivorship_pit import load_delisting_events
from alphalens.screeners.multi_source_two_stage import (
    EMBARGO_DAYS_DEFAULT,
    LAMBDA_GRID_POINTS_DEFAULT,
    N_FOLDS_DEFAULT,
    aligned_train_targets,
    build_feature_frame,
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

CARHART_COLS = ["Mkt-RF", "SMB", "HML", "Mom"]

# v3 frozen by pre-reg
HOLDING_PERIOD = 20
HAC_MAXLAGS = 5


# ---------------------------------------------------------------------------
# Data loaders (identical to v1/v2)


def _load_pit_union(start: date, end: date) -> list[str]:
    union: set[str] = set()
    for path in sorted(PIT_DIR.glob("*.yaml")):
        snap_date = date.fromisoformat(path.stem + "-01")
        if not (start.replace(day=1) <= snap_date <= end):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for ticker in data.get("tickers", []):
            union.add(ticker)
    return sorted(union)


def _load_fred_dict() -> dict[str, pd.Series]:
    client = FREDClient.from_env()
    return {sid: client.fetch_series(sid) for sid in ("VIXCLS", "DGS10", "DGS3MO")}


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


# ---------------------------------------------------------------------------
# ADV filter (identical to v1/v2)


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
# v3-specific: 20d-forward portfolio returns + 20d-aggregated factor returns


def _holdout_portfolio_returns_20d(
    holdout_features: pd.DataFrame,
    scores: pd.Series,
    history_store: HistoryStore,
    *,
    benchmark: str,
    top_n: int,
    holding_period: int = HOLDING_PERIOD,
) -> tuple[pd.Series, pd.Series, list[list[str]]]:
    """Per holdout asof: top-N by score → 20d-forward EW return.

    Returns
    -------
    (portfolio_returns_20d, bench_returns_20d, top_n_lists)
        Both Series indexed by asof; values = 20d-forward total return of
        equal-weight top-N basket / benchmark.
    """
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
    """Aggregate daily Carhart factors over 20d-forward windows starting after each asof.

    For each asof in `asof_dates`:
    - Find the next trading day after asof in `carhart.index` (entry day = day +1bd).
    - Sum the daily factor returns over the next `holding_period` trading days
      (entry through exit close-to-close).
    - This matches the convention used by HistoryStore.forward_return:
      entry at first bar after asof, holding_period bars later.

    Returns DataFrame indexed by asof (Timestamp), with columns
    matching `carhart.columns`. Rows where insufficient forward bars exist
    (e.g. asof too close to end of available factor data) are dropped.
    """
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
    """v3-specific assessment: 20d-forward returns + 20d-aggregated factors + HAC=5 + Lo-2002 Sharpe."""
    rets = portfolio_returns_20d.dropna()
    if rets.empty:
        return {"n": 0}
    rebalances_per_year = 252 / max(1, rebalance_stride)

    # Lo-2002 autocorrelation-adjusted Sharpe — accounts for overlap-induced AR.
    sharpe_gross = sharpe_autocorr_adjusted(
        rets.tolist(),
        periods_per_year=int(rebalances_per_year),
        max_lag=5,
    )
    # Naive Sharpe for diagnostic comparison only.
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

    # Carhart-4F regression on 20d-aggregated factors with explicit HAC=5.
    carhart_20d = _aggregate_carhart_20d(carhart, rets.index, holding_period=HOLDING_PERIOD)
    if len(carhart_20d) < 20:
        logger.warning(
            "20d-aggregated Carhart has only %d rows; HAC regression may be underpowered",
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
    ap.add_argument("--train-start", type=date.fromisoformat, default=date(2014, 1, 1))
    ap.add_argument("--holdout-start", type=date.fromisoformat, default=date(2024, 4, 30))
    ap.add_argument("--holdout-end", type=date.fromisoformat, default=date(2026, 4, 30))
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/multi_source_global_lasso_20d_phase_b.md"),
    )
    return ap


def _emit_result_line(
    label: str, stats: Mapping, *, half_spread_bps: float, adv_min_usd: float
) -> None:
    if stats.get("n", 0) == 0:
        return
    logger.info(
        "%s | ADV≥$%.0fM cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
        "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | α 4F=%.1f%% t=%.2f",
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


def _verdict_for(stats: Mapping, n_nonzero_coefs: int) -> str:
    """Pre-reg success_criteria translation. n=3 → t≥2.39 for PASS.

    Adds explicit zero-coef gate from v2's lesson — if all coefs zeroed, any
    apparent alpha is structural artifact, NOT a real signal.
    """
    t = stats.get("t_4f", 0.0)
    sh = stats.get("sharpe_net", 0.0)
    a = stats.get("alpha_gross_4f", 0.0)
    mdd = stats.get("max_drawdown", 0.0)

    if n_nonzero_coefs == 0:
        return "FAIL (zero-coef structural artifact — see v2 finding)"
    if t >= 2.39 and sh >= 0.5 and a >= 0.03 and mdd >= -0.35:
        return "PASS (provisional — pending multi-phase audit)"
    if t >= 1.5 and sh >= 0.3:
        return "MID (refine and re-pre-register before deploy)"
    return "FAIL (log to ledger)"


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

    universe = _load_pit_union(full_start, full_end)
    logger.info("PIT universe size %s..%s = %d", full_start, full_end, len(universe))
    histories = load_cached_histories([*universe, args.benchmark], PRICES_DIR)
    history_store = HistoryStore(histories)
    logger.info("history store has %d tickers", len(history_store.tickers()))

    delisting_events = load_delisting_events(
        parquet_path=SURVIVORSHIP_PARQUET if SURVIVORSHIP_PARQUET.exists() else None,
        yaml_path=SURVIVORSHIP_YAML if SURVIVORSHIP_YAML.exists() else None,
    )
    logger.info("loaded %d delisting events", len(delisting_events))
    insider_scorer = ParquetInsiderScorer(INSIDER_PARQUET, delisting_events=delisting_events)

    ff_start = full_start - timedelta(days=400)
    carhart = load_carhart_daily(start=ff_start, end=full_end)
    logger.info(
        "carhart %d rows %s..%s",
        len(carhart),
        carhart.index.min().date() if len(carhart) else None,
        carhart.index.max().date() if len(carhart) else None,
    )
    fred = _load_fred_dict()

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
        carhart_factors=carhart,
        fred_series=fred,
        universe=feat_universe,
        asof_dates=asof_dates,
        train_end=train_end,
        benchmark=args.benchmark,
    )
    logger.info("feature frame raw shape: %s", features.shape)

    features = _apply_adv_filter(features, history_store, args.adv_min_usd)
    logger.info("feature frame post-ADV (≥$%.0fM): %s", args.adv_min_usd / 1e6, features.shape)
    if features.empty:
        logger.error("feature frame empty after ADV filter — abort")
        return 1

    # v3 difference: target horizon is 20d not 5d.
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
    )
    if fit is None:
        logger.error("fit_global returned None — abort")
        return 1
    logger.info(
        "global fit (20d target): n_train=%d, λ chosen=%.4g, nonzero coefs=%d/%d, CV mean MSE=%.4g",
        fit.n_train_obs,
        fit.chosen_alpha,
        fit.n_nonzero_coefs,
        len(fit.feature_names),
        fit.cv_mean_mse,
    )

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
    verdict = _verdict_for(stats, fit.n_nonzero_coefs)
    logger.info(
        "verdict: %s | t=%.2f Sh_net=%.2f α_4F=%.1f%% MaxDD=%.1f%% nonzero=%d/21",
        verdict,
        stats.get("t_4f", float("nan")),
        stats.get("sharpe_net", float("nan")),
        stats.get("alpha_gross_4f", float("nan")) * 100,
        stats.get("max_drawdown", float("nan")) * 100,
        fit.n_nonzero_coefs,
    )

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
    logger.info("wrote → %s", args.out)
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
    lines = [
        "# multi_source_global_lasso_20d Phase B — holdout reveal",
        "",
        "**Pre-registration:** `multi_source_global_lasso_20d_2026_04_30` "
        "(class `multi_source_two_stage_search_2026_04_30`, n=3 → Bonferroni |t|≥2.39)",
        f"**Phase offset:** {args.phase_offset} (stride {args.rebalance_stride}, holding {args.holding}d, overlap 4-tranche)",
        f"**Train:** {args.train_start} → {args.holdout_start - timedelta(days=1)}",
        f"**Holdout:** {args.holdout_start} → {args.holdout_end}",
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
        f"| α (gross, 4F) annualised | {stats.get('alpha_gross_4f', 0) * 100:+.2f}% |",
        f"| α (net, 4F) annualised | {stats.get('alpha_net_4f', 0) * 100:+.2f}% |",
        f"| α t-stat (HAC, maxlags={HAC_MAXLAGS}) | {stats.get('t_4f', 0):+.2f} |",
        f"| Excess vs SPY (gross) ann | {stats.get('excess_vs_bench_ann', 0) * 100:+.2f}% |",
        f"| Excess vs SPY (net) ann | {stats.get('excess_vs_bench_net', 0) * 100:+.2f}% |",
        f"| Max drawdown (net cum) | {stats.get('max_drawdown', 0) * 100:+.2f}% |",
        f"| Cost drag annualised | {stats.get('cost_drag_ann', 0) * 100:.2f}% |",
        f"| Carhart aggregated n | {stats.get('carhart_aggregated_n', 0)} |",
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
        f"- λ chosen: {fit.chosen_alpha:.4g}",
        f"- nonzero coefs: {fit.n_nonzero_coefs} / {len(fit.feature_names)}",
        f"- CV mean MSE: {fit.cv_mean_mse:.4g}",
        "",
        "## v3 vs v1+v2 (horizon ablation)",
        "",
        "v1 (4-regime, 5d): mean phase αt=+0.65, dispersion 4.9pp, FAIL.",
        "v2 (global, 5d): mean phase αt=+0.55, dispersion 5.6pp, FAIL — 0/21 nonzero coefs.",
        "v3 (global, 20d): tested whether 20d horizon surfaces signal that 5d doesn't.",
        "",
        "## Pre-registration discipline",
        "",
        "- 21-feature whitelist FROZEN; identical to v1 + v2.",
        "- λ grid (25 points), embargo (60d), n_folds (3) — all per pre-reg.",
        "- ONE-shot holdout, no peek-and-tune.",
        "- Carhart attribution post-hoc on 20d-aggregated factors with HAC maxlags=5.",
        "- Lo (2002) variance-ratio Sharpe adjustment for overlap autocorrelation.",
        "- Single-variable ablation FROM v2: only target horizon (5d→20d) and inference machinery (HAC=5, Lo Sharpe) change.",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())

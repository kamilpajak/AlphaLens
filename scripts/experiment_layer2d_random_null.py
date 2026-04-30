"""Layer 2d ranking-invariance null test — empirical confirmation.

The 2026-04-28 variant exploration suggested the IS Carhart α was a property
of the cluster-positive *set*, not the ranking on top of it. This script tests
that hypothesis directly: at each rebalance, instead of picking top-15 by
``insider_count``, sample 15 names uniformly at random from the cluster-positive
candidates. Repeat K=100 times to build a null distribution of Carhart α and
t-stat. Then locate the V0 baseline (α=103.5%, t=2.14) within that distribution.

If V0 sits near the median of the null, ranking added zero information — the
artifact is purely distributional. If V0 is at the upper tail (p>95), ranking
genuinely picked above-average names within the candidate pool.

RESEARCH ONLY.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import yaml

from alphalens.archive.screeners.insider.parquet_scorer import ParquetInsiderScorer
from alphalens.attribution.factor_analysis import run_carhart_attribution
from alphalens.backtest.metrics import sharpe
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import load_carhart_daily
from alphalens.data.store.history import HistoryStore

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_PARQUET_PATH = Path.home() / ".alphalens" / "insider_form4.parquet"


def load_pit_union(start: date, end: date) -> list[str]:
    union: set[str] = set()
    for path in sorted(_PIT_DIR.glob("*.yaml")):
        snap_date = date.fromisoformat(path.stem + "-01")
        if not (start.replace(day=1) <= snap_date <= end):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        for ticker in data.get("tickers", []):
            union.add(ticker)
    return sorted(union)


def collect_candidate_frames(
    insider_store: ParquetInsiderScorer,
    history_store: HistoryStore,
    universe: list[str],
    benchmark: str,
    start: date,
    end: date,
    rebalance_stride: int,
    phase_offset: int = 0,
) -> tuple[list[pd.Timestamp], list[np.ndarray], list[np.ndarray]]:
    """Pre-compute, per rebalance, (cluster-positive tickers, fwd_1d returns).

    Returns (calendar_dates, ticker_arrays, return_arrays). The K random-sample
    trials reuse these in-memory — the slow part (parquet lookup + forward-return
    fetch) happens once.
    """
    calendar = HistoryStore.benchmark_calendar(history_store, benchmark, start, end)
    calendar = calendar[phase_offset::rebalance_stride]
    logger.info(
        "benchmark calendar: %d rebalance days (stride=%d phase=%d)",
        len(calendar),
        rebalance_stride,
        phase_offset,
    )

    rebal_dates: list[pd.Timestamp] = []
    rebal_tickers: list[np.ndarray] = []
    rebal_returns: list[np.ndarray] = []

    skipped_no_candidates = 0
    skipped_no_returns = 0

    for ts in calendar:
        day = ts.date()
        positive_tickers: list[str] = []
        positive_returns: list[float] = []
        for ticker in universe:
            if ticker == benchmark:
                continue
            feat = insider_store.features_as_of(ticker, day)
            if not feat:
                continue
            r = history_store.forward_return(ticker, day, 1)
            if r is None:
                continue
            positive_tickers.append(ticker)
            positive_returns.append(float(r))

        if not positive_tickers:
            skipped_no_candidates += 1
            continue
        if len(positive_returns) == 0:
            skipped_no_returns += 1
            continue

        rebal_dates.append(ts)
        rebal_tickers.append(np.asarray(positive_tickers, dtype=object))
        rebal_returns.append(np.asarray(positive_returns, dtype=float))

    logger.info(
        "collected %d rebalances; skipped %d no-candidates, %d no-returns",
        len(rebal_dates),
        skipped_no_candidates,
        skipped_no_returns,
    )
    return rebal_dates, rebal_tickers, rebal_returns


def trial_portfolio_returns(
    rebal_returns: list[np.ndarray], top_n: int, rng: np.random.Generator
) -> np.ndarray:
    """Sample min(top_n, len) random indices per rebalance, mean of returns."""
    out = np.empty(len(rebal_returns), dtype=float)
    for i, rets in enumerate(rebal_returns):
        if len(rets) <= top_n:
            out[i] = rets.mean()
        else:
            idx = rng.choice(len(rets), size=top_n, replace=False)
            out[i] = rets[idx].mean()
    return out


def run_null_distribution(
    rebal_dates: list[pd.Timestamp],
    rebal_returns: list[np.ndarray],
    top_n: int,
    n_trials: int,
    rebalance_stride: int,
    carhart_factors: pd.DataFrame,
    seed: int = 17,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rebalances_per_year = 252 / max(1, rebalance_stride)
    idx = pd.DatetimeIndex(rebal_dates)

    rows: list[dict] = []
    for trial in range(n_trials):
        rets = trial_portfolio_returns(rebal_returns, top_n, rng)
        series = pd.Series(rets, index=idx, name=f"trial_{trial}")
        try:
            carhart_res = run_carhart_attribution(series, carhart_factors)[-1]
        except (ValueError, RuntimeError) as exc:
            logger.warning("trial %d carhart failed: %s", trial, exc)
            continue
        rows.append(
            {
                "trial": trial,
                "alpha_ann": float(carhart_res.alpha_annualized),
                "alpha_t": float(carhart_res.alpha_tstat),
                "r_squared": float(carhart_res.r_squared),
                "sharpe": float(sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))),
            }
        )
        if (trial + 1) % 20 == 0:
            logger.info("trial %d/%d done", trial + 1, n_trials)

    return pd.DataFrame(rows)


def quantile_rank(value: float, distribution: np.ndarray) -> float:
    return float(np.mean(distribution <= value))


def format_report(
    null_df: pd.DataFrame, baseline: dict, label: str, top_n: int, n_rebal: int
) -> str:
    alpha_arr = null_df["alpha_ann"].to_numpy()
    t_arr = null_df["alpha_t"].to_numpy()
    sharpe_arr = null_df["sharpe"].to_numpy()
    r2_arr = null_df["r_squared"].to_numpy()

    def quantiles(arr: np.ndarray) -> dict:
        return {
            "min": float(arr.min()),
            "p05": float(np.quantile(arr, 0.05)),
            "p25": float(np.quantile(arr, 0.25)),
            "median": float(np.quantile(arr, 0.50)),
            "p75": float(np.quantile(arr, 0.75)),
            "p95": float(np.quantile(arr, 0.95)),
            "max": float(arr.max()),
            "mean": float(arr.mean()),
            "std": float(arr.std(ddof=1)),
        }

    α_q = quantiles(alpha_arr)
    t_q = quantiles(t_arr)
    s_q = quantiles(sharpe_arr)
    r2_q = quantiles(r2_arr)

    α_pct = quantile_rank(baseline["alpha_ann"], alpha_arr) * 100
    t_pct = quantile_rank(baseline["alpha_t"], t_arr) * 100
    sharpe_pct = quantile_rank(baseline["sharpe"], sharpe_arr) * 100

    lines = [
        f"## Null distribution — {label}",
        "",
        f"- N trials: {len(null_df)}",
        f"- Top-N per rebalance: {top_n}",
        f"- Rebalance count: {n_rebal}",
        "- Sampling: uniform without replacement from cluster-positive set per rebalance",
        "",
        "### Distribution quantiles",
        "",
        "| Metric | min | p05 | p25 | median | p75 | p95 | max | mean | std |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        "| Carhart α (ann) | "
        + " | ".join(
            f"{α_q[k] * 100:.2f}%"
            for k in ("min", "p05", "p25", "median", "p75", "p95", "max", "mean", "std")
        )
        + " |",
        "| Carhart t-stat | "
        + " | ".join(
            f"{t_q[k]:.2f}"
            for k in ("min", "p05", "p25", "median", "p75", "p95", "max", "mean", "std")
        )
        + " |",
        "| Sharpe | "
        + " | ".join(
            f"{s_q[k]:.2f}"
            for k in ("min", "p05", "p25", "median", "p75", "p95", "max", "mean", "std")
        )
        + " |",
        "| R² | "
        + " | ".join(
            f"{r2_q[k]:.4f}"
            for k in ("min", "p05", "p25", "median", "p75", "p95", "max", "mean", "std")
        )
        + " |",
        "",
        "### V0 baseline percentile within null",
        "",
        "| Metric | V0 baseline | Null median | Null p95 | V0 percentile in null |",
        "|---|---:|---:|---:|---:|",
        f"| Carhart α (ann) | {baseline['alpha_ann'] * 100:.2f}% | {α_q['median'] * 100:.2f}% | {α_q['p95'] * 100:.2f}% | {α_pct:.0f}th |",
        f"| Carhart t-stat | {baseline['alpha_t']:.2f} | {t_q['median']:.2f} | {t_q['p95']:.2f} | {t_pct:.0f}th |",
        f"| Sharpe | {baseline['sharpe']:.2f} | {s_q['median']:.2f} | {s_q['p95']:.2f} | {sharpe_pct:.0f}th |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument(
        "--phase-offset",
        type=int,
        default=0,
        help="Phase offset for strided rebalance calendar; 0..rebalance_stride-1.",
    )
    ap.add_argument("--n-trials", type=int, default=100)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--label", default="IS_2011_2022")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument(
        "--baseline-alpha",
        type=float,
        default=1.0353,
        help="V0 baseline Carhart α (annualized, decimal). Default 103.53%% from layer2d_variants.md.",
    )
    ap.add_argument("--baseline-t", type=float, default=2.14)
    ap.add_argument("--baseline-sharpe", type=float, default=0.96)
    ap.add_argument("--out", type=Path, default=Path("docs/research/layer2d_null_distribution.md"))
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    logger.info("loading parquet insider store")
    insider_store = ParquetInsiderScorer(_PARQUET_PATH)

    universe = load_pit_union(args.start, args.end)
    logger.info("PIT union universe: %d tickers", len(universe))

    tickers_with_bench = [*universe, args.benchmark]
    histories = load_cached_histories(tickers_with_bench, _PRICES_DIR)
    logger.info("loaded %d histories", len(histories))
    history_store = HistoryStore(histories)

    rebal_dates, _rebal_tickers, rebal_returns = collect_candidate_frames(
        insider_store=insider_store,
        history_store=history_store,
        universe=universe,
        benchmark=args.benchmark,
        start=args.start,
        end=args.end,
        rebalance_stride=args.rebalance_stride,
        phase_offset=args.phase_offset,
    )
    if not rebal_dates:
        logger.error("no rebalances with candidates")
        return 2

    candidate_sizes = np.asarray([len(r) for r in rebal_returns])
    logger.info(
        "candidate-pool sizes: min=%d p50=%d p95=%d max=%d (rebalances where pool>top_n: %d/%d)",
        int(candidate_sizes.min()),
        int(np.median(candidate_sizes)),
        int(np.quantile(candidate_sizes, 0.95)),
        int(candidate_sizes.max()),
        int((candidate_sizes > args.top_n).sum()),
        len(candidate_sizes),
    )

    carhart_factors = load_carhart_daily(start=args.start, end=args.end)

    logger.info("running %d random-subset trials", args.n_trials)
    null_df = run_null_distribution(
        rebal_dates=rebal_dates,
        rebal_returns=rebal_returns,
        top_n=args.top_n,
        n_trials=args.n_trials,
        rebalance_stride=args.rebalance_stride,
        carhart_factors=carhart_factors,
        seed=args.seed,
    )

    baseline = {
        "alpha_ann": args.baseline_alpha,
        "alpha_t": args.baseline_t,
        "sharpe": args.baseline_sharpe,
    }
    report = format_report(
        null_df, baseline, label=args.label, top_n=args.top_n, n_rebal=len(rebal_dates)
    )
    print("\n" + report + "\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        existing = args.out.read_text()
    else:
        existing = (
            "# Layer 2d random-subset null distribution\n\n"
            "RESEARCH ONLY. Tests whether V0_count baseline IS α is rank-driven\n"
            "or distributional. If V0 sits near null median, ranking added no\n"
            "information beyond cluster-positive membership.\n\n"
            f"- Top-N: {args.top_n}\n"
            f"- Rebalance stride: {args.rebalance_stride}\n"
            f"- N trials: {args.n_trials}\n"
            f"- Universe: PIT union ({len(universe)} tickers)\n\n"
        )
    args.out.write_text(existing + "\n" + report + "\n")

    csv_out = args.out.with_suffix(".csv")
    null_df.to_csv(csv_out, index=False)
    logger.info("wrote null distribution → %s + %s", args.out, csv_out)

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Extended diagnostics for MVP1 backtest results.

Addresses three questions Perplexity flagged about the baseline run:

1. **Tail-concentration hypothesis** — is the edge in the top decile only, or
   distributed? `ic_by_decile()` computes Rank IC separately across score
   deciles so we can see U-shape vs flat-line.

2. **1-day vs 5-day IC horizon mismatch** — our Sharpe is 1-day forward, our
   IC is 5-day forward. `ic_at_horizon()` computes IC for arbitrary horizons
   off the same daily snapshots.

3. **Bear-regime paradox** (Sharpe up, IC down) — `bear_vol_decomposition()`
   checks whether top-30 simply has lower vol than the universe in bear, which
   would mean we're capturing defensive positioning, not predictive signal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from alphalens.backtest.engine import BacktestReport
from alphalens.backtest.metrics import rank_ic


@dataclass(frozen=True)
class DecileICResult:
    decile: int  # 1 = bottom 10%, 10 = top 10%
    n_samples: int
    mean_return: float
    std_return: float
    sharpe_within_decile: float  # annualised Sharpe of this slice


@dataclass(frozen=True)
class HorizonICResult:
    horizon: int
    mean_ic: float
    ic_tstat: float
    n_dates: int


@dataclass(frozen=True)
class VolDecomposition:
    regime: str
    days: int
    top_n_vol_annualised: float
    universe_median_vol_annualised: float
    top_n_mean_return_annualised: float
    universe_median_mean_return_annualised: float
    vol_ratio: float  # top_n / universe_median
    excess_return_annualised: float


# ---------------------------------------------------------------------------
# IC by decile — tail-concentration test
# ---------------------------------------------------------------------------


def decile_returns_panel(report: BacktestReport) -> pd.DataFrame:
    """Flatten (date, decile, return) from rebalance_results into long-format DataFrame.

    For each snapshot we bucket scored tickers into 10 deciles by score and
    record the forward return. The bucket column uses 1 = lowest decile,
    10 = highest decile.
    """
    rows: list[dict] = []
    # We don't have the full scored frame per day in `report`; only top-N + stats.
    # Need access to the original engine state — in MVP this requires re-running
    # the scorer. Instead, approximate from top_n_scores + top_n_forward_returns
    # which gives us DECILE 10 behaviour; the rest requires engine instrumentation.
    # Returns empty if report doesn't carry enough info.
    for snap in report.rebalance_results:
        # Using only the top-N stored in the report.
        for ticker, score, fwd_ret in zip(
            snap.top_n_tickers,
            snap.top_n_scores,
            snap.top_n_forward_returns,
        ):
            rows.append(
                {
                    "date": snap.date,
                    "ticker": ticker,
                    "score": score,
                    "fwd_return": fwd_ret,
                    "decile": 10,
                }
            )
    return pd.DataFrame(rows)


def ic_by_decile_from_scored_frames(
    scored_frames: dict[pd.Timestamp, pd.DataFrame],
    return_column: str = "fwd_holding",
) -> list[DecileICResult]:
    """Compute per-decile summary stats.

    `scored_frames`: mapping of date → DataFrame with columns `score` plus a
    `return_column` (default `fwd_holding`, also accepts `fwd_1d`). The engine
    must be run with `retain_scored_frames=True` to populate this.
    """
    bucket_rows: list[dict] = []
    for d, df in scored_frames.items():
        if return_column not in df.columns:
            continue
        valid = df.dropna(subset=["score", return_column])
        if len(valid) < 10:
            continue
        deciles = pd.qcut(valid["score"], 10, labels=False, duplicates="drop")
        for decile, group in valid.groupby(deciles):
            bucket_rows.append(
                {
                    "date": d,
                    "decile": int(decile) + 1,
                    "mean_return": float(group[return_column].mean()),
                    "n": len(group),
                }
            )
    if not bucket_rows:
        return []

    panel = pd.DataFrame(bucket_rows)
    out: list[DecileICResult] = []
    for decile, group in panel.groupby("decile"):
        vals = group["mean_return"].dropna()
        if vals.empty:
            continue
        mean = float(vals.mean())
        std = float(vals.std(ddof=1)) if len(vals) > 1 else 0.0
        sharpe = (mean / std * np.sqrt(252)) if std > 1e-12 else 0.0
        out.append(
            DecileICResult(
                decile=int(decile),
                n_samples=int(group["n"].sum()),
                mean_return=mean,
                std_return=std,
                sharpe_within_decile=sharpe,
            )
        )
    return sorted(out, key=lambda r: r.decile)


def tail_concentration_score(results: list[DecileICResult]) -> float:
    """Ratio of (|decile 10| + |decile 1|) / (sum of |middle 8|).

    >1.5 implies strong tail concentration. ~1.0 means flat cross-section.
    """
    if len(results) < 10:
        return 0.0
    by_decile = {r.decile: r for r in results}
    tails = abs(by_decile[10].mean_return) + abs(by_decile[1].mean_return)
    middle = sum(abs(by_decile[d].mean_return) for d in range(2, 10))
    if middle == 0:
        return float("inf")
    # Normalise: tails is 2 buckets, middle is 8 buckets.
    return (tails / 2) / (middle / 8)


# ---------------------------------------------------------------------------
# IC at alternate horizon — Sharpe/IC horizon matching
# ---------------------------------------------------------------------------


def ic_at_horizon(
    report: BacktestReport,
    history_store,
    horizon: int,
) -> HorizonICResult:
    """Recompute the cross-sectional Rank IC at an alternate forward horizon.

    Re-scans each daily snapshot, fetches `horizon`-day forward returns for
    every ticker listed in `top_n_tickers` (proxy for the scored set — this is
    what the report retains). For a fair full-universe measurement, run the
    backtest with instrumented snapshotting instead.
    """
    from alphalens.backtest.metrics import rank_ic_tstat

    ic_values: list[float] = []
    for snap in report.rebalance_results:
        scores = snap.top_n_scores
        fwd_rets = [
            history_store.forward_return(t, snap.date.date(), horizon) for t in snap.top_n_tickers
        ]
        # Filter None
        pairs = [(s, r) for s, r in zip(scores, fwd_rets) if r is not None]
        if len(pairs) < 3:
            continue
        scores_v, rets_v = zip(*pairs)
        ic_values.append(rank_ic(scores_v, rets_v))

    if not ic_values:
        return HorizonICResult(horizon=horizon, mean_ic=0.0, ic_tstat=0.0, n_dates=0)
    mean = float(np.mean(ic_values))
    tstat = rank_ic_tstat(ic_values)
    return HorizonICResult(horizon=horizon, mean_ic=mean, ic_tstat=tstat, n_dates=len(ic_values))


# ---------------------------------------------------------------------------
# Bear-regime volatility decomposition
# ---------------------------------------------------------------------------


def vol_decomposition_by_regime(
    report: BacktestReport,
    regime_labels: pd.Series,
    periods_per_year: int = 252,
) -> Mapping[str, VolDecomposition]:
    """Compare top-N portfolio vol/return to universe median vol/return per regime.

    If top-N vol is significantly below universe-median vol in bear regime AND
    mean returns are similar, we're capturing defensive positioning, not alpha.
    Genuine alpha should show BOTH lower vol AND higher mean return.
    """
    port = report.portfolio_returns
    median = report.universe_median_returns
    aligned = pd.concat(
        [port.rename("port"), median.rename("median"), regime_labels.rename("regime")],
        axis=1,
        join="inner",
    ).dropna()

    out: dict[str, VolDecomposition] = {}
    for regime, group in aligned.groupby("regime"):
        if len(group) < 5:
            continue
        port_vol = float(group["port"].std(ddof=1)) * np.sqrt(periods_per_year)
        median_vol = float(group["median"].std(ddof=1)) * np.sqrt(periods_per_year)
        port_mean = float(group["port"].mean()) * periods_per_year
        median_mean = float(group["median"].mean()) * periods_per_year
        out[str(regime)] = VolDecomposition(
            regime=str(regime),
            days=len(group),
            top_n_vol_annualised=port_vol,
            universe_median_vol_annualised=median_vol,
            top_n_mean_return_annualised=port_mean,
            universe_median_mean_return_annualised=median_mean,
            vol_ratio=(port_vol / median_vol) if median_vol > 1e-9 else float("inf"),
            excess_return_annualised=port_mean - median_mean,
        )
    return out


def format_vol_decomposition(vol_stats: Mapping[str, VolDecomposition]) -> str:
    """Human-readable formatter for CLI output."""
    lines = [
        "Regime | Days | Top-N Vol | Med Vol | Vol Ratio | Top-N Ret | Med Ret | Excess",
        "----   | ---- | --------- | ------- | --------- | --------- | ------- | ------",
    ]
    for regime in ("bull", "bear", "flat"):
        if regime not in vol_stats:
            continue
        v = vol_stats[regime]
        lines.append(
            f"{regime:6s} | {v.days:4d} | {v.top_n_vol_annualised * 100:+.2f}% "
            f"| {v.universe_median_vol_annualised * 100:+.2f}% | {v.vol_ratio:+.2f} "
            f"| {v.top_n_mean_return_annualised * 100:+.2f}% "
            f"| {v.universe_median_mean_return_annualised * 100:+.2f}% "
            f"| {v.excess_return_annualised * 100:+.2f}%"
        )
    return "\n".join(lines)

"""Layer 2d Experiment 3f — prior-return reverse-causality check.

Perplexity peer review (2026-04-28) raised: "Are insider clusters formed on
stocks that are already going up? If cluster-positive names have positive
abnormal prior 5d/20d returns vs non-cluster names, your α is a momentum-
residual artifact, not insider content."

This script computes, per rebalance, the mean prior 5d and 20d returns of
cluster-positive tickers vs non-cluster tickers in the same PIT universe,
then aggregates over rebalances per period.

If cluster-set prior returns are systematically higher: α is at least
partially momentum-residual (not absorbed by Carhart MOM because MOM is
12-1 month, not 1d/5d/20d).
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

from alphalens.alt_data.yfinance_cache import load_cached_histories
from alphalens.backtest.history_store import HistoryStore
from alphalens.screeners.insider.parquet_scorer import ParquetInsiderScorer

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_PARQUET_PATH = Path.home() / ".alphalens" / "insider_form4.parquet"

PRIOR_WINDOWS = (5, 20, 60)


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


def prior_return(history_store: HistoryStore, ticker: str, asof: date, window: int) -> float | None:
    """Return = close[asof] / close[asof - window_trading_days] - 1.

    Uses the ticker's own bars (no calendar alignment) — same convention as
    HistoryStore.forward_return. None if insufficient history.
    """
    df = history_store.truncate_to(ticker, asof)
    if len(df) < window + 1:
        return None
    closes = df["close"].to_numpy(dtype=float)
    if closes[-window - 1] <= 0:
        return None
    return float(closes[-1] / closes[-window - 1] - 1.0)


def collect_prior_returns_per_rebalance(
    insider_store: ParquetInsiderScorer,
    history_store: HistoryStore,
    universe: list[str],
    benchmark: str,
    start: date,
    end: date,
    rebalance_stride: int,
) -> list[dict]:
    """For each rebalance, compute mean prior 5/20/60d returns of cluster-positive
    set and of non-cluster set. Returns one row per rebalance.
    """
    calendar = HistoryStore.benchmark_calendar(history_store, benchmark, start, end)
    calendar = calendar[::rebalance_stride]

    rows: list[dict] = []
    for ts in calendar:
        day = ts.date()
        cluster_returns: dict[int, list[float]] = {w: [] for w in PRIOR_WINDOWS}
        noncluster_returns: dict[int, list[float]] = {w: [] for w in PRIOR_WINDOWS}

        for ticker in universe:
            if ticker == benchmark:
                continue
            feat = insider_store.features_as_of(ticker, day)
            is_cluster = feat is not None
            for window in PRIOR_WINDOWS:
                r = prior_return(history_store, ticker, day, window)
                if r is None:
                    continue
                if is_cluster:
                    cluster_returns[window].append(r)
                else:
                    noncluster_returns[window].append(r)

        if not cluster_returns[5]:
            continue

        row = {
            "date": ts,
            "n_cluster": len(cluster_returns[5]),
            "n_noncluster": len(noncluster_returns[5]),
        }
        for window in PRIOR_WINDOWS:
            cl = cluster_returns[window]
            nc = noncluster_returns[window]
            row[f"cluster_mean_{window}d"] = float(np.mean(cl)) if cl else float("nan")
            row[f"noncluster_mean_{window}d"] = float(np.mean(nc)) if nc else float("nan")
            row[f"diff_{window}d"] = (
                row[f"cluster_mean_{window}d"] - row[f"noncluster_mean_{window}d"]
            )
        rows.append(row)

    return rows


def summarize_period(rows: list[dict], label: str) -> str:
    df = pd.DataFrame(rows)
    if df.empty:
        return f"## {label}\n\nNo rebalances with cluster-positive set.\n"

    n = len(df)
    lines = [
        f"## {label}",
        "",
        f"- N rebalances: {n}",
        f"- Mean cluster-set size per rebalance: {df['n_cluster'].mean():.1f}",
        f"- Mean non-cluster-set size per rebalance: {df['n_noncluster'].mean():.0f}",
        "",
        "### Mean prior return (cluster vs non-cluster, paired t-test of difference)",
        "",
        "| Window | Cluster mean | Non-cluster mean | Diff | Diff SE | Diff t-stat | p<0.05? |",
        "|---|---:|---:|---:|---:|---:|:---:|",
    ]
    for window in PRIOR_WINDOWS:
        diff = df[f"diff_{window}d"].dropna()
        if len(diff) < 2:
            continue
        mean_diff = float(diff.mean())
        se_diff = float(diff.std(ddof=1) / np.sqrt(len(diff)))
        t_stat = mean_diff / se_diff if se_diff > 0 else float("nan")
        cluster_mean = float(df[f"cluster_mean_{window}d"].mean())
        noncluster_mean = float(df[f"noncluster_mean_{window}d"].mean())
        sig = "**Yes**" if abs(t_stat) >= 1.96 else "no"
        lines.append(
            f"| {window}d | {cluster_mean * 100:.3f}% | {noncluster_mean * 100:.3f}% | "
            f"{mean_diff * 100:.3f}pp | {se_diff * 100:.4f}pp | {t_stat:.2f} | {sig} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--out", type=Path, default=Path("docs/research/layer2d_prior_returns_3f.md"))
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    insider_store = ParquetInsiderScorer(_PARQUET_PATH)

    full_start, full_end = date(2011, 1, 1), date(2026, 4, 22)
    universe = load_pit_union(full_start, full_end)
    logger.info("PIT union universe: %d tickers", len(universe))

    histories = load_cached_histories([*universe, args.benchmark], _PRICES_DIR)
    history_store = HistoryStore(histories)

    periods = [
        ("Full IS 2011-2022", date(2011, 1, 1), date(2022, 12, 31)),
        ("IS 2011-2016", date(2011, 1, 1), date(2016, 12, 31)),
        ("IS 2017-2022", date(2017, 1, 1), date(2022, 12, 31)),
        ("OOS 2023-2026", date(2023, 1, 1), date(2026, 4, 22)),
    ]

    sections = [
        "# Layer 2d Experiment 3f — prior-return reverse-causality check",
        "",
        "**RESEARCH ONLY.** Tests Perplexity reviewer concern: do insider",
        "clusters preferentially form on stocks with positive recent returns?",
        "If yes, our Carhart-residual α is a short-window momentum residual",
        "(MOM factor is 12-1 month so it does not absorb 5d/20d/60d momentum).",
        "",
        f"- Rebalance stride: {args.rebalance_stride} (weekly)",
        f"- Universe: PIT union {len(universe)} tickers",
        f"- Benchmark: {args.benchmark}",
        f"- Prior windows: {', '.join(f'{w}d' for w in PRIOR_WINDOWS)}",
        "- Method: per-rebalance mean cluster-set prior return − mean non-cluster-set prior return,",
        "  then per-period mean and t-stat across rebalances.",
        "",
    ]

    for label, start, end in periods:
        logger.info("=== %s (%s..%s) ===", label, start, end)
        rows = collect_prior_returns_per_rebalance(
            insider_store=insider_store,
            history_store=history_store,
            universe=universe,
            benchmark=args.benchmark,
            start=start,
            end=end,
            rebalance_stride=args.rebalance_stride,
        )
        sections.append(summarize_period(rows, label))

    sections.append("## Interpretation guide")
    sections.append("")
    sections.append(
        "- **Diff t-stat near 0** across windows: cluster formation is NOT correlated with "
        "recent past returns. Carhart-residual α is not a short-window momentum artifact."
    )
    sections.append(
        "- **Diff t-stat consistently positive (>2)** across windows: cluster-positive set "
        "selects on recent positive returns. Headline α is partially MOM-residual; "
        "would be reduced if Carhart used a shorter momentum factor."
    )
    sections.append(
        "- **Diff t-stat consistently negative**: clusters form on losers (contrarian-buying "
        "by insiders). Different selection bias — value/distress angle. Argues for "
        "stronger value-factor controls (HML, RMW, CMA)."
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

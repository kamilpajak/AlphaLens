"""Weekly-rebalance sanity check per Perplexity Q1.

Hypothesis: EarlyStageScorer's Sharpe drop (1.49 → 1.13) is driven by
daily-rebalance turnover friction, not signal degradation. If Sharpe recovers
when we sample non-overlapping 5-day returns (simulating weekly rebalance),
the drop is mechanical, not fundamental.

Method:
  1. Run BacktestEngine daily for both scorers (MomentumScorer, EarlyStageScorer).
  2. Take `portfolio_return_holding` series (5-day forward return of top-5 at
     each pick day) — this is already the "holding-period" P&L per pick.
  3. Sample every 5th day → non-overlapping 5-day returns = weekly rebalance.
  4. Compute Sharpe with periods_per_year=52 (weekly annualization).
  5. Compare daily-rebalance Sharpe vs weekly-rebalance Sharpe.

Usage: .venv/bin/python scripts/weekly_rebalance_test.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.engine import BacktestEngine  # noqa: E402
from alphalens.backtest.history_store import HistoryStore  # noqa: E402
from alphalens.backtest.metrics import sharpe  # noqa: E402
from alphalens.screeners.lean.lean_csv_loader import load_lean_histories  # noqa: E402
from alphalens.screeners.themed.backtest_adapter import (  # noqa: E402
    early_stage_scorer_adapter,
    momentum_scorer_adapter,
)
from alphalens.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH  # noqa: E402
from alphalens.screeners.themed.early_stage_scorer import EARLY_STAGE_DEFAULTS  # noqa: E402
from alphalens.screeners.themed.universe import flatten_universe  # noqa: E402

LEAN_DATA = Path.home() / ".alphalens" / "lean" / "data"


def _max_dd(returns: pd.Series) -> float:
    if returns.empty:
        return float("nan")
    cum = (1 + returns).cumprod()
    peak = cum.cummax()
    return float(((cum - peak) / peak).min())


def run_scorer(store: HistoryStore, tickers: list[str], scorer_fn, cfg: dict, label: str):
    print(f"\n[{label}] running …")
    engine = BacktestEngine(
        store, scorer=scorer_fn, scorer_config=cfg,
        holding_period=5, top_n=5, benchmark="SPY",
        screener_tickers=tickers, weighting="linear",
    )
    engine.MIN_BARS_REQUIRED = 252
    return engine.run(start=date(2021, 6, 1), end=date(2026, 4, 17))


def compare(report, label: str):
    """Compute daily-Sharpe vs weekly-resampled-Sharpe on same report."""
    # Daily rebalance — 1-day forward returns
    daily_returns = report.portfolio_returns  # already per-day
    # Weekly rebalance simulation — take 5-day forward returns, sample every 5 days non-overlapping
    holding_returns = report.portfolio_returns_holding  # 5-day fwd per day
    weekly_returns = holding_returns.iloc[::5]  # sample every 5th row

    sharpe_daily = sharpe(daily_returns.tolist(), periods_per_year=252)
    sharpe_weekly = sharpe(weekly_returns.tolist(), periods_per_year=52)
    turnover_daily = float(report.turnover * 100)
    # Weekly turnover proxy: recompute from every-5th-day top-5 lists
    weekly_top_n = [r.top_n_tickers for r in report.daily_results[::5]]
    if len(weekly_top_n) < 2:
        turnover_weekly = float("nan")
    else:
        changes = [
            len(set(weekly_top_n[i]) ^ set(weekly_top_n[i-1])) / max(len(weekly_top_n[i-1]), 1)
            for i in range(1, len(weekly_top_n))
        ]
        turnover_weekly = sum(changes) / len(changes) * 100

    return {
        "label": label,
        "sharpe_daily": sharpe_daily,
        "sharpe_weekly": sharpe_weekly,
        "sharpe_recovery": sharpe_weekly - sharpe_daily,
        "turnover_daily_pct": turnover_daily,
        "turnover_weekly_pct": turnover_weekly,
        "max_dd_daily": _max_dd(daily_returns) * 100,
        "max_dd_weekly": _max_dd(weekly_returns) * 100,
        "mean_daily_ret_bps": daily_returns.mean() * 10000,
        "mean_weekly_ret_bps": weekly_returns.mean() * 10000,
        "daily_n": len(daily_returns),
        "weekly_n": len(weekly_returns),
    }


def main() -> None:
    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    curated = sorted(flatten_universe(universe).keys())
    histories = load_lean_histories(LEAN_DATA, curated + ["SPY", "IWM"])
    store = HistoryStore(histories)
    print(f"universe: {len(curated)} names")

    mom_cfg = dict(THEMED_DEFAULTS); mom_cfg["benchmark"] = "SPY"
    early_cfg = dict(EARLY_STAGE_DEFAULTS); early_cfg["benchmark"] = "SPY"

    mom_report = run_scorer(store, curated, momentum_scorer_adapter, mom_cfg, "momentum")
    early_report = run_scorer(store, curated, early_stage_scorer_adapter, early_cfg, "early_stage")

    mom_stats = compare(mom_report, "momentum")
    early_stats = compare(early_report, "early_stage")

    print("\n=== Weekly-rebalance sanity check ===\n")
    print(f"{'metric':<25} {'momentum':>15} {'early_stage':>15}")
    print("-" * 58)
    for k in ["daily_n", "weekly_n",
              "sharpe_daily", "sharpe_weekly", "sharpe_recovery",
              "turnover_daily_pct", "turnover_weekly_pct",
              "max_dd_daily", "max_dd_weekly",
              "mean_daily_ret_bps", "mean_weekly_ret_bps"]:
        mv = mom_stats[k]
        ev = early_stats[k]
        try:
            print(f"{k:<25} {mv:>15.4f} {ev:>15.4f}")
        except (TypeError, ValueError):
            print(f"{k:<25} {str(mv):>15} {str(ev):>15}")

    # Interpret
    print("\n=== Interpretation ===\n")
    if early_stats["sharpe_recovery"] > 0.15:
        print(f"✓ EarlyStage Sharpe recovers +{early_stats['sharpe_recovery']:.2f} on weekly rebalance.")
        print(f"  Turnover drops from {early_stats['turnover_daily_pct']:.0f}% → {early_stats['turnover_weekly_pct']:.0f}%.")
        print(f"  → Perplexity Q1 hypothesis CONFIRMED: daily rebalance friction is the Sharpe killer.")
        print(f"  → Consider weekly rebalance in production pipeline.")
    elif early_stats["sharpe_recovery"] > 0.0:
        print(f"△ EarlyStage Sharpe recovers +{early_stats['sharpe_recovery']:.2f} — small but positive.")
        print(f"  Partial confirmation of turnover hypothesis; signal contribution is not purely mechanical.")
    else:
        print(f"✗ EarlyStage Sharpe does NOT recover ({early_stats['sharpe_recovery']:+.2f}).")
        print(f"  → Turnover hypothesis REJECTED. Signal itself has lower quality per-pick.")

    # Save CSV
    out = Path.home() / ".alphalens" / "phase2" / "weekly_rebalance.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([mom_stats, early_stats]).to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()

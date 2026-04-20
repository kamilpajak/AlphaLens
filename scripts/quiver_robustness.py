"""Quiver Phase 0 robustness: does Congress L/S alpha collapse when we exclude
the top-N trades by $-magnitude?

Per Perplexity critique: 113-ticker × 3.3% signal density means top quintile is
mechanically driven by 2–3 large-$ trades on any given day. If removing the
10 largest congressional trades kills the alpha, the signal is artifact — a
handful of big outliers, not a distributed cross-sectional effect.

Reads cached trades from previous validate run (no new API calls).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.factor_analysis import format_attribution_table, run_regression  # noqa: E402
from alphalens.backtest.factors import load_carhart_daily  # noqa: E402
from alphalens.backtest.history_store import HistoryStore  # noqa: E402
from alphalens.lean_screener.config import DATA_DIR  # noqa: E402
from alphalens.lean_screener.lean_csv_loader import load_lean_histories  # noqa: E402
from alphalens.momentum_screener.config import UNIVERSE_PATH  # noqa: E402
from alphalens.momentum_screener.universe import flatten_universe  # noqa: E402
from alphalens.quiver_screener.features import build_congress_feature_panel  # noqa: E402

# Replicate exactly what quiver_validate.py does, minus the SDK fetch (use cache).
from scripts.quiver_validate import build_long_short_factor  # noqa: E402

CACHE_DIR = Path.home() / ".alphalens" / "quiver" / "congress"
START = date(2021, 4, 19)
END = date(2026, 4, 17)
LOOKBACK_DAYS = 30
N_QUINTILES = 5


def load_all_congress_trades() -> pd.DataFrame:
    frames = []
    for pkl in sorted(CACHE_DIR.glob("*.pkl")):
        df = pd.read_pickle(pkl)
        if not df.empty:
            frames.append(df)
    if not frames:
        raise SystemExit(f"No cached trade files in {CACHE_DIR} — run quiver_validate.py first.")
    return pd.concat(frames, ignore_index=True)


def regress(trades: pd.DataFrame, tickers, dates, ret_panel, carhart, label: str):
    panel = build_congress_feature_panel(
        trades, tickers=tickers, dates=dates, lookback_days=LOOKBACK_DAYS, feature="net_flow"
    )
    density = (panel != 0).mean().mean()
    factor = build_long_short_factor(panel, ret_panel, n_quintiles=N_QUINTILES)
    carhart_for_ls = carhart.copy()
    carhart_for_ls["RF"] = carhart_for_ls["RF"] * 0.0
    res = run_regression(
        factor, carhart_for_ls,
        factor_columns=["Mkt-RF", "SMB", "HML", "Mom"],
        spec_name=label,
    )
    return res, density, len(factor)


def main() -> None:
    print("Loading cached congressional trades…")
    all_trades = load_all_congress_trades()
    n_total = len(all_trades)
    print(f"  {n_total} total trades in cache")

    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    tickers = sorted(flatten_universe(universe).keys())

    print("Loading returns panel…")
    dates = pd.date_range(START, END, freq="B")
    hist = load_lean_histories(DATA_DIR, tickers + ["SPY"])
    store = HistoryStore(hist)
    close_panel = pd.DataFrame(index=dates, columns=tickers, dtype=float)
    for t in tickers:
        full = store.full(t)
        if not full.empty:
            close_panel[t] = full["close"].reindex(dates)
    ret_panel = close_panel.pct_change().shift(-1)

    carhart = load_carhart_daily(start=START, end=END)

    print("\n=== BASELINE (all trades) ===")
    res_base, d_base, n_base = regress(all_trades, tickers, dates, ret_panel, carhart, "baseline (all)")
    print(format_attribution_table([res_base]))
    print(f"  signal density {d_base * 100:.1f}%, {n_base} valid days")

    # Robustness: drop top-10, top-25, top-50 trades by |amount_mid|
    for top_n in [10, 25, 50]:
        sorted_trades = all_trades.reindex(all_trades["amount_mid"].abs().sort_values(ascending=False).index)
        dropped = sorted_trades.head(top_n)
        kept = sorted_trades.iloc[top_n:].reset_index(drop=True)
        print(f"\n=== DROP TOP {top_n} TRADES BY $-MAGNITUDE ===")
        print(f"  dropped $-range: ${dropped['amount_mid'].min():,.0f} … ${dropped['amount_mid'].max():,.0f}")
        print(f"  kept {len(kept)} trades (removed {top_n})")
        res, density, n_valid = regress(kept, tickers, dates, ret_panel, carhart, f"drop-top-{top_n}")
        print(format_attribution_table([res]))
        print(f"  density {density * 100:.1f}%, {n_valid} valid days")
        delta_t = res.alpha_tstat - res_base.alpha_tstat
        print(f"  Δ alpha_tstat vs baseline: {delta_t:+.2f}")

    print("\n=== INTERPRETATION GUIDE ===")
    print("  If |t| falls toward 0 as we drop trades → signal is driven by a few big outliers (artifact risk).")
    print("  If |t| stays stable or grows → signal is distributed, real but universe-specific.")


if __name__ == "__main__":
    main()

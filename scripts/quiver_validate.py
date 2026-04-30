"""Phase 0 Quiver validation: does Congress + Form 4 signal add incremental alpha?

Methodology:
  1. Fetch + normalize congress_trading + insiders for Layer 2b 113-ticker universe
     (cached in ~/.alphalens/quiver/).
  2. Build daily cross-sectional signal: congress_net_flow per (date, ticker).
  3. Construct long-short factor return series:
       each day, rank tickers by signal, go long top quintile / short bottom quintile,
       equal-weight, next-day return = factor return for that day.
  4. Regress that factor return series on Carhart-4F.  If alpha t-stat > 1.5 (HAC)
     and |beta_Mom| modest → Quiver signal is a distinct source of alpha, not a
     momentum re-expression.

Decision:
  - GO   if Carhart α t-stat > 1.5 → subscribe Hobbyist $10/mo, proceed to Phase 2
  - KILL if t-stat < 0.5 → udokumentuj, skip integration
  - WEAK in between → iterate (try insider_buy_ratio, sub-period split)

Requires QUIVER_API_KEY env var (api.quiverquant.com, 1-month free trial with code TWITTER).
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.archive.quiver_screener.client import (  # noqa: E402
    fetch_congress_for_tickers,
    fetch_insiders_for_tickers,
)
from alphalens.archive.quiver_screener.features import (  # noqa: E402
    build_congress_feature_panel,
    build_insider_feature_panel,
)
from alphalens.archive.screeners.lean.config import DATA_DIR  # noqa: E402
from alphalens.archive.screeners.lean.lean_csv_loader import load_lean_histories  # noqa: E402
from alphalens.archive.screeners.themed.config import UNIVERSE_PATH  # noqa: E402
from alphalens.archive.screeners.themed.universe import flatten_universe  # noqa: E402
from alphalens.backtest.factor_analysis import (  # noqa: E402
    format_attribution_table,
    run_regression,
)
from alphalens.data.factors import load_carhart_daily  # noqa: E402
from alphalens.data.store.history import HistoryStore  # noqa: E402

START = date(2021, 4, 19)
END = date(2026, 4, 17)
LOOKBACK_DAYS = 30
N_QUINTILES = 5
FORWARD_DAYS = 1  # factor return = next-day


def build_long_short_factor(
    signal_panel: pd.DataFrame,
    returns_panel: pd.DataFrame,
    n_quintiles: int = N_QUINTILES,
) -> pd.Series:
    """Daily top-quintile return minus bottom-quintile return by signal.

    signal_panel: (date × ticker) feature values.
    returns_panel: (date × ticker) next-period returns (already forward-shifted).
    Returns: daily factor return, one per date where both have data.
    """
    aligned_dates = signal_panel.index.intersection(returns_panel.index)
    signal = signal_panel.loc[aligned_dates]
    returns = returns_panel.loc[aligned_dates]

    out = pd.Series(index=aligned_dates, dtype=float)
    for d in aligned_dates:
        s = signal.loc[d]
        r = returns.loc[d]
        both = s.index.intersection(r.dropna().index)
        if len(both) < 10:  # too few tickers with data → skip
            continue
        s = s.loc[both]
        r = r.loc[both]
        # Skip days where signal is flat (no differentiation → undefined quintiles)
        if s.nunique() < 3:
            continue
        try:
            quintiles = pd.qcut(s, n_quintiles, labels=False, duplicates="drop")
        except ValueError:
            continue
        qmax = int(quintiles.max())
        top = r[quintiles == qmax].mean()
        bot = r[quintiles == 0].mean()
        out.at[d] = top - bot
    return out.dropna()


def main() -> None:
    api_key = os.environ.get("QUIVER_API_KEY")
    if not api_key:
        print("ERROR: QUIVER_API_KEY not set in environment (.env).")
        print(
            "Sign up at https://api.quiverquant.com with promo code TWITTER for 1-month free trial."
        )
        print("Then add QUIVER_API_KEY=<your_key> to .env and re-run.")
        sys.exit(1)

    import quiverquant

    quiver = quiverquant.quiver(api_key)

    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    tickers = sorted(flatten_universe(universe).keys())
    print(f"Universe: {len(tickers)} curated thematic tickers")

    print("Fetching Quiver congress_trading (cached in ~/.alphalens/quiver/)…")
    congress = fetch_congress_for_tickers(quiver, tickers)
    print(f"  {len(congress)} total congress trades across universe")
    if not congress.empty:
        print(f"  date range: {congress['date'].min().date()} → {congress['date'].max().date()}")

    print("\nLoading Lean CSV histories for returns…")
    hist = load_lean_histories(DATA_DIR, tickers + ["SPY"])
    store = HistoryStore(hist)

    # Build daily close-to-close returns panel
    print("Building returns panel…")
    dates = pd.date_range(START, END, freq="B")
    close_panel = pd.DataFrame(index=dates, columns=tickers, dtype=float)
    for t in tickers:
        full = store.full(t)
        if full.empty:
            continue
        close_panel[t] = full["close"].reindex(dates)
    ret_panel = close_panel.pct_change().shift(-FORWARD_DAYS)  # next-day return

    # Build signal panel
    print(
        f"Building congress_net_flow panel ({len(dates)} days × {len(tickers)} tickers, lookback={LOOKBACK_DAYS}d)…"
    )
    signal_panel = build_congress_feature_panel(
        congress, tickers=tickers, dates=dates, lookback_days=LOOKBACK_DAYS, feature="net_flow"
    )
    active_share = (signal_panel != 0).mean().mean()
    print(
        f"  signal density: {active_share * 100:.1f}% of (date, ticker) cells have non-zero signal"
    )

    # Construct long-short factor
    print("\nConstructing Quiver long-short factor (top quintile − bottom quintile)…")
    factor_ret = build_long_short_factor(signal_panel, ret_panel, n_quintiles=N_QUINTILES)
    print(f"  {len(factor_ret)} days with valid factor return")
    if len(factor_ret) < 100:
        print("WARNING: very few valid days — signal likely too sparse for meaningful regression.")

    carhart = load_carhart_daily(start=START, end=END)

    print("\nRegressing Quiver factor return on Carhart-4F (HAC)…")
    # factor_ret is already a long-short (excess) return; don't subtract RF.
    res = run_regression(
        factor_ret,
        carhart,
        factor_columns=["Mkt-RF", "SMB", "HML", "Mom"],
        spec_name="Quiver L/S ~ Carhart-4F",
        subtract_rf=False,
    )
    print(format_attribution_table([res]))
    print(
        f"\n  beta[Mom] = {res.betas['Mom']:+.3f}  (|β|>0.3 → signal loads heavily on momentum → redundant)"
    )

    congress_t = res.alpha_tstat
    congress_ann = res.alpha_annualized * 100

    # ------------------------------------------------------------------
    # Insider (Form 4) signal pass — separate feature, possibly different verdict
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("INSIDER (Form 4) signal pass — net $ flow")
    print("=" * 70)
    print("Fetching insiders (cached in ~/.alphalens/quiver/insiders/)…")
    insiders = fetch_insiders_for_tickers(quiver, tickers)
    print(f"  {len(insiders)} total insider trades across universe")
    if not insiders.empty:
        print(f"  date range: {insiders['date'].min().date()} → {insiders['date'].max().date()}")

    INS_LOOKBACK = 60  # insider signal typically uses longer window
    print(f"\nBuilding insider_net_flow panel (lookback={INS_LOOKBACK}d)…")
    ins_panel = build_insider_feature_panel(
        insiders, tickers=tickers, dates=dates, lookback_days=INS_LOOKBACK, feature="net_flow"
    )
    ins_density = (ins_panel != 0).mean().mean()
    print(f"  signal density: {ins_density * 100:.1f}% of (date, ticker) cells non-zero")

    print("\nConstructing insider long-short factor…")
    ins_factor = build_long_short_factor(ins_panel, ret_panel, n_quintiles=N_QUINTILES)
    print(f"  {len(ins_factor)} days with valid factor return")

    ins_res = None
    if len(ins_factor) < 20:
        print(
            "\n  SKIPPED: insider regression needs >=20 days with valid factor return.\n"
            "  Most likely cause: /beta/live/insiders is gated behind Quiver Trader tier\n"
            "  ($75/mo) — free trial / Hobbyist returns 403 for every ticker."
        )
    else:
        print("\nRegressing insider factor on Carhart-4F (HAC)…")
        ins_res = run_regression(
            ins_factor,
            carhart,
            factor_columns=["Mkt-RF", "SMB", "HML", "Mom"],
            spec_name="Insider L/S ~ Carhart-4F",
            subtract_rf=False,
        )
        print(format_attribution_table([ins_res]))
        print(f"\n  beta[Mom] = {ins_res.betas['Mom']:+.3f}")

    # ------------------------------------------------------------------
    # Final decision across both signals
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("FINAL DECISION")
    print("=" * 70)
    print(
        f"  Congress L/S:  α ann = {congress_ann:+.2f}%, t = {congress_t:+.2f} HAC, density {(signal_panel != 0).mean().mean() * 100:.1f}%"
    )
    if ins_res is None:
        print("  Insider  L/S:  INSUFFICIENT_DATA (endpoint likely paywalled)")
    else:
        print(
            f"  Insider  L/S:  α ann = {ins_res.alpha_annualized * 100:+.2f}%, t = {ins_res.alpha_tstat:+.2f} HAC, density {ins_density * 100:.1f}%"
        )

    def verdict(t: float) -> str:
        if t > 1.5:
            return "GO"
        if t > 0.5:
            return "WEAK"
        if t > -0.5:
            return "NO-SIGNAL"
        return "REVERSE-SIGNAL-OR-NOISE"

    print(f"\n  Congress verdict: {verdict(congress_t)}")
    print(
        f"  Insider  verdict: {'INSUFFICIENT_DATA' if ins_res is None else verdict(ins_res.alpha_tstat)}"
    )

    best_t = max([congress_t] + ([ins_res.alpha_tstat] if ins_res else []))
    if best_t > 1.5:
        print(
            "\n  → GO on whichever passed. Subscribe Hobbyist $10/mo, proceed to Phase 2 with that feature."
        )
    else:
        print(
            "\n  → KILL on both. No Quiver subscription. Document in project_quiver_validation.md."
        )


if __name__ == "__main__":
    main()

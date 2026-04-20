"""Compare Layer 2b backtest results: flat cost model vs. per-ticker.

Runs the identical backtest twice (same engine config, same scorer) and
emits side-by-side Sharpe / annual return / max DD, plus per-ticker and
per-theme cost breakdowns from the per-ticker model.

Decision gate: |Δ Sharpe| > 0.3 flags investigation.

Usage:
    .venv/bin/python scripts/regression_vs_flat_model.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.cost_applier import CostApplier  # noqa: E402
from alphalens.backtest.cost_model import (  # noqa: E402
    PerTickerCostModel,
    cost_sensitivity_table,
)
from alphalens.backtest.engine import BacktestEngine  # noqa: E402
from alphalens.backtest.history_store import HistoryStore  # noqa: E402
from alphalens.backtest.market_chars_store import MarketCharacteristicsStore  # noqa: E402
from alphalens.backtest.metrics import sharpe  # noqa: E402
from alphalens.screeners.lean.config import DATA_DIR  # noqa: E402
from alphalens.screeners.lean.lean_csv_loader import load_lean_histories  # noqa: E402
from alphalens.screeners.themed.backtest_adapter import momentum_scorer_adapter  # noqa: E402
from alphalens.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH  # noqa: E402
from alphalens.screeners.themed.universe import flatten_universe  # noqa: E402

import yaml  # noqa: E402


DOC_OUT = Path(__file__).resolve().parent.parent / "docs" / "backtest" / "per_ticker_vs_flat.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2021-04-19")
    parser.add_argument("--end", default="2026-04-17")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--holding", type=int, default=5)
    parser.add_argument("--weighting", default="linear")
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--portfolio-value", type=float, default=100_000.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    ticker_themes = flatten_universe(universe)
    screener_tickers = sorted(ticker_themes.keys())

    print(f"Loading OHLCV for {len(screener_tickers)+1} tickers…")
    histories = load_lean_histories(DATA_DIR, screener_tickers + [args.benchmark])
    store = HistoryStore(histories)

    engine = BacktestEngine(
        store,
        scorer=momentum_scorer_adapter,
        scorer_config=dict(THEMED_DEFAULTS, benchmark=args.benchmark),
        holding_period=args.holding,
        top_n=args.top_n,
        benchmark=args.benchmark,
        screener_tickers=screener_tickers,
        weighting=args.weighting,
        portfolio_value=args.portfolio_value,
    )

    print("Running backtest…")
    result = engine.run(start=start_date, end=end_date)
    print(f"  {len(result.daily_results)} daily snapshots")

    port_returns = result.portfolio_returns.tolist()
    cost_df = cost_sensitivity_table(port_returns)
    gross_sharpe = float(cost_df.loc[cost_df["profile"] == "gross", "sharpe"].iloc[0])
    moderate_sharpe = float(cost_df.loc[cost_df["profile"] == "moderate", "sharpe"].iloc[0])
    annual_return_moderate = float(
        cost_df.loc[cost_df["profile"] == "moderate", "annual_return"].iloc[0]
    )

    print("Priming MarketCharacteristicsStore…")
    chars = MarketCharacteristicsStore(store)
    chars.prime(screener_tickers, start=start_date, end=end_date)

    ticker_to_theme = {t: themes[0] for t, themes in ticker_themes.items() if themes}
    applier = CostApplier(
        market_chars=chars,
        cost_model=PerTickerCostModel(),
        theme_map=ticker_to_theme,
    )
    per_ticker = applier.apply(result)
    per_ticker_sharpe = sharpe(per_ticker.net_returns.tolist())

    pv = args.portfolio_value
    per_ticker_drag_bps = per_ticker.total_cost_bps_annualized

    lines = []
    lines.append("# Layer 2b: flat 100 bps vs. per-ticker cost model")
    lines.append("")
    lines.append(f"- Window: {start_date} → {end_date}")
    lines.append(f"- Portfolio value: ${pv:,.0f}")
    lines.append(f"- Top-N: {args.top_n}, holding: {args.holding}, weighting: {args.weighting}")
    lines.append("")

    lines.append("## Side-by-side")
    lines.append("")
    lines.append("| Metric | Gross | Flat 100 bps | Per-ticker |")
    lines.append("| --- | ---: | ---: | ---: |")
    lines.append(f"| Sharpe | {gross_sharpe:+.3f} | {moderate_sharpe:+.3f} | {per_ticker_sharpe:+.3f} |")
    lines.append(
        f"| Annual drag (bps) | 0 | 100 | {per_ticker_drag_bps:.1f} |"
    )
    lines.append("")

    delta_sharpe = per_ticker_sharpe - moderate_sharpe
    lines.append("## Decision gate")
    lines.append("")
    lines.append(f"- Δ Sharpe (per-ticker vs. flat 100 bps): **{delta_sharpe:+.3f}**")
    if abs(delta_sharpe) > 0.3:
        lines.append(
            f"- **INVESTIGATE** (|Δ| > 0.3): per-ticker model materially changes "
            f"the decision. Inspect top-10 costliest tickers + per-theme breakdown."
        )
    else:
        lines.append(
            f"- **OK** (|Δ| ≤ 0.3): models broadly agree at this portfolio size."
        )
    lines.append("")

    top10 = per_ticker.per_ticker_breakdown.head(10)
    if not top10.empty:
        lines.append("## Top-10 kosztowne tickery (per-ticker model)")
        lines.append("")
        lines.append("| Ticker | Enters | Exits | Total cost (USD) | Bps of NAV |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for _, row in top10.iterrows():
            lines.append(
                f"| {row['ticker']} | {int(row['enter_count'])} | "
                f"{int(row['exit_count'])} | ${row['total_cost_usd']:,.2f} | "
                f"{row['total_cost_bps_of_nav']:.2f} |"
            )
        lines.append("")
        total = per_ticker.per_ticker_breakdown["total_cost_usd"].sum()
        top5 = top10.head(5)["total_cost_usd"].sum()
        if total > 0:
            lines.append(f"**Koncentracja**: top-5 = {top5 / total * 100:.1f}% całego kosztu.")
            lines.append("")

    if not per_ticker.per_theme_breakdown.empty:
        lines.append("## Decompozycja per-theme")
        lines.append("")
        lines.append("| Temat | Cost (USD) | % całości |")
        lines.append("| --- | ---: | ---: |")
        for _, row in per_ticker.per_theme_breakdown.iterrows():
            lines.append(
                f"| {row['theme']} | ${row['total_cost_usd']:,.2f} | "
                f"{row['pct_of_total'] * 100:.1f}% |"
            )
        lines.append("")

    DOC_OUT.parent.mkdir(parents=True, exist_ok=True)
    DOC_OUT.write_text("\n".join(lines))
    print(f"Report written to {DOC_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

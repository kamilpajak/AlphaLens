"""Run Layer 2b backtest on baseline (113) vs augmented (113 + 50 delisted thematics).

Compares Sharpe, alpha, IC with and without the delisted-thematic survivorship-bias
correction. Writes docs/backtest/layer2b_survivorship.md and CSV.

The augmented universe drops any delisted ticker the scorer could not plausibly
have picked at a given date (the HistoryStore truncates to t; names with zero
future bars from date t naturally score NaN and are excluded).

Usage: .venv/bin/python scripts/augmented_backtest.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.archive.screeners.lean.lean_csv_loader import load_lean_histories
from alphalens.archive.screeners.themed.backtest_adapter import momentum_scorer_adapter
from alphalens.archive.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
from alphalens.archive.screeners.themed.universe import flatten_universe
from alphalens.attribution.factor_analysis import run_carhart_attribution
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.metrics import (
    rank_ic_tstat,
    sharpe,
)
from alphalens.data.factors import load_carhart_daily
from alphalens.data.store.history import HistoryStore

MAIN_DATA = Path.home() / ".alphalens" / "lean" / "data"
SURV_DATA = Path.home() / ".alphalens" / "survivorship" / "lean_data"
SURV_DIR = SURV_DATA.parent


def build_augmented_store() -> tuple[HistoryStore, list[str], list[str]]:
    """Return HistoryStore populated from main cache + survivorship cache,
    plus (curated_tickers, delisted_tickers)."""
    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    themes_map = flatten_universe(universe)
    curated = sorted(themes_map.keys())
    print(f"curated universe: {len(curated)} names")

    main_histories = load_lean_histories(MAIN_DATA, [*curated, "SPY", "IWM"])

    manifest = json.loads((SURV_DIR / "fetched_manifest.json").read_text())
    delisted_tickers = [e["ticker"] for e in manifest["fetched"]]
    print(f"delisted candidates: {len(delisted_tickers)} names")

    surv_histories = load_lean_histories(SURV_DATA, delisted_tickers)
    for t in delisted_tickers:
        if t.upper() not in surv_histories:
            print(f"  ! {t} not loaded from survivorship cache, skip")

    merged = {**main_histories, **surv_histories}
    store = HistoryStore(merged)
    return store, curated, delisted_tickers


def run(store: HistoryStore, screener_tickers: list[str], label: str) -> tuple:
    """Run BacktestEngine with momentum scorer on given universe."""
    cfg = dict(THEMED_DEFAULTS)
    cfg["benchmark"] = "SPY"
    engine = BacktestEngine(
        store,
        scorer=momentum_scorer_adapter,
        scorer_config=cfg,
        holding_period=5,
        top_n=5,
        benchmark="SPY",
        screener_tickers=screener_tickers,
        weighting="linear",
    )
    engine.MIN_BARS_REQUIRED = 252

    print(f"\n[{label}] running backtest over {len(screener_tickers)} tickers …")
    start = date(2021, 6, 1)
    end = date(2026, 4, 17)
    report = engine.run(start=start, end=end)
    print(f"[{label}] {len(report.rebalance_results)} daily snapshots")

    returns = report.portfolio_returns
    ic = report.ic_series

    metrics = {
        "label": label,
        "universe_size": len(screener_tickers),
        "daily_snapshots": len(report.rebalance_results),
        "sharpe_gross": sharpe(returns.tolist()),
        "ic_mean": float(ic.mean()) if len(ic) else float("nan"),
        "ic_tstat": rank_ic_tstat(ic.tolist()),
        "annual_return_gross_pct": float(returns.mean() * 252 * 100),
        "max_drawdown_pct": _max_dd(returns) * 100,
    }

    # Factor attribution — Carhart-4F row is the one to trust (controls for UMD).
    try:
        carhart_factors = load_carhart_daily(start=start, end=end)
        attrib = run_carhart_attribution(returns, carhart_factors)
        by_spec = {r.spec_name: r for r in attrib}
        for spec in ("CAPM", "FF3", "Carhart-4F"):
            r = by_spec[spec]
            key = spec.lower().replace("-", "_")
            metrics[f"{key}_alpha_bps_day"] = float(r.alpha_daily * 10000)
            metrics[f"{key}_alpha_ann_pct"] = float(r.alpha_annualized * 100)
            metrics[f"{key}_alpha_tstat"] = float(r.alpha_tstat)
            metrics[f"{key}_r2"] = float(r.r_squared)
    except (FileNotFoundError, ValueError) as e:
        print(f"  Factor attribution skipped: {e}")
        metrics["carhart_4f_alpha_tstat"] = None

    # Count appearances of delisted names in top-5 picks
    counter: dict[str, int] = {}
    for d in report.rebalance_results:
        for t in d.top_n_tickers:
            counter[t] = counter.get(t, 0) + 1
    return report, metrics, counter


def _max_dd(returns: pd.Series) -> float:
    cum = (1 + returns).cumprod()
    peak = cum.cummax()
    dd = (cum - peak) / peak
    return float(dd.min())


def main() -> None:
    store, curated, delisted = build_augmented_store()

    _baseline_report, baseline_metrics, _baseline_picks = run(store, curated, "baseline")
    _augmented_report, augmented_metrics, augmented_picks = run(
        store, curated + delisted, "augmented"
    )

    # Difference in picks — how often a delisted name made it into top-5
    delisted_appearances = {
        t: augmented_picks.get(t, 0) for t in delisted if augmented_picks.get(t, 0) > 0
    }
    delisted_sorted = sorted(delisted_appearances.items(), key=lambda x: -x[1])

    print("\n=== Metrics comparison ===")
    print(f"{'metric':<25} {'baseline':>15} {'augmented':>15} {'delta':>15}")
    for k in [
        "universe_size",
        "daily_snapshots",
        "sharpe_gross",
        "ic_mean",
        "ic_tstat",
        "annual_return_gross_pct",
        "max_drawdown_pct",
        "ff3_alpha_ann_pct",
        "ff3_alpha_tstat",
        "ff3_r2",
        "carhart_4f_alpha_ann_pct",
        "carhart_4f_alpha_tstat",
        "carhart_4f_r2",
    ]:
        b = baseline_metrics.get(k)
        a = augmented_metrics.get(k)
        if b is None or a is None:
            print(f"{k:<25} {str(b)[:15]:>15} {str(a)[:15]:>15} {'-':>15}")
            continue
        try:
            delta = a - b
            print(f"{k:<25} {b:>15.4f} {a:>15.4f} {delta:>+15.4f}")
        except TypeError:
            print(f"{k:<25} {str(b)[:15]:>15} {str(a)[:15]:>15} -")

    print("\n=== Delisted names that entered top-5 (augmented run) ===")
    print(f"  {len(delisted_sorted)} of {len(delisted)} delisted names appeared in top-5")
    for t, n in delisted_sorted[:30]:
        print(f"    {t:8s} appeared in top-5 on {n} days")

    # Write report
    out_md = (
        Path(__file__).resolve().parent.parent / "docs" / "backtest" / "layer2b_survivorship.md"
    )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Layer 2b survivorship-bias probe",
        "",
        "Test B from the Perplexity-recommended falsification battery. Augments the",
        "curated 113-name universe with **delisted thematic small/mid caps** (biotechs",
        "acquired or liquidated, semi photonics, consumer robotics) identified via",
        "Polygon `active=false` sweep over 2021-06-01 → 2026-04-17.",
        "",
        "- **Window**: 2021-06-01 → 2026-04-17 (Polygon plan boundary; original backtest started 2021-04-19)",
        f"- **Delisted candidates fetched**: {len(delisted)} (from 4265 disappeared tickers, 969 liquidity-filtered, 72 thematic, 50 fetchable with ≥60 bars)",
        "",
        "## Metrics comparison",
        "",
        "| Metric | Baseline (curated 113) | Augmented (+delisted thematic) | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    for k in [
        "universe_size",
        "daily_snapshots",
        "sharpe_gross",
        "ic_mean",
        "ic_tstat",
        "annual_return_gross_pct",
        "max_drawdown_pct",
        "ff3_alpha_ann_pct",
        "ff3_alpha_tstat",
        "ff3_r2",
        "carhart_4f_alpha_ann_pct",
        "carhart_4f_alpha_tstat",
        "carhart_4f_r2",
    ]:
        b = baseline_metrics.get(k)
        a = augmented_metrics.get(k)
        if b is None or a is None:
            lines.append(f"| {k} | {b} | {a} | - |")
            continue
        try:
            delta = a - b
            lines.append(f"| {k} | {b:.4f} | {a:.4f} | {delta:+.4f} |")
        except TypeError:
            lines.append(f"| {k} | {b} | {a} | - |")

    lines.extend(
        [
            "",
            "## Delisted names that entered top-5 in augmented run",
            "",
            f"- {len(delisted_sorted)} of {len(delisted)} delisted names ever scored into top-5",
            "",
            "| Ticker | Days in top-5 |",
            "| --- | ---: |",
        ]
    )
    for t, n in delisted_sorted:
        lines.append(f"| {t} | {n} |")

    out_md.write_text("\n".join(lines))
    print(f"\nWrote {out_md}")

    # Save CSV summary
    out_csv = SURV_DIR / "baseline_vs_augmented.csv"
    pd.DataFrame([baseline_metrics, augmented_metrics]).to_csv(out_csv, index=False)
    print(f"Wrote {out_csv}")


if __name__ == "__main__":
    main()

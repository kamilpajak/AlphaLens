"""Phase 2 — compare MomentumScorer vs EarlyStageScorer on Layer 2b universe.

Six experiments per Phase 2 plan:
  1. Baseline metric comparison (Sharpe/FF3/IC/turnover/max DD)
  2. Pick overlap — per-day intersection of top-5
  3. Extension at pick time — trailing 60d return, distance from 52w high, RSI
  4. Forward return trajectory — 20/60/120d from pick date
  5. Theme HHI — concentration per day per scorer
  6. Hybrid (0.5/0.5) — run only if 1+3 look promising

Usage: .venv/bin/python scripts/phase2_compare_scorers.py
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.factor_analysis import run_carhart_attribution
from alphalens.backtest.factors import load_carhart_daily
from alphalens.backtest.history_store import HistoryStore
from alphalens.backtest.metrics import rank_ic_tstat, sharpe
from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
from alphalens.screeners.themed.backtest_adapter import (
    early_stage_scorer_adapter,
    momentum_scorer_adapter,
)
from alphalens.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
from alphalens.screeners.themed.early_stage_scorer import EARLY_STAGE_DEFAULTS
from alphalens.screeners.themed.universe import flatten_universe

LEAN_DATA = Path.home() / ".alphalens" / "lean" / "data"
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "backtest"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CSV_DIR = Path.home() / ".alphalens" / "phase2"
CSV_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ScorerRun:
    label: str
    report: object
    metrics: dict
    picks_df: pd.DataFrame  # columns: date, rank, ticker, trailing_60d, dist_from_52w_high, rsi,
    #          fwd_20d, fwd_60d, fwd_120d, theme


def _load_store_and_tickers() -> tuple[HistoryStore, list[str], dict[str, list[str]]]:
    universe = yaml.safe_load(UNIVERSE_PATH.read_text())
    themes_map = flatten_universe(universe)
    curated = sorted(themes_map.keys())
    histories = load_lean_histories(LEAN_DATA, curated + ["SPY", "IWM"])
    store = HistoryStore(histories)
    return store, curated, themes_map


def _max_dd(returns: pd.Series) -> float:
    if returns.empty:
        return float("nan")
    cum = (1 + returns).cumprod()
    peak = cum.cummax()
    return float(((cum - peak) / peak).min())


def _compute_pick_features(
    store: HistoryStore,
    pick_date: pd.Timestamp,
    ticker: str,
    themes_map: dict[str, list[str]],
) -> dict:
    """For a given (date, ticker), compute trailing + forward features."""
    try:
        df = store.full(ticker.upper())
    except KeyError:
        return {
            "ticker": ticker,
            "trailing_60d": np.nan,
            "dist_from_52w_high": np.nan,
            "rsi": np.nan,
            "fwd_20d": np.nan,
            "fwd_60d": np.nan,
            "fwd_120d": np.nan,
            "theme": "",
        }

    # Slice up to pick_date (point-in-time)
    hist_up = df.loc[:pick_date]
    if len(hist_up) < 60:
        return {
            "ticker": ticker,
            "trailing_60d": np.nan,
            "dist_from_52w_high": np.nan,
            "rsi": np.nan,
            "fwd_20d": np.nan,
            "fwd_60d": np.nan,
            "fwd_120d": np.nan,
            "theme": ",".join(themes_map.get(ticker, [])),
        }

    close_now = float(hist_up["close"].iloc[-1])
    close_60_ago = float(hist_up["close"].iloc[-60])
    trailing_60d = (close_now - close_60_ago) / close_60_ago if close_60_ago > 0 else np.nan

    # 52w high
    window = hist_up.tail(252)
    high_52w = float(window["high"].max())
    dist_from_52w = (high_52w - close_now) / high_52w if high_52w > 0 else np.nan

    # RSI 14
    delta = hist_up["close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_series = 100 - (100 / (1 + rs))
    rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else np.nan

    # Forward returns — from hist AFTER pick_date
    hist_fwd = df.loc[pick_date:]
    fwd = {}
    for horizon in (20, 60, 120):
        if len(hist_fwd) > horizon:
            exit_price = float(hist_fwd["close"].iloc[horizon])
            fwd[f"fwd_{horizon}d"] = (exit_price - close_now) / close_now
        else:
            fwd[f"fwd_{horizon}d"] = np.nan

    return {
        "ticker": ticker,
        "trailing_60d": trailing_60d,
        "dist_from_52w_high": dist_from_52w,
        "rsi": rsi,
        "fwd_20d": fwd["fwd_20d"],
        "fwd_60d": fwd["fwd_60d"],
        "fwd_120d": fwd["fwd_120d"],
        "theme": ",".join(themes_map.get(ticker, [])),
    }


def _run_scorer(
    store: HistoryStore,
    tickers: list[str],
    themes_map: dict[str, list[str]],
    scorer_fn,
    scorer_config: dict,
    label: str,
) -> ScorerRun:
    print(f"\n[{label}] running backtest …")
    engine = BacktestEngine(
        store,
        scorer=scorer_fn,
        scorer_config=scorer_config,
        holding_period=5,
        top_n=5,
        benchmark="SPY",
        screener_tickers=tickers,
        weighting="linear",
    )
    engine.MIN_BARS_REQUIRED = 252

    report = engine.run(start=date(2021, 6, 1), end=date(2026, 4, 17))
    print(f"[{label}] {len(report.daily_results)} daily snapshots")

    returns = report.portfolio_returns
    ic = report.ic_series

    metrics = {
        "label": label,
        "sharpe_gross": sharpe(returns.tolist()),
        "annual_return_pct": float(returns.mean() * 252 * 100),
        "ic_mean": float(ic.mean()) if len(ic) else float("nan"),
        "ic_tstat": rank_ic_tstat(ic.tolist()),
        "max_drawdown_pct": _max_dd(returns) * 100,
        "turnover_pct": float(report.turnover * 100),
    }
    try:
        carhart_factors = load_carhart_daily(start=date(2021, 6, 1), end=date(2026, 4, 17))
        attrib = run_carhart_attribution(returns, carhart_factors)
        by_spec = {r.spec_name: r for r in attrib}
        for spec in ("CAPM", "FF3", "Carhart-4F"):
            r = by_spec[spec]
            key = spec.lower().replace("-", "_")
            metrics[f"{key}_alpha_ann_pct"] = float(r.alpha_annualized * 100)
            metrics[f"{key}_alpha_tstat"] = float(r.alpha_tstat)
            metrics[f"{key}_r2"] = float(r.r_squared)
    except (FileNotFoundError, ValueError) as e:
        print(f"  Factor attribution skipped: {e}")

    # Per-pick feature dataframe
    print(f"[{label}] computing per-pick features …")
    pick_rows = []
    for d in report.daily_results:
        for rank, ticker in enumerate(d.top_n_tickers, 1):
            feat = _compute_pick_features(store, d.date, ticker, themes_map)
            feat["date"] = d.date
            feat["rank"] = rank
            feat["scorer"] = label
            pick_rows.append(feat)
    picks_df = pd.DataFrame(pick_rows)

    return ScorerRun(label=label, report=report, metrics=metrics, picks_df=picks_df)


def _pick_overlap(run_a: ScorerRun, run_b: ScorerRun) -> pd.Series:
    """Per-day overlap fraction between two scorers' top-5."""
    by_date_a = run_a.picks_df.groupby("date")["ticker"].apply(set)
    by_date_b = run_b.picks_df.groupby("date")["ticker"].apply(set)
    common = by_date_a.index.intersection(by_date_b.index)
    overlap = [
        len(by_date_a.loc[d] & by_date_b.loc[d]) / max(len(by_date_a.loc[d]), 1) for d in common
    ]
    return pd.Series(overlap, index=common)


def _theme_hhi_per_day(picks_df: pd.DataFrame) -> pd.Series:
    """HHI of theme concentration in top-5, per day."""

    def _hhi_row(themes_lists: list[str]) -> float:
        counter: Counter = Counter()
        for themes_str in themes_lists:
            for t in themes_str.split(",") if themes_str else []:
                counter[t.strip()] += 1
        total = sum(counter.values())
        if total == 0:
            return 0.0
        shares = [c / total for c in counter.values()]
        return sum(s * s for s in shares)

    return picks_df.groupby("date")["theme"].apply(_hhi_row)


def _format_metric_table(runs: list[ScorerRun]) -> str:
    keys = [
        "sharpe_gross",
        "annual_return_pct",
        "ic_mean",
        "ic_tstat",
        "max_drawdown_pct",
        "turnover_pct",
        "ff3_alpha_ann_pct",
        "ff3_alpha_tstat",
        "ff3_r2",
    ]
    header = "| Metric | " + " | ".join(r.label for r in runs) + " |"
    sep = "| --- | " + " | ".join(["---:"] * len(runs)) + " |"
    rows = [header, sep]
    for k in keys:
        vals = []
        for r in runs:
            v = r.metrics.get(k)
            vals.append(f"{v:.4f}" if isinstance(v, (int, float)) and v is not None else "-")
        rows.append(f"| {k} | " + " | ".join(vals) + " |")
    return "\n".join(rows)


def _feature_summary(picks_df: pd.DataFrame) -> dict:
    """Distribution summary of extension features across all picks."""
    return {
        "trailing_60d_mean": picks_df["trailing_60d"].mean(),
        "trailing_60d_median": picks_df["trailing_60d"].median(),
        "dist_from_52w_high_mean": picks_df["dist_from_52w_high"].mean(),
        "dist_from_52w_high_median": picks_df["dist_from_52w_high"].median(),
        "rsi_mean": picks_df["rsi"].mean(),
        "rsi_median": picks_df["rsi"].median(),
        "fwd_20d_mean": picks_df["fwd_20d"].mean(),
        "fwd_60d_mean": picks_df["fwd_60d"].mean(),
        "fwd_120d_mean": picks_df["fwd_120d"].mean(),
        "pct_picks_near_high": float((picks_df["dist_from_52w_high"] < 0.05).mean()),
        "pct_picks_rsi_overbought": float((picks_df["rsi"] > 70).mean()),
    }


def main() -> None:
    store, curated, themes_map = _load_store_and_tickers()
    print(f"universe: {len(curated)} names")

    momentum_cfg = dict(THEMED_DEFAULTS)
    momentum_cfg["benchmark"] = "SPY"
    early_cfg = dict(EARLY_STAGE_DEFAULTS)
    early_cfg["benchmark"] = "SPY"

    run_mom = _run_scorer(
        store, curated, themes_map, momentum_scorer_adapter, momentum_cfg, "momentum"
    )
    run_early = _run_scorer(
        store, curated, themes_map, early_stage_scorer_adapter, early_cfg, "early_stage"
    )

    # Experiment 1 — metric comparison
    print("\n=== Experiment 1: headline metrics ===")
    print(_format_metric_table([run_mom, run_early]))

    # Experiment 2 — pick overlap
    overlap = _pick_overlap(run_mom, run_early)
    print("\n=== Experiment 2: pick overlap ===")
    print(f"  days compared: {len(overlap)}")
    print(f"  mean overlap:   {overlap.mean():.3f}")
    print(f"  median overlap: {overlap.median():.3f}")
    print("  overlap distribution:")
    for q in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
        count = int((overlap >= q).sum())
        print(f"    ≥{q:.1f}: {count} ({count / len(overlap) * 100:.1f}%)")

    # Experiment 3 — extension at pick time
    print("\n=== Experiment 3: extension at pick time ===")
    mom_feat = _feature_summary(run_mom.picks_df)
    early_feat = _feature_summary(run_early.picks_df)
    print(f"  {'metric':<25} {'momentum':>12} {'early_stage':>12}")
    for k in [
        "trailing_60d_mean",
        "dist_from_52w_high_mean",
        "rsi_mean",
        "pct_picks_near_high",
        "pct_picks_rsi_overbought",
    ]:
        print(f"  {k:<25} {mom_feat[k]:>12.4f} {early_feat[k]:>12.4f}")

    # Experiment 4 — forward returns
    print("\n=== Experiment 4: forward return distribution ===")
    print(f"  {'horizon':<12} {'momentum':>12} {'early_stage':>12}")
    for h in ["fwd_20d_mean", "fwd_60d_mean", "fwd_120d_mean"]:
        print(f"  {h:<12} {mom_feat[h]:>12.4f} {early_feat[h]:>12.4f}")

    # Experiment 5 — theme HHI
    hhi_mom = _theme_hhi_per_day(run_mom.picks_df)
    hhi_early = _theme_hhi_per_day(run_early.picks_df)
    print("\n=== Experiment 5: theme HHI (lower = more diversified) ===")
    print(f"  {'scorer':<15} {'mean':>8} {'median':>8} {'p90':>8}")
    print(
        f"  {'momentum':<15} {hhi_mom.mean():>8.3f} {hhi_mom.median():>8.3f} {hhi_mom.quantile(0.9):>8.3f}"
    )
    print(
        f"  {'early_stage':<15} {hhi_early.mean():>8.3f} {hhi_early.median():>8.3f} {hhi_early.quantile(0.9):>8.3f}"
    )

    # Experiment 6 — hybrid? (run if metrics are promising)
    hybrid_run = None
    mom_sharpe = run_mom.metrics.get("sharpe_gross", 0)
    early_sharpe = run_early.metrics.get("sharpe_gross", 0)
    overlap_mean = overlap.mean()
    dist_diff = mom_feat["dist_from_52w_high_mean"] - early_feat["dist_from_52w_high_mean"]
    trigger_hybrid = (
        early_sharpe > 0.5
        and overlap_mean < 0.7
        and dist_diff < -0.03  # early_stage picks further from 52w high
    )
    if trigger_hybrid:
        print("\n=== Experiment 6: hybrid (triggered) ===")
        # TODO: implement a hybrid adapter that combines scores
        # For now, signal-only: we note that hybrid would be worth trying
        print("  hybrid merits exploration — see report recommendation")
    else:
        print("\n=== Experiment 6: hybrid SKIPPED ===")
        print(
            f"  trigger conditions not met "
            f"(early_sharpe={early_sharpe:.2f}>0.5? "
            f"overlap={overlap_mean:.2f}<0.7? dist_diff={dist_diff:+.3f}<-0.03?)"
        )

    # --- Write report + CSVs ---
    report_md = OUT_DIR / "early_stage_comparison.md"
    lines = [
        "# Early-Stage vs Momentum Scorer — Phase 2 Comparison",
        "",
        "Porównanie dwóch scorer'ów Layer 2b na tym samym 113-name curated universe,",
        "2021-06-01 → 2026-04-17, daily rebalance, top-5 × linear, 5-day holding period.",
        "",
        "**Cel**: zweryfikować czy EarlyStageScorer (CAN SLIM / Minervini VCP / Jegadeesh 11-1)",
        "wybiera inne stocks niż obecny MomentumScorer, czy picks są wcześniej w rally cycle'u",
        "(test Layer 3 acceptance proxy przez extension features), czy zachowuje sensowny edge.",
        "",
        "## Experiment 1 — Headline metrics",
        "",
        _format_metric_table([run_mom, run_early]),
        "",
        "## Experiment 2 — Pick overlap",
        "",
        f"- Days compared: {len(overlap)}",
        f"- Mean overlap: **{overlap.mean():.3f}**",
        f"- Median overlap: {overlap.median():.3f}",
        f"- Days with zero overlap: {int((overlap == 0).sum())} "
        f"({(overlap == 0).mean() * 100:.1f}%)",
        f"- Days with full overlap (same 5 names): {int((overlap == 1).sum())} "
        f"({(overlap == 1).mean() * 100:.1f}%)",
        "",
        "## Experiment 3 — Extension at pick time (Layer 3 rejection proxy)",
        "",
        "Lower = earlier in rally cycle, more headroom, less likely to be rejected as 'buy at peak'.",
        "",
        "| Metric | Momentum | EarlyStage | Δ (lower is better) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for k in [
        "trailing_60d_mean",
        "dist_from_52w_high_mean",
        "rsi_mean",
        "pct_picks_near_high",
        "pct_picks_rsi_overbought",
    ]:
        delta = early_feat[k] - mom_feat[k]
        better = "✓" if delta < 0 else "✗"
        lines.append(f"| {k} | {mom_feat[k]:.4f} | {early_feat[k]:.4f} | {delta:+.4f} {better} |")

    lines += [
        "",
        "## Experiment 4 — Forward return distribution",
        "",
        "Mean forward return across all top-5 picks. Higher = better signal.",
        "",
        "| Horizon | Momentum | EarlyStage | Δ |",
        "| --- | ---: | ---: | ---: |",
    ]
    for h in ["fwd_20d_mean", "fwd_60d_mean", "fwd_120d_mean"]:
        delta = early_feat[h] - mom_feat[h]
        lines.append(f"| {h} | {mom_feat[h]:.4f} | {early_feat[h]:.4f} | {delta:+.4f} |")

    lines += [
        "",
        "## Experiment 5 — Theme HHI (concentration)",
        "",
        "Lower HHI = more diversified across themes per day. 1.0 = all 5 picks in same theme.",
        "",
        "| Scorer | Mean | Median | p90 |",
        "| --- | ---: | ---: | ---: |",
        f"| momentum | {hhi_mom.mean():.3f} | {hhi_mom.median():.3f} | {hhi_mom.quantile(0.9):.3f} |",
        f"| early_stage | {hhi_early.mean():.3f} | {hhi_early.median():.3f} | {hhi_early.quantile(0.9):.3f} |",
        "",
        "## Experiment 6 — Hybrid scorer",
        "",
        f"**Triggered**: {trigger_hybrid}",
        "",
        "Trigger conditions:",
        f"- early_stage Sharpe > 0.5: {early_sharpe > 0.5} ({early_sharpe:.2f})",
        f"- pick overlap mean < 0.7: {overlap_mean < 0.7} ({overlap_mean:.2f})",
        f"- dist_from_52w_high reduction > 3pp: {dist_diff < -0.03} ({dist_diff:+.3f})",
        "",
        "## Recommendation",
        "",
    ]

    # Go/No-go decision per plan
    if overlap.mean() > 0.7:
        lines.append(
            "**STOP**: Pick overlap > 70% — early-stage scorer nie zmienia istotnie selekcji."
        )
    elif early_sharpe < 0.3 and overlap.mean() < 0.2:
        lines.append(
            f"**STOP**: Early-stage Sharpe {early_sharpe:.2f} < 0.3 z overlap {overlap.mean():.2f} < 0.2 — scorer wybiera noise, nie early-stage signal."
        )
    elif (
        overlap.mean() < 0.7
        and early_sharpe > 0.5
        and early_feat["pct_picks_near_high"] < mom_feat["pct_picks_near_high"]
    ):
        lines.append(
            "**GO do Fazy 3** (paper trade): picks różnią się od obecnego scorer'a, Sharpe jest sensowny, i extension features pokazują że picks są wcześniej w rally'u. Layer 3 powinien je częściej akceptować."
        )
    else:
        lines.append(
            "**REVIEW**: wyniki nie mieszczą się w prostym decision tree. Przeanalizuj indywidualne metryki."
        )

    report_md.write_text("\n".join(lines))
    print(f"\nWrote {report_md}")

    # CSV dumps
    combined = pd.concat([run_mom.picks_df, run_early.picks_df], ignore_index=True)
    combined.to_csv(CSV_DIR / "daily_picks.csv", index=False)
    overlap.to_csv(CSV_DIR / "pick_overlap.csv", header=["overlap"])
    pd.DataFrame([run_mom.metrics, run_early.metrics]).to_csv(
        CSV_DIR / "headline_metrics.csv", index=False
    )
    print(f"Wrote {CSV_DIR}/*.csv")


if __name__ == "__main__":
    main()

"""v8 holdout reveal — literature-direct (Xing 2010) options-implied screener.

Pre-registered as `v8_literature_direct_options_implied_2026_05_03` per
`docs/research/preregistration/params_v8_literature_direct_options_implied_2026_05_03.json`.

Built after v7 FAIL'd a 5-phase multi-phase audit on 2026-05-02 (mean αt
+2.60, dispersion 67.2pp, sign-flip on ivx30+ivp30 across all 5 phases,
L/S spread αt = -2.78). v7's Lasso-on-train (2018-2024) learned positive
coefs on vol-level features — opposite of the Xing 2010 negative-IV-return
prior committed ex-ante. Strategic narrative: classic regime-shift overfit.

Per perplexity adversarial review (Sonar Reasoning Pro 2026-05-03), v8
removes the optimizer entirely:

  score(asof, ticker) = -features.loc[(asof, ticker), "ivp30"]

Top-decile by score = bottom-decile by ivp30 = LOW-IV names = LONG leg per
Xing 2010. No fit, no sign-flip surface — the redesign axis ranked highest
for HARKing-risk × statistical-power × Bonferroni-cost defensibility.

Pipeline:
1. Load smd cache for universe (Tier 1 PIT-active + ETFs, ~1635 tickers).
2. Build calendar (stride=5, phase_offset=arg) over [holdout_start, holdout_end].
   No train calendar — v8 has no fit phase.
3. Build 7-feature frame via `options_implied.build_feature_frame` on holdout asofs only.
4. Coverage gate: ≥70% of (universe × asofs) must have non-NaN `ivp30`.
5. Score = -ivp30 via `score_literature_direct`.
6. Per holdout asof: top-decile (LONG) + bottom-decile (SHORT, L/S diag) by score. EW.
7. Carhart-4F (HAC=5) on top-decile vs MDY-excess.
8. Verdict per pre-reg success_criteria.

Audit-multi-phase compatible: emits a single WARNING line matching
`audit_multi_phase.py:_RESULT_LINE`.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import yaml
from alphalens_pipeline.data.alt_data.ivolatility_smd_cache import load_cached_smd
from alphalens_pipeline.data.factors import load_carhart_daily
from alphalens_research.attribution.cost_model import CostModel
from alphalens_research.attribution.factor_analysis import run_regression
from alphalens_research.backtest.metrics import max_drawdown, sharpe
from alphalens_research.screeners.options_implied import (
    DEFAULT_HOLDING,
    build_feature_frame,
    load_delisting_events_index,
    score_literature_direct,
)
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SMD_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
ETFS = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD")

CARHART_COLS = ["Mkt-RF", "SMB", "HML", "Mom"]


# ---------------------------------------------------------------------------
# Universe + smd loader (verbatim from v7)


def _pit_union(start_year: int = 2018) -> list[str]:
    union: set[str] = set()
    for p in sorted(PIT_DIR.glob("*.yaml")):
        try:
            snap_year = int(p.stem.split("-")[0])
        except ValueError:
            continue
        if snap_year < start_year:
            continue
        data = yaml.safe_load(p.read_text()) or {}
        for t in data.get("tickers", []):
            union.add(str(t).upper())
    union |= set(ETFS)
    return sorted(union)


_SMD_CACHE: dict[str, pd.DataFrame | None] = {}


def _smd_loader(ticker: str) -> pd.DataFrame | None:
    """In-memory memoized loader. See v7 driver for I/O bottleneck rationale."""
    key = ticker.upper()
    if key not in _SMD_CACHE:
        _SMD_CACHE[key] = load_cached_smd(key, SMD_CACHE_DIR)
    return _SMD_CACHE[key]


# ---------------------------------------------------------------------------
# Calendar — derive trading days from MDY benchmark cache


def _benchmark_calendar(
    benchmark: str, start: date, end: date, stride: int, phase_offset: int
) -> list[date]:
    df = load_cached_smd(benchmark, SMD_CACHE_DIR)
    if df is None or df.empty:
        raise RuntimeError(f"benchmark {benchmark!r} not in smd cache {SMD_CACHE_DIR}")
    if "ivp30" in df.columns:
        df = df.loc[df["ivp30"].notna()]
    df = df.sort_values("tradeDate")
    dates = pd.to_datetime(df["tradeDate"])
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    sliced = sorted(set(dates.loc[mask].dt.date.tolist()))
    if not 0 <= phase_offset < stride:
        raise ValueError(f"phase_offset must satisfy 0 <= offset < {stride}")
    return sliced[phase_offset::stride]


# ---------------------------------------------------------------------------
# Decile selection + portfolio returns
#
# 1d-forward portfolio-return convention mirrors v7 (clean HAC, no overlap).
# 20d-forward at 5d-stride creates 15d overlap and spuriously inflates αt;
# v7's bug-fix carries over verbatim here.


def _portfolio_returns(
    feat_holdout: pd.DataFrame,
    scores: pd.Series,
    *,
    decile_pct: float = 0.1,
    delisting_events: dict | None = None,
) -> tuple[pd.Series, pd.Series, pd.DatetimeIndex, pd.Series]:
    """Per holdout asof: top decile = LONG, bottom decile = SHORT (L/S only)."""
    holdout = feat_holdout.assign(_score=scores).dropna(subset=["_score"])
    asof_dates = sorted(holdout["asof"].unique())

    long_rets, short_rets, indices, sizes = [], [], [], []
    for asof in asof_dates:
        slice_df = holdout.loc[holdout["asof"] == asof]
        n = len(slice_df)
        decile_size = max(1, round(n * decile_pct))
        if n < 2 * decile_size:
            continue

        ranked = slice_df.sort_values("_score", ascending=False)
        top = ranked.head(decile_size)["ticker"].tolist()
        bottom = ranked.tail(decile_size)["ticker"].tolist()

        from alphalens_research.screeners.options_implied.target import forward_raw_return

        top_rets = [
            forward_raw_return(
                _smd_loader,
                t,
                asof,
                holding_period=1,
                delisting_events=delisting_events,
            )
            for t in top
        ]
        bot_rets = [
            forward_raw_return(
                _smd_loader,
                t,
                asof,
                holding_period=1,
                delisting_events=delisting_events,
            )
            for t in bottom
        ]
        top_arr = np.array([r if r is not None else np.nan for r in top_rets], dtype=float)
        bot_arr = np.array([r if r is not None else np.nan for r in bot_rets], dtype=float)
        if np.all(np.isnan(top_arr)) or np.all(np.isnan(bot_arr)):
            continue
        long_rets.append(float(np.nanmean(top_arr)))
        short_rets.append(float(np.nanmean(bot_arr)))
        indices.append(asof)
        sizes.append(decile_size)

    if not indices:
        empty = pd.Series(dtype=float)
        return empty, empty, pd.DatetimeIndex([]), empty
    asof_idx = pd.DatetimeIndex(pd.to_datetime(indices))
    return (
        pd.Series(long_rets, index=asof_idx, name="long_return"),
        pd.Series(short_rets, index=asof_idx, name="short_return"),
        asof_idx,
        pd.Series(sizes, index=asof_idx, name="decile_size"),
    )


def _benchmark_holding_returns(asof_index: pd.DatetimeIndex, benchmark: str) -> pd.Series:
    from alphalens_research.screeners.options_implied.target import forward_raw_return

    rets = []
    for asof in asof_index:
        r = forward_raw_return(_smd_loader, benchmark, asof, holding_period=1)
        rets.append(np.nan if r is None else r)
    return pd.Series(rets, index=asof_index, dtype=float, name="benchmark_return")


# ---------------------------------------------------------------------------
# Carhart-4F + Sharpe + MDD


def _assess(
    portfolio_returns: pd.Series,
    bench_returns: pd.Series,
    carhart: pd.DataFrame,
    *,
    rebalance_stride: int,
    cost_drag_per_period: float,
    label: str,
) -> dict:
    rets = portfolio_returns.dropna()
    if rets.empty:
        return {"n": 0, "label": label}

    rebalances_per_year = 252 / max(1, rebalance_stride)
    rets_net = rets - cost_drag_per_period
    sharpe_gross = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))
    sharpe_net = sharpe(rets_net.tolist(), periods_per_year=int(rebalances_per_year))

    res4 = run_regression(rets, carhart[[*CARHART_COLS, "RF"]], CARHART_COLS)

    bench_aligned = bench_returns.reindex(rets.index).dropna()
    excess_per_rebal = (rets.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann_gross = (
        float(excess_per_rebal * rebalances_per_year)
        if not math.isnan(excess_per_rebal)
        else float("nan")
    )
    drag_ann = cost_drag_per_period * rebalances_per_year

    cum = (1 + rets_net.fillna(0)).cumprod()
    mdd = float(max_drawdown(cum.tolist()))

    return {
        "label": label,
        "n": len(rets),
        "sharpe_gross": float(sharpe_gross),
        "sharpe_net": float(sharpe_net),
        "alpha_gross_4f": float(res4.alpha_annualized),
        "alpha_t_4f": float(res4.alpha_tstat),
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "excess_vs_bench_ann_gross": excess_ann_gross,
        "excess_vs_bench_ann_net": excess_ann_gross - drag_ann,
        "max_drawdown_net": mdd,
        "cost_drag_ann": drag_ann,
    }


# ---------------------------------------------------------------------------
# Verdict


def _verdict(
    primary_stats: Mapping,
    *,
    coverage_pct: float,
    bonferroni_t: float = 2.95,
    coverage_min: float = 0.70,
) -> str:
    """Pre-reg single-bar PASS rule. No stretch tier (v8 simplification).

    PASS:  primary holdout αt ≥ 2.95 (program-Bonferroni n=15)
           AND ivp30 coverage ≥ 70%
    FAIL:  any gate misses
    """
    t = primary_stats.get("alpha_t_4f", 0.0)
    if coverage_pct < coverage_min:
        return f"FAIL (ivp30 coverage {coverage_pct * 100:.1f}% < {coverage_min * 100:.0f}%)"
    if abs(t) >= bonferroni_t:
        return f"PASS single-phase (αt={t:+.2f} ≥ {bonferroni_t}); pending multi-phase audit"
    return f"FAIL (αt={t:+.2f} < {bonferroni_t} program-Bonferroni n=15)"


# ---------------------------------------------------------------------------
# CLI


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--holdout-start", type=date.fromisoformat, default=date(2024, 4, 30))
    ap.add_argument("--holdout-end", type=date.fromisoformat, default=date(2026, 4, 30))
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument("--holding", type=int, default=DEFAULT_HOLDING)
    ap.add_argument("--decile-pct", type=float, default=0.1)
    ap.add_argument("--benchmark", default="MDY")
    ap.add_argument("--adv-min-usd", type=float, default=2_000_000.0)
    ap.add_argument("--cost-bps-rt", type=float, default=30.0, help="Long-only RT cost")
    # `--train-start` accepted (and ignored) for audit_multi_phase.py forwarding parity
    # — the driver passes the same argparse surface to every script.
    ap.add_argument("--train-start", type=date.fromisoformat, default=date(2018, 4, 30))
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v8_literature_direct_holdout.md",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v8_literature_direct_holdout.json",
    )
    ap.add_argument("--max-tickers", type=int, default=None, help="Cap universe for testing")
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = _build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # 1. Universe
    universe = _pit_union()
    if args.max_tickers:
        universe = universe[: args.max_tickers]
    logger.info("Universe: %d tickers", len(universe))

    # 2. Calendar — holdout only (v8 has no train phase)
    asofs = _benchmark_calendar(
        args.benchmark,
        args.holdout_start,
        args.holdout_end,
        args.rebalance_stride,
        args.phase_offset,
    )
    if not asofs:
        logger.error("Empty holdout calendar")
        return 1
    logger.info(
        "Calendar: %d asofs (stride=%d, phase=%d) %s..%s",
        len(asofs),
        args.rebalance_stride,
        args.phase_offset,
        asofs[0],
        asofs[-1],
    )

    # 3. Carhart factors (covers holdout + lookback for HAC)
    ff_start = args.holdout_start - timedelta(days=400)
    carhart = load_carhart_daily(start=ff_start, end=args.holdout_end)
    logger.info("Carhart: %d rows", len(carhart))

    # 4. Feature frame (holdout asofs only)
    t0 = time.time()
    asof_strs = [d.isoformat() for d in asofs]
    features = build_feature_frame(
        smd_loader=_smd_loader,
        universe=universe,
        asof_dates=asof_strs,
        adv_min_dollar=args.adv_min_usd,
    )
    logger.info(
        "Feature frame: %d rows × %d cols in %.1fs",
        len(features),
        len(features.columns),
        time.time() - t0,
    )
    if features.empty:
        logger.error("Empty feature frame — abort")
        return 1

    # 5. Coverage gate (ivp30 non-NaN / (universe × asofs)).
    # Universe pre-filters mean denominator-with-attempt is build_feature_frame
    # rows; ivp30-NaN rate inside that is the relevant signal for v8.
    nonnan_ivp30 = int(features["ivp30"].notna().sum())
    coverage = nonnan_ivp30 / max(1, len(features))
    logger.info(
        "Coverage: %.1f%% non-NaN ivp30 (%d / %d)",
        coverage * 100,
        nonnan_ivp30,
        len(features),
    )

    # 6. Delisting events for terminal-bar handling
    delisting_events = load_delisting_events_index(SURVIVORSHIP_PARQUET)
    logger.info("Loaded %d delisting events for terminal-return rule", len(delisting_events))

    # 7. Score = -ivp30
    scores = score_literature_direct(features)
    n_scored = int(scores.notna().sum())
    logger.info("Scored: %d / %d non-NaN", n_scored, len(scores))

    # 8. Decile portfolios
    long_rets, short_rets, asof_idx, decile_sizes = _portfolio_returns(
        features,
        scores,
        decile_pct=args.decile_pct,
        delisting_events=delisting_events,
    )
    if long_rets.empty:
        logger.error("Empty holdout portfolio — abort")
        return 1
    logger.info(
        "Holdout: %d rebalances, mean decile size=%.1f",
        len(long_rets),
        float(decile_sizes.mean()),
    )

    # 9. Benchmark + Carhart attribution
    bench_rets = _benchmark_holding_returns(asof_idx, args.benchmark)

    cost_long_only = CostModel.from_profile("long_only_30bps")
    drag_long_only = cost_long_only.annual_drag_bps / 10_000.0 / (252 / args.rebalance_stride)
    drag_ls = (cost_long_only.annual_drag_bps * 2) / 10_000.0 / (252 / args.rebalance_stride)

    primary_stats = _assess(
        long_rets,
        bench_rets,
        carhart,
        rebalance_stride=args.rebalance_stride,
        cost_drag_per_period=drag_long_only,
        label="LONG-only top decile by -ivp30",
    )
    ls_rets = long_rets - short_rets
    ls_stats = _assess(
        ls_rets,
        bench_rets,
        carhart,
        rebalance_stride=args.rebalance_stride,
        cost_drag_per_period=drag_ls,
        label="L/S decile spread (diagnostic)",
    )

    verdict = _verdict(primary_stats, coverage_pct=coverage)

    logger.info(
        "PRIMARY  | n=%d Sh_gross=%.2f Sh_net=%.2f α_4F=%.2f%% αt=%.2f excess_net=%.2f%%",
        primary_stats["n"],
        primary_stats.get("sharpe_gross", 0.0),
        primary_stats.get("sharpe_net", 0.0),
        primary_stats.get("alpha_gross_4f", 0.0) * 100,
        primary_stats.get("alpha_t_4f", 0.0),
        primary_stats.get("excess_vs_bench_ann_net", 0.0) * 100,
    )
    label = f"HOLDOUT {args.holdout_start.year}-{args.holdout_end.year}"
    # Audit-multi-phase compatible single-line format. WARNING level survives
    # `--log-level WARNING` audits (else logger.info would be suppressed and
    # the audit's regex would match nothing).
    logger.warning(
        "%s | bench=%s ADV≥$%.0fM cost=%.0fbps RT | n=%d topN=%.1f turn=N/A | "
        "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
        "α 4F=%.1f%% t=%.2f",
        label,
        args.benchmark,
        args.adv_min_usd / 1e6,
        args.cost_bps_rt,
        primary_stats["n"],
        float(decile_sizes.mean()),
        primary_stats.get("sharpe_gross", 0.0),
        primary_stats.get("sharpe_net", 0.0),
        primary_stats.get("excess_vs_bench_ann_gross", 0.0) * 100,
        primary_stats.get("excess_vs_bench_ann_net", 0.0) * 100,
        primary_stats.get("alpha_gross_4f", 0.0) * 100,
        primary_stats.get("alpha_t_4f", 0.0),
    )
    logger.info(
        "L/S diag | n=%d Sh_gross=%.2f Sh_net=%.2f α_4F=%.2f%% αt=%.2f",
        ls_stats["n"],
        ls_stats.get("sharpe_gross", 0.0),
        ls_stats.get("sharpe_net", 0.0),
        ls_stats.get("alpha_gross_4f", 0.0) * 100,
        ls_stats.get("alpha_t_4f", 0.0),
    )
    logger.info("VERDICT: %s", verdict)

    # 10. Persist
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "v8_literature_direct_v1",
        "date": date.today().isoformat(),
        "verdict": verdict,
        "config": {
            "holdout_window": (args.holdout_start.isoformat(), args.holdout_end.isoformat()),
            "stride_days": args.rebalance_stride,
            "phase_offset": args.phase_offset,
            "holding_period_days": 1,  # portfolio-return horizon (target horizon unused in v8)
            "decile_pct": args.decile_pct,
            "benchmark": args.benchmark,
            "adv_min_usd": args.adv_min_usd,
            "cost_bps_rt": args.cost_bps_rt,
            "universe_size": len(universe),
            "scoring_feature": "ivp30 (negated)",
        },
        "coverage_pct": coverage,
        "n_features_rows": len(features),
        "n_scored": n_scored,
        "primary_stats": primary_stats,
        "ls_diagnostic_stats": ls_stats,
    }
    args.out_json.write_text(json.dumps(payload, indent=2, default=str))
    _write_md_report(args.out, payload)
    logger.info("→ %s\n→ %s", args.out_json, args.out)
    return 0


def _write_md_report(out_path: Path, payload: dict) -> None:
    p = payload["primary_stats"]
    ls = payload["ls_diagnostic_stats"]
    lines = [
        f"# v8 literature-direct holdout reveal — {payload['verdict']}",
        "",
        f"**Date:** {payload['date']}",
        "**Pre-reg:** v8_literature_direct_options_implied_2026_05_03",
        "**Score:** -ivp30 (Xing 2010 1y-rolling IV-percentile).",
        "",
        "## Headline (PRIMARY = LONG TOP decile by -ivp30 = LOW-IV names)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| n holdout rebalances | {p.get('n', 0)} |",
        f"| Sharpe (gross) | {p.get('sharpe_gross', 0):.2f} |",
        f"| Sharpe (net 30bps RT) | {p.get('sharpe_net', 0):.2f} |",
        f"| Carhart-4F α (gross, ann) | {p.get('alpha_gross_4f', 0) * 100:+.2f}% |",
        f"| Carhart-4F α (net, ann) | {p.get('alpha_net_4f', 0) * 100:+.2f}% |",
        f"| α t-stat (HAC=5) | **{p.get('alpha_t_4f', 0):+.2f}** |",
        f"| Excess vs MDY (gross, ann) | {p.get('excess_vs_bench_ann_gross', 0) * 100:+.2f}% |",
        f"| Excess vs MDY (net, ann) | {p.get('excess_vs_bench_ann_net', 0) * 100:+.2f}% |",
        f"| Max drawdown (net cum) | {p.get('max_drawdown_net', 0) * 100:+.2f}% |",
        "",
        "## L/S diagnostic (top − bottom decile, NOT primary verdict)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Sharpe (gross) | {ls.get('sharpe_gross', 0):.2f} |",
        f"| Sharpe (net 60bps RT) | {ls.get('sharpe_net', 0):.2f} |",
        f"| Carhart-4F α (gross, ann) | {ls.get('alpha_gross_4f', 0) * 100:+.2f}% |",
        f"| α t-stat (HAC=5) | {ls.get('alpha_t_4f', 0):+.2f} |",
        "",
        "## Coverage",
        "",
        f"- Non-NaN ivp30 / total feature rows: {payload['n_scored']} / {payload['n_features_rows']}"
        f" = {payload['coverage_pct'] * 100:.1f}%",
        "",
        "## Pre-reg discipline",
        "",
        "- DETERMINISTIC scorer = -ivp30 (no fit, no sign-flip surface).",
        "- Direction LOCKED ex-ante per Xing 2010 / Bali-Hovakimian 2009 NEGATIVE-sign prior.",
        "- ONE-shot holdout, no peek-and-tune.",
        "- Carhart-4F (HAC=5) attribution post-hoc on top-decile vs MDY-excess.",
        "- L/S diagnostic reported as power-loss check, NOT additional Bonferroni test.",
        "- Single-bar PASS rule (no stretch tier): αt ≥ 2.95 program-Bonferroni n=15.",
    ]
    out_path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())

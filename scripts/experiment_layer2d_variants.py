"""Layer 2d variant exploration — same harness, swap the ranking signal.

Layer 2d closed 2026-04-24 as overfit (Carhart IS t=2.14 → OOS t=0.68). Before
treating the screener as definitively dead, run a one-evening exploration to
ask: did we test the *right* signal? The original adapter ranks by
``insider_count`` — Lakonishok-Lee 2001 / Cohen-Malloy-Pomorski 2012 emphasize
dollar-weighted activity. We have the parquet cluster cache (94 MB, 2011-2026)
so each variant runs in minutes, not hours.

This is RESEARCH ONLY — purpose is to learn whether the failure mode is
"signal absent" or "signal mis-encoded". Not a path to capital deployment;
the parent layer remains CLOSED per CLAUDE.md repositioning.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yaml

from alphalens.alt_data.yfinance_cache import load_cached_histories
from alphalens.archive.screeners.insider.parquet_scorer import ParquetInsiderScorer
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.factor_analysis import (
    run_carhart_attribution,
    run_ff5_umd_attribution,
)
from alphalens.backtest.factors import load_carhart_daily, load_ff5_umd_daily
from alphalens.backtest.history_store import HistoryStore
from alphalens.backtest.metrics import sharpe

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_PARQUET_PATH = Path.home() / ".alphalens" / "insider_form4.parquet"


VariantScoreFn = Callable[[dict], float | None]


def _score_count(feat: dict) -> float | None:
    return float(feat["insider_count"])


def _score_dollar(feat: dict) -> float | None:
    return float(feat["aggregate_dollar"])


def _score_log_dollar_x_count(feat: dict) -> float | None:
    dollar = float(feat["aggregate_dollar"])
    if dollar <= 0:
        return None
    return math.log10(dollar) * float(feat["insider_count"])


def _score_count_threshold4(feat: dict) -> float | None:
    n = float(feat["insider_count"])
    if n < 4:
        return None
    return n


def _score_count_threshold5(feat: dict) -> float | None:
    n = float(feat["insider_count"])
    if n < 5:
        return None
    return n


def _score_dollar_threshold_1m(feat: dict) -> float | None:
    dollar = float(feat["aggregate_dollar"])
    if dollar < 1_000_000:
        return None
    return dollar


VARIANTS: dict[str, VariantScoreFn] = {
    "V0_count": _score_count,
    "V1_dollar": _score_dollar,
    "V2_log_dollar_x_count": _score_log_dollar_x_count,
    "V3_count_ge4": _score_count_threshold4,
    "V4_count_ge5": _score_count_threshold5,
    "V5_dollar_ge_1M": _score_dollar_threshold_1m,
}


def make_variant_adapter(score_fn: VariantScoreFn):
    """Build an engine-compatible scorer from a per-feature score function."""

    def adapter(
        histories: Mapping[str, pd.DataFrame],
        config: Mapping | None = None,
    ) -> pd.DataFrame:
        config = dict(config or {})
        store = config["_insider_store"]
        benchmark = config.get("benchmark")

        dates: list[pd.Timestamp] = []
        for df in histories.values():
            if df is not None and not df.empty:
                dates.append(df.index.max())
        if not dates:
            return pd.DataFrame(columns=["ticker", "score"])
        asof = max(dates).date()

        rows: list[dict] = []
        for ticker in histories:
            if ticker == benchmark:
                continue
            feat = store.features_as_of(ticker, asof)
            if not feat:
                continue
            score = score_fn(feat)
            if score is None:
                continue
            rows.append({"ticker": ticker, "score": float(score)})
        if not rows:
            return pd.DataFrame(columns=["ticker", "score"])
        return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)

    adapter.MIN_BARS_REQUIRED = 0
    return adapter


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


def run_variant(
    name: str,
    score_fn: VariantScoreFn,
    insider_store: ParquetInsiderScorer,
    history_store: HistoryStore,
    universe: list[str],
    start: date,
    end: date,
    *,
    top_n: int,
    holding: int,
    benchmark: str,
    rebalance_stride: int,
    phase_offset: int,
    carhart: pd.DataFrame,
    ff5_umd: pd.DataFrame,
) -> dict:
    logger.info("=== variant %s ===", name)
    adapter = make_variant_adapter(score_fn)
    engine = BacktestEngine(
        history_store,
        scorer=adapter,
        scorer_config={"benchmark": benchmark, "_insider_store": insider_store},
        holding_period=holding,
        top_n=top_n,
        benchmark=benchmark,
        screener_tickers=universe,
        weighting="linear",
        rebalance_stride=rebalance_stride,
        phase_offset=phase_offset,
    )
    report = engine.run(start, end)
    rets = report.portfolio_returns
    if rets.empty:
        logger.warning("variant %s produced no portfolio returns", name)
        return {"name": name, "n": 0}

    rebalances_per_year = 252 / max(1, rebalance_stride)
    sharpe_naive = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))

    carhart_res = run_carhart_attribution(rets, carhart)[-1]
    try:
        ff5_res = run_ff5_umd_attribution(rets, ff5_umd)
    except Exception as exc:
        logger.warning("FF5+UMD failed for %s: %s", name, exc)
        ff5_res = None

    out = {
        "name": name,
        "n": len(rets),
        "mean_top_n": float(
            sum(len(r.top_n_tickers) for r in report.rebalance_results)
            / max(1, len(report.rebalance_results))
        ),
        "sharpe": sharpe_naive,
        "carhart_alpha_ann": carhart_res.alpha_annualized,
        "carhart_t": carhart_res.alpha_tstat,
        "carhart_r2": carhart_res.r_squared,
        "ff5_alpha_ann": ff5_res.alpha_annualized if ff5_res else float("nan"),
        "ff5_t": ff5_res.alpha_tstat if ff5_res else float("nan"),
    }
    logger.info(
        "%s: n=%d sharpe=%.2f Carhart α=%.2f%% t=%.2f | FF5+UMD α=%.2f%% t=%.2f",
        name,
        out["n"],
        out["sharpe"],
        out["carhart_alpha_ann"] * 100,
        out["carhart_t"],
        out["ff5_alpha_ann"] * 100 if ff5_res else float("nan"),
        out["ff5_t"] if ff5_res else float("nan"),
    )
    return out


def format_results_table(results: list[dict], split_label: str) -> str:
    lines = [
        f"## {split_label}",
        "",
        "| Variant | N | mean top-N | Sharpe | Carhart α | t-stat | R² | FF5+UMD α | t-stat |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        if r.get("n", 0) == 0:
            lines.append(f"| {r['name']} | 0 | – | – | – | – | – | – | – |")
            continue
        lines.append(
            "| {name} | {n} | {top:.1f} | {sharpe:.2f} | {ca:.2f}% | {ct:.2f} | {r2:.3f} | {fa:.2f}% | {ft:.2f} |".format(
                name=r["name"],
                n=r["n"],
                top=r["mean_top_n"],
                sharpe=r["sharpe"],
                ca=r["carhart_alpha_ann"] * 100,
                ct=r["carhart_t"],
                r2=r["carhart_r2"],
                fa=r["ff5_alpha_ann"] * 100,
                ft=r["ff5_t"],
            )
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument(
        "--phase-offset",
        type=int,
        default=0,
        help="Phase offset for strided rebalance calendar; 0..rebalance_stride-1.",
    )
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--label", default="OOS")
    ap.add_argument("--variants", nargs="+", default=list(VARIANTS.keys()))
    ap.add_argument("--out", type=Path, default=Path("docs/research/layer2d_variants.md"))
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("loading parquet insider store from %s", _PARQUET_PATH)
    insider_store = ParquetInsiderScorer(_PARQUET_PATH)
    logger.info("parquet store stats: %s", insider_store.stats)

    universe = load_pit_union(args.start, args.end)
    logger.info("PIT union universe: %d tickers", len(universe))
    if not universe:
        logger.error("empty universe; check ~/.alphalens/pit_universe/")
        return 2

    tickers_with_bench = [*universe, args.benchmark]
    histories = load_cached_histories(tickers_with_bench, _PRICES_DIR)
    if args.benchmark not in histories:
        logger.error("benchmark %s missing from yfinance cache", args.benchmark)
        return 2
    logger.info("loaded %d histories (need %d)", len(histories), len(tickers_with_bench))
    history_store = HistoryStore(histories)

    carhart = load_carhart_daily(start=args.start, end=args.end)
    ff5_umd = load_ff5_umd_daily(start=args.start, end=args.end)

    results: list[dict] = []
    for name in args.variants:
        if name not in VARIANTS:
            logger.warning("unknown variant: %s", name)
            continue
        results.append(
            run_variant(
                name,
                VARIANTS[name],
                insider_store,
                history_store,
                universe,
                args.start,
                args.end,
                top_n=args.top_n,
                holding=args.holding,
                benchmark=args.benchmark,
                rebalance_stride=args.rebalance_stride,
                phase_offset=args.phase_offset,
                carhart=carhart,
                ff5_umd=ff5_umd,
            )
        )

    table = format_results_table(results, f"{args.label} ({args.start} → {args.end})")
    print("\n" + table + "\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        existing = args.out.read_text()
    else:
        existing = (
            "# Layer 2d variant exploration\n\n"
            "RESEARCH ONLY — Layer 2d remains CLOSED for capital deployment.\n"
            "Purpose: test whether the OOS failure is signal-absent or signal-mis-encoded.\n\n"
            f"- Top-N: {args.top_n}\n"
            f"- Holding period (signal-quality only): {args.holding}\n"
            f"- Rebalance stride: {args.rebalance_stride} ({252 // args.rebalance_stride}/y)\n"
            f"- Universe: PIT union ({len(universe)} tickers)\n"
            f"- Benchmark: {args.benchmark}\n\n"
        )
    args.out.write_text(existing + "\n" + table + "\n")
    logger.info("wrote results table → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

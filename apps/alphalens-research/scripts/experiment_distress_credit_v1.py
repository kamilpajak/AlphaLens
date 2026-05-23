"""distress_credit_v1_2026_05_04 — long-only safe-decile Merton-PD experiment.

Pre-reg context:
- Class: distress_credit_search_2026_05_04 (NEW, first in class)
- Compound L2 x L4 → AUTO-PIVOTED to pure L2 at Phase A 2026-05-04 (overlay
  sanity check FAILed: BAA10Y(spread_z, fwd_21d_SPY) = +0.047 on TRAIN,
  expected ≤ -0.05). Layer 4 dropped from PRIMARY per pre-committed rule.
- PRIMARY (post-pivot): long-only equal-weighted bottom-quintile Merton-PD
  portfolio drawn from S&P 1500 PIT (excluding top-50 mega-caps and
  excluding top-quintile-distress always). Carhart 4F α t-stat ≥ 3.50.

CLI flags mirror experiment_momentum_lowvol_combo.py for parity with
audit_multi_phase.py regex parser.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from alphalens_pipeline.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens_pipeline.data.factors import load_carhart_daily  # noqa: E402
from alphalens_pipeline.data.macro.fred_client import FREDClient  # noqa: E402
from alphalens_pipeline.data.store.history import HistoryStore  # noqa: E402
from alphalens_pipeline.data.universes.sp1500_pit import (  # noqa: E402
    load_sp400_pit_for_date,
    load_sp500_pit_for_date,
    load_sp600_pit_for_date,
)
from alphalens_research.attribution.cost_model import RealisticCostModel  # noqa: E402
from alphalens_research.attribution.factor_analysis import run_regression  # noqa: E402
from alphalens_research.backtest.engine import BacktestEngine  # noqa: E402
from alphalens_research.backtest.metrics import sharpe, turnover_pct  # noqa: E402
from alphalens_research.screeners.distress_credit.features import (
    make_production_stores,
)
from alphalens_research.screeners.distress_credit.scorer import (
    distress_credit_adapter,
)

logger = logging.getLogger(__name__)

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]


def load_sp1500_union(asof: pd.Timestamp) -> list[str]:
    """SP1500 PIT membership at asof (snapshot fallback semantics)."""
    sp500 = load_sp500_pit_for_date(asof)
    sp400 = load_sp400_pit_for_date(asof)
    sp600 = load_sp600_pit_for_date(asof)
    return sorted(set(sp500 + sp400 + sp600))


def benchmark_returns(
    history_store: HistoryStore, benchmark: str, start: date, end: date
) -> pd.Series:
    df = history_store.full(benchmark)
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    return df["close"].pct_change().dropna()


def assess(report, factors, rebalance_stride, cost_bps, bench_rets) -> dict:
    rets = report.portfolio_returns
    if rets.empty:
        return {"n": 0}
    rebalances_per_year = 252 / max(1, rebalance_stride)
    sharpe_gross = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))
    avg_turnover = turnover_pct(r.top_n_tickers for r in report.rebalance_results)

    cost_model = RealisticCostModel(adverse_selection_bps=5.0)
    drag_per_rebal_bps = cost_model.primary_period_drag_bps(cost_bps, avg_turnover)
    drag_ann = drag_per_rebal_bps * rebalances_per_year / 10_000.0
    drag_per_rebal = drag_per_rebal_bps / 10_000.0
    rets_net = rets - drag_per_rebal
    sharpe_net = sharpe(rets_net.tolist(), periods_per_year=int(rebalances_per_year))

    res4 = run_regression(rets, factors[[*_CARHART_FACTORS, "RF"]], _CARHART_FACTORS)

    bench_aligned = bench_rets.reindex(rets.index).dropna()
    excess_per_rebal = (rets.reindex(bench_aligned.index) - bench_aligned).mean()
    excess_ann = float(excess_per_rebal * 252) if not np.isnan(excess_per_rebal) else float("nan")

    mean_top_n = float(
        sum(len(r.top_n_tickers) for r in report.rebalance_results)
        / max(1, len(report.rebalance_results))
    )
    return {
        "n": len(rets),
        "mean_top_n": mean_top_n,
        "turnover_per_rebal": avg_turnover,
        "sharpe_gross": sharpe_gross,
        "sharpe_net": sharpe_net,
        "alpha_gross_4f": float(res4.alpha_annualized),
        "t_4f": float(res4.alpha_tstat),
        "beta_smb": float(res4.betas.get("SMB", 0.0)),
        "beta_hml": float(res4.betas.get("HML", 0.0)),
        "beta_mom": float(res4.betas.get("Mom", 0.0)),
        "cost_drag_ann": drag_ann,
        "alpha_net_4f": float(res4.alpha_annualized) - drag_ann,
        "excess_vs_bench_ann": excess_ann,
        "excess_vs_bench_net": excess_ann - drag_ann,
    }


def filter_top_n_megacap(
    tickers: list[str],
    asof: pd.Timestamp,
    history_store: HistoryStore,
    share_store,
    n_exclude: int = 50,
) -> set[str]:
    """Return set of top-n_exclude tickers by mcap at asof. Caller excludes these."""
    mcaps = []
    for t in tickers:
        try:
            df = history_store.full(t)
            df = df[df.index <= asof]
            if len(df) == 0:
                continue
            close = float(df["close"].iloc[-1])
            shares = share_store.get(t, asof)
            if shares is None or close <= 0:
                continue
            mcaps.append((t, close * shares))
        except Exception:
            continue
    mcaps.sort(key=lambda x: -x[1])
    return {t for t, _ in mcaps[:n_exclude]}


class _DistressCreditScorer:
    """Wrapper around distress_credit_adapter that injects the PIT stores
    and dynamic asof per rebalance. BacktestEngine calls scorer with current
    asof in scorer_config; we need to set asof + dynamic megacap exclusion."""

    MIN_BARS_REQUIRED = 65

    def __init__(
        self, *, liab_store, share_store, rf_series, benchmark, history_store, n_megacap_exclude=50
    ):
        self._liab = liab_store
        self._shares = share_store
        self._rf = rf_series
        self._benchmark = benchmark
        self._history_store = history_store
        self._n_megacap = n_megacap_exclude
        self._mcap_excl_cache: dict[pd.Timestamp, set[str]] = {}

    def __call__(self, histories, config=None):
        cfg = dict(config or {})
        asof = cfg.get("asof")
        if asof is None:
            # Engine doesn't always pass asof; derive from histories
            common = None
            for df in histories.values():
                if df is None or len(df) == 0:
                    continue
                if common is None or df.index[-1] > common:
                    common = df.index[-1]
            asof = common
        if asof is None:
            return pd.DataFrame(columns=["ticker", "score", "pd"])

        # Cache megacap exclusion per asof (expensive; reused across phases at same asof)
        if asof not in self._mcap_excl_cache:
            self._mcap_excl_cache[asof] = filter_top_n_megacap(
                list(histories.keys()),
                asof,
                self._history_store,
                self._shares,
                n_exclude=self._n_megacap,
            )
        excluded = self._mcap_excl_cache[asof]
        filtered_histories = {t: h for t, h in histories.items() if t not in excluded}

        cfg.update(
            {
                "asof": asof,
                "liabilities_store": self._liab,
                "shares_store": self._shares,
                "rf_series": self._rf,
                "benchmark": self._benchmark,
            }
        )
        return distress_credit_adapter(filtered_histories, cfg)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=200)
    ap.add_argument("--holding", type=int, default=21)
    ap.add_argument("--rebalance-stride", type=int, default=21)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--cost-half-spreads", nargs="+", type=float, default=[5.0])
    ap.add_argument("--out", type=Path, default=Path("docs/research/distress_credit/audit_run.md"))
    ap.add_argument("--is-start", type=date.fromisoformat, default=date(2017, 1, 3))
    ap.add_argument("--is-end", type=date.fromisoformat, default=date(2024, 4, 29))
    ap.add_argument("--oos-start", type=date.fromisoformat, default=date(2024, 4, 30))
    ap.add_argument("--oos-end", type=date.fromisoformat, default=date(2026, 4, 30))
    ap.add_argument("--phase-offset", type=int, default=0)
    ap.add_argument("--n-megacap-exclude", type=int, default=50)
    ap.add_argument(
        "--oos-only", action="store_true", help="Only run OOS holdout window (skip IS)."
    )
    return ap


def main() -> int:
    ap = _build_parser()
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # Universe = union of SP1500 across full window
    full_universe_set: set[str] = set()
    sample_dates = pd.date_range(start=args.is_start, end=args.oos_end, freq="180D")
    for d in sample_dates:
        try:
            full_universe_set.update(load_sp1500_union(pd.Timestamp(d)))
        except Exception:
            continue
    full_universe = sorted(full_universe_set)
    logger.info("SP1500 union universe size: %d tickers", len(full_universe))

    histories = load_cached_histories([*full_universe, args.benchmark], _PRICES_DIR)
    history_store = HistoryStore(histories)

    # Production stores
    liab_store, share_store = make_production_stores()
    fred = FREDClient.from_env()
    rf_series = fred.fetch_series("DGS1") / 100.0  # FRED returns percent

    scorer = _DistressCreditScorer(
        liab_store=liab_store,
        share_store=share_store,
        rf_series=rf_series,
        benchmark=args.benchmark,
        history_store=history_store,
        n_megacap_exclude=args.n_megacap_exclude,
    )

    if args.oos_only:
        periods = [(f"OOS {args.oos_start.year}-{args.oos_end.year}", args.oos_start, args.oos_end)]
    else:
        periods = [
            (f"IS {args.is_start.year}-{args.is_end.year}", args.is_start, args.is_end),
            (f"OOS {args.oos_start.year}-{args.oos_end.year}", args.oos_start, args.oos_end),
        ]

    sections: list[str] = [
        "# distress_credit_v1_2026_05_04 — Audit Run",
        "",
        "Long-only equal-weighted bottom-quintile Merton-PD safe-decile.",
        f"Universe: SP1500 PIT, exclude top-{args.n_megacap_exclude} megacap.",
        f"Rebalance stride: {args.rebalance_stride}d, holding: {args.holding}d.",
        f"Phase offset: {args.phase_offset}",
        "",
    ]

    all_rows: list[dict] = []
    for label, start, end in periods:
        carhart = load_carhart_daily(start=start, end=end)
        bench_rets = benchmark_returns(history_store, args.benchmark, start, end)
        logger.info("=== %s | universe %d ===", label, len(full_universe))

        engine = BacktestEngine(
            history_store,
            scorer=scorer,
            scorer_config={},  # injected via wrapper
            holding_period=args.holding,
            top_n=args.top_n,
            benchmark=args.benchmark,
            screener_tickers=full_universe,
            weighting="equal",
            rebalance_stride=args.rebalance_stride,
            phase_offset=args.phase_offset,
        )
        report = engine.run(start, end)
        for cost_bps in args.cost_half_spreads:
            stats = assess(report, carhart, args.rebalance_stride, cost_bps, bench_rets)
            stats["period"] = label
            stats["cost_bps"] = cost_bps
            all_rows.append(stats)
            if stats.get("n", 0) > 0:
                # Canonical line for audit_multi_phase regex parser
                logger.info(
                    "%s | cost=%.0fbps | n=%d topN=%.1f turn=%.1f%% | "
                    "Sh gross=%.2f net=%.2f | excess gross=%.1f%% net=%.1f%% | "
                    "α 4F=%.1f%% t=%.2f",
                    label,
                    cost_bps,
                    stats["n"],
                    stats["mean_top_n"],
                    stats["turnover_per_rebal"] * 100,
                    stats["sharpe_gross"],
                    stats["sharpe_net"],
                    stats["excess_vs_bench_ann"] * 100,
                    stats["excess_vs_bench_net"] * 100,
                    stats["alpha_gross_4f"] * 100,
                    stats["t_4f"],
                )

    sections.append("## Results")
    sections.append("")
    sections.append(
        "| Period | cost | n | mean topN | turn | Sharpe gross | Sharpe net | "
        "excess gross | excess net | α 4F | t (4F) | β_SMB | β_HML | β_MOM |"
    )
    sections.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in all_rows:
        if r.get("n", 0) == 0:
            continue
        sections.append(
            "| {p} | {cb:.0f}bp | {n} | {tn:.1f} | {tr:.1f}% | "
            "{sg:.2f} | {sn:.2f} | {eg:+.1f}% | {en:+.1f}% | {a4:+.1f}% | {t4:+.2f} | "
            "{bsmb:+.2f} | {bhml:+.2f} | {bmom:+.2f} |".format(
                p=r["period"],
                cb=r["cost_bps"],
                n=r["n"],
                tn=r["mean_top_n"],
                tr=r["turnover_per_rebal"] * 100,
                sg=r["sharpe_gross"],
                sn=r["sharpe_net"],
                eg=r["excess_vs_bench_ann"] * 100,
                en=r["excess_vs_bench_net"] * 100,
                a4=r["alpha_gross_4f"] * 100,
                t4=r["t_4f"],
                bsmb=r["beta_smb"],
                bhml=r["beta_hml"],
                bmom=r["beta_mom"],
            )
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n", encoding="utf-8")
    logger.info("Wrote %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

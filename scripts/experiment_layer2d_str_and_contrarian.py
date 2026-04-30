"""Layer 2d Experiment 3 — STR-augmented factor model + pure-contrarian screener.

Two parallel hypotheses tested simultaneously:

H1 (factor side): Layer 2d V0 α loads on the unmodeled short-term-reversal
premium. Test: re-run V0 with FF3+UMD+STR (5-factor). If α attenuates
substantially when STR enters the regression, the residual α was
factor-loading, not insider edge.

H2 (signal side): a pure-contrarian rank (largest 60d drawdown × recent 5d
bounce, no insider data) reproduces Layer 2d's α. Test: build a
pure_contrarian scorer and run identical IS/OOS/subsample harness. If
α matches Layer 2d, insider data was a redundant proxy for contrarian
selection.

If H1 AND H2 both confirmed: insider information had ZERO marginal value
beyond the contrarian/reversal premium. The screener was a reversal
strategy in disguise.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import yaml

from alphalens.archive.screeners.insider.parquet_scorer import ParquetInsiderScorer
from alphalens.attribution.factor_analysis import run_regression
from alphalens.backtest.engine import BacktestEngine
from alphalens.backtest.metrics import sharpe
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import load_carhart_daily
from alphalens.data.store.history import HistoryStore

logger = logging.getLogger(__name__)

_PIT_DIR = Path.home() / ".alphalens" / "pit_universe"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"
_PARQUET_PATH = Path.home() / ".alphalens" / "insider_form4.parquet"
_STR_PATH = Path.home() / ".alphalens" / "factors" / "str_daily.csv"

_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]
_CARHART_PLUS_STR = [*_CARHART_FACTORS, "STR"]


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


def insider_v0_adapter(
    histories: Mapping[str, pd.DataFrame], config: Mapping | None = None
) -> pd.DataFrame:
    config = dict(config or {})
    store = config["_insider_store"]
    benchmark = config.get("benchmark")
    dates = [df.index.max() for df in histories.values() if df is not None and not df.empty]
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
        rows.append({"ticker": ticker, "score": float(feat["insider_count"])})
    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])
    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


insider_v0_adapter.MIN_BARS_REQUIRED = 0


def _contrarian_score(closes: np.ndarray, bounce_weight: float) -> float | None:
    if len(closes) < 65 or closes[-1] <= 0 or closes[-61] <= 0 or closes[-6] <= 0:
        return None
    ret_60d = closes[-1] / closes[-61] - 1.0
    ret_5d = closes[-1] / closes[-6] - 1.0
    return float(-ret_60d + bounce_weight * ret_5d)


def pure_contrarian_adapter(
    histories: Mapping[str, pd.DataFrame], config: Mapping | None = None
) -> pd.DataFrame:
    """Score = -ret_60d + bounce_weight * ret_5d on FULL universe.

    Rationale: Layer 2d cluster-positive set has stable -5pp 60d underperformance
    + +0.7pp 5d bounce vs non-cluster universe (per docs/research/layer2d_prior_returns_3f.md).
    This scorer mimics the SAME selection structure without using insider data.
    """
    config = dict(config or {})
    benchmark = config.get("benchmark")
    bounce_weight = float(config.get("_bounce_weight", 0.5))

    rows: list[dict] = []
    for ticker, df in histories.items():
        if ticker == benchmark or df is None:
            continue
        score = _contrarian_score(df["close"].to_numpy(dtype=float), bounce_weight)
        if score is None:
            continue
        rows.append({"ticker": ticker, "score": score})
    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])
    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


pure_contrarian_adapter.MIN_BARS_REQUIRED = 65


def cluster_contrarian_adapter(
    histories: Mapping[str, pd.DataFrame], config: Mapping | None = None
) -> pd.DataFrame:
    """Cluster-positive set ONLY, ranked by contrarian score.

    Apples-to-apples comparison vs V0_count: same candidate pool (only stocks
    with insider cluster active) but ranked by contrarian instead of insider_count.
    If this matches V0_count α, ranking-within-set didn't matter; the set itself
    drove returns. If this BEATS V0_count, contrarian rank within the cluster-
    positive set adds value (insider data PLUS contrarian signal > insider alone).
    """
    config = dict(config or {})
    store = config["_insider_store"]
    benchmark = config.get("benchmark")
    bounce_weight = float(config.get("_bounce_weight", 0.5))

    dates = [df.index.max() for df in histories.values() if df is not None and not df.empty]
    if not dates:
        return pd.DataFrame(columns=["ticker", "score"])
    asof = max(dates).date()

    rows: list[dict] = []
    for ticker, df in histories.items():
        if ticker == benchmark or df is None:
            continue
        feat = store.features_as_of(ticker, asof)
        if not feat:
            continue
        score = _contrarian_score(df["close"].to_numpy(dtype=float), bounce_weight)
        if score is None:
            continue
        rows.append({"ticker": ticker, "score": score})
    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])
    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


cluster_contrarian_adapter.MIN_BARS_REQUIRED = 65


def load_str_factor(start: date, end: date) -> pd.Series:
    df = pd.read_csv(_STR_PATH, parse_dates=["date"], index_col="date")
    s = df["STR"]
    s.index = pd.DatetimeIndex(s.index).tz_localize(None)
    return s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]


def merge_factors(carhart: pd.DataFrame, str_factor: pd.Series) -> pd.DataFrame:
    merged = carhart.copy()
    merged.index = pd.DatetimeIndex(merged.index).tz_localize(None)
    merged["STR"] = str_factor.reindex(merged.index)
    return merged.dropna(subset=["STR"])


def run_factor_regressions(returns: pd.Series, factors_5: pd.DataFrame) -> dict:
    """Both Carhart-4F and Carhart+STR regressions on the same series."""
    carhart_only = factors_5[[*_CARHART_FACTORS, "RF"]]
    res4 = run_regression(returns, carhart_only, _CARHART_FACTORS)
    res5 = run_regression(returns, factors_5, _CARHART_PLUS_STR)
    return {
        "alpha_4f_ann": float(res4.alpha_annualized),
        "t_4f": float(res4.alpha_tstat),
        "r2_4f": float(res4.r_squared),
        "alpha_5f_ann": float(res5.alpha_annualized),
        "t_5f": float(res5.alpha_tstat),
        "r2_5f": float(res5.r_squared),
        "beta_str": float(res5.betas.get("STR", 0.0)),
        "n": int(res4.n_observations),
    }


def run_one_backtest(
    name: str,
    scorer,
    insider_store: ParquetInsiderScorer | None,
    history_store: HistoryStore,
    universe: list[str],
    benchmark: str,
    start: date,
    end: date,
    top_n: int,
    holding: int,
    rebalance_stride: int,
    phase_offset: int = 0,
) -> pd.Series:
    config = {"benchmark": benchmark}
    if insider_store is not None:
        config["_insider_store"] = insider_store
    engine = BacktestEngine(
        history_store,
        scorer=scorer,
        scorer_config=config,
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
    rets.name = name
    return rets


def format_period_table(rows: list[dict]) -> str:
    headers = [
        "Strategy",
        "N",
        "Sharpe",
        "Carhart-4F α",
        "t (4F)",
        "R² (4F)",
        "+STR α",
        "t (5F)",
        "R² (5F)",
        "β_STR",
    ]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append(
            "| {strategy} | {n} | {sharpe:.2f} | {a4:.2f}% | {t4:.2f} | {r4:.3f} | {a5:.2f}% | {t5:.2f} | {r5:.3f} | {bstr:.2f} |".format(
                strategy=r["strategy"],
                n=r["n"],
                sharpe=r["sharpe"],
                a4=r["alpha_4f_ann"] * 100,
                t4=r["t_4f"],
                r4=r["r2_4f"],
                a5=r["alpha_5f_ann"] * 100,
                t5=r["t_5f"],
                r5=r["r2_5f"],
                bstr=r["beta_str"],
            )
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
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
    ap.add_argument("--bounce-weight", type=float, default=0.5)
    ap.add_argument("--out", type=Path, default=Path("docs/research/layer2d_str_and_contrarian.md"))
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    insider_store = ParquetInsiderScorer(_PARQUET_PATH)
    full_universe = load_pit_union(date(2011, 1, 1), date(2026, 4, 22))
    histories = load_cached_histories([*full_universe, args.benchmark], _PRICES_DIR)
    history_store = HistoryStore(histories)

    periods = [
        ("Full IS 2011-2022", date(2011, 1, 1), date(2022, 12, 31)),
        ("IS 2011-2016", date(2011, 1, 1), date(2016, 12, 31)),
        ("IS 2017-2022", date(2017, 1, 1), date(2022, 12, 31)),
        ("OOS 2023-2026", date(2023, 1, 1), date(2026, 4, 22)),
    ]

    sections: list[str] = [
        "# Layer 2d Experiment — STR factor decomposition + pure-contrarian comparison",
        "",
        "**RESEARCH ONLY.** Tests two hypotheses simultaneously:",
        "",
        "- H1 (factor): Layer 2d α loads on uncontrolled short-term reversal premium. Adding STR (Jegadeesh 1990, 21d formation) as 5th factor should attenuate α.",
        "- H2 (signal): a pure-contrarian scorer (-60d_return + 0.5 × 5d_return) reproduces Layer 2d α without insider data.",
        "",
        f"- Top-N: {args.top_n}",
        f"- Holding period (signal-only): {args.holding}d",
        f"- Rebalance stride: {args.rebalance_stride}",
        f"- Pure contrarian bounce weight: {args.bounce_weight}",
        f"- Universe: per-subperiod PIT union (full=2011-2026 has {len(full_universe)} tickers; subperiods filter to contemporaneous snapshots)",
        "- Factor specs: Carhart-4F (Mkt-RF, SMB, HML, Mom) and Carhart-4F + STR",
        "",
    ]

    summary_rows: list[dict] = []

    for label, start, end in periods:
        logger.info("=== %s ===", label)
        universe = load_pit_union(start, end)
        logger.info("PIT universe for %s: %d tickers", label, len(universe))
        carhart = load_carhart_daily(start=start, end=end)
        str_factor = load_str_factor(start=start, end=end)
        factors_5 = merge_factors(carhart, str_factor)
        logger.info(
            "factor data: carhart %d rows, STR %d rows, merged %d rows",
            len(carhart),
            len(str_factor),
            len(factors_5),
        )

        rets_v0 = run_one_backtest(
            "V0_count",
            insider_v0_adapter,
            insider_store,
            history_store,
            universe,
            args.benchmark,
            start,
            end,
            args.top_n,
            args.holding,
            args.rebalance_stride,
            args.phase_offset,
        )
        rets_pc = run_one_backtest(
            "pure_contrarian",
            pure_contrarian_adapter,
            None,
            history_store,
            universe,
            args.benchmark,
            start,
            end,
            args.top_n,
            args.holding,
            args.rebalance_stride,
            args.phase_offset,
        )
        rets_cc = run_one_backtest(
            "cluster_contrarian",
            cluster_contrarian_adapter,
            insider_store,
            history_store,
            universe,
            args.benchmark,
            start,
            end,
            args.top_n,
            args.holding,
            args.rebalance_stride,
            args.phase_offset,
        )

        rebalances_per_year = 252 / max(1, args.rebalance_stride)
        period_rows = []
        for strat_name, rets in (
            ("V0_count", rets_v0),
            ("pure_contrarian", rets_pc),
            ("cluster_contrarian", rets_cc),
        ):
            if rets.empty:
                logger.warning("%s empty in %s", strat_name, label)
                continue
            stats = run_factor_regressions(rets, factors_5)
            stats["strategy"] = strat_name
            stats["sharpe"] = sharpe(rets.tolist(), periods_per_year=int(rebalances_per_year))
            stats["period"] = label
            period_rows.append(stats)
            summary_rows.append(stats)
            logger.info(
                "%s | %s: 4F α=%.2f%% t=%.2f R²=%.3f | 5F α=%.2f%% t=%.2f R²=%.3f β_STR=%.2f",
                label,
                strat_name,
                stats["alpha_4f_ann"] * 100,
                stats["t_4f"],
                stats["r2_4f"],
                stats["alpha_5f_ann"] * 100,
                stats["t_5f"],
                stats["r2_5f"],
                stats["beta_str"],
            )

        sections.append(f"## {label}")
        sections.append("")
        sections.append(format_period_table(period_rows))
        sections.append("")

    # Side-by-side attenuation summary
    sections.append("## STR-attenuation summary (does adding STR explain Layer 2d α?)")
    sections.append("")
    sections.append("| Period | Strategy | α_4F | α_5F (with STR) | Δα (pp) | Δt (4F→5F) | β_STR |")
    sections.append("|---|---|---:|---:|---:|---:|---:|")
    for r in summary_rows:
        d_alpha = (r["alpha_4f_ann"] - r["alpha_5f_ann"]) * 100
        d_t = r["t_4f"] - r["t_5f"]
        sections.append(
            f"| {r['period']} | {r['strategy']} | {r['alpha_4f_ann'] * 100:.2f}% | {r['alpha_5f_ann'] * 100:.2f}% | "
            f"{d_alpha:+.2f}pp | {d_t:+.2f} | {r['beta_str']:.2f} |"
        )

    sections.append("")
    sections.append("## Interpretation guide")
    sections.append("")
    sections.append(
        "- **β_STR significantly positive (e.g. > 0.2)**: portfolio loads on STR; some of the 4F α was reversal residual."
    )
    sections.append(
        "- **Δα > 50% of α_4F** when STR added: STR explains majority of the Carhart-4F residual."
    )
    sections.append(
        "- **V0_count and pure_contrarian have similar α_4F**: insider data is a redundant proxy for the contrarian set."
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sections) + "\n")
    logger.info("wrote → %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
